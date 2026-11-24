"""Dashboard settings, read once from the environment.

Names and defaults are the ones the Deployment (manifests.yaml) and the
Terraform-written `cluster-dashboard-links` ConfigMap already use.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(env: dict, name: str, default: int) -> int:
    raw = env.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e


@dataclass(frozen=True)
class Settings:
    port: int = 8080
    html_dir: str = "/html"
    pydeps_dir: str = "/pydeps"

    cluster_name: str = ""
    aws_region: str = ""

    # Agent database (approvals)
    db_host: str = "platform-db.ai-platform.svc.cluster.local"
    db_port: int = 5432
    db_name: str = "platform_health_agent"
    db_user: str = ""
    db_password: str = field(default="", repr=False)
    agent_namespace: str = "ai-platform"
    approval_expiry_hours: int = 24
    stale_investigation_minutes: int = 15
    max_remediations_per_day: int = 20
    python_image: str = "python:3.12-slim"
    kubectl_version: str = "v1.32.5"

    # Mutation auth
    oidc_issuer_url: str = ""
    oidc_client_id: str = ""
    oidc_allowed_groups: tuple[str, ...] = ("ai-platform-admins",)
    auth_required: bool = True

    # Links
    argocd_url: str = ""
    open_webui_url: str = ""
    litellm_url: str = ""
    langfuse_url: str = ""
    links_ingress_namespace: str = "ai-platform"
    links_ingress_name: str = "ai-platform-litellm"

    # Sources
    litellm_namespace: str = "ai-platform"
    litellm_api_url: str = "http://litellm.ai-platform.svc.cluster.local:4000"
    litellm_pod_selector: str = "app=litellm"
    litellm_metrics_port: int = 4000
    litellm_key_secret: str = "cluster-dashboard-litellm-key"
    litellm_config_cm: str = "litellm-config"
    node_prices_cm: str = "cluster-dashboard-node-prices"
    pricing_endpoint_region: str = "us-east-1"
    dcgm_url: str = "http://nvidia-dcgm-exporter.gpu-operator.svc.cluster.local:9400/metrics"
    dcgm_namespace: str = "gpu-operator"
    dcgm_pod_selector: str = "app=nvidia-dcgm-exporter"
    dcgm_port: str = "9400"
    autoscaling_prom_url: str = "http://autoscaling-prometheus.inference.svc.cluster.local:9090"

    @property
    def db_configured(self) -> bool:
        return bool(self.db_user and self.db_password)


def load(env: dict | None = None) -> Settings:
    e = dict(os.environ if env is None else env)
    groups = tuple(g.strip() for g in e.get("OIDC_ALLOWED_GROUPS", "ai-platform-admins").split(",") if g.strip())
    return Settings(
        cluster_name=e.get("CLUSTER_NAME", ""),
        aws_region=e.get("AWS_REGION", ""),
        db_host=e.get("DB_HOST", Settings.db_host),
        db_port=_int(e, "DB_PORT", 5432),
        db_name=e.get("DB_NAME", Settings.db_name),
        db_user=e.get("DB_USER", ""),
        db_password=e.get("DB_PASSWORD", ""),
        agent_namespace=e.get("PLATFORM_HEALTH_AGENT_NAMESPACE", Settings.agent_namespace),
        approval_expiry_hours=_int(e, "APPROVAL_EXPIRY_HOURS", 24),
        stale_investigation_minutes=_int(e, "STALE_INVESTIGATION_MINUTES", 15),
        max_remediations_per_day=_int(e, "MAX_REMEDIATIONS_PER_DAY", 20),
        python_image=e.get("PYTHON_IMAGE", Settings.python_image),
        kubectl_version=e.get("KUBECTL_VERSION", Settings.kubectl_version),
        oidc_issuer_url=e.get("OIDC_ISSUER_URL", "").rstrip("/"),
        oidc_client_id=e.get("OIDC_CLIENT_ID", ""),
        oidc_allowed_groups=groups,
        auth_required=e.get("DASHBOARD_AUTH_REQUIRED", "true").lower() not in ("false", "0", "no"),
        argocd_url=e.get("ARGOCD_URL", ""),
        open_webui_url=e.get("OPEN_WEBUI_URL", ""),
        litellm_url=e.get("LITELLM_URL", ""),
        langfuse_url=e.get("LANGFUSE_URL", ""),
        links_ingress_namespace=e.get("LINKS_INGRESS_NAMESPACE", Settings.links_ingress_namespace),
        links_ingress_name=e.get("LINKS_INGRESS_NAME", Settings.links_ingress_name),
        litellm_namespace=e.get("LITELLM_NAMESPACE", Settings.litellm_namespace),
        litellm_api_url=e.get("LITELLM_API_URL", Settings.litellm_api_url).rstrip("/"),
        litellm_pod_selector=e.get("LITELLM_POD_SELECTOR", Settings.litellm_pod_selector),
        litellm_metrics_port=_int(e, "LITELLM_METRICS_PORT", Settings.litellm_metrics_port),
        litellm_key_secret=e.get("LITELLM_KEY_SECRET", Settings.litellm_key_secret),
        litellm_config_cm=e.get("LITELLM_CONFIG_CM", Settings.litellm_config_cm),
        node_prices_cm=e.get("NODE_PRICES_CM", Settings.node_prices_cm),
        pricing_endpoint_region=e.get("PRICING_ENDPOINT_REGION", Settings.pricing_endpoint_region),
        dcgm_url=e.get("DCGM_URL", Settings.dcgm_url),
        dcgm_namespace=e.get("DCGM_NAMESPACE", Settings.dcgm_namespace),
        dcgm_pod_selector=e.get("DCGM_POD_SELECTOR", Settings.dcgm_pod_selector),
        dcgm_port=e.get("DCGM_PORT", Settings.dcgm_port),
        autoscaling_prom_url=e.get("AUTOSCALING_PROM_URL", Settings.autoscaling_prom_url),
    )
