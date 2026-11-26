"""Agent loops (pha.watch, pha.rbac, pha.health) and the event-watcher entry
point, on fakes: no cluster, no database."""
import http.client
import threading
from types import SimpleNamespace as NS

import pytest

from dash_testlib import FakeConn
from pha_cases import LLMD_POD, ago, cs, deployment_available, event, llmd_endpoint, node, pod
from pha import triggers


class Conn(FakeConn):
    def __init__(self):
        super().__init__()
        self.broken = False


def agent_for(watch, **kw):
    seen = {"throttle": [], "record": [], "jobs": []}
    reason = kw.pop("reason", None)
    a = watch.Agent(
        rules=kw.pop("rules", triggers.Rules()), namespace="ai-platform",
        throttle=lambda c, *k: seen["throttle"].append(k) or reason,
        record=lambda c, *k: seen["record"].append(k),
        build_job=lambda i, p: seen["jobs"].append((i, p)) or {"metadata": {"name": f"investigator-{i[:8]}"}},
        sleep=kw.pop("sleep", lambda s: None), **kw)
    return a, seen


class Batch:
    def __init__(self, error=None):
        self.created, self.error = [], error

    def create_namespaced_job(self, namespace, body):
        if self.error:
            raise self.error
        self.created.append((namespace, body))


PAYLOAD = {"kind": "Deployment", "namespace": "inference", "name": "qwen", "detail": "x"}


# ---------------------------------------------------------------------------
# One incident
# ---------------------------------------------------------------------------

def test_spawn_creates_row_job_and_debounce(pha_watch):
    watch, _ = pha_watch
    agent, seen = agent_for(watch)
    conn, batch = Conn(), Batch()
    watch.spawn_investigation(agent, conn, batch, "CrashLoopBackOff", PAYLOAD)
    sql = conn.sql()
    assert "INSERT INTO investigations" in sql[0] and "status='running'" in sql[1]
    assert batch.created[0][0] == "ai-platform"
    assert seen["record"] == [("Deployment", "inference", "qwen")]
    inv_id, payload = seen["jobs"][0]
    assert payload == {"trigger_kind": "CrashLoopBackOff", **PAYLOAD} and conn.executed[0][1][0] == inv_id


def test_throttled_incident_starts_nothing(pha_watch):
    watch, _ = pha_watch
    agent, seen = agent_for(watch, reason="debounce")
    conn, batch = Conn(), Batch()
    watch.spawn_investigation(agent, conn, batch, "CrashLoopBackOff", PAYLOAD)
    assert conn.executed == [] and batch.created == [] and seen["record"] == []


def test_failed_job_create_marks_failed_without_debounce(pha_watch, stub_modules):
    watch, _ = pha_watch
    agent, seen = agent_for(watch)
    err = stub_modules["kubernetes.client.exceptions"].ApiException(403, "forbidden")
    conn = Conn()
    watch.spawn_investigation(agent, conn, Batch(error=err), "OOMKilled", PAYLOAD)
    assert "status='failed'" in conn.sql()[1] and "Job create failed" in conn.executed[1][1][0]
    assert seen["record"] == []


# ---------------------------------------------------------------------------
# Watch loops
# ---------------------------------------------------------------------------

def test_detectors_apply_the_rules(pha_watch):
    watch, _ = pha_watch
    d = watch.detectors(triggers.Rules())
    assert d["pod"][1](pod(containers=[cs(restarts=4, waiting=("CrashLoopBackOff", "b"))]))[0] == "CrashLoopBackOff"
    assert d["event"][1](event("FailedMount"))[0] == "FailedMount"
    assert d["node"][1](node([("Ready", "False", "r", "m")]))[0] == "NodeNotReady"
    assert watch.detectors(triggers.Rules(enabled=frozenset()))["pod"][1](
        pod(containers=[cs(restarts=4, waiting=("CrashLoopBackOff", "b"))])) is None


def test_event_detector_reads_the_model_of_an_unschedulable_llmd_pod(pha_watch, monkeypatch):
    """A scale-to-zero model's new pod waits for its GPU node on every wake:
    no investigation while the model wakes, one when the hold has run out."""
    watch, _ = pha_watch
    custom = Custom(endpoints={"sleepy": llmd_endpoint(0), "warm": llmd_endpoint(1, name="warm"),
                               "late": llmd_endpoint(0, name="late")},
                    deployments={"sleepy-llmd": deployment_available("False", 2),
                                 "warm-llmd": deployment_available("False", 2),
                                 "late-llmd": deployment_available("False", 13)})
    monkeypatch.setattr(watch.client, "CustomObjectsApi", lambda: custom, raising=False)
    detect = watch.detectors(triggers.Rules())["event"][1]

    def unschedulable(pod_name):
        return detect(event("FailedScheduling", name=pod_name, last=ago(minutes=1)))

    assert unschedulable(LLMD_POD) is None
    assert custom.gets == [("llmdendpoints", "inference", "sleepy"), ("deployments", "inference", "sleepy-llmd")]
    assert unschedulable("late-llmd-6c8f9d7b5-x2k4q")[0] == "FailedScheduling"
    custom.gets.clear()
    assert unschedulable("warm-llmd-6c8f9d7b5-x2k4q")[0] == "FailedScheduling"
    assert custom.gets == [("llmdendpoints", "inference", "warm")], "an always-warm model: no Deployment read"
    assert unschedulable("gone-llmd-6c8f9d7b5-x2k4q")[0] == "FailedScheduling", "no such endpoint"
    custom.gets.clear()
    assert unschedulable("qwen-7d9f8c6b5d-abcde")[0] == "FailedScheduling"
    assert detect(event("FailedMount", name=LLMD_POD))[0] == "FailedMount"
    assert detect(event("BackOff", name=LLMD_POD)) is None
    assert custom.gets == [], "only an unschedulable llm-d pod costs API calls"


def test_event_detector_respects_a_disabled_failed_scheduling_rule(pha_watch, monkeypatch):
    watch, _ = pha_watch
    custom = Custom(endpoints={"sleepy": llmd_endpoint(0)})
    monkeypatch.setattr(watch.client, "CustomObjectsApi", lambda: custom, raising=False)
    rules = triggers.Rules(enabled=frozenset(triggers.TRIGGER_ENV) - {"FailedScheduling"})
    assert watch.detectors(rules)["event"][1](event("FailedScheduling", name=LLMD_POD)) is None
    assert custom.gets == []


def _watch_with(watch, monkeypatch, streams, agent):
    """kubernetes.watch.Watch stand-in yielding `streams` one per call."""
    calls = {"n": 0}

    class W:
        def stream(self, fn, timeout_seconds):
            assert timeout_seconds == watch.WATCH_TIMEOUT
            i = calls["n"]
            calls["n"] += 1
            if i >= len(streams):
                agent.stop.set()
                return iter(())
            s = streams[i]
            if isinstance(s, Exception):
                raise s
            return iter(s)

        def stop(self):
            pass

    monkeypatch.setattr(watch.watch, "Watch", W, raising=False)
    monkeypatch.setattr(watch.client, "CoreV1Api", lambda: NS(list_pod_for_all_namespaces=None), raising=False)
    monkeypatch.setattr(watch.client, "BatchV1Api", lambda: Batch(), raising=False)
    return calls


def test_watch_loop_keeps_one_connection_across_restarts(pha_watch, monkeypatch):
    watch, _ = pha_watch
    agent, seen = agent_for(watch)
    crash = {"object": pod(containers=[cs(restarts=4, waiting=("CrashLoopBackOff", "b"))])}
    _watch_with(watch, monkeypatch, [[crash], [], RuntimeError("stream broke"), [crash]], agent)
    opened = []
    lister, detect = watch.detectors(agent.rules)["pod"]
    watch.watch_loop(agent, "pod", lambda: opened.append(Conn()) or opened[-1], lister, detect)
    assert len(opened) == 1 and opened[0].closed
    assert len(seen["jobs"]) == 2


def test_watch_loop_replaces_a_broken_connection(pha_watch, monkeypatch):
    watch, _ = pha_watch
    agent, _ = agent_for(watch)
    opened = []

    def factory():
        opened.append(Conn())
        return opened[-1]

    class Breaks(Exception):
        pass

    def stream_then_break():
        opened[0].broken = True
        raise Breaks("connection lost")

    calls = {"n": 0}

    class W:
        def stream(self, fn, timeout_seconds):
            calls["n"] += 1
            if calls["n"] == 1:
                stream_then_break()
            agent.stop.set()
            return iter(())

        def stop(self):
            pass

    monkeypatch.setattr(watch.watch, "Watch", W, raising=False)
    monkeypatch.setattr(watch.client, "CoreV1Api", lambda: NS(list_node=None), raising=False)
    monkeypatch.setattr(watch.client, "BatchV1Api", lambda: Batch(), raising=False)
    lister, detect = watch.detectors(agent.rules)["node"]
    watch.watch_loop(agent, "node", factory, lister, detect)
    assert len(opened) == 2 and all(c.closed for c in opened)


# ---------------------------------------------------------------------------
# Stuck-resource scan
# ---------------------------------------------------------------------------

class Custom:
    def __init__(self, items=None, errors=None, deployments=None, endpoints=None):
        self.items, self.errors, self.calls = items or {}, errors or {}, 0
        self.deployments = deployments or {}
        self.endpoints, self.gets = endpoints or {}, []

    def list_namespaced_custom_object(self, group, version, namespace, plural):
        self.calls += 1
        if plural in self.errors:
            raise self.errors[plural]
        return {"items": self.items.get(plural, [])}

    def get_namespaced_custom_object(self, group, version, namespace, plural, name):
        self.gets.append((plural, namespace, name))
        if (group, version, plural) == ("kro.run", "v1alpha1", "llmdendpoints"):
            found = self.endpoints
        else:
            assert (group, version, plural) == ("apps", "v1", "deployments")
            found = self.deployments
        if name not in found:
            raise LookupError(name)
        return found[name]


def _scan(watch, monkeypatch, custom, cycles=1, between=None, **agent_kw):
    slept = {"n": 0}
    agent = None

    def sleep(_s):
        slept["n"] += 1
        if between:
            between(slept["n"], opened)
        if slept["n"] >= cycles:
            agent.stop.set()

    agent, seen = agent_for(watch, sleep=sleep, stuck_poll_interval=1, **agent_kw)
    batch = Batch()
    monkeypatch.setattr(watch.client, "CustomObjectsApi", lambda: custom, raising=False)
    monkeypatch.setattr(watch.client, "BatchV1Api", lambda: batch, raising=False)
    opened = []
    watch.stuck_scan_loop(agent, lambda: opened.append(Conn()) or opened[-1])
    return opened, seen, batch


def test_stuck_scan_reuses_one_connection_and_closes_it(pha_watch, monkeypatch):
    watch, _ = pha_watch
    custom = Custom()
    opened, _, _ = _scan(watch, monkeypatch, custom, cycles=3)
    assert len(opened) == 1 and opened[0].closed
    assert custom.calls == 3 * 4                     # 3 endpoint kinds + AITeams per scan


def test_stuck_scan_replaces_a_broken_connection(pha_watch, monkeypatch):
    watch, _ = pha_watch

    def break_first(n, opened):
        if n == 1:
            opened[0].broken = True
    opened, _, _ = _scan(watch, monkeypatch, Custom(), cycles=3, between=break_first)
    assert len(opened) == 2 and all(c.closed for c in opened)


def test_stuck_scan_spawns_only_stuck_items(pha_watch, monkeypatch, stub_modules):
    watch, _ = pha_watch
    api_exc = stub_modules["kubernetes.client.exceptions"].ApiException
    old, young = ago(minutes=30), ago(minutes=1)
    custom = Custom(items={
        "vllmendpoints": [{"metadata": {"name": "a", "namespace": "inference", "creationTimestamp": old},
                           "status": {"ready": "False"}, "spec": {"model": "m"}},
                          {"metadata": {"name": "b", "namespace": "inference", "creationTimestamp": young}}],
        "aiteams": [{"metadata": {"name": "t", "namespace": "ai-platform", "creationTimestamp": old},
                     "status": {"phase": "Pending", "message": "waiting"}}],
    }, errors={"llmdendpoints": api_exc(404, "no CRD"), "llmddisaggendpoints": api_exc(500, "boom")})
    _, seen, batch = _scan(watch, monkeypatch, custom)
    payloads = [p for _, p in seen["jobs"]]
    assert [(p["trigger_kind"], p["kind"], p["name"]) for p in payloads] == [
        ("StuckResource", "VLLMEndpoint", "a"), ("StuckResource", "AITeam", "t")]
    assert payloads[0]["model"] == "m" and payloads[0]["ready"] == "False" and payloads[0]["age_seconds"] >= 1800
    assert payloads[1]["phase"] == "Pending" and payloads[1]["message"] == "waiting"
    assert len(batch.created) == 2


def test_stuck_scan_skips_a_model_waking_from_zero(pha_watch, monkeypatch):
    """A minReplicas-0 llm-d model starting its first replica is a cold start;
    once that has lasted longer than the router holds requests, it is stuck."""
    watch, _ = pha_watch
    old = ago(minutes=30)

    def ep(name):
        return {"metadata": {"name": name, "namespace": "inference", "creationTimestamp": old},
                "spec": {"model": "m", "minReplicas": 0, "coldStartTimeoutSeconds": 600},
                "status": {"ready": "False", "routerHealth": "Healthy"}}

    def deploy(minutes):
        return {"status": {"conditions": [{"type": "Available", "status": "False",
                                            "lastTransitionTime": ago(minutes=minutes)}]}}
    custom = Custom(items={"llmdendpoints": [ep("waking"), ep("too-long"), ep("unreadable")]},
                    deployments={"waking-llmd": deploy(3), "too-long-llmd": deploy(20)})
    _, seen, _ = _scan(watch, monkeypatch, custom)
    assert [p["name"] for _, p in seen["jobs"]] == ["too-long", "unreadable"]


def test_stuck_scan_disabled_exits(pha_watch, monkeypatch):
    watch, _ = pha_watch
    opened, _, _ = _scan(watch, monkeypatch, Custom(),
                         rules=triggers.Rules(enabled=frozenset(triggers.TRIGGER_ENV) - {"StuckResource"}))
    assert opened == []


# ---------------------------------------------------------------------------
# Writer RoleBindings
# ---------------------------------------------------------------------------

class Rbac:
    def __init__(self, existing=(), ours=(), read_error=None, list_error=None):
        self.existing, self.ours = set(existing), list(ours)
        self.read_error, self.list_error = read_error, list_error
        self.created, self.deleted = [], []

    def read_namespaced_role_binding(self, name, namespace):
        if self.read_error:
            raise self.read_error
        if namespace not in self.existing:
            raise self._404
        return {}

    def create_namespaced_role_binding(self, namespace, body):
        self.created.append((namespace, body))

    def list_role_binding_for_all_namespaces(self, label_selector):
        assert label_selector == "app.kubernetes.io/managed-by=platform-health-agent-reconciler"
        if self.list_error:
            raise self.list_error
        return NS(items=[NS(metadata=NS(name="platform-health-agent-writer", namespace=n)) for n in self.ours])

    def delete_namespaced_role_binding(self, name, namespace):
        self.deleted.append((name, namespace))


def _namespaces(*names):
    return NS(list_namespace=lambda: NS(items=[NS(metadata=NS(name=n)) for n in names]))


def test_reconcile_creates_missing_keeps_existing_removes_stale(pha_watch, stub_modules):
    _, rbac = pha_watch
    api_exc = stub_modules["kubernetes.client.exceptions"].ApiException
    fake = Rbac(existing={"team-a"}, ours=["team-a", "team-gone"])
    fake._404 = api_exc(404, "not found")
    created, removed = rbac.reconcile_once(_namespaces("inference", "team-a", "team-b", "ai-platform", "default"),
                                           fake, "ai-platform")
    assert created == ["inference", "team-b"] and removed == ["team-gone"]
    body = fake.created[0][1]
    assert body["roleRef"] == {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole",
                               "name": "platform-health-agent-writer"}
    assert body["subjects"] == [{"kind": "ServiceAccount", "name": "platform-health-agent-writer",
                                 "namespace": "ai-platform"}]
    assert body["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "platform-health-agent-reconciler"
    assert fake.deleted == [("platform-health-agent-writer", "team-gone")]


def test_reconcile_surfaces_errors_other_than_404(pha_watch, stub_modules):
    _, rbac = pha_watch
    api_exc = stub_modules["kubernetes.client.exceptions"].ApiException
    fake = Rbac(read_error=api_exc(403, "forbidden"))
    with pytest.raises(api_exc):
        rbac.reconcile_once(_namespaces("team-a"), fake, "ai-platform")


def test_reconcile_without_list_permission_removes_nothing(pha_watch, stub_modules):
    _, rbac = pha_watch
    api_exc = stub_modules["kubernetes.client.exceptions"].ApiException
    fake = Rbac(existing={"team-a"}, list_error=api_exc(403, "forbidden"))
    fake._404 = api_exc(404, "x")
    assert rbac.reconcile_once(_namespaces("team-a"), fake, "ai-platform") == ([], [])


# ---------------------------------------------------------------------------
# Health endpoint and entry point
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fails,status,body", [(False, 200, b"ok"), (True, 503, b"db down")])
def test_health_endpoint(fails, status, body):
    from pha import health

    def check():
        if fails:
            raise OSError("db down")
    srv = health.make_server(check, port=0, address="127.0.0.1")
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        assert (r.status, r.read()) == (status, body)
    finally:
        srv.shutdown()
        srv.server_close()


def test_idle_without_key_starts_no_watcher(event_watcher, monkeypatch):
    started = []
    monkeypatch.setattr(event_watcher, "PHA_ENABLED", False)
    monkeypatch.setattr(event_watcher.config, "load_incluster_config", lambda: None, raising=False)
    monkeypatch.setattr(event_watcher.signal, "signal", lambda *a: None)
    monkeypatch.setattr(event_watcher.health, "serve", lambda check: started.append("health"))
    monkeypatch.setattr(event_watcher.watch, "watch_loop", lambda *a: started.append("watch"))
    monkeypatch.setattr(event_watcher.watch, "stuck_scan_loop", lambda *a: started.append("stuck"))
    monkeypatch.setattr(event_watcher.rbac, "reconcile_loop", lambda *a: started.append("rbac"))

    def sleep(_s):
        raise SystemExit(0)                          # leave the idle loop after one tick
    monkeypatch.setattr(event_watcher.time, "sleep", sleep)
    with pytest.raises(SystemExit):
        event_watcher.main()
    assert started == ["health"]
