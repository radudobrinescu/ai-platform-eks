"""Golden test for the /data.json document the UI (and the owner's pre-flight
script) read.

The recorded inputs in fixtures/ describe a small cluster: a GPU node serving a
vLLM model, an llm-d model stuck on GPU capacity, a kro endpoint in ERROR, a
node under memory pressure, a NotReady node, a node Karpenter is removing, a
Bedrock CR plus a static Bedrock entry, LiteLLM traffic and team spend, DCGM
utilization and one KEDA autoscaler.

fixtures/data.json.golden was produced by the pre-rebuild backend (U1). The
rebuilt backend (watch caches, sources, derive loop, store) must produce the
same document from the same inputs; the fields added by the rebuild
(`sourceStatus`, `degraded`) are checked separately.
"""
from dash_testlib import check_golden, fixture_app


def build_data_json():
    app = fixture_app()
    app.derive_once()
    doc = app.store.data_json()
    added = {k: doc.pop(k) for k in ("sourceStatus", "degraded")}
    doc["ts"] = doc["mts"] = 0
    return doc, added


def test_data_json_matches_golden():
    doc, _ = build_data_json()
    check_golden("data.json.golden", doc)


def test_added_fields_report_every_source():
    _, added = build_data_json()
    names = [s["name"] for s in added["sourceStatus"]]
    assert names[0] == "kubernetes" and {"litellm", "dcgm", "pricing", "approvals"} <= set(names)
    assert {s["name"]: s["state"] for s in added["sourceStatus"]}["kubernetes"] == "ok"
    assert added["degraded"] == []


def test_golden_covers_the_interesting_cases():
    """Guard against a fixture edit that silently drops a scenario."""
    data, _ = build_data_json()
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
