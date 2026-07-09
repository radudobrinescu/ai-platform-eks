#!/usr/bin/env python3
"""Cluster dashboard backend.

Two responsibilities:

1. (Original) Poll the Kubernetes API every 2s, build a JSON snapshot of
   nodes/pods/InferenceEndpoints, and serve it at /data.json plus static
   HTML. Browser polls /data.json — no streaming, no proxying, no auth
   in browser.

2. (New) Surface Platform Health Agent approvals from the `platform_health_agent` postgres
   database. The agent now ships as a component of THIS dashboard app (its
   event-watcher + RBAC + config live alongside these manifests, all in the
   ai-platform namespace):
     - GET  /investigations           → list of pending investigations
     - POST /investigations/<id>/approve → spawn Remediator Job in
                                           the ai-platform namespace
     - POST /investigations/<id>/dismiss → mark dismissed (no Job)
   The /data.json payload also includes `approvals_pending` (count) and
   `approvals_available` (boolean) so the topbar can render a badge.

Backwards-compatibility: when the platform_health_agent DB is unreachable (e.g. the
agent is not deployed), all approvals endpoints return 503 and the
snapshot reports `approvals_available: false` — the existing dashboard
keeps working unchanged.
"""

from __future__ import annotations

import http.client
import http.server
import decimal
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import uuid
from urllib.parse import urlparse


def _json_default(o):
    """psycopg returns NUMERIC columns as Decimal, which json can't serialize."""
    if isinstance(o, decimal.Decimal):
        return float(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")

# psycopg is optional from the code's POV — if it can't import (image
# missing the dep), approvals are disabled rather than crashing the
# dashboard.
try:
    import psycopg                       # type: ignore[import-not-found]
    HAVE_PSYCOPG = True
except Exception:
    HAVE_PSYCOPG = False

try:
    import boto3                          # type: ignore[import-not-found]
    HAVE_BOTO3 = True
except Exception:
    HAVE_BOTO3 = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PORT = 8080
HTML_DIR = "/html"
POLL_INTERVAL = 2

K8S_HOST = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
K8S_PORT = int(os.environ.get("KUBERNETES_SERVICE_PORT", "443"))
TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

DB_HOST = os.environ.get("DB_HOST", "platform-db.ai-platform.svc.cluster.local")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "platform_health_agent")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")

PLATFORM_HEALTH_AGENT_NAMESPACE = os.environ.get("PLATFORM_HEALTH_AGENT_NAMESPACE", "ai-platform")
APPROVAL_EXPIRY_HOURS = int(os.environ.get("APPROVAL_EXPIRY_HOURS", "24"))
# An investigation Job has a 10-min hard deadline (activeDeadlineSeconds=600). If a
# row is still 'running'/'remediating' well past that, the Job died without writing
# its result and the row is stuck. Past this age it becomes dismissable from the
# History tab so users can clear the noise. Must comfortably exceed the Job deadline.
STALE_INVESTIGATION_MINUTES = int(os.environ.get("STALE_INVESTIGATION_MINUTES", "15"))
MAX_REMEDIATIONS_PER_DAY = int(os.environ.get("MAX_REMEDIATIONS_PER_DAY", "20"))
KIRO_MODEL_REMEDIATE = os.environ.get("KIRO_MODEL_REMEDIATE", "auto")
PYTHON_IMAGE = os.environ.get("PYTHON_IMAGE", "python:3.12-slim")
KUBECTL_VERSION = os.environ.get("KUBECTL_VERSION", "v1.32.5")

# Quick links — centralised entry points to the platform's web UIs, surfaced in
# the dashboard so users don't have to hunt for per-cluster hostnames.
#
# All of LiteLLM / Open WebUI / Langfuse / the dashboard share one internet-
# facing ALB (the `ai-platform` ingress group), differing only by port — so we
# discover the ALB hostname at runtime from the ingress status and append the
# known ports. ArgoCD is an EKS-managed capability with its own endpoint, so its
# URL can't be derived from the ALB; Terraform passes it in via ARGOCD_URL.
ARGOCD_URL = os.environ.get("ARGOCD_URL", "")
# Namespace + ingress name the ALB-fronted services live behind.
LINKS_INGRESS_NAMESPACE = os.environ.get("LINKS_INGRESS_NAMESPACE", "ai-platform")
LINKS_INGRESS_NAME = os.environ.get("LINKS_INGRESS_NAME", "ai-platform-litellm")


# ---------------------------------------------------------------------------
# Kubernetes API client (existing logic, preserved as-is)
# ---------------------------------------------------------------------------

def get_token() -> str:
    with open(TOKEN_PATH) as f:
        return f.read().strip()


def _k8s_request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    """Generic K8s API call. Returns (status, parsed_json_or_None)."""
    try:
        ctx = ssl.create_default_context(cafile=CA_PATH)
        conn = http.client.HTTPSConnection(K8S_HOST, K8S_PORT, context=ctx, timeout=10)
        headers = {
            "Authorization": f"Bearer {get_token()}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=(json.dumps(body) if body else None), headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        return resp.status, data
    except Exception:
        return 0, None


def k8s_get(path: str) -> dict | None:
    status, data = _k8s_request("GET", path)
    return data if status == 200 else None


def k8s_post(path: str, body: dict) -> tuple[int, dict | None]:
    return _k8s_request("POST", path, body=body)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------

class DB:
    """Thin postgres wrapper. Approvals features go through this; if any
    method fails, callers fall back to 'unavailable'."""

    @staticmethod
    def available() -> bool:
        if not HAVE_PSYCOPG:
            return False
        if not (DB_USER and DB_PASSWORD):
            return False
        try:
            with DB.connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    @staticmethod
    def connect():  # type: ignore[no-untyped-def]
        return psycopg.connect(
            host=DB_HOST, port=DB_PORT, dbname=DB_NAME,
            user=DB_USER, password=DB_PASSWORD,
            autocommit=True, connect_timeout=5,
        )


# ---------------------------------------------------------------------------
# Remediator Job spec (duplicated from event_watcher.py — small, stable)
# ---------------------------------------------------------------------------

def build_remediator_job(investigation_id: str) -> dict:
    job_name = f"remediator-{investigation_id[:8]}"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name,
            "namespace": PLATFORM_HEALTH_AGENT_NAMESPACE,
            "labels": {
                "app.kubernetes.io/name": "platform-health-agent-remediator",
                "app.kubernetes.io/part-of": "platform-health-agent",
                "investigation-id": investigation_id,
            },
        },
        "spec": {
            "ttlSecondsAfterFinished": 3600,
            "activeDeadlineSeconds": 600,
            "backoffLimit": 0,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "platform-health-agent-remediator",
                        "investigation-id": investigation_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "platform-health-agent-writer",
                    "automountServiceAccountToken": True,
                    "nodeSelector": {"kubernetes.io/arch": "amd64"},
                    "initContainers": [
                        {
                            "name": "install-tools",
                            "image": "alpine:3.20",
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                f"set -eu; cd /tools; "
                                f"echo 'fetching kubectl {KUBECTL_VERSION}'; "
                                f"wget -qO kubectl https://dl.k8s.io/release/{KUBECTL_VERSION}/bin/linux/amd64/kubectl && chmod +x kubectl; "
                                f"apk add -q curl bash; "
                                f"export HOME=/tools; "
                                f"curl -fsSL https://cli.kiro.dev/install | bash; "
                                # Move ALL three binaries (kiro-cli, kiro-cli-chat,
                                # kiro-cli-term). The launcher forks to kiro-cli-chat.
                                f"mv /tools/.local/bin/* /tools/ 2>/dev/null || true; "
                                f"chmod +x /tools/kiro-cli /tools/kiro-cli-chat /tools/kiro-cli-term 2>/dev/null || true; "
                                f"ls -la /tools/",
                            ],
                            "volumeMounts": [{"name": "tools", "mountPath": "/tools"}],
                            "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False,
                                                "capabilities": {"drop": ["ALL"]}},
                        },
                        {
                            "name": "install-pydeps",
                            "image": PYTHON_IMAGE,
                            "command": ["/bin/sh", "-c"],
                            "args": [
                                "set -eu; pip install --no-cache-dir --target=/pydeps "
                                "psycopg[binary]==3.2.3 awslabs.eks-mcp-server",
                            ],
                            "volumeMounts": [{"name": "pydeps", "mountPath": "/pydeps"}],
                            "securityContext": {"runAsNonRoot": True, "runAsUser": 65532,
                                                "allowPrivilegeEscalation": False,
                                                "capabilities": {"drop": ["ALL"]}},
                        },
                    ],
                    "containers": [{
                        "name": "remediator",
                        "image": PYTHON_IMAGE,
                        "command": ["/bin/sh", "/scripts/remediate.sh"],
                        "env": [
                            {"name": "INVESTIGATION_ID", "value": investigation_id},
                            {"name": "PATH", "value": "/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
                            {"name": "PYTHONPATH", "value": "/pydeps"},
                            {"name": "HOME", "value": "/tmp"},
                            *[{"name": k, "valueFrom": {"configMapKeyRef": {"name": "platform-health-agent-config", "key": k}}}
                              for k in ["CLUSTER_NAME", "AWS_REGION", "DB_HOST", "DB_PORT", "DB_NAME",
                                        "KIRO_MODEL_REMEDIATE"]],
                            {"name": "DB_USER",      "valueFrom": {"secretKeyRef": {"name": "platform-db-credentials", "key": "username"}}},
                            {"name": "DB_PASSWORD",  "valueFrom": {"secretKeyRef": {"name": "platform-db-credentials", "key": "password"}}},
                            {"name": "KIRO_API_KEY", "valueFrom": {"secretKeyRef": {"name": "platform-health-agent-secrets",   "key": "KIRO_API_KEY"}}},
                        ],
                        "volumeMounts": [
                            {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
                            {"name": "tools",   "mountPath": "/tools",   "readOnly": True},
                            {"name": "pydeps",  "mountPath": "/pydeps",  "readOnly": True},
                            {"name": "results", "mountPath": "/results"},
                            {"name": "tmp",     "mountPath": "/tmp"},
                        ],
                        "resources": {
                            "requests": {"cpu": "200m", "memory": "256Mi"},
                            "limits":   {"cpu": "1000m", "memory": "1Gi"},
                        },
                        "securityContext": {
                            "allowPrivilegeEscalation": False,
                            "runAsNonRoot": True,
                            "runAsUser": 65532,
                            "capabilities": {"drop": ["ALL"]},
                        },
                    }],
                    "volumes": [
                        {"name": "scripts", "configMap": {"name": "platform-health-agent-scripts", "defaultMode": 0o755}},
                        {"name": "tools",   "emptyDir": {}},
                        {"name": "pydeps",  "emptyDir": {}},
                        {"name": "results", "emptyDir": {}},
                        {"name": "tmp",     "emptyDir": {}},
                    ],
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------

snapshot = {"nodes": [], "pods": [], "endpoints": [], "cost": {},
            "approvals_available": False, "approvals_pending": 0,
            "links": [], "ts": 0}
snapshot_lock = threading.Lock()


def _alb_hostname() -> str:
    """Discover the shared ALB hostname from the ingress status (empty until the
    AWS Load Balancer Controller provisions it)."""
    try:
        ing = k8s_get(
            f"/apis/networking.k8s.io/v1/namespaces/"
            f"{LINKS_INGRESS_NAMESPACE}/ingresses/{LINKS_INGRESS_NAME}"
        )
        return ((ing.get("status", {}).get("loadBalancer", {})
                 .get("ingress", [{}]) or [{}])[0].get("hostname", "")) or ""
    except Exception:
        return ""


def _build_links() -> list:
    """Quick links to the platform's web UIs. The ALB-fronted services share one
    hostname and differ by port; ArgoCD is an EKS capability with its own URL
    (ARGOCD_URL from Terraform). Links with no resolvable URL are omitted."""
    alb = _alb_hostname()
    links = []
    if alb:
        links += [
            {"label": "Open WebUI", "url": f"http://{alb}:8080",
             "desc": "Chat with models", "icon": "💬"},
            {"label": "LiteLLM Admin", "url": f"http://{alb}:4000/ui",
             "desc": "API gateway + keys + usage", "icon": "🔑"},
            {"label": "Langfuse", "url": f"http://{alb}:3000",
             "desc": "Traces, evals, cost", "icon": "📊"},
        ]
    if ARGOCD_URL:
        links.append({"label": "ArgoCD", "url": ARGOCD_URL,
                      "desc": "GitOps sync status", "icon": "🚢"})
    return links


# Serving kinds surfaced on the dashboard. Ray InferenceEndpoint is legacy
# (being retired); VLLMEndpoint (simple) and LLMDEndpoint (llm-d scale tier) are
# the current path. All three are kro.run/v1alpha1 in the `inference` namespace.
SERVING_KINDS = [
    ("inferenceendpoints", "ray"),
    ("vllmendpoints", "vllm"),
    ("llmdendpoints", "llm-d"),
]


# Node hourly cost estimate — eu-central-1 on-demand list price ($/hr, Linux/
# Shared) from the AWS Pricing API. Spot nodes are approximated at SPOT_FACTOR of
# on-demand (real spot price varies). Unknown instance types are counted as
# `unpriced` so the total stays honest rather than silently undercounting.
# On-demand $/hr by region → instance type. Ships with eu-central-1 (the
# reference deployment). Other regions are supplied at runtime via an optional
# ConfigMap (see _prices_by_region) so the estimate works in any account/region
# without code changes. Unpriced instance types are counted and surfaced in the
# UI rather than guessed.
DEFAULT_PRICES_BY_REGION = {
    "eu-central-1": {
        "g6.xlarge": 1.0064, "g6.2xlarge": 1.2225, "g6.4xlarge": 1.6547, "g6.8xlarge": 2.519,
        "g6.12xlarge": 5.7543, "g6.16xlarge": 4.2477, "g6.24xlarge": 8.3473, "g6.48xlarge": 16.6946,
        "g5.xlarge": 1.258, "g5.2xlarge": 1.5156, "g5.4xlarge": 2.0308, "g5.8xlarge": 3.0612,
        "g5.12xlarge": 7.0928, "g5.48xlarge": 20.3681,
        "c5.large": 0.097, "c5.xlarge": 0.194, "c5.2xlarge": 0.388,
        "c5a.large": 0.087, "c5a.xlarge": 0.174,
        "c7i.large": 0.1018, "c7i.xlarge": 0.2037, "c7a.large": 0.1171, "c7a.xlarge": 0.2343,
        "m6g.large": 0.092, "m6g.xlarge": 0.184, "m5.large": 0.115, "m5.xlarge": 0.23, "r5.large": 0.152,
    },
}
SPOT_FACTOR = 0.35        # rough spot discount vs on-demand
HOURS_PER_MONTH = 730
NODE_PRICES_CM = os.environ.get("NODE_PRICES_CM", "cluster-dashboard-node-prices")
_price_cache: dict = {"ts": 0.0, "data": None}


def _prices_by_region() -> dict:
    """Region -> {instance: $/hr}. Merges the shipped defaults with an optional
    operator/Terraform-supplied ConfigMap (data['prices.json'] =
    {"<region>": {"<instance>": <usd_hourly>}}) so the cost estimate is not tied
    to a single account or region. Cached for an hour; best-effort."""
    now = time.time()
    if _price_cache["data"] is not None and now - _price_cache["ts"] < 3600:
        return _price_cache["data"]
    merged = {r: dict(v) for r, v in DEFAULT_PRICES_BY_REGION.items()}
    try:
        cm = k8s_get(f"/api/v1/namespaces/{LITELLM_NAMESPACE}/configmaps/{NODE_PRICES_CM}")
        raw = ((cm or {}).get("data") or {}).get("prices.json", "")
        for region, table in (json.loads(raw) if raw else {}).items():
            merged.setdefault(region, {}).update({k: float(v) for k, v in table.items()})
    except Exception:
        pass
    _price_cache.update(ts=now, data=merged)
    return merged


def _detect_region(nodes: list) -> str:
    """Most common node region label, falling back to the AWS_REGION env."""
    counts: dict = {}
    for n in nodes:
        r = n.get("region")
        if r:
            counts[r] = counts.get(r, 0) + 1
    return max(counts, key=counts.get) if counts else os.environ.get("AWS_REGION", "")


PRICING_ENDPOINT_REGION = os.environ.get("PRICING_ENDPOINT_REGION", "us-east-1")
_live_price_cache: dict = {}   # region -> {"ts": float, "prices": {instance: usd}}


def _fetch_ec2_ondemand(client, region: str, instance_type: str):
    """One instance type's on-demand Linux/shared $/hr from the Price List API."""
    resp = client.get_products(ServiceCode="AmazonEC2", MaxResults=1, Filters=[
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
    ])
    for item in resp.get("PriceList", []):
        terms = (json.loads(item).get("terms", {}) or {}).get("OnDemand", {}) or {}
        for term in terms.values():
            for dim in (term.get("priceDimensions", {}) or {}).values():
                usd = (dim.get("pricePerUnit", {}) or {}).get("USD")
                if usd and float(usd) > 0:
                    return round(float(usd), 4)
    return None


def _live_prices(region: str, instances: tuple) -> dict:
    """Live on-demand prices from the AWS Price List API (needs pricing:GetProducts
    via Pod Identity + boto3). Cached per region for 6h; missing types fetched on
    demand. Best-effort — returns {} if boto3/creds/perms are unavailable so the
    caller falls back to the shipped/ConfigMap table."""
    if not (HAVE_BOTO3 and region and instances):
        return {}
    ent = _live_price_cache.get(region)
    now = time.time()
    if ent is None or now - ent["ts"] > 21600:
        ent = {"ts": now, "prices": {}}
        _live_price_cache[region] = ent
    missing = [i for i in instances if i not in ent["prices"]]
    if missing:
        try:
            client = boto3.client("pricing", region_name=PRICING_ENDPOINT_REGION)
            for it in missing:
                try:
                    p = _fetch_ec2_ondemand(client, region, it)
                    if p is not None:
                        ent["prices"][it] = p
                except Exception:
                    pass
        except Exception:
            pass
    return dict(ent["prices"])


def _compute_cost(nodes: list) -> dict:
    """Sum the running nodes' hourly rates -> realtime $/hr + 730h monthly
    projection, split GPU vs platform. Region-aware; annotates each node with
    its `hourly`. Instance types with no price data are counted (`unpriced`).
    Prefers live AWS Price List data, falling back to the ConfigMap/shipped table."""
    region = _detect_region(nodes)
    instances = tuple(sorted({n.get("instance") for n in nodes
                              if n.get("instance") and n.get("instance") != "unknown"}))
    prices = dict(_prices_by_region().get(region, {}))
    live = _live_prices(region, instances)
    prices.update(live)
    price_source = "aws-price-list" if live else ("table" if prices else "none")
    hourly = gpu_hourly = 0.0
    unpriced = 0
    for n in nodes:
        base = prices.get(n["instance"])
        if base is None:
            unpriced += 1
            n["hourly"] = 0.0
            continue
        rate = base * (SPOT_FACTOR if n.get("capacity") == "spot" else 1.0)
        n["hourly"] = round(rate, 4)
        hourly += rate
        if n["gpu"] > 0:
            gpu_hourly += rate
    return {
        "hourly": round(hourly, 4),
        "monthly": round(hourly * HOURS_PER_MONTH, 2),
        "gpuHourly": round(gpu_hourly, 4),
        "platformHourly": round(hourly - gpu_hourly, 4),
        "unpriced": unpriced,
        "region": region,
        "priceSource": price_source,
    }


def _normalize_endpoint(ep: dict, mode: str) -> dict:
    """Flatten a serving CR (any of the three kinds) into one dashboard record
    with a `mode` field. Status is normalized so the UI can render all kinds
    uniformly: Ray reports modelStatus; vLLM/llm-d report ready +
    availableReplicas; llm-d also reports routerHealth (EPP/router health)."""
    spec = ep.get("spec", {}) or {}
    status = ep.get("status", {}) or {}
    gpu_count = int(spec.get("gpuCount", 1) or 1)
    tp = int(spec.get("tensorParallelSize", 0) or 0)
    pp = int(spec.get("pipelineParallelSize", 1) or 1)
    if tp == 0:
        tp = gpu_count if pp == 1 else 1
    # Desired replicas: llm-d uses `replicas`; ray/vllm use `minReplicas`.
    replicas = int(spec.get("replicas", spec.get("minReplicas", 1)) or 1)
    ready = str(status.get("ready", "False"))
    avail = int(status.get("availableReplicas", 0) or 0)
    norm_status = status.get("modelStatus", "Pending") if mode == "ray" \
        else ("Running" if ready == "True" else "Pending")
    return {
        "name": ep["metadata"]["name"],
        "model": spec.get("model", ""),
        "mode": mode,
        "shared": bool(spec.get("shared", False)),
        "gpuCount": gpu_count,
        "tp": tp,
        "pp": pp,
        "replicas": replicas,
        "availableReplicas": avail,
        "ready": ready,
        # `modelStatus` kept for back-compat with the current HTML; `status` is
        # the normalized, mode-agnostic label the reworked UI should use.
        "modelStatus": norm_status,
        "status": norm_status,
        "routerHealth": str(status.get("routerHealth", "")) if mode == "llm-d" else "",
        "message": status.get("message", "") if mode == "ray" else "",
        "created": ep["metadata"].get("creationTimestamp", ""),
    }


def _fetch_endpoints() -> list:
    """Poll every serving kind in the `inference` namespace and return normalized
    records. A kind whose CRD isn't installed is skipped silently."""
    out = []
    for plural, mode in SERVING_KINDS:
        data = k8s_get(f"/apis/kro.run/v1alpha1/namespaces/inference/{plural}")
        if not data:
            continue
        for ep in data.get("items", []):
            try:
                out.append(_normalize_endpoint(ep, mode))
            except Exception:
                continue
    out.sort(key=lambda e: (e["mode"], e["name"]))
    return out


def _cpu_m(s) -> int:
    """K8s CPU quantity -> millicores."""
    s = str(s or "0").strip()
    try:
        if s.endswith("m"): return int(float(s[:-1]))
        if s.endswith("n"): return int(float(s[:-1]) / 1e6)
        if s.endswith("u"): return int(float(s[:-1]) / 1e3)
        return int(float(s) * 1000)
    except Exception:
        return 0


def _mem_mi(s) -> int:
    """K8s memory quantity -> MiB."""
    s = str(s or "0").strip()
    for u, f in (("Ki", 1 / 1024), ("Mi", 1.0), ("Gi", 1024.0), ("Ti", 1048576.0),
                 ("K", 0.000953674), ("M", 0.953674), ("G", 953.674)):
        if s.endswith(u):
            try: return int(float(s[:-len(u)]) * f)
            except Exception: return 0
    try: return int(float(s) / 1048576)
    except Exception: return 0


def _build_k8s_snapshot() -> dict:
    nodes_data = k8s_get("/api/v1/nodes")
    pods_data = k8s_get("/api/v1/pods")

    nodes = []
    node_alerts = []
    if nodes_data:
        for n in nodes_data.get("items", []):
            labels = n.get("metadata", {}).get("labels", {})
            nm = n["metadata"]["name"]
            conditions = n.get("status", {}).get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            # EKS node health: surface resource pressures + NotReady as alerts.
            for c in conditions:
                t = c.get("type")
                if t in ("MemoryPressure", "DiskPressure", "PIDPressure") and c.get("status") == "True":
                    node_alerts.append({"node": nm, "issue": t, "sev": "warn"})
            if not ready:
                node_alerts.append({"node": nm, "issue": "NotReady", "sev": "warn"})
                continue
            allocatable = n.get("status", {}).get("allocatable", {})
            zone = labels.get("topology.kubernetes.io/zone", "")
            nodes.append({
                "name": nm,
                "instance": labels.get("node.kubernetes.io/instance-type", "unknown"),
                "pool": labels.get("karpenter.sh/nodepool",
                                   "__mng__" if labels.get("eks.amazonaws.com/nodegroup") else "__mng__"),
                "zone": zone,
                "region": labels.get("topology.kubernetes.io/region", zone[:-1] if zone else ""),
                "capacity": labels.get("karpenter.sh/capacity-type",
                                       labels.get("eks.amazonaws.com/capacityType", "on-demand")),
                "gpu": int(allocatable.get("nvidia.com/gpu", "0")),
                "gpuProduct": labels.get("nvidia.com/gpu.product", "").replace("-", " "),
                "cpuAllocM": _cpu_m(allocatable.get("cpu", "0")),
                "memAllocMi": _mem_mi(allocatable.get("memory", "0")),
                "created": n["metadata"].get("creationTimestamp", ""),
            })

    # Actual node usage from metrics-server (metrics.k8s.io). Best-effort: with
    # no metrics-server, cpuUsedM/memUsedMi stay None and the UI shows only
    # requested. cpu is nanocores/millicores, memory is Ki/Mi — reuse parsers.
    node_usage = {}
    try:
        mu = k8s_get("/apis/metrics.k8s.io/v1beta1/nodes")
        for it in (mu or {}).get("items", []):
            u = it.get("usage", {}) or {}
            node_usage[it.get("metadata", {}).get("name", "")] = {
                "cpuUsedM": _cpu_m(u.get("cpu", "0")),
                "memUsedMi": _mem_mi(u.get("memory", "0")),
            }
    except Exception:
        node_usage = {}
    for n in nodes:
        n["cpuUsedM"] = node_usage.get(n["name"], {}).get("cpuUsedM")
        n["memUsedMi"] = node_usage.get(n["name"], {}).get("memUsedMi")

    pods = []
    if pods_data:
        for p in pods_data.get("items", []):
            phase = p.get("status", {}).get("phase", "")
            if phase not in ("Running", "Pending"):
                continue
            containers = p.get("spec", {}).get("containers", [])
            reqs = [c.get("resources", {}).get("requests", {}) or {} for c in containers]
            gpu_req = sum(int(r.get("nvidia.com/gpu", "0") or 0) for r in reqs)
            cpu_req = sum(_cpu_m(r.get("cpu", "0")) for r in reqs)
            mem_req = sum(_mem_mi(r.get("memory", "0")) for r in reqs)
            pods.append({
                "name": p["metadata"]["name"],
                "namespace": p["metadata"]["namespace"],
                "node": p.get("spec", {}).get("nodeName", ""),
                "phase": phase,
                "gpu": gpu_req,
                "cpuReqM": cpu_req,
                "memReqMi": mem_req,
                "created": p["metadata"].get("creationTimestamp", ""),
            })

    endpoints = _fetch_endpoints()
    cost = _compute_cost(nodes)

    # Approvals snapshot — best-effort, never blocks the dashboard.
    approvals_available = False
    approvals_pending = 0
    if HAVE_PSYCOPG and DB_USER and DB_PASSWORD:
        try:
            with DB.connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM investigations WHERE status='awaiting_approval'")
                approvals_pending = int(cur.fetchone()[0] or 0)  # type: ignore[index]
                approvals_available = True
        except Exception:
            approvals_available = False

    return {
        "nodes": nodes, "pods": pods, "endpoints": endpoints, "nodeAlerts": node_alerts,
        "cost": cost,
        "approvals_available": approvals_available,
        "approvals_pending": approvals_pending,
        "links": _build_links(),
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# v2 metrics layer — LiteLLM DB (traffic/latency/spend/teams/health) + DCGM GPU
# utilization + ArgoCD sync + Bedrock model list. All in-cluster; each source is
# best-effort and reported in `sources` for graceful degradation in the UI.
# ---------------------------------------------------------------------------

LITELLM_DB_NAME    = os.environ.get("LITELLM_DB_NAME", "litellm")
DCGM_URL           = os.environ.get("DCGM_URL", "http://nvidia-dcgm-exporter.gpu-operator.svc.cluster.local:9400/metrics")
DCGM_NAMESPACE     = os.environ.get("DCGM_NAMESPACE", "gpu-operator")
DCGM_POD_SELECTOR  = os.environ.get("DCGM_POD_SELECTOR", "app=nvidia-dcgm-exporter")
DCGM_PORT          = os.environ.get("DCGM_PORT", "9400")
LITELLM_CONFIG_CM  = os.environ.get("LITELLM_CONFIG_CM", "litellm-config")
LITELLM_NAMESPACE  = os.environ.get("LITELLM_NAMESPACE", "ai-platform")
METRICS_INTERVAL   = int(os.environ.get("METRICS_INTERVAL", "25"))

try:
    import yaml as _yaml
    HAVE_YAML = True
except Exception:
    HAVE_YAML = False

metrics = {"models": [], "teams": [], "gpu": [], "argocd": [],
           "costExt": {}, "sources": {}, "mts": 0}
metrics_lock = threading.Lock()


def _lldb_connect():  # type: ignore[no-untyped-def]
    return psycopg.connect(host=DB_HOST, port=DB_PORT, dbname=LITELLM_DB_NAME,
                           user=DB_USER, password=DB_PASSWORD,
                           autocommit=True, connect_timeout=5)


def _litellm_metrics() -> dict:
    """Per-model 5-min metrics + model health + per-team spend/budgets + cost trend."""
    out = {"perModel": {}, "teams": [], "trend": [], "health": {}, "byTeam": []}
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return out
    with _lldb_connect() as conn, conn.cursor() as cur:
        cur.execute("""
          SELECT model_group, custom_llm_provider, count(*)/5.0,
                 percentile_cont(0.5)  WITHIN GROUP (ORDER BY extract(epoch FROM ("endTime"-"startTime"))*1000),
                 percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM ("endTime"-"startTime"))*1000),
                 COALESCE(sum(total_tokens),0)/300.0, COALESCE(sum(spend),0),
                 100.0*avg(CASE WHEN status='success' THEN 0 ELSE 1 END)
          FROM "LiteLLM_SpendLogs" WHERE "startTime" > now() - interval '5 minutes'
          GROUP BY model_group, custom_llm_provider""")
        for r in cur.fetchall():
            out["perModel"][r[0]] = {"provider": r[1], "rpm": round(r[2], 1),
                "p50": round(r[3]) if r[3] is not None else None,
                "p95": round(r[4]) if r[4] is not None else None,
                "tps": round(r[5], 1), "spend5m": float(r[6]),
                "errPct": round(float(r[7]), 1) if r[7] is not None else 0}
        try:
            cur.execute('SELECT DISTINCT ON (model_name) model_name,status,response_time_ms FROM "LiteLLM_HealthCheckTable" ORDER BY model_name,checked_at DESC')
            for r in cur.fetchall():
                out["health"][r[0]] = {"status": r[1], "rt": float(r[2]) if r[2] is not None else None}
        except Exception:
            pass
        try:
            cur.execute("""
              SELECT t.team_id, COALESCE(t.team_alias, left(t.team_id::text,8)), t.max_budget, COALESCE(t.spend,0),
                     COALESCE(sum(s.api_requests),0), COALESCE(sum(s.failed_requests),0),
                     COALESCE(sum(s.spend),0), COALESCE(sum(s.prompt_tokens+s.completion_tokens),0)
              FROM "LiteLLM_TeamTable" t
              LEFT JOIN "LiteLLM_DailyTeamSpend" s ON s.team_id=t.team_id AND s.date=CURRENT_DATE::text
              GROUP BY t.team_id, t.team_alias, t.max_budget, t.spend""")
            teams = {r[0]: {"id": r[0], "name": r[1], "budget": round(float(r[3]), 2),
                            "limit": float(r[2]) if r[2] else None, "reqsToday": int(r[4]),
                            "failed": int(r[5]), "spendToday": round(float(r[6]), 2),
                            "tokens": int(r[7]), "keys": 0} for r in cur.fetchall()}
            try:
                cur.execute('SELECT team_id,count(*) FROM "LiteLLM_VerificationToken" WHERE team_id IS NOT NULL GROUP BY team_id')
                for tid, c in cur.fetchall():
                    if tid in teams:
                        teams[tid]["keys"] = int(c)
            except Exception:
                pass
            out["teams"] = list(teams.values())
            out["byTeam"] = sorted([[t["name"], t["spendToday"]] for t in out["teams"] if t["spendToday"] > 0], key=lambda x: -x[1])
        except Exception:
            pass
        try:
            cur.execute('SELECT date::text, COALESCE(sum(spend),0) FROM "LiteLLM_DailyTeamSpend" GROUP BY date ORDER BY date DESC LIMIT 14')
            out["trend"] = [[r[0], round(float(r[1]), 2)] for r in cur.fetchall()][::-1]
        except Exception:
            pass
    return out


def _dcgm_util() -> dict:
    """Per-node GPU utilization (%) parsed from dcgm-exporter Prometheus text.

    Scrapes EVERY dcgm-exporter pod directly (one per GPU node), not the
    round-robin Service VIP — the VIP returns only whichever pod it load-balanced
    to, so a multi-GPU-node cluster would show just one node. Falls back to the
    Service URL if pod discovery yields nothing (e.g. missing pods RBAC)."""
    import urllib.request

    def _parse(text: str, out: dict) -> None:
        for line in text.splitlines():
            if not line.startswith("DCGM_FI_DEV_GPU_UTIL{"):
                continue
            try:
                labels = line[line.index("{") + 1:line.index("}")]
                val = float(line.rsplit(" ", 1)[1])
                host = next((kv.split("=", 1)[1].strip('"') for kv in labels.split(",")
                             if kv.startswith("Hostname=")), None)
                if host:
                    out.setdefault(host, []).append(round(val))
            except Exception:
                continue

    def _scrape(url: str, out: dict) -> None:
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=4) as r:
                _parse(r.read().decode("utf-8", "ignore"), out)
        except Exception:
            pass

    out: dict = {}
    urls = []
    try:
        pods = k8s_get(f"/api/v1/namespaces/{DCGM_NAMESPACE}/pods"
                       f"?labelSelector={DCGM_POD_SELECTOR}")
        for p in (pods or {}).get("items", []):
            st = p.get("status", {})
            if st.get("phase") == "Running" and st.get("podIP"):
                urls.append(f"http://{st['podIP']}:{DCGM_PORT}/metrics")
    except Exception:
        urls = []

    for url in urls:
        _scrape(url, out)
    if not out:            # discovery failed or pods unreachable → Service VIP
        _scrape(DCGM_URL, out)
    return out


def _bedrock_models() -> list:
    """Declared Bedrock (config-defined) models from the litellm-config ConfigMap."""
    if not HAVE_YAML:
        return []
    cm = k8s_get(f"/api/v1/namespaces/{LITELLM_NAMESPACE}/configmaps/{LITELLM_CONFIG_CM}")
    if not cm:
        return []
    try:
        cfg = _yaml.safe_load((cm.get("data") or {}).get("config.yaml", "")) or {}
        out = []
        for m in (cfg.get("model_list") or []):
            mdl = str((m.get("litellm_params") or {}).get("model", ""))
            if mdl.startswith("bedrock/"):
                out.append({"name": m.get("model_name", ""), "model": mdl})
        return out
    except Exception:
        return []


def _build_metrics(nodes: list, pods: list) -> dict:
    endpoints = _fetch_endpoints()
    src: dict = {}
    try:
        met = _litellm_metrics(); src["litellm"] = True
    except Exception:
        met = {"perModel": {}, "teams": [], "trend": [], "health": {}, "byTeam": []}; src["litellm"] = False
    try:
        bedrock = _bedrock_models(); src["bedrock"] = True
    except Exception:
        bedrock = []; src["bedrock"] = False

    node_by_name = {n["name"]: n for n in nodes}
    def cost_for(name: str) -> float:
        c = 0.0
        for p in pods:
            if p.get("namespace") == "inference" and p["name"].startswith(name + "-") and p.get("gpu", 0) > 0:
                nd = node_by_name.get(p.get("node"))
                if nd and nd.get("gpu"):
                    c += nd.get("hourly", 0) * p["gpu"] / nd["gpu"]
        return round(c, 2)

    models = []
    for ep in endpoints:
        tier = "ft" if ep.get("modelSource") else ep["mode"]
        models.append({"name": ep["name"], "model": ep["model"], "tier": tier,
            "status": "running" if ep["ready"] == "True" else "deploying",
            "gpu": ("shared" if ep.get("shared") else f'{ep["gpuCount"]}×GPU'),
            "replicas": ep.get("replicas"), "availableReplicas": ep.get("availableReplicas"),
            "router": ep.get("routerHealth", ""), "costHr": cost_for(ep["name"]),
            "created": ep.get("created")})
    for b in bedrock:
        models.append({"name": b["name"], "model": b["model"], "tier": "bedrock",
            "status": "running", "gpu": "—", "replicas": None, "availableReplicas": None,
            "router": "", "costHr": 0.0, "created": None})
    for m in models:
        pm = met["perModel"].get(m["name"])
        m["rpm"] = pm["rpm"] if pm else 0
        m["p50"] = pm["p50"] if pm else None
        m["p95"] = pm["p95"] if pm else None
        m["tps"] = pm["tps"] if pm else 0
        m["errPct"] = pm["errPct"] if pm else 0
        if pm and m["tier"] == "bedrock":
            m["costHr"] = round(pm["spend5m"] * 12, 2)
        h = met["health"].get(m["name"])
        m["health"] = h["status"] if h else ""
    models.sort(key=lambda x: (-(x.get("rpm") or 0), x["name"]))

    gpu = []
    try:
        util = _dcgm_util(); src["dcgm"] = True
        for n in nodes:
            if n.get("gpu", 0) > 0:
                host = n["name"].split(".")[0]
                us = util.get(host) or util.get(n["name"]) or [0] * n["gpu"]
                mdl = next((e["name"] for e in endpoints for p in pods
                            if p.get("node") == n["name"] and p.get("gpu", 0) > 0
                            and p["name"].startswith(e["name"] + "-")), None)
                gpu.append({"node": host, "instance": n["instance"], "util": us, "model": mdl})
    except Exception:
        src["dcgm"] = False

    argo = []
    try:
        d = k8s_get("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications")
        if d is not None:
            for a in d.get("items", []):
                st = a.get("status", {})
                argo.append({"name": a["metadata"]["name"],
                             "sync": st.get("sync", {}).get("status", "?"),
                             "health": st.get("health", {}).get("status", "?")})
            src["argocd"] = True
        else:
            src["argocd"] = False
    except Exception:
        src["argocd"] = False

    by_model = sorted([[m["name"], m["costHr"], m["tier"]] for m in models if m["costHr"] > 0], key=lambda x: -x[1])
    cost_ext = {"byModel": by_model, "byTeam": met.get("byTeam", []), "trend": met.get("trend", [])}
    return {"models": models, "teams": met.get("teams", []), "gpu": gpu, "argocd": argo,
            "costExt": cost_ext, "sources": src, "mts": time.time()}


def metrics_loop() -> None:
    global metrics
    while True:
        try:
            with snapshot_lock:
                nodes = list(snapshot.get("nodes", [])); pods = list(snapshot.get("pods", []))
            m = _build_metrics(nodes, pods)
            with metrics_lock:
                metrics = m
        except Exception:
            pass
        time.sleep(METRICS_INTERVAL)


def poll_loop() -> None:
    global snapshot
    while True:
        try:
            new_snapshot = _build_k8s_snapshot()
            with snapshot_lock:
                snapshot = new_snapshot
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Approvals API
# ---------------------------------------------------------------------------

def list_pending_investigations() -> list[dict]:
    """Return investigations awaiting approval (newest first)."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return []
    try:
        with DB.connect() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
            cur.execute(
                """SELECT id, created_at, trigger_kind, resource_kind,
                          resource_namespace, resource_name,
                          findings, fix_commands
                     FROM investigations
                    WHERE status = 'awaiting_approval'
                    ORDER BY created_at DESC LIMIT 50""",
            )
            rows = cur.fetchall() or []
        out = []
        for r in rows:
            r["id"] = str(r["id"])
            r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
            out.append(r)
        return out
    except Exception:
        return []


def list_all_investigations(limit: int = 20) -> list[dict]:
    """Return recent investigations (any status), newest first.
    Used by the History tab in the dashboard."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return []
    try:
        with DB.connect() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
            cur.execute(
                """SELECT id, created_at, completed_at, approved_at, status,
                          trigger_kind, resource_kind, resource_namespace, resource_name,
                          findings, fix_commands, remediation_result,
                          approved_by, error_message, out_of_scope
                     FROM investigations
                    ORDER BY created_at DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall() or []
        out = []
        stale_cutoff = time.time() - STALE_INVESTIGATION_MINUTES * 60
        for r in rows:
            r["id"] = str(r["id"])
            # An in-flight row whose Job should long since have finished is stuck —
            # flag it so the UI can offer a Dismiss action (see dismiss_investigation).
            in_flight = r["status"] in ("pending", "running", "remediating")
            created = r.get("created_at")
            r["stale"] = bool(in_flight and created and created.timestamp() < stale_cutoff)
            for k in ("created_at", "completed_at", "approved_at"):
                if r.get(k):
                    r[k] = r[k].isoformat()
            out.append(r)
        return out
    except Exception:
        return []


def get_investigation_detail(investigation_id: str) -> dict | None:
    """Return full row for a single investigation. Used for post-approve polling."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return None
    try:
        with DB.connect() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
            cur.execute("SELECT * FROM investigations WHERE id = %s", (investigation_id,))
            r = cur.fetchone()
        if not r:
            return None
        r["id"] = str(r["id"])
        for k in ("created_at", "completed_at", "approved_at"):
            if r.get(k):
                r[k] = r[k].isoformat()
        return r
    except Exception:
        return None


def approve_investigation(investigation_id: str, approver: str) -> tuple[int, dict]:
    """Spawn the Remediator Job for the given investigation. Returns
    (HTTP status, response body)."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}

    try:
        with DB.connect() as conn:
            with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
                cur.execute("SELECT * FROM investigations WHERE id=%s", (investigation_id,))
                inv = cur.fetchone()
                if not inv:
                    return 404, {"error": "not found"}
                if inv["status"] != "awaiting_approval":
                    return 409, {"error": f"status is {inv['status']}, cannot approve"}
                # Expiry check.
                age_hrs = (time.time() - inv["created_at"].timestamp()) / 3600
                if age_hrs > APPROVAL_EXPIRY_HOURS:
                    cur.execute("UPDATE investigations SET status='expired', completed_at=now() WHERE id=%s",
                                (investigation_id,))
                    return 409, {"error": "expired"}
                # Daily cap.
                cur.execute("SELECT remediations FROM today_counters")
                today = (cur.fetchone() or {}).get("remediations", 0) or 0
                if today >= MAX_REMEDIATIONS_PER_DAY:
                    return 429, {"error": f"daily remediation budget exceeded ({today}/{MAX_REMEDIATIONS_PER_DAY})"}

                # Spawn the Job.
                job = build_remediator_job(investigation_id)
                status, body = k8s_post(
                    f"/apis/batch/v1/namespaces/{PLATFORM_HEALTH_AGENT_NAMESPACE}/jobs", job)
                if status not in (200, 201, 202):
                    return 502, {"error": "job create failed",
                                 "status": status,
                                 "body": (body or {}).get("message") or str(body)[:500]}

                # Update DB.
                cur.execute(
                    """UPDATE investigations
                          SET status='remediating',
                              approved_by=%s,
                              approved_at=now()
                        WHERE id=%s""",
                    (approver, investigation_id),
                )
                cur.execute(
                    """INSERT INTO daily_counters (day, remediations) VALUES (CURRENT_DATE, 1)
                       ON CONFLICT (day) DO UPDATE SET remediations = daily_counters.remediations + 1""",
                )
        return 200, {"ok": True, "investigation_id": investigation_id, "status": "remediating"}
    except Exception as e:
        return 500, {"error": str(e)}


def dismiss_investigation(investigation_id: str, approver: str) -> tuple[int, dict]:
    """Mark an investigation dismissed. Two paths:
      - 'awaiting_approval' rows: the user declined the proposed fix (normal case).
      - stale in-flight rows ('pending'/'running'/'remediating' older than
        STALE_INVESTIGATION_MINUTES): the Job died without writing a result, so
        the row is stuck — let the user clear it. Fresh in-flight rows are kept,
        so we never dismiss a genuinely active investigation."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}
    try:
        with DB.connect() as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:  # type: ignore[arg-type]
            cur.execute("SELECT status, created_at FROM investigations WHERE id=%s", (investigation_id,))
            inv = cur.fetchone()
            if not inv:
                return 404, {"error": "not found"}
            status = inv["status"]
            if status == "awaiting_approval":
                pass  # always dismissable
            elif status in ("pending", "running", "remediating"):
                age_min = (time.time() - inv["created_at"].timestamp()) / 60
                if age_min < STALE_INVESTIGATION_MINUTES:
                    return 409, {"error": "investigation still in progress"}
            else:
                return 409, {"error": f"status is {status}, cannot dismiss"}
            cur.execute(
                """UPDATE investigations
                      SET status='dismissed',
                          approved_by=%s,
                          approved_at=now(),
                          completed_at=now()
                    WHERE id=%s""",
                (approver, investigation_id),
            )
        return 200, {"ok": True, "investigation_id": investigation_id, "status": "dismissed"}
    except Exception as e:
        return 500, {"error": str(e)}


def delete_investigation(investigation_id: str) -> tuple[int, dict]:
    """Permanent removal — drops the row from postgres. Used by the History
    tab's X button to clear UI noise. Won't delete an in-flight investigation
    (status='running' or 'remediating') because doing so would orphan the
    spawned Job + leave it without a place to write its result."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}
    try:
        with DB.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM investigations WHERE id=%s AND status NOT IN ('running','remediating','awaiting_approval')",
                (investigation_id,),
            )
            if cur.rowcount == 0:
                # Either not found, or in a non-deletable state.
                return 409, {"error": "not deletable (in-flight or not found)"}
        return 200, {"ok": True, "investigation_id": investigation_id, "deleted": True}
    except Exception as e:
        return 500, {"error": str(e)}


def delete_all_investigations() -> tuple[int, dict]:
    """Bulk removal — drops every terminal investigation from postgres. Used by
    the History tab's 'Dismiss all' button. In-flight rows (running / remediating
    / awaiting_approval) are preserved so we never orphan a spawned Job or drop a
    pending approval. NOT recoverable."""
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}
    try:
        with DB.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM investigations WHERE status NOT IN ('running','remediating','awaiting_approval')",
            )
            deleted = cur.rowcount
        return 200, {"ok": True, "deleted": deleted}
    except Exception as e:
        return 500, {"error": str(e)}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=HTML_DIR, **kwargs)  # type: ignore[arg-type]

    def _json(self, status: int, body: object) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # type: ignore[override]
        # Root → cluster-topology.html (no Apache-style directory listing).
        if self.path == "/" or self.path == "":
            self.path = "/cluster-topology.html"
            return super().do_GET()

        if self.path == "/data.json":
            with snapshot_lock:
                base = dict(snapshot)
            with metrics_lock:
                base.update(metrics)
            data = json.dumps(base, default=_json_default).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if self.path == "/investigations":
            return self._json(200, {"items": list_pending_investigations()})

        if self.path == "/investigations/all" or self.path.startswith("/investigations/all?"):
            return self._json(200, {"items": list_all_investigations(limit=20)})

        # /investigations/<uuid> — for post-approve polling
        m = re.match(r"^/investigations/([0-9a-f-]{36})$", self.path)
        if m:
            inv = get_investigation_detail(m.group(1))
            if not inv:
                return self._json(404, {"error": "not found"})
            return self._json(200, inv)

        super().do_GET()

    def do_POST(self) -> None:  # type: ignore[override]
        # Endpoints: /investigations/<id>/approve and /investigations/<id>/dismiss
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 3 and parts[0] == "investigations" and parts[2] in ("approve", "dismiss"):
            investigation_id = parts[1]
            try:
                uuid.UUID(investigation_id)
            except ValueError:
                return self._json(400, {"error": "invalid investigation id"})
            # No body required; we rely on the ALB allowlist for "auth".
            # Caller identity is the X-Forwarded-For header (best-effort
            # audit only — not a security boundary).
            approver = self.headers.get("X-Forwarded-For", "anon").split(",")[0].strip() or "anon"
            if parts[2] == "approve":
                status, body = approve_investigation(investigation_id, approver)
            else:
                status, body = dismiss_investigation(investigation_id, approver)
            return self._json(status, body)

        self.send_response(404); self.end_headers()

    def do_DELETE(self) -> None:  # type: ignore[override]
        # DELETE /investigations/<id> — permanently removes the row from postgres.
        # Used by the dashboard's History tab to clear noise. NOT recoverable.
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        # DELETE /investigations — bulk "dismiss all" of terminal rows.
        if len(parts) == 1 and parts[0] == "investigations":
            status, body = delete_all_investigations()
            return self._json(status, body)
        if len(parts) == 2 and parts[0] == "investigations":
            investigation_id = parts[1]
            try:
                uuid.UUID(investigation_id)
            except ValueError:
                return self._json(400, {"error": "invalid investigation id"})
            status, body = delete_investigation(investigation_id)
            return self._json(status, body)
        self.send_response(404); self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # type: ignore[override]
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class _ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """Allow concurrent requests so a slow approval doesn't block /data.json."""
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=metrics_loop, daemon=True).start()
    time.sleep(3)  # wait for first snapshot
    print(f"dashboard backend on :{PORT} (psycopg={'yes' if HAVE_PSYCOPG else 'no'}, db={DB_NAME})", flush=True)
    _ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
