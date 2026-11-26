"""LiteLLM's /metrics is scraped in the cluster and is not served through the ALB.

The platform's Prometheus scrapes LiteLLM for the per-model request rate that
wakes an llm-d pool from zero. From the long-term-support line LiteLLM asks for
a key on /metrics unless told otherwise; the scrape had been answering 401
since that update and nothing failed (found 2026-11-24 on a live cluster: the
target was down, scale-up under load still worked, waking from zero could not).

Turning the key off makes the endpoint open to whoever reaches the gateway, so
the gateway's ingress answers /metrics itself instead of forwarding it.
"""
import hashlib
import pathlib
import json

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

REVISION_ANNOTATION = "ai-platform/config-revision"

# Revision on the litellm pod template -> hash of the config it stands for (see
# config_hash). A change to the settings needs a new revision: add a line here,
# never edit one.
LITELLM_CONFIG_REVISIONS = {
    "2": "6cc73df136c82f9a89bb04ec2d022fcbae2ec7bfe3fda007a3b1dabd50d2dfbc",
}


def _docs(rel):
    return [d for d in yaml.safe_load_all((ROOT / rel).read_text()) if d]


def config_hash(text: str) -> str:
    """sha256 of the parsed config: comments and layout do not count, every
    setting does."""
    return hashlib.sha256(json.dumps(yaml.safe_load(text), sort_keys=True).encode()).hexdigest()


def assert_revision_follows_config(revisions: dict, deployment: dict, config_text: str) -> None:
    """The pods read their ConfigMap at start only and the manifests are plain
    files (no generated checksum), so the revision on the pod template is what
    restarts them. `revisions` records which config each revision stands for: a
    config edited without a new revision no longer matches its hash."""
    name = deployment["metadata"]["name"]
    revision = deployment["spec"]["template"]["metadata"].get("annotations", {}).get(REVISION_ANNOTATION)
    assert revision is not None, f"{name}: the pod template has no {REVISION_ANNOTATION} annotation"
    assert revision == max(revisions, key=int), (
        f"{name}: the pod template carries revision {revision!r}, the newest recorded one is "
        f"{max(revisions, key=int)!r}")
    assert len(set(revisions.values())) == len(revisions), "two revisions record the same config"
    assert config_hash(config_text) == revisions[revision], (
        f"The config of {name} changed and its pods read it at start only. Raise {REVISION_ANNOTATION} "
        f"on the pod template and record the new revision in this test with the hash "
        f"{config_hash(config_text)} (add a line, keep the old ones).")


def test_litellm_serves_metrics_without_a_key():
    cm = next(d for d in _docs("platform/services/litellm/litellm.yaml")
              if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    config = yaml.safe_load(cm["data"]["config.yaml"])
    assert config["litellm_settings"]["require_auth_for_metrics_endpoint"] is False
    assert "prometheus" in config["litellm_settings"]["callbacks"]


def test_pods_restart_when_the_config_changes():
    """LiteLLM reads its ConfigMap at start only, and the manifests are plain
    files (no generated checksum): the revision on the pod template is what
    rolls the pods. It must be bumped with the ConfigMap."""
    docs = _docs("platform/services/litellm/litellm.yaml")
    deploy = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "litellm")
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    assert_revision_follows_config(LITELLM_CONFIG_REVISIONS, deploy, cm["data"]["config.yaml"])


def test_a_config_change_without_a_new_revision_is_caught():
    """The guard itself: same revision, one setting changed."""
    docs = _docs("platform/services/litellm/litellm.yaml")
    deploy = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "litellm")
    cm = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "litellm-config")
    text = cm["data"]["config.yaml"]
    assert_revision_follows_config(LITELLM_CONFIG_REVISIONS, deploy, text + "\n# a comment changes nothing\n")
    changed = text.replace("request_timeout: 120", "request_timeout: 121")
    assert changed != text
    try:
        assert_revision_follows_config(LITELLM_CONFIG_REVISIONS, deploy, changed)
    except AssertionError as e:
        assert REVISION_ANNOTATION in str(e)
    else:
        raise AssertionError("a changed setting passed with the old revision")


def test_prometheus_scrapes_the_litellm_service():
    cm = next(d for d in _docs("platform/config/autoscaling-prometheus.yaml") if d["kind"] == "ConfigMap")
    prom = yaml.safe_load(next(iter(cm["data"].values())))
    job = next(j for j in prom["scrape_configs"] if j["job_name"] == "litellm")
    assert job["metrics_path"] == "/metrics"
    assert job["static_configs"][0]["targets"] == ["litellm.ai-platform.svc.cluster.local:4000"]
    assert "authorization" not in job and "bearer_token" not in job


def test_the_gateway_ingress_does_not_forward_metrics():
    ing = next(d for d in _docs("platform/config/ingress.yaml")
               if d["kind"] == "Ingress" and d["metadata"]["name"] == "ai-platform-litellm")
    action = json.loads(ing["metadata"]["annotations"]["alb.ingress.kubernetes.io/actions.metrics-not-served"])
    assert action["type"] == "fixed-response" and action["fixedResponseConfig"]["statusCode"] == "404"
    paths = ing["spec"]["rules"][0]["http"]["paths"]
    blocked = [p for p in paths if p["backend"]["service"]["name"] == "metrics-not-served"]
    assert [(p["path"], p["pathType"]) for p in blocked] == [("/metrics", "Exact"), ("/metrics/", "Prefix")]
    assert all(p["backend"]["service"]["port"] == {"name": "use-annotation"} for p in blocked)
    assert paths.index(blocked[0]) < next(i for i, p in enumerate(paths) if p["path"] == "/"), "before the catch-all"
