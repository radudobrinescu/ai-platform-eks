"""LiteLLM's /metrics is scraped in the cluster and is not served through the ALB.

The platform's Prometheus scrapes LiteLLM for the per-model request rate that
wakes an llm-d pool from zero. From the long-term-support line LiteLLM asks for
a key on /metrics unless told otherwise; the scrape had been answering 401
since that update and nothing failed (found 2026-11-24 on a live cluster: the
target was down, scale-up under load still worked, waking from zero could not).

Turning the key off makes the endpoint open to whoever reaches the gateway, so
the gateway's ingress answers /metrics itself instead of forwarding it.
"""
import pathlib
import json

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _docs(rel):
    return [d for d in yaml.safe_load_all((ROOT / rel).read_text()) if d]


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
    deploy = next(d for d in _docs("platform/services/litellm/litellm.yaml")
                  if d["kind"] == "Deployment" and d["metadata"]["name"] == "litellm")
    assert deploy["spec"]["template"]["metadata"]["annotations"]["ai-platform/config-revision"] == "2"


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
