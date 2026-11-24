"""Runtime building blocks: source status (R1), store (R3/R4), watch caches
(R2), degraded mode (R6), health (R9)."""
import threading

import pytest

from dash_testlib import FakeKube, fixture_app
from dashboard import deps as ddeps
from dashboard.k8s import ApiError, WatchCache, project_event, project_node
from dashboard.snapshot import Store
from dashboard.sources import PolledSource, Scheduler, Tracker


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- R1 source states ---------------------------------------------------------

def test_tracker_states():
    c = Clock()
    t = Tracker("x", 10, 30, c)
    assert t.status().state == "stale" and t.status().lastSuccess is None   # never ran
    t.ok()
    assert t.status().state == "ok"
    c.t += 31
    assert t.status().state == "stale"
    t.fail("boom")
    st = t.status()
    assert (st.state, st.lastError, st.lastSuccess) == ("error", "boom", 1000.0)
    t.ok("detail")
    assert (t.status().state, t.status().lastError, t.status().detail) == ("ok", None, "detail")
    t.disable("dependency missing: x")
    assert (t.status().state, t.status().detail) == ("disabled", "dependency missing: x")


def test_state_changes_are_logged_once(caplog):
    t = Tracker("x", 10, 30, Clock())
    with caplog.at_level("INFO", logger="dashboard.sources"):
        for _ in range(3):
            t.fail("down")
        t.ok()
        t.ok()
    events = [(r.__dict__.get("from"), r.__dict__.get("to")) for r in caplog.records]
    assert events == [("new", "error"), ("error", "ok")]


def test_polled_source_keeps_running_after_failure_and_records_the_error():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("upstream down")
        return "partial"

    s = PolledSource("x", 10, fn, clock=Clock())
    s.run_once()
    assert s.tracker.status().lastError == "RuntimeError: upstream down"
    s.run_once()
    assert (s.tracker.status().state, s.tracker.status().detail) == ("ok", "partial")


def test_disabled_source_does_not_run():
    s = PolledSource("x", 10, lambda: pytest.fail("must not run"), clock=Clock())
    s.tracker.disable("no")
    s.run_once()


def test_scheduler_reports_stopped_loops():
    c = Clock()
    a, b = PolledSource("a", 10, lambda: None, clock=c), PolledSource("b", 30, lambda: None, clock=c)
    sch = Scheduler([a, b], c)
    c.t += 71
    a.run_once()
    assert sch.dead_loops() == []          # b: 71 s < max(90, 60) + 30
    c.t += 60
    a.run_once()
    assert sch.dead_loops() == ["b"]


# --- store ----------------------------------------------------------------------

def test_store_versions_only_on_change_and_wakes_waiters():
    st = Store(Clock())
    assert st.put("a", [1]) and st.version == 1
    assert not st.put("a", [1]) and st.version == 1
    st.put("b", {"x": 1})
    assert st.changed_since(1) == (2, {"b": {"x": 1}})
    woke = []
    th = threading.Thread(target=lambda: woke.append(st.wait(2, 5)))
    th.start()
    st.put("a", [2])
    th.join(2)
    assert woke == [3]
    assert st.wait(3, 0.01) == 3           # times out without change


def test_data_json_defaults_when_nothing_arrived():
    doc = Store(Clock()).data_json()
    assert (doc["nodes"], doc["approvals_available"], doc["links"], doc["sourceStatus"]) == ([], False, [], [])


# --- R2 watch caches ------------------------------------------------------------

def _node(name, rv="1"):
    return {"metadata": {"name": name, "resourceVersion": rv, "managedFields": ["big"]},
            "status": {"conditions": [{"type": "Ready", "status": "True", "reason": "x"}]}}


def _cache(kube, **kw):
    changes = []
    c = WatchCache("nodes", "/api/v1/nodes", project_node, on_change=changes.append, clock=Clock(), **kw)
    return c, changes


def test_list_then_watch_applies_events_and_projects():
    kube = FakeKube({"/api/v1/nodes": {"metadata": {"resourceVersion": "5"}, "items": [_node("a"), _node("b")]}},
                    {"/api/v1/nodes": [[{"type": "MODIFIED", "object": _node("a", "6")},
                                        {"type": "DELETED", "object": _node("b", "7")},
                                        {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "9"}}},
                                        {"type": "ADDED", "object": _node("c", "10")}]]})
    c, changes = _cache(kube)
    c.list(kube)
    assert c.watch_once(kube) is True
    assert sorted(i["metadata"]["name"] for i in c.items()) == ["a", "c"]
    assert "managedFields" not in c.items()[0]["metadata"]
    assert c.get("a")["status"]["conditions"] == [{"type": "Ready", "status": "True"}]
    assert ("WATCH", "/api/v1/nodes", "5") in kube.calls
    assert c._rv == "10" and changes == ["nodes"] * 4 and c.synced


def test_expired_watch_relists_and_errors_back_off():
    kube = FakeKube({"/api/v1/nodes": {"metadata": {"resourceVersion": "1"}, "items": []}},
                    {"/api/v1/nodes": [[{"type": "ERROR", "object": {"code": 410}}],
                                       [ConnectionResetError("reset")]]})
    c, _ = _cache(kube)
    c.list(kube)
    assert c.watch_once(kube) is False                         # 410: caller relists
    stop, sleeps = threading.Event(), []

    def sleep(s):
        sleeps.append(s)
        stop.set()
        return True

    c._sleep = sleep
    c.run(kube, stop)
    assert c.error == "nodes: reset" and 0.8 <= sleeps[0] <= 1.2


def test_missing_crd_is_not_installed_not_an_error():
    kube = FakeKube({})                                         # every list answers 404
    c, _ = _cache(kube)
    stop = threading.Event()
    c._sleep = lambda s: stop.set() or True
    c.run(kube, stop)
    assert (c.installed, c.synced, c.error, c.items()) == (False, True, None, [])


def test_forbidden_is_an_error():
    kube = FakeKube(errors={"/api/v1/nodes": ApiError(403, "forbidden")})
    c, _ = _cache(kube)
    stop = threading.Event()
    c._sleep = lambda s: stop.set() or True
    c.run(kube, stop)
    assert c.error == "nodes: HTTP 403: forbidden" and not c.synced


def test_events_expire_and_are_capped():
    clock = Clock(1795168800.0)                                  # 2026-11-20T10:00:00Z
    ev = [{"metadata": {"name": f"e{i}", "namespace": "n"}, "lastTimestamp": ts, "involvedObject": {}}
          for i, ts in enumerate(["2026-11-20T09:59:00Z", "2026-11-20T08:00:00Z", "2026-11-20T09:30:00Z"])]
    kube = FakeKube({"/e": {"items": ev}})
    c = WatchCache("ev", "/e", project_event, retention_s=3600, max_items=2, clock=clock)
    c.list(kube)
    assert [e["metadata"]["name"] for e in c.items()] == ["e0", "e2"]


# --- R6 degraded mode ---------------------------------------------------------------

def test_deps_reports_missing_packages_and_install_errors(tmp_path):
    def importer(name):
        if name in ("kubernetes", "boto3"):
            raise ImportError(name)

    d = ddeps.check(str(tmp_path), importer)
    assert [x["package"] for x in d.degraded()] == ["boto3", "kubernetes"]
    (tmp_path / "INSTALL_FAILED").write_text("ERROR: no network\n")
    d = ddeps.check(str(tmp_path), lambda n: None)
    assert d.install_error == "ERROR: no network" and d.degraded()[0]["package"] == "(install)"


def test_app_without_kubernetes_is_degraded_but_serves():
    from dashboard import config
    from dashboard.app import App
    from dashboard.deps import Deps
    app = App(config.load({}), Deps(frozenset({"kubernetes", "psycopg"})), kube=None, clock=Clock())
    app.derive_once()
    doc = app.store.data_json()
    states = {s["name"]: s["state"] for s in doc["sourceStatus"]}
    assert states["kubernetes"] == states["dcgm"] == states["approvals"] == "disabled"
    assert {d["package"] for d in doc["degraded"]} == {"kubernetes", "psycopg"}
    live, ready, _ = app.health()
    assert live and ready


# --- R9 health -----------------------------------------------------------------------

def test_health_ready_after_first_list_and_live_until_kubernetes_goes_silent():
    clock = Clock(1795168800.0)
    app = fixture_app(clock=clock)
    live, ready, _ = app.health()
    assert live and ready
    clock.t += 601
    for s in app.sources:              # the polled loops kept running
        s.tracker.tick()
    app._derive_tick = clock.t
    live, _, body = app.health()
    assert not live and any("no Kubernetes data" in r for r in body["reasons"])


def test_one_denied_watch_does_not_block_readiness_or_liveness():
    app = fixture_app()
    app.caches["hpas"].synced, app.caches["hpas"].error = False, "hpas: HTTP 403: forbidden"
    live, ready, _ = app.health()
    assert live and ready
    assert {s.name: s.state for s in app.statuses()}["kubernetes"] == "error"


# --- KubeClient over the kubernetes package's ApiClient (stubbed) --------------------

class _Resp:
    def __init__(self, status, data=b"", chunks=()):
        self.status, self.reason, self.data, self._chunks, self.released = status, "R", data, chunks, False

    def stream(self, _n, decode_content=True):
        yield from self._chunks

    def release_conn(self):
        self.released = True


class _Rest:
    def __init__(self, raw):
        self.response, self.status, self.reason = raw, raw.status, raw.reason

    def read(self):
        return self.response.data


def _client(monkeypatch, stub_modules, raw):
    import sys

    from dashboard import k8s
    calls = []

    class ApiClient:
        def __init__(self, cfg):
            pass

        def param_serialize(self, method, path, query_params=None, header_params=None, body=None, auth_settings=None):
            calls.append((method, path, query_params, auth_settings))
            return method, "http://x" + path, header_params, body, None

        def call_api(self, method, url, header_params=None, body=None, post_params=None, _request_timeout=None):
            return _Rest(raw)

    monkeypatch.setattr(sys.modules["kubernetes"].client, "ApiClient", ApiClient, raising=False)
    return k8s.KubeClient(configuration=object()), calls


def test_kube_client_get_and_errors(monkeypatch, stub_modules):
    raw = _Resp(200, b'{"items": [1]}')
    kc, calls = _client(monkeypatch, stub_modules, raw)
    assert kc.get("/api/v1/nodes", {"limit": "1"}) == {"items": [1]}
    assert calls[0] == ("GET", "/api/v1/nodes", [("limit", "1")], ["BearerToken"]) and raw.released
    kc, _ = _client(monkeypatch, stub_modules, _Resp(403, b'{"message": "nodes is forbidden"}'))
    with pytest.raises(ApiError) as e:
        kc.get("/api/v1/nodes")
    assert (e.value.status, str(e.value)) == (403, "HTTP 403: nodes is forbidden")


def test_kube_client_watch_splits_chunked_lines(monkeypatch, stub_modules):
    raw = _Resp(200, chunks=[b'{"type":"ADDED","object":{"a":1}}\n{"type":"MOD', b'IFIED","object":{}}\n', b"\n"])
    kc, calls = _client(monkeypatch, stub_modules, raw)
    events = list(kc.watch("/api/v1/nodes", {"resourceVersion": "5"}))
    assert [e["type"] for e in events] == ["ADDED", "MODIFIED"] and raw.released
    q = dict(calls[0][2])
    assert (q["watch"], q["resourceVersion"], q["allowWatchBookmarks"]) == ("true", "5", "true")
