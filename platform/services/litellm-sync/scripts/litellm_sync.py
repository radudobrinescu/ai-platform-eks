#!/usr/bin/env python3
"""litellm-sync — keep LiteLLM's model registry in sync with InferenceEndpoints.

KRO registers a model with LiteLLM (POST /model/new) via the `-register` Job
when an InferenceEndpoint is created, but KRO has no delete-hook, so nothing
deregisters the model when the InferenceEndpoint is deleted. Left alone,
LiteLLM's /v1/models accumulates dead entries every time a model is removed.

This controller closes that gap with a finalizer:

  1. WATCH inferenceendpoints.kro.run in the `inference` namespace.
  2. On a live object missing our finalizer  -> PATCH to ADD it.
  3. On an object with deletionTimestamp set -> deregister the model from
     LiteLLM, then PATCH to REMOVE our finalizer so deletion can complete.
  4. RECONCILE every RECONCILE_INTERVAL_SEC as a backstop: re-add finalizers
     to live endpoints, and sweep orphaned DB-registered models whose
     InferenceEndpoint no longer exists.

Deregistration only ever touches DB-registered models (model_info.db_model ==
True). Static config-file models (e.g. the Bedrock claude-opus-4-8 declared in
litellm.yaml) have db_model == False and are skipped — they must never be
deleted.

Single replica, no database. If killed mid-loop, the next start re-lists
current state (the watch is list-then-watch) and the reconcile loop repairs
any finalizer/registration drift. All config is via env vars (see
deployment.yaml).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

from kubernetes import client, config, watch  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("litellm-sync")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

LITELLM_BASE_URL = os.environ.get(
    "LITELLM_BASE_URL", "http://litellm.ai-platform.svc.cluster.local:4000"
).rstrip("/")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
WATCH_NAMESPACE = os.environ.get("WATCH_NAMESPACE", "inference")
FINALIZER = os.environ.get("FINALIZER", "litellm.ai-platform/deregister")
RECONCILE_INTERVAL_SEC = int(os.environ.get("RECONCILE_INTERVAL_SEC", "600"))
HTTP_TIMEOUT_SEC = int(os.environ.get("HTTP_TIMEOUT_SEC", "15"))
WATCH_TIMEOUT_SEC = int(os.environ.get("WATCH_TIMEOUT_SEC", "300"))

# KRO InferenceEndpoint custom resource coordinates.
CR_GROUP = "kro.run"
CR_VERSION = "v1alpha1"
CR_PLURAL = "inferenceendpoints"

stop_event = threading.Event()


def _stop(*_: object) -> None:
    log.info("shutdown requested")
    stop_event.set()


# ---------------------------------------------------------------------------
# LiteLLM client (stdlib urllib — no extra deps beyond the k8s client)
# ---------------------------------------------------------------------------

def _litellm_request(method: str, path: str, body: dict | None = None) -> dict | None:
    """Call the LiteLLM admin API. Returns parsed JSON, or None on transport error.

    Raises nothing — callers treat None as "could not complete, retry later".
    HTTP errors are logged with their status so misconfig is visible.
    """
    url = f"{LITELLM_BASE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url=url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {LITELLM_MASTER_KEY}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:  # noqa: S310 (internal cluster URL)
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        log.warning("LiteLLM %s %s -> HTTP %s: %s", method, path, e.code, e.reason)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.warning("LiteLLM %s %s failed: %s", method, path, e)
    return None


def list_db_models() -> dict[str, str] | None:
    """Return {model_name: model_id} for DB-registered models only.

    Static config-file models (db_model == False) are excluded so they can
    never be selected for deletion. Returns None if LiteLLM is unreachable.
    """
    info = _litellm_request("GET", "/model/info")
    if info is None:
        return None
    result: dict[str, str] = {}
    for entry in info.get("data", []) or []:
        model_info = entry.get("model_info") or {}
        if not model_info.get("db_model"):
            continue
        name = entry.get("model_name")
        model_id = model_info.get("id")
        if name and model_id:
            result[name] = model_id
    return result


def deregister_model(name: str) -> bool:
    """Delete a DB-registered model from LiteLLM by display name.

    Looks the name up in the current DB-model set so we delete by the stable
    model_info.id and never touch a static config model. Idempotent: a name
    that isn't a DB model (already gone, or static) is a no-op success.
    """
    db_models = list_db_models()
    if db_models is None:
        log.warning("deregister %s: LiteLLM unreachable — will retry on reconcile", name)
        return False
    model_id = db_models.get(name)
    if model_id is None:
        log.info("deregister %s: not a DB-registered model (already gone or static) — skipping", name)
        return True
    resp = _litellm_request("POST", "/model/delete", {"id": model_id})
    if resp is None:
        return False
    log.info("deregistered model %s (id=%s) from LiteLLM", name, model_id)
    return True


# ---------------------------------------------------------------------------
# Finalizer management (immutable list updates)
# ---------------------------------------------------------------------------

def _finalizers(ie: dict) -> list[str]:
    return list((ie.get("metadata") or {}).get("finalizers") or [])

def _has_deletion_timestamp(ie: dict) -> bool:
    return bool((ie.get("metadata") or {}).get("deletionTimestamp"))


def _patch_finalizers(custom: client.CustomObjectsApi, name: str, finalizers: list[str]) -> bool:
    """Replace metadata.finalizers on an InferenceEndpoint. Returns success."""
    patch = {"metadata": {"finalizers": finalizers}}
    try:
        custom.patch_namespaced_custom_object(
            group=CR_GROUP, version=CR_VERSION, namespace=WATCH_NAMESPACE,
            plural=CR_PLURAL, name=name, body=patch,
        )
        return True
    except ApiException as e:
        # 404: the object was deleted out from under us — nothing to patch.
        if e.status == 404:
            return True
        log.warning("patch finalizers on %s failed: %s", name, e)
        return False


def ensure_finalizer(custom: client.CustomObjectsApi, ie: dict) -> None:
    """Add our finalizer to a live (non-terminating) endpoint if absent."""
    if _has_deletion_timestamp(ie):
        return
    current = _finalizers(ie)
    if FINALIZER in current:
        return
    name = ie["metadata"]["name"]
    if _patch_finalizers(custom, name, current + [FINALIZER]):
        log.info("added finalizer to %s", name)


def handle_deletion(custom: client.CustomObjectsApi, ie: dict) -> None:
    """A terminating endpoint that still carries our finalizer: deregister the
    model, then drop the finalizer so Kubernetes can finish deletion.

    The finalizer is only removed once deregistration succeeds — if LiteLLM is
    unreachable we leave it in place and the next watch event / reconcile retries.
    """
    current = _finalizers(ie)
    if FINALIZER not in current:
        return
    name = ie["metadata"]["name"]
    if not deregister_model(name):
        log.warning("keeping finalizer on %s until LiteLLM deregistration succeeds", name)
        return
    remaining = [f for f in current if f != FINALIZER]
    if _patch_finalizers(custom, name, remaining):
        log.info("removed finalizer from %s — deletion can proceed", name)


def process(custom: client.CustomObjectsApi, ie: dict) -> None:
    """Route one InferenceEndpoint object to the right handler."""
    if _has_deletion_timestamp(ie):
        handle_deletion(custom, ie)
    else:
        ensure_finalizer(custom, ie)


# ---------------------------------------------------------------------------
# Watch loop
# ---------------------------------------------------------------------------

def watch_endpoints() -> None:
    """Stream InferenceEndpoint events and react. Reconnects on any error."""
    while not stop_event.is_set():
        try:
            custom = client.CustomObjectsApi()
            w = watch.Watch()
            stream = w.stream(
                custom.list_namespaced_custom_object,
                group=CR_GROUP, version=CR_VERSION,
                namespace=WATCH_NAMESPACE, plural=CR_PLURAL,
                timeout_seconds=WATCH_TIMEOUT_SEC,
            )
            for event in stream:
                if stop_event.is_set():
                    w.stop()
                    break
                obj = event.get("object")
                # On 410 Gone / resourceVersion expiry the client yields a
                # non-dict status object; skip it and let the loop re-list.
                if not isinstance(obj, dict) or "metadata" not in obj:
                    continue
                process(custom, obj)
        except ApiException as e:
            log.warning("watch error (HTTP %s): %s — reconnecting", e.status, e.reason)
            time.sleep(5)
        except Exception as e:  # noqa: BLE001 — never let the watch thread die
            log.warning("watch error: %s — reconnecting", e)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Reconcile loop (backstop for missed events / controller downtime)
# ---------------------------------------------------------------------------

def reconcile_once(custom: client.CustomObjectsApi) -> None:
    """Repair finalizer drift and sweep orphaned DB-registered models."""
    try:
        endpoints = custom.list_namespaced_custom_object(
            group=CR_GROUP, version=CR_VERSION,
            namespace=WATCH_NAMESPACE, plural=CR_PLURAL,
        ).get("items", [])
    except ApiException as e:
        log.warning("reconcile: list inferenceendpoints failed: %s", e)
        return

    live_names: set[str] = set()
    for ie in endpoints:
        live_names.add(ie["metadata"]["name"])
        process(custom, ie)

    # Sweep: DB-registered models with no surviving InferenceEndpoint. Covers
    # the case where the controller was down at delete-time and an operator
    # force-removed the finalizer (the watch event for that delete is lost).
    db_models = list_db_models()
    if db_models is None:
        return
    for name in db_models:
        if name not in live_names:
            log.info("reconcile: orphaned model %s has no InferenceEndpoint — deregistering", name)
            deregister_model(name)


def reconcile_loop() -> None:
    while not stop_event.is_set():
        try:
            reconcile_once(client.CustomObjectsApi())
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile error: %s", e)
        for _ in range(RECONCILE_INTERVAL_SEC):
            if stop_event.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Health server
# ---------------------------------------------------------------------------

def _health_server() -> None:
    """Tiny :8080 server — 200 when the K8s API is reachable, else 503."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            try:
                client.CustomObjectsApi().list_namespaced_custom_object(
                    group=CR_GROUP, version=CR_VERSION,
                    namespace=WATCH_NAMESPACE, plural=CR_PLURAL, limit=1,
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
            except Exception as e:  # noqa: BLE001
                self.send_response(503)
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, *_: object) -> None:
            pass

    http.server.HTTPServer(("", 8080), Handler).serve_forever()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not LITELLM_MASTER_KEY:
        log.error("LITELLM_MASTER_KEY is empty — cannot authenticate to LiteLLM")
        return 1

    config.load_incluster_config()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    threading.Thread(target=_health_server, daemon=True).start()
    threading.Thread(target=watch_endpoints, daemon=True, name="watch").start()
    threading.Thread(target=reconcile_loop, daemon=True, name="reconcile").start()

    log.info(
        "litellm-sync started: ns=%s finalizer=%s litellm=%s reconcile=%ds",
        WATCH_NAMESPACE, FINALIZER, LITELLM_BASE_URL, RECONCILE_INTERVAL_SEC,
    )

    while not stop_event.is_set():
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
