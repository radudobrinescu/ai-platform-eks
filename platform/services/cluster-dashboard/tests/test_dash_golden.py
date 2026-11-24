"""Golden test for the /data.json document the UI (and the owner's pre-flight
script) read.

The recorded inputs in fixtures/ describe a small cluster: a GPU node serving a
vLLM model, an llm-d model stuck on GPU capacity, a kro endpoint in ERROR, a
node under memory pressure, a NotReady node, a node Karpenter is removing, a
Bedrock CR plus a static Bedrock entry, LiteLLM traffic and team spend, DCGM
utilization and one KEDA autoscaler. The expected output in
fixtures/data.json.golden was produced by the pre-rebuild backend, so this test
pins today's behaviour while the backend is restructured.
"""
from dash_testlib import check_golden, load_fixture


def build_data_json(backend, monkeypatch):
    k8s = load_fixture("k8s.json.in")
    prom = load_fixture("prom.json.in")

    monkeypatch.setattr(backend, "k8s_get", lambda path: k8s.get(path))
    monkeypatch.setattr(backend, "k8s_get_text", lambda path: "")
    monkeypatch.setattr(backend, "_litellm_metrics", lambda: load_fixture("litellm_metrics.json.in"))
    monkeypatch.setattr(backend, "_dcgm_util", lambda: load_fixture("dcgm.json.in"))
    monkeypatch.setattr(
        backend, "_prom_query",
        lambda q: next((v for metric, v in prom.items() if metric in q), []))
    monkeypatch.setattr(backend, "HAVE_BOTO3", False)
    monkeypatch.setattr(backend, "HAVE_PSYCOPG", False)
    monkeypatch.setattr(backend, "ARGOCD_URL", "https://argocd.example")
    monkeypatch.setattr(backend, "_price_cache", {"ts": 0.0, "data": None})
    monkeypatch.setattr(backend, "_live_price_cache", {})
    monkeypatch.setenv("CLUSTER_NAME", "ai-platform-dev")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    for var in ("OPEN_WEBUI_URL", "LITELLM_URL", "LANGFUSE_URL"):
        monkeypatch.delenv(var, raising=False)

    snap = backend._build_k8s_snapshot()
    data = dict(snap)
    data.update(backend._build_metrics(snap["nodes"], snap["pods"]))
    data["ts"] = 0
    data["mts"] = 0
    return data


def test_data_json_matches_golden(backend, monkeypatch):
    check_golden("data.json.golden", build_data_json(backend, monkeypatch))


def test_golden_covers_the_interesting_cases(backend, monkeypatch):
    """Guard against a fixture edit that silently drops a scenario."""
    data = build_data_json(backend, monkeypatch)
    models = {m["name"]: m for m in data["models"]}
    assert models["qwen2-5-3b-instruct"]["status"] == "running"
    assert models["gemma-12b"]["status"] == "deploying"
    assert models["gemma-12b"]["deploy"]["provisioning"], "GPU capacity warning expected"
    assert models["broken"]["status"] == "error"
    assert {"nova-lite", "claude-static"} <= set(models)
    alerts = {(a["node"], a["issue"]) for a in data["nodeAlerts"]}
    assert ("ip-10-10-1-12.eu-central-1.compute.internal", "MemoryPressure") in alerts
    assert ("ip-10-10-9-99.eu-central-1.compute.internal", "NotReady") in alerts
    assert not any(a[0].startswith("ip-10-10-9-98") for a in alerts), "terminating node must not alert"
    assert data["cost"]["unpriced"] == 1, "c5d.large has no price in the shipped table"
    assert data["autoscaling"][0]["pool"] == "gemma-12b"
