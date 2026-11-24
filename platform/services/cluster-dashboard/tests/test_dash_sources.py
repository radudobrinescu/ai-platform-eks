"""Polled sources as the App runs them, on a small fake cluster: each test
builds the nodes and pods it needs and runs one source once."""
from dash_testlib import FakeKube
from dashboard import config
from dashboard.app import App
from dashboard.deps import Deps
from dashboard.k8s import ApiError
from dashboard.pricing import LivePrices

GPU_UTIL = 'DCGM_FI_DEV_GPU_UTIL{Hostname="gpu-1",gpu="0"} 42'


def node(name, gpu=0):
    alloc = {"cpu": "4", "memory": "16Gi", **({"nvidia.com/gpu": str(gpu)} if gpu else {})}
    labels = {"node.kubernetes.io/instance-type": "g6.xlarge" if gpu else "m5.large",
              "topology.kubernetes.io/region": "eu-central-1"}
    return {"metadata": {"name": name, "labels": labels},
            "status": {"conditions": [{"type": "Ready", "status": "True"}], "allocatable": alloc}}


def pod(name, namespace="inference", phase="Running", ip="10.0.0.9", labels=None, containers=(), gpu=0, created=""):
    """`containers` is (name, ready) pairs."""
    requests = {"nvidia.com/gpu": str(gpu)} if gpu else {}
    return {"metadata": {"name": name, "namespace": namespace, "labels": labels or {}, "creationTimestamp": created},
            "spec": {"nodeName": "n", "containers": [{"name": c, "resources": {"requests": requests}}
                                                      for c, _ready in containers]},
            "status": {"phase": phase, "podIP": ip,
                       "containerStatuses": [{"name": c, "ready": ready} for c, ready in containers]}}


def exporter(name="dcgm-1", phase="Running", ip="10.0.0.5"):
    return pod(name, "gpu-operator", phase, ip, {"app": "nvidia-dcgm-exporter"})


class Kube(FakeKube):
    """FakeKube whose pod logs come from `logs` ({pod: text or exception})."""

    def __init__(self, lists, logs=None):
        super().__init__(lists)
        self.logs = dict(logs or {})

    def get_text(self, path, query=None):
        pod = path.split("/")[-2]
        self.calls.append(("LOG", pod, (query or {}).get("container")))
        answer = self.logs.get(pod, "")
        if isinstance(answer, Exception):
            raise answer
        return answer

    def log_calls(self):
        return [(pod, container) for kind, pod, container in self.calls if kind == "LOG"]


def make_app(nodes=(), pods=(), lists=None, listed=True, logs=None):
    """An App over a fake cluster. With `listed`, every watch cache has done
    its first list (kinds the fake does not serve count as not installed)."""
    kube = Kube({"/api/v1/nodes": {"items": list(nodes)}, "/api/v1/pods": {"items": list(pods)},
                 **(lists or {})}, logs)
    app = App(config.load({}), Deps(), kube=kube, clock=lambda: 1000.0)
    for cache in app.caches.values() if listed else ():
        try:
            cache.list(kube)
        except ApiError:
            cache._not_installed()
    return app, kube


def source(app, name):
    return next(s for s in app.sources if s.name == name)


def run(app, name):
    src = source(app, name)
    src.run_once()
    return src.tracker.status()


# --- dcgm ---------------------------------------------------------------------

class Exporters:
    """Stands in for the HTTP fetch: answers the URLs in `up`, refuses the rest."""

    def __init__(self, *up):
        self.up, self.calls = set(up), []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.up:
            raise ConnectionRefusedError(111, "Connection refused")
        return GPU_UTIL


def test_dcgm_idle_cluster_is_ok_and_empty():
    """GPU pools scaled to zero: no GPU nodes, no exporter pods, and nothing
    answers on the exporter Service. That is the normal idle state."""
    app, _ = make_app(nodes=[node("cpu-1")])
    app.dcgm_fetch = Exporters()
    st = run(app, "dcgm")
    assert (st.state, st.lastError, st.detail) == ("ok", None, "no GPU nodes")
    assert app.inputs()["dcgm"] == {}
    assert app.dcgm_fetch.calls == [], "nothing to scrape, so nothing is fetched"


def test_dcgm_scrapes_every_running_exporter_pod():
    app, _ = make_app(nodes=[node("gpu-1", gpu=1)],
                      pods=[exporter(), exporter("dcgm-pending", "Pending", None), pod("other", "gpu-operator")])
    app.dcgm_fetch = Exporters("http://10.0.0.5:9400/metrics")
    st = run(app, "dcgm")
    assert (st.state, st.detail) == ("ok", None)
    assert app.inputs()["dcgm"] == {"gpu-1": {"util": [42], "mem": [None]}}
    assert app.dcgm_fetch.calls == ["http://10.0.0.5:9400/metrics"]


def test_dcgm_gpu_nodes_without_any_exporter_is_still_an_error():
    app, _ = make_app(nodes=[node("gpu-1", gpu=1)])
    app.dcgm_fetch = Exporters()
    st = run(app, "dcgm")
    assert st.state == "error" and "Connection refused" in st.lastError
    assert "dcgm" not in app.inputs()


def test_dcgm_falls_back_to_the_service_when_no_exporter_pod_matches():
    """An exporter deployed under other labels is still read through DCGM_URL."""
    app, _ = make_app(nodes=[node("gpu-1", gpu=1)])
    app.dcgm_fetch = Exporters(app.s.dcgm_url)
    assert run(app, "dcgm").state == "ok"
    assert app.inputs()["dcgm"] == {"gpu-1": {"util": [42], "mem": [None]}}


def test_dcgm_waits_for_the_first_node_and_pod_lists():
    """Before the first list an empty cache says nothing about the cluster."""
    app, _ = make_app(nodes=[node("gpu-1", gpu=1)], pods=[exporter()], listed=False)
    app.dcgm_fetch = Exporters()
    st = run(app, "dcgm")
    assert (st.state, st.detail) == ("disabled", "waiting for the first node and pod lists")
    assert app.dcgm_fetch.calls == [] and "dcgm" not in app.inputs()


# --- pricing ------------------------------------------------------------------

def test_pricing_detail_names_types_without_a_price_and_types_not_reached():
    class Client:
        def get_products(self, **kw):
            return {"PriceList": []}                    # the Price List knows none of them

    ticks = iter(range(0, 1000, 15))                    # each clock reading is 15 s later
    app, _ = make_app(nodes=[node("a"), node("gpu-1", gpu=1)])
    app._live_prices = LivePrices(Client, budget_s=20, monotonic=lambda: next(ticks))
    st = run(app, "pricing")
    assert st.state == "ok"
    assert st.detail == "no live price for: g6.xlarge; not looked up yet: m5.large"
    assert app.inputs()["live_prices"] == {"eu-central-1": {}}


def test_pricing_without_a_region_looks_nothing_up_and_reports_nothing():
    def no_client():
        raise AssertionError("no client is needed without nodes")

    app, _ = make_app(nodes=[])
    app._live_prices = LivePrices(no_client)
    st = run(app, "pricing")
    assert (st.state, st.detail) == ("ok", None)


# --- liveness -----------------------------------------------------------------

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _stall(name, seconds=600):
    """An app whose loops all keep running except `name`, stuck for `seconds`."""
    from dash_testlib import fixture_app
    clock = Clock(1795168800.0)
    app = fixture_app(clock=clock)
    clock.t += seconds
    for s in app.sources:
        if s.name != name:
            s.tracker.tick()
    for cache in app.caches.values():
        cache.last_success = clock.t
    app._derive_tick = clock.t
    return app


def test_stuck_optional_source_shows_in_its_status_and_does_not_fail_liveness():
    """Live prices are optional (the tables are used without them) and come
    from outside the cluster: a stuck lookup must not restart the pod."""
    app = _stall("pricing")
    live, ready, body = app.health()
    assert live and ready and body["reasons"] == []
    st = {s.name: s for s in app.statuses()}["pricing"]
    assert st.state == "error" and st.lastError == "no run started for 600 s (a run is stuck or the loop stopped)"


def test_stuck_required_source_still_fails_liveness():
    app = _stall("litellm-api")
    live, _, body = app.health()
    assert not live and body["reasons"] == ["source loop stopped: litellm-api"]
    assert {s.name: s.state for s in app.statuses()}["litellm-api"] == "error"


def test_slow_run_inside_the_limit_changes_nothing():
    app = _stall("pricing", seconds=200)                # limit: max(3 x 60, 60) + 60 = 240 s
    assert app.health()[0]
    assert {s.name: s.state for s in app.statuses()}["pricing"] == "ok"


# --- diagnostics (log tails) ----------------------------------------------------

VLLM = "/apis/kro.run/v1alpha1/namespaces/inference/vllmendpoints"


def endpoints(*names, ready=()):
    return {VLLM: {"items": [{"metadata": {"name": n}, "spec": {"model": f"org/{n}"},
                              "status": {"ready": "True" if n in ready else "False"}} for n in names]}}


def loading(name, created="2026-11-20T09:00:00Z", gpu=1):
    """A serving pod that is Running while its model loads (no container ready)."""
    return pod(name, containers=[("vllm", False)], gpu=gpu, created=created)


def shown_log(app, model):
    app.derive_once()
    _, sections = app.store.get()
    return next(m for m in sections["models"] if m["name"] == model)["deploy"]["log"]


def test_log_tails_one_failing_pod_does_not_stop_the_others():
    """A Running pod whose first container is still waiting answers 400."""
    app, kube = make_app(pods=[loading("a-vllm-1"), loading("b-vllm-1")], lists=endpoints("a", "b"),
                         logs={"a-vllm-1": ApiError(400, 'container "vllm" in pod "a-vllm-1" is waiting to start'),
                               "b-vllm-1": "Loading weights 40%"})
    st = run(app, "diagnostics")
    assert (st.state, st.detail) == ("ok", None), "a container that has no log yet is not a failure"
    assert app.inputs()["logs"] == {"b-vllm-1": "Loading weights 40%"}
    assert kube.log_calls() == [("a-vllm-1", "vllm"), ("b-vllm-1", "vllm")]
    assert shown_log(app, "b") == ["Loading weights 40%"] and shown_log(app, "a") == []


def test_log_tails_report_real_failures_without_dropping_the_rest():
    app, _ = make_app(pods=[loading("a-vllm-1"), loading("b-vllm-1")], lists=endpoints("a", "b"),
                      logs={"a-vllm-1": ApiError(403, "pods/log is forbidden"), "b-vllm-1": "ok so far"})
    st = run(app, "diagnostics")
    assert st.state == "ok" and st.detail == "some log tails failed: a-vllm-1: HTTP 403: pods/log is forbidden"
    assert app.inputs()["logs"] == {"b-vllm-1": "ok so far"}


def test_log_tails_all_failing_is_an_error():
    app, _ = make_app(pods=[loading("a-vllm-1")], lists=endpoints("a"),
                      logs={"a-vllm-1": ConnectionResetError("reset")})
    st = run(app, "diagnostics")
    assert st.state == "error" and "a-vllm-1: reset" in st.lastError


def test_log_tail_budget_goes_to_the_pod_the_diagnostics_show():
    """deploy_diagnostics shows the newest GPU pod of a deploying model. Older
    pods of the same model, earlier in the cache, must not use up the budget."""
    old = [loading(f"m-vllm-old{i}", created=f"2026-11-20T08:0{i}:00Z") for i in range(4)]
    sidecar = loading("m-vllm-proxy", created="2026-11-20T09:30:00Z", gpu=0)          # newer, but no GPU
    app, kube = make_app(pods=[*old, sidecar, loading("m-vllm-new", created="2026-11-20T09:00:00Z")],
                         lists=endpoints("m"),
                         logs={"m-vllm-new": "Loading weights 90%", **{p["metadata"]["name"]: "stale" for p in old}})
    run(app, "diagnostics")
    assert kube.log_calls() == [("m-vllm-new", "vllm")]
    assert shown_log(app, "m") == ["Loading weights 90%"]


def test_log_tails_skip_what_the_diagnostics_do_not_show():
    pods = [loading("ready-vllm-1"),                                    # its endpoint is ready
            loading("m-register-abc"),                                  # LiteLLM register Job pod
            pod("m-vllm-1", containers=[("vllm", True)], gpu=1),        # a container is ready
            pod("p-vllm-1", phase="Pending", containers=[("vllm", False)], gpu=1),
            pod("m-vllm-2", namespace="other", containers=[("vllm", False)], gpu=1)]
    app, kube = make_app(pods=pods, lists=endpoints("ready", "m", "p", ready=("ready",)))
    assert run(app, "diagnostics").state == "ok"
    assert kube.log_calls() == [] and app.inputs()["logs"] == {}


def test_log_tails_take_turns_when_more_models_deploy_than_the_budget():
    names = [f"m{i}" for i in range(5)]                                 # budget: 3 per run
    app, kube = make_app(pods=[loading(f"{n}-vllm-1") for n in names], lists=endpoints(*names),
                         logs={f"{n}-vllm-1": f"log of {n}" for n in names})
    run(app, "diagnostics")
    assert len(kube.log_calls()) == 3
    run(app, "diagnostics")
    assert app.inputs()["logs"] == {f"{n}-vllm-1": f"log of {n}" for n in names}, "every shown pod got its turn"
    assert len(kube.log_calls()) == 6 and len(set(kube.log_calls())) == 5
    kube.logs["m0-vllm-1"] = ApiError(404, "pod not found")
    for _ in range(2):
        run(app, "diagnostics")
    assert "m0-vllm-1" not in app.inputs()["logs"], "a tail that can no longer be read is dropped"
