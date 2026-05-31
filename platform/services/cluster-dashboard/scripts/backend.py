#!/usr/bin/env python3
"""Cluster dashboard backend.

Two responsibilities:

1. (Original) Poll the Kubernetes API every 2s, build a JSON snapshot of
   nodes/pods/InferenceEndpoints, and serve it at /data.json plus static
   HTML. Browser polls /data.json — no streaming, no proxying, no auth
   in browser.

2. (New) Surface Platform Health Agent approvals from the `platform_health_agent` postgres
   database (created by the optional platform-health-agent platform service):
     - GET  /investigations           → list of pending investigations
     - POST /investigations/<id>/approve → spawn Remediator Job in
                                           platform-health-agent namespace
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

# psycopg is optional from the code's POV — if it can't import (image
# missing the dep), approvals are disabled rather than crashing the
# dashboard.
try:
    import psycopg                       # type: ignore[import-not-found]
    HAVE_PSYCOPG = True
except Exception:
    HAVE_PSYCOPG = False


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

PLATFORM_HEALTH_AGENT_NAMESPACE = os.environ.get("PLATFORM_HEALTH_AGENT_NAMESPACE", "platform-health-agent")
APPROVAL_EXPIRY_HOURS = int(os.environ.get("APPROVAL_EXPIRY_HOURS", "24"))
MAX_REMEDIATIONS_PER_DAY = int(os.environ.get("MAX_REMEDIATIONS_PER_DAY", "20"))
KIRO_MODEL_REMEDIATE = os.environ.get("KIRO_MODEL_REMEDIATE", "claude-opus-4.6")
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

snapshot = {"nodes": [], "pods": [], "endpoints": [],
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


def _build_k8s_snapshot() -> dict:
    nodes_data = k8s_get("/api/v1/nodes")
    pods_data = k8s_get("/api/v1/pods")
    ep_data = k8s_get("/apis/kro.run/v1alpha1/namespaces/inference/inferenceendpoints")

    nodes = []
    if nodes_data:
        for n in nodes_data.get("items", []):
            labels = n.get("metadata", {}).get("labels", {})
            conditions = n.get("status", {}).get("conditions", [])
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            if not ready:
                continue
            allocatable = n.get("status", {}).get("allocatable", {})
            nodes.append({
                "name": n["metadata"]["name"],
                "instance": labels.get("node.kubernetes.io/instance-type", "unknown"),
                "pool": labels.get("karpenter.sh/nodepool",
                                   "__mng__" if labels.get("eks.amazonaws.com/nodegroup") else "__mng__"),
                "zone": labels.get("topology.kubernetes.io/zone", ""),
                "capacity": labels.get("karpenter.sh/capacity-type",
                                       labels.get("eks.amazonaws.com/capacityType", "on-demand")),
                "gpu": int(allocatable.get("nvidia.com/gpu", "0")),
                "gpuProduct": labels.get("nvidia.com/gpu.product", "").replace("-", " "),
                "created": n["metadata"].get("creationTimestamp", ""),
            })

    pods = []
    if pods_data:
        for p in pods_data.get("items", []):
            phase = p.get("status", {}).get("phase", "")
            if phase not in ("Running", "Pending"):
                continue
            containers = p.get("spec", {}).get("containers", [])
            gpu_req = sum(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", "0"))
                          for c in containers)
            pods.append({
                "name": p["metadata"]["name"],
                "namespace": p["metadata"]["namespace"],
                "node": p.get("spec", {}).get("nodeName", ""),
                "phase": phase,
                "gpu": gpu_req,
                "created": p["metadata"].get("creationTimestamp", ""),
            })

    endpoints = []
    if ep_data:
        for ep in ep_data.get("items", []):
            status = ep.get("status", {})
            spec = ep.get("spec", {})
            gpu_count = int(spec.get("gpuCount", 1))
            tp = int(spec.get("tensorParallelSize", 0))
            pp = int(spec.get("pipelineParallelSize", 1))
            if tp == 0:
                tp = gpu_count if pp == 1 else 1
            endpoints.append({
                "name": ep["metadata"]["name"],
                "model": spec.get("model", ""),
                "shared": spec.get("shared", False),
                "gpuCount": gpu_count,
                "tp": tp,
                "pp": pp,
                "modelStatus": status.get("modelStatus", "Pending"),
                "ready": status.get("ready", "False"),
                "message": status.get("message", ""),
                "created": ep["metadata"].get("creationTimestamp", ""),
            })

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
        "nodes": nodes, "pods": pods, "endpoints": endpoints,
        "approvals_available": approvals_available,
        "approvals_pending": approvals_pending,
        "links": _build_links(),
        "ts": time.time(),
    }


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
        for r in rows:
            r["id"] = str(r["id"])
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
    if not (HAVE_PSYCOPG and DB_USER and DB_PASSWORD):
        return 503, {"error": "approvals db unavailable"}
    try:
        with DB.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE investigations
                      SET status='dismissed',
                          approved_by=%s,
                          approved_at=now(),
                          completed_at=now()
                    WHERE id=%s AND status='awaiting_approval'""",
                (approver, investigation_id),
            )
            if cur.rowcount == 0:
                return 409, {"error": "not awaiting approval"}
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
                data = json.dumps(snapshot).encode("utf-8")
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
    time.sleep(3)  # wait for first snapshot
    print(f"dashboard backend on :{PORT} (psycopg={'yes' if HAVE_PSYCOPG else 'no'}, db={DB_NAME})", flush=True)
    _ThreadingHTTPServer(("", PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
