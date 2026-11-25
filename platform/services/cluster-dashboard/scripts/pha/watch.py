"""The agent's loops: watch pods, events and nodes, scan kro resources for
stuck ones, and start an Investigator Job for each actionable incident that
the throttle lets through. One thread per loop; each keeps one database
connection and replaces it only when it broke.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from kubernetes import client, watch  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from pha import db as pha_db
from pha import triggers

log = logging.getLogger("event-watcher")

WATCH_TIMEOUT = 300          # server-side watch timeout (s); the stream is then restarted
ERROR_PAUSE = 5              # s between a failed watch and the next one


@dataclass
class Agent:
    """What the loops need from the event watcher's configuration."""
    rules: triggers.Rules
    namespace: str                                        # where Investigator Jobs run
    throttle: Callable[..., str | None]                   # (conn, kind, ns, name) -> reason to skip
    record: Callable[..., None]                           # (conn, kind, ns, name): debounce + daily count
    build_job: Callable[[str, dict], dict]                # (investigation id, payload) -> Job
    stuck_poll_interval: int = 60
    stop: threading.Event = field(default_factory=threading.Event)
    sleep: Callable[[float], None] = time.sleep

    def wait(self, seconds: int) -> bool:
        """Sleep up to `seconds`, one second at a time; True if stopped."""
        for _ in range(seconds):
            if self.stop.is_set():
                return True
            self.sleep(1)
        return self.stop.is_set()


# ---------------------------------------------------------------------------
# One incident
# ---------------------------------------------------------------------------

def spawn_investigation(agent: Agent, conn, batch_v1, trigger_kind: str, payload: dict) -> None:
    """Throttle, then record the investigation and create its Job. A failed
    Job create marks the row failed and starts no debounce window, so the next
    occurrence is tried again."""
    namespace, name, kind = payload.get("namespace", ""), payload["name"], payload["kind"]
    skip = agent.throttle(conn, kind, namespace, name)
    if skip:
        log.info("skip %s/%s/%s — %s", kind, namespace or "-", name, skip)
        return
    investigation_id = str(uuid.uuid4())
    pha_db.insert_investigation(conn, investigation_id, trigger_kind, kind, namespace, name, payload)
    job = agent.build_job(investigation_id, {"trigger_kind": trigger_kind, **payload})
    try:
        batch_v1.create_namespaced_job(namespace=agent.namespace, body=job)
    except ApiException as e:
        log.error("Job create failed for %s: %s", investigation_id, e)
        pha_db.mark_failed(conn, investigation_id, f"Job create failed: {e}")
        return
    agent.record(conn, kind, namespace, name)
    pha_db.mark_running(conn, investigation_id)
    log.info("spawned investigator %s for %s/%s/%s (kind=%s)",
             investigation_id, kind, namespace or "-", name, trigger_kind)


# ---------------------------------------------------------------------------
# Connections and objects
# ---------------------------------------------------------------------------

def db_alive(conn) -> bool:
    """psycopg sets `closed`, and `broken` after a failed operation."""
    return not conn.closed and not getattr(conn, "broken", False)


def _renew(conn, conn_factory):
    if db_alive(conn):
        return conn
    try:
        conn.close()
    except Exception:  # already unusable; closing is best effort
        pass
    return conn_factory()


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:  # shutting down; nothing else to do
        pass


_serializer = None


def as_dict(obj) -> dict:
    """A typed client object as JSON (camelCase, ISO times), as pha.triggers expects."""
    global _serializer
    if isinstance(obj, dict):
        return obj
    if _serializer is None:
        _serializer = client.ApiClient()
    return _serializer.sanitize_for_serialization(obj)


# ---------------------------------------------------------------------------
# Watch loops (pods, events, nodes)
# ---------------------------------------------------------------------------

def detectors(rules: triggers.Rules) -> dict[str, tuple[Callable, Callable]]:
    """label -> (CoreV1Api -> list method to watch, object -> trigger or None)."""
    return {
        "pod": (lambda v1: v1.list_pod_for_all_namespaces,
                lambda o: triggers.detect_pod(as_dict(o), rules, triggers.utcnow())),
        "event": (lambda v1: v1.list_event_for_all_namespaces,
                  lambda o: triggers.detect_event(as_dict(o), rules)),
        "node": (lambda v1: v1.list_node,
                 lambda o: triggers.detect_node(as_dict(o), rules)),
    }


def watch_loop(agent: Agent, label: str, conn_factory: Callable, lister: Callable, detect: Callable) -> None:
    """Stream one resource kind and spawn investigations, restarting the
    stream after its server-side timeout or an error. One database connection
    for the whole loop, closed on shutdown."""
    conn = conn_factory()
    batch_v1 = client.BatchV1Api()
    try:
        while not agent.stop.is_set():
            try:
                conn = _renew(conn, conn_factory)
                w = watch.Watch()
                for ev in w.stream(lister(client.CoreV1Api()), timeout_seconds=WATCH_TIMEOUT):
                    if agent.stop.is_set():
                        w.stop()
                        break
                    trig = detect(ev["object"])
                    if trig:
                        spawn_investigation(agent, conn, batch_v1, trig[0], trig[1])
            except Exception as e:  # stream or DB error: log, keep the loop alive
                log.warning("%s watch error: %s — restarting", label, e)
                conn = _renew(conn, conn_factory)
                agent.sleep(ERROR_PAUSE)
    finally:
        _close(conn)


# ---------------------------------------------------------------------------
# Stuck kro resources (periodic scan)
# ---------------------------------------------------------------------------

# (plural, kind, rule): endpoints in `inference`.
ENDPOINTS = (("vllmendpoints", "VLLMEndpoint", triggers.stuck_vllm),
             ("llmdendpoints", "LLMDEndpoint", triggers.stuck_llmd),
             ("llmddisaggendpoints", "LLMDDisaggEndpoint", triggers.stuck_llmd))


def _list(custom, namespace: str, plural: str) -> list[dict]:
    try:
        return custom.list_namespaced_custom_object(group="kro.run", version="v1alpha1",
                                                    namespace=namespace, plural=plural).get("items", [])
    except ApiException as e:
        if e.status != 404:                  # 404: the CRD is not installed
            log.warning("list %s: %s", plural, e)
        return []


def _model_deployment(custom, ep: dict) -> dict | None:
    """The LLMDEndpoint's vLLM Deployment (<name>-llmd), or None if unreadable.
    Read through the generic API so it comes back as a plain dict."""
    meta = ep.get("metadata") or {}
    try:
        return custom.get_namespaced_custom_object(group="apps", version="v1", namespace=meta.get("namespace", ""),
                                                   plural="deployments", name=f"{meta.get('name', '')}-llmd")
    except Exception as e:  # noqa: BLE001 - no Deployment to judge by: fall back to the age rule
        log.debug("read deployment for %s: %s", meta.get("name"), e)
        return None


def stuck_incidents(custom, rules: triggers.Rules, now) -> list[dict]:
    """Payloads of kro resources not healthy STUCK_RESOURCE_THRESHOLD_SEC after creation.
    A scale-to-zero LLMDEndpoint starting from zero is skipped while its cold start lasts."""
    out = []
    for plural, kind, stuck in ENDPOINTS:
        for ep in _list(custom, "inference", plural):
            if not stuck(ep, rules, now):
                continue
            if (plural == "llmdendpoints" and str((ep.get("spec") or {}).get("minReplicas", 1)) == "0"
                    and triggers.waking_from_zero(ep, _model_deployment(custom, ep), now)):
                continue
            st = ep.get("status") or {}
            out.append({"kind": kind, "namespace": ep["metadata"].get("namespace", ""),
                        "name": ep["metadata"]["name"], "ready": str(st.get("ready", "False")),
                        "routerHealth": st.get("routerHealth", ""),
                        "availableReplicas": st.get("availableReplicas", 0),
                        "model": (ep.get("spec") or {}).get("model", ""),
                        "age_seconds": int(triggers.age_seconds(ep, now))})
    for team in _list(custom, "ai-platform", "aiteams"):
        if not triggers.stuck_aiteam(team, rules, now):
            continue
        st = team.get("status") or {}
        out.append({"kind": "AITeam", "namespace": team["metadata"].get("namespace", ""),
                    "name": team["metadata"]["name"], "phase": st.get("phase", "Pending"),
                    "message": st.get("message", ""), "age_seconds": int(triggers.age_seconds(team, now))})
    return out


def stuck_scan_loop(agent: Agent, conn_factory: Callable) -> None:
    """Every stuck_poll_interval seconds, spawn a StuckResource investigation
    for each stuck resource. One database connection for all scans."""
    if not agent.rules.on("StuckResource"):
        log.info("StuckResource trigger disabled — watcher exiting")
        return
    custom, batch_v1 = client.CustomObjectsApi(), client.BatchV1Api()
    conn = conn_factory()
    try:
        while not agent.stop.is_set():
            try:
                conn = _renew(conn, conn_factory)
                for payload in stuck_incidents(custom, agent.rules, triggers.utcnow()):
                    spawn_investigation(agent, conn, batch_v1, "StuckResource", payload)
            except Exception as e:  # one failed scan must not stop the loop
                log.warning("stuck-resource scan error: %s", e)
            if agent.wait(agent.stuck_poll_interval):
                return
    finally:
        _close(conn)
