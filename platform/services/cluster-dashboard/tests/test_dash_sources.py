"""Polled sources as the App runs them, on a small fake cluster: each test
builds the nodes and pods it needs and runs one source once."""
from dash_testlib import FakeKube
from dashboard import config
from dashboard.app import App
from dashboard.deps import Deps
from dashboard.k8s import ApiError

GPU_UTIL = 'DCGM_FI_DEV_GPU_UTIL{Hostname="gpu-1",gpu="0"} 42'


def node(name, gpu=0):
    alloc = {"cpu": "4", "memory": "16Gi", **({"nvidia.com/gpu": str(gpu)} if gpu else {})}
    return {"metadata": {"name": name, "labels": {"node.kubernetes.io/instance-type": "g6.xlarge" if gpu else "m5.large"}},
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


def make_app(nodes=(), pods=(), lists=None, listed=True):
    """An App over a fake cluster. With `listed`, every watch cache has done
    its first list (kinds the fake does not serve count as not installed)."""
    kube = FakeKube({"/api/v1/nodes": {"items": list(nodes)}, "/api/v1/pods": {"items": list(pods)},
                     **(lists or {})})
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
