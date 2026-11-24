#!/usr/bin/env python3
"""Cluster dashboard backend: entry point.

The backend lives in the `dashboard` package (sources, watch caches, derive
loop, API; see dashboard/app.py) and shares the Platform Health Agent's
database and Job code in `pha`. This file starts it and keeps a small
compatibility surface used by tests at the repository root
(tests/test_tokens_per_dollar.py, tests/test_bedrock_models.py):
`_tok_per_dollar`, `_bedrock_models`, `_bedrock_models_from_config` and a
monkeypatchable `k8s_get`.
"""
from __future__ import annotations

import logging
import sys

from dashboard import config, cost, deps, models
from pha import jobs as pha_jobs
from pha import log as pha_log

log = logging.getLogger("dashboard")

# Python packages for the Remediator Job. The exact `name==version` pins stay
# in this file: automated dependency updates look for them here.
REMEDIATOR_PIP_PACKAGES = "psycopg[binary]==3.3.9 awslabs.eks-mcp-server==0.2.3"


def build_remediator_job(investigation_id: str, settings: config.Settings | None = None) -> dict:
    s = settings or config.load()
    return pha_jobs.remediator_job(investigation_id, namespace=s.agent_namespace, python_image=s.python_image,
                                   kubectl_version=s.kubectl_version, pip_packages=REMEDIATOR_PIP_PACKAGES)


# ---------------------------------------------------------------------------
# Compatibility surface (repository-root tests)
# ---------------------------------------------------------------------------

_tok_per_dollar = cost.tok_per_dollar
_client = None


def k8s_get(path: str) -> dict | None:
    """GET a Kubernetes API path; None on any error (pre-rebuild contract)."""
    global _client
    try:
        if _client is None:
            from dashboard.k8s import KubeClient
            _client = KubeClient()
        return _client.get(path)
    except Exception:
        return None


def _bedrock_models_from_config() -> list:
    """Static Bedrock models declared in the litellm-config ConfigMap."""
    try:
        import yaml
    except ImportError:
        return []
    s = config.load()
    cm = k8s_get(f"/api/v1/namespaces/{s.litellm_namespace}/configmaps/{s.litellm_config_cm}")
    if not cm:
        return []
    try:
        return models.bedrock_from_config(yaml.safe_load((cm.get("data") or {}).get("config.yaml", "")) or {})
    except yaml.YAMLError:
        return []


def _bedrock_models() -> list:
    """Static litellm-config entries plus BedrockModel CRs; a CR wins on a name clash."""
    crs = (k8s_get("/apis/bedrock.ai-platform/v1alpha1/bedrockmodels") or {}).get("items", [])
    return models.merge_bedrock(_bedrock_models_from_config(), models.bedrock_from_crs(crs))


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

def main() -> int:
    pha_log.setup("cluster-dashboard")
    settings = config.load()
    found = deps.check()

    from dashboard.app import App
    from dashboard.api import serve

    kube, kube_error = None, None
    if found.has("kubernetes"):
        try:
            from dashboard.k8s import KubeClient
            kube = KubeClient()
        except Exception as e:  # no in-cluster config: run without Kubernetes data
            kube_error = f"kubernetes client unavailable: {type(e).__name__}: {e}"[:300]
            log.exception("kubernetes client unavailable")
    yaml_load = None
    if found.has("yaml"):
        import yaml
        yaml_load = yaml.safe_load
    boto3_client = None
    if found.has("boto3"):
        import boto3
        boto3_client = lambda: boto3.client("pricing", region_name=settings.pricing_endpoint_region)  # noqa: E731

    app = App(settings, found, kube=kube, kube_error=kube_error, yaml_load=yaml_load, boto3_client=boto3_client,
              remediator_job=lambda inv_id: build_remediator_job(inv_id, settings))
    app.start()
    log.info("startup", extra={"port": settings.port, "degraded": found.degraded(),
                               "kubernetes": kube is not None, "approvals": app.approvals.available})
    serve(app, settings.port, settings.html_dir, settings.fonts_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
