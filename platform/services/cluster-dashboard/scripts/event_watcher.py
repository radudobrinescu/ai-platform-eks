#!/usr/bin/env python3
"""Event Watcher — the trigger engine.

Long-running Deployment. Responsibilities:

  1. Watch K8s API for actionable signals (Pod CrashLoopBackOff, OOMKilled,
     ImagePullBackOff, Node NotReady, FailedScheduling/FailedMount events).
  2. Debounce per (kind, namespace, name) using the postgres `debounce` table.
  3. Enforce concurrency cap and daily investigation budget.
  4. INSERT investigation row + spawn an Investigator Job.
  5. Reconcile per-namespace writer RoleBindings for `team-*` namespaces
     every 5 minutes.

Single replica. If killed mid-loop, the next start picks up live events;
historical events are not replayed (informers list-then-watch the current
state, which captures any persisting bad state at start time).

All env vars are documented in configmap.yaml + the README.
"""

from __future__ import annotations

import os
import re
import signal
import sys
import threading
import time
import uuid

import psycopg
from kubernetes import client, config, watch  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from pha import db as pha_db
from pha import jobs as pha_jobs
from pha import log as pha_log
from pha import triggers

log = pha_log.setup("event-watcher")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "unknown")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

DEBOUNCE_WINDOW_SEC = int(os.environ.get("DEBOUNCE_WINDOW_SEC", "600"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_INVESTIGATIONS", "3"))
MAX_PER_DAY = int(os.environ.get("MAX_INVESTIGATIONS_PER_DAY", "50"))
# After we conclude a resource is out-of-scope (can't auto-remediate, e.g. a
# stuck AITeam in ai-platform), don't re-investigate it for this long. Without
# this, the stuck-resource scanner re-fires every DEBOUNCE_WINDOW_SEC forever,
# burning the daily budget on a verdict that won't change. Re-checks daily so a
# genuinely-changed resource is eventually picked up again.
OUT_OF_SCOPE_COOLDOWN_SEC = int(os.environ.get("OUT_OF_SCOPE_COOLDOWN_SEC", "86400"))

# Trigger rules (pha/triggers.py): enabled triggers (TRIGGER_* variables),
# watched and excluded namespaces, POD_NOT_READY_THRESHOLD_SEC and
# STUCK_RESOURCE_THRESHOLD_SEC, read from the environment.
RULES = triggers.Rules.from_env(os.environ)
# How often to scan for stuck resources.
STUCK_RESOURCE_POLL_INTERVAL = int(os.environ.get("STUCK_RESOURCE_POLL_INTERVAL", "60"))

KIRO_MODEL_INVESTIGATE = os.environ.get("KIRO_MODEL_INVESTIGATE", "auto")
PYTHON_IMAGE = os.environ.get("PYTHON_IMAGE", "python:3.12-slim")
KUBECTL_VERSION = os.environ.get("KUBECTL_VERSION", "v1.32.5")
NAMESPACE = "ai-platform"  # where the watcher runs and Investigator/Remediator Jobs are spawned

# PHA is "enabled" only when a Kiro API key is present. The
# platform-health-agent-secrets Secret (mounted here, optional) is created
# manually with kubectl (see PLATFORM-HEALTH-AGENT.md). Without it the watcher
# idles gracefully — it boots, serves a healthy endpoint, watches nothing and
# spawns no Jobs — so the always-synced cluster-dashboard app stays Healthy on
# clusters that never opted in. See main().
KIRO_API_KEY = os.environ.get("KIRO_API_KEY", "")
PHA_ENABLED = bool(KIRO_API_KEY.strip())

ALLOWED_REMEDIATION_NAMESPACES_RE = re.compile(r"^(inference|team-.+)$")


# ---------------------------------------------------------------------------
# Database and throttling (SQL in pha/db.py)
# ---------------------------------------------------------------------------

def db_connect() -> psycopg.Connection:
    return pha_db.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def db_with_retry() -> psycopg.Connection:
    """Reconnect with exponential backoff."""
    return pha_db.connect_with_retry(db_connect)


def should_throttle(conn: psycopg.Connection, kind: str, namespace: str, name: str) -> str | None:
    """Return the reason to skip this incident, or None to investigate it."""
    return pha_db.throttle_reason(
        conn, kind, namespace, name, debounce_sec=DEBOUNCE_WINDOW_SEC,
        max_concurrent=MAX_CONCURRENT, max_per_day=MAX_PER_DAY,
        out_of_scope_cooldown_sec=OUT_OF_SCOPE_COOLDOWN_SEC)


def record_trigger(conn: psycopg.Connection, kind: str, namespace: str, name: str) -> None:
    pha_db.record_trigger(conn, kind, namespace, name)


# ---------------------------------------------------------------------------
# Investigator Job spec (shape shared with the Remediator: pha/jobs.py)
# ---------------------------------------------------------------------------

# Python packages for the Investigator Job: psycopg for the result write,
# awslabs.eks-mcp-server for kiro-cli's read-only Kubernetes tools. The exact
# `name==version` pins stay in this file: automated dependency updates look for
# them here.
INVESTIGATOR_PIP_PACKAGES = "psycopg[binary]==3.3.9 awslabs.eks-mcp-server==0.2.3"


def build_investigator_job(investigation_id: str, event_payload: dict) -> dict:
    return pha_jobs.investigator_job(
        investigation_id, event_payload, namespace=NAMESPACE, python_image=PYTHON_IMAGE,
        kubectl_version=KUBECTL_VERSION, pip_packages=INVESTIGATOR_PIP_PACKAGES)


# ---------------------------------------------------------------------------
# Trigger detection (rules in pha/triggers.py, on objects in their JSON shape)
# ---------------------------------------------------------------------------

_serializer = None


def as_dict(obj) -> dict:
    """A watched object as JSON (camelCase, ISO times), as the API sends it."""
    global _serializer
    if isinstance(obj, dict):
        return obj
    if _serializer is None:
        _serializer = client.ApiClient()
    return _serializer.sanitize_for_serialization(obj)


def detect_pod_trigger(pod) -> tuple[str, dict] | None:
    return triggers.detect_pod(as_dict(pod), RULES, triggers.utcnow())


def detect_event_trigger(event) -> tuple[str, dict] | None:
    return triggers.detect_event(as_dict(event), RULES)


def detect_node_trigger(node) -> tuple[str, dict] | None:
    return triggers.detect_node(as_dict(node), RULES)


# ---------------------------------------------------------------------------
# Spawn helpers
# ---------------------------------------------------------------------------

def spawn_investigation(conn: psycopg.Connection, batch_v1: client.BatchV1Api,
                        trigger_kind: str, payload: dict) -> None:
    namespace = payload.get("namespace", "")
    name = payload["name"]
    resource_kind = payload["kind"]

    skip = should_throttle(conn, resource_kind, namespace, name)
    if skip:
        log.info("skip %s/%s/%s — %s", resource_kind, namespace or "-", name, skip)
        return

    investigation_id = str(uuid.uuid4())
    pha_db.insert_investigation(conn, investigation_id, trigger_kind, resource_kind,
                                namespace, name, payload)

    job = build_investigator_job(investigation_id, {"trigger_kind": trigger_kind, **payload})
    try:
        batch_v1.create_namespaced_job(namespace=NAMESPACE, body=job)
    except ApiException as e:
        log.error("Job create failed for %s: %s", investigation_id, e)
        pha_db.mark_failed(conn, investigation_id, f"Job create failed: {e}")
        return

    record_trigger(conn, resource_kind, namespace, name)
    pha_db.mark_running(conn, investigation_id)
    log.info("spawned investigator %s for %s/%s/%s (kind=%s)",
             investigation_id, resource_kind, namespace or "-", name, trigger_kind)


# ---------------------------------------------------------------------------
# Watcher loops (one thread each)
# ---------------------------------------------------------------------------

stop_event = threading.Event()


def _stop(*_):
    log.info("shutdown requested")
    stop_event.set()


def _db_alive(conn) -> bool:
    """True if the connection is usable. psycopg exposes `closed`; `broken` is
    set after a failed operation."""
    return not conn.closed and not getattr(conn, "broken", False)


def _watch_loop(label, conn_factory, lister, detect) -> None:
    """Generic K8s watch loop, shared by the pod/event/node watchers.

    Opens ONE postgres connection and REUSES it across the ~5-minute watch-stream
    restarts (the stream returns every timeout_seconds and on transient errors).
    The previous code re-opened `conn_factory()` on every loop iteration and never
    closed the old connection, leaking ~1 connection per 5 minutes per thread and
    eventually exhausting the shared platform Postgres. We reconnect ONLY when the
    connection has actually broken, and always close it on shutdown.

    Args:
        label: short name for log lines ("pod"/"event"/"node").
        conn_factory: returns a live psycopg connection (db_with_retry).
        lister: given a CoreV1Api, returns the list_* method to stream.
        detect: given the streamed object, returns a trigger tuple or None.
    """
    conn = conn_factory()
    batch_v1 = client.BatchV1Api()
    try:
        while not stop_event.is_set():
            try:
                if not _db_alive(conn):
                    conn = conn_factory()
                v1 = client.CoreV1Api()
                w = watch.Watch()
                for ev in w.stream(lister(v1), timeout_seconds=300):
                    if stop_event.is_set():
                        w.stop()
                        break
                    trig = detect(ev["object"])
                    if trig:
                        spawn_investigation(conn, batch_v1, trig[0], trig[1])
            except Exception as e:
                log.warning("%s watch error: %s — restarting", label, e)
                # Reconnect only if the DB connection is the casualty; a K8s
                # stream error leaves the connection healthy and reusable.
                if not _db_alive(conn):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = conn_factory()
                time.sleep(5)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def watch_pods(conn_factory) -> None:
    _watch_loop("pod", conn_factory, lambda v1: v1.list_pod_for_all_namespaces, detect_pod_trigger)


def watch_events(conn_factory) -> None:
    _watch_loop("event", conn_factory, lambda v1: v1.list_event_for_all_namespaces, detect_event_trigger)


def watch_nodes(conn_factory) -> None:
    _watch_loop("node", conn_factory, lambda v1: v1.list_node, detect_node_trigger)


# ---------------------------------------------------------------------------
# Namespace reconciler — keeps writer RoleBindings in sync with team-* ns.
# ---------------------------------------------------------------------------

def reconcile_rolebindings_loop() -> None:
    """Every 5 min: ensure a writer RoleBinding exists in every `team-*`
    namespace and the `inference` namespace."""
    while not stop_event.is_set():
        try:
            v1 = client.CoreV1Api()
            rbac = client.RbacAuthorizationV1Api()

            namespaces = [n.metadata.name for n in v1.list_namespace().items]
            allowed = [n for n in namespaces if ALLOWED_REMEDIATION_NAMESPACES_RE.match(n)]
            log.info("reconciling writer RoleBindings for: %s", ",".join(allowed))

            for ns in allowed:
                try:
                    rbac.read_namespaced_role_binding(name="platform-health-agent-writer", namespace=ns)
                    continue
                except ApiException as e:
                    if e.status != 404:
                        raise
                rb = client.V1RoleBinding(
                    metadata=client.V1ObjectMeta(
                        name="platform-health-agent-writer", namespace=ns,
                        labels={"app.kubernetes.io/managed-by": "platform-health-agent-reconciler",
                                "app.kubernetes.io/part-of": "platform-health-agent"},
                    ),
                    role_ref=client.V1RoleRef(
                        api_group="rbac.authorization.k8s.io",
                        kind="ClusterRole", name="platform-health-agent-writer"),
                    # Plain dict for subjects: kubernetes-client renamed
                    # V1Subject → RbacV1Subject in v28+. Dict form is
                    # accepted by the API and version-stable.
                    subjects=[{
                        "kind": "ServiceAccount",
                        "name": "platform-health-agent-writer",
                        "namespace": NAMESPACE,
                    }],
                )
                rbac.create_namespaced_role_binding(namespace=ns, body=rb)
                log.info("created writer RoleBinding in %s", ns)

            # Sweep stale bindings: any RoleBinding labeled by us in a namespace
            # that no longer matches the pattern → delete.
            try:
                stale = rbac.list_role_binding_for_all_namespaces(
                    label_selector="app.kubernetes.io/managed-by=platform-health-agent-reconciler",
                ).items
            except ApiException:
                stale = []
            for rb in stale:
                if rb.metadata.namespace not in allowed:
                    rbac.delete_namespaced_role_binding(
                        name=rb.metadata.name, namespace=rb.metadata.namespace)
                    log.info("removed stale writer RoleBinding from %s", rb.metadata.namespace)

        except Exception as e:
            log.warning("reconciler error: %s", e)

        for _ in range(300):
            if stop_event.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Stuck custom-resource watcher — periodic poll, fires when KRO CRs
# haven't reached their healthy state within STUCK_RESOURCE_THRESHOLD_SEC.
# Catches the gap that pod-level triggers miss: KRO reconcile errors,
# an endpoint stuck deploying, an AITeam stuck onboarding, etc.
# ---------------------------------------------------------------------------

def watch_stuck_resources(conn_factory) -> None:
    """Periodically scan KRO custom resources; fire StuckResource trigger
    on those that haven't reached healthy state in time.

    Resources scanned (read-only):
      - vllmendpoints.kro.run             (in `inference` ns; simple vLLM)
      - llmdendpoints.kro.run             (in `inference` ns; llm-d scale tier)
      - llmddisaggendpoints.kro.run       (in `inference` ns; llm-d P/D disaggregated)
      - aiteams.kro.run                   (cluster-wide, in `ai-platform` ns)
    """
    if not RULES.on("StuckResource"):
        log.info("StuckResource trigger disabled — watcher exiting")
        return
    custom = client.CustomObjectsApi()
    batch_v1 = client.BatchV1Api()

    # One connection for all scans, as _watch_loop does; a new one only when
    # it broke. (Opening one per scan left the previous one to the garbage
    # collector.)
    conn = conn_factory()
    try:
        while not stop_event.is_set():
            try:
                if not _db_alive(conn):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = conn_factory()

                # --- VLLMEndpoints (simple vLLM) + LLMDEndpoints (llm-d scale) ---
                for plural, kind, stuck_fn in (
                    ("vllmendpoints", "VLLMEndpoint", triggers.stuck_vllm),
                    ("llmdendpoints", "LLMDEndpoint", triggers.stuck_llmd),
                    ("llmddisaggendpoints", "LLMDDisaggEndpoint", triggers.stuck_llmd),
                ):
                    try:
                        items = custom.list_namespaced_custom_object(
                            group="kro.run", version="v1alpha1",
                            namespace="inference", plural=plural,
                        ).get("items", [])
                    except ApiException as e:
                        if e.status != 404:
                            log.warning("list %s: %s", plural, e)
                        items = []
                    for ep in items:
                        if not stuck_fn(ep, RULES, triggers.utcnow()):
                            continue
                        st = ep.get("status") or {}
                        payload = {
                            "kind": kind,
                            "namespace": ep["metadata"].get("namespace", ""),
                            "name": ep["metadata"]["name"],
                            "ready": str(st.get("ready", "False")),
                            "routerHealth": st.get("routerHealth", ""),
                            "availableReplicas": st.get("availableReplicas", 0),
                            "model": (ep.get("spec") or {}).get("model", ""),
                            "age_seconds": int(triggers.age_seconds(ep, triggers.utcnow())),
                        }
                        spawn_investigation(conn, batch_v1, "StuckResource", payload)

                # --- AITeams ---
                try:
                    teams = custom.list_namespaced_custom_object(
                        group="kro.run", version="v1alpha1",
                        namespace="ai-platform", plural="aiteams",
                    ).get("items", [])
                except ApiException as e:
                    if e.status != 404:
                        log.warning("list aiteams: %s", e)
                    teams = []
                for team in teams:
                    if not triggers.stuck_aiteam(team, RULES, triggers.utcnow()):
                        continue
                    payload = {
                        "kind": "AITeam",
                        "namespace": team["metadata"].get("namespace", ""),
                        "name": team["metadata"]["name"],
                        "phase": (team.get("status") or {}).get("phase", "Pending"),
                        "message": (team.get("status") or {}).get("message", ""),
                        "age_seconds": int(triggers.age_seconds(team, triggers.utcnow())),
                    }
                    spawn_investigation(conn, batch_v1, "StuckResource", payload)

            except Exception as e:
                log.warning("stuck-resource scan error: %s", e)

            for _ in range(STUCK_RESOURCE_POLL_INTERVAL):
                if stop_event.is_set():
                    return
                time.sleep(1)
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Health / liveness HTTP server
# ---------------------------------------------------------------------------

def _health_server() -> None:
    """Tiny HTTP server on :8080 → 200 if K8s API + DB are reachable."""
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                client.CoreV1Api().list_namespace(limit=1)
                with db_connect() as c, c.cursor() as cur:
                    cur.execute("SELECT 1")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, *_): pass

    http.server.HTTPServer(("", 8080), H).serve_forever()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    config.load_incluster_config()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Health server starts unconditionally so the Deployment is Ready/Healthy
    # even when PHA is disabled — that's what keeps the always-synced
    # cluster-dashboard app green on clusters that never opted in.
    threading.Thread(target=_health_server, daemon=True).start()

    # Graceful idle: no Kiro API key → the agent is disabled. Don't watch, don't
    # spawn Jobs (the spawned Jobs require KIRO_API_KEY and would fail anyway).
    # Just idle until SIGTERM. Enable by creating the platform-health-agent-secrets
    # Secret (kubectl) and restarting this Deployment.
    if not PHA_ENABLED:
        log.warning("Platform Health Agent DISABLED — no KIRO_API_KEY present. "
                    "Watcher idling (no investigations). To enable: create the "
                    "platform-health-agent-secrets Secret in ai-platform with a "
                    "KIRO_API_KEY, then restart this deployment "
                    "(kubectl rollout restart deployment event-watcher -n ai-platform).")
        while not stop_event.is_set():
            time.sleep(1)
        return 0

    threads = [
        threading.Thread(target=watch_pods,            args=(db_with_retry,), daemon=True, name="pods"),
        threading.Thread(target=watch_events,          args=(db_with_retry,), daemon=True, name="events"),
        threading.Thread(target=watch_nodes,           args=(db_with_retry,), daemon=True, name="nodes"),
        threading.Thread(target=watch_stuck_resources, args=(db_with_retry,), daemon=True, name="stuck-resources"),
        threading.Thread(target=reconcile_rolebindings_loop,                  daemon=True, name="reconciler"),
    ]
    for t in threads:
        t.start()
    log.info("event-watcher started: cluster=%s region=%s triggers=%s",
             CLUSTER_NAME, AWS_REGION,
             ",".join(sorted(RULES.enabled)))

    while not stop_event.is_set():
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
