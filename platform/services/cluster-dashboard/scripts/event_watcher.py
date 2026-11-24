#!/usr/bin/env python3
"""Event watcher: the Platform Health Agent's trigger engine (entry point).

A single-replica Deployment. It watches pods, events and nodes, scans kro
resources for stuck ones, throttles per resource (debounce, concurrency cap,
daily budget, out-of-scope cooldown) and starts a read-only Investigator Job
per incident. It also keeps the Remediator's writer RoleBindings in the
`inference` and `team-*` namespaces. A restart lists the current state again;
past events are not replayed.

Code: rules in pha/triggers.py, loops in pha/watch.py, RoleBindings in
pha/rbac.py, health endpoint in pha/health.py, database in pha/db.py, Job
specs in pha/jobs.py. Settings come from the environment (pha-config.yaml).
Without a KIRO_API_KEY the watcher idles: healthy, watching nothing.
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import time

import psycopg
from kubernetes import client, config  # type: ignore[import-untyped]

from pha import db as pha_db
from pha import health, rbac, triggers, watch
from pha import jobs as pha_jobs
from pha import log as pha_log

log = pha_log.setup("event-watcher")

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

CLUSTER_NAME = os.environ.get("CLUSTER_NAME", "unknown")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

DEBOUNCE_WINDOW_SEC = int(os.environ.get("DEBOUNCE_WINDOW_SEC", "600"))
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_INVESTIGATIONS", "3"))
MAX_PER_DAY = int(os.environ.get("MAX_INVESTIGATIONS_PER_DAY", "50"))
# After an out-of-scope verdict (cannot be remediated, for example a stuck
# AITeam in ai-platform) the resource is not investigated again for this long;
# otherwise the stuck-resource scan would re-fire every debounce window and
# drain the daily budget on a verdict that will not change.
OUT_OF_SCOPE_COOLDOWN_SEC = int(os.environ.get("OUT_OF_SCOPE_COOLDOWN_SEC", "86400"))

# Trigger rules: TRIGGER_* switches, WATCH_NAMESPACES, EXCLUDE_NAMESPACES,
# POD_NOT_READY_THRESHOLD_SEC, STUCK_RESOURCE_THRESHOLD_SEC.
RULES = triggers.Rules.from_env(os.environ)
STUCK_RESOURCE_POLL_INTERVAL = int(os.environ.get("STUCK_RESOURCE_POLL_INTERVAL", "60"))

PYTHON_IMAGE = os.environ.get("PYTHON_IMAGE", "python:3.12-slim")
KUBECTL_VERSION = os.environ.get("KUBECTL_VERSION", "v1.32.5")
NAMESPACE = "ai-platform"  # where the watcher runs and the Jobs are created

# The agent is enabled only when the optional platform-health-agent-secrets
# Secret (created with kubectl, see PLATFORM-HEALTH-AGENT.md) provides a key.
PHA_ENABLED = bool(os.environ.get("KIRO_API_KEY", "").strip())

# Python packages for the Investigator Job: psycopg for the result write,
# awslabs.eks-mcp-server for kiro-cli's read-only Kubernetes tools. The exact
# `name==version` pins stay in this file: automated dependency updates look for
# them here.
INVESTIGATOR_PIP_PACKAGES = "psycopg[binary]==3.3.9 awslabs.eks-mcp-server==0.2.3"


# ---------------------------------------------------------------------------
# Database, throttle, Job spec
# ---------------------------------------------------------------------------

def db_connect() -> psycopg.Connection:
    return pha_db.connect(host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "5432")),
                          dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                          password=os.environ["DB_PASSWORD"])


def db_with_retry() -> psycopg.Connection:
    """Connect, retrying with exponential backoff."""
    return pha_db.connect_with_retry(db_connect)


def should_throttle(conn, kind: str, namespace: str, name: str) -> str | None:
    """The reason to skip this incident, or None to investigate it."""
    return pha_db.throttle_reason(
        conn, kind, namespace, name, debounce_sec=DEBOUNCE_WINDOW_SEC, max_concurrent=MAX_CONCURRENT,
        max_per_day=MAX_PER_DAY, out_of_scope_cooldown_sec=OUT_OF_SCOPE_COOLDOWN_SEC)


def record_trigger(conn, kind: str, namespace: str, name: str) -> None:
    pha_db.record_trigger(conn, kind, namespace, name)


def build_investigator_job(investigation_id: str, event_payload: dict) -> dict:
    return pha_jobs.investigator_job(
        investigation_id, event_payload, namespace=NAMESPACE, python_image=PYTHON_IMAGE,
        kubectl_version=KUBECTL_VERSION, pip_packages=INVESTIGATOR_PIP_PACKAGES)


def health_check() -> None:
    """Kubernetes API and agent database both reachable (raises otherwise)."""
    client.CoreV1Api().list_namespace(limit=1)
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    config.load_incluster_config()
    agent = watch.Agent(rules=RULES, namespace=NAMESPACE, throttle=should_throttle, record=record_trigger,
                        build_job=build_investigator_job, stuck_poll_interval=STUCK_RESOURCE_POLL_INTERVAL)

    def stop(*_):
        log.info("shutdown requested")
        agent.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # The health endpoint runs even when the agent is disabled, so the always
    # synced cluster-dashboard app stays Healthy on clusters that never opted in.
    threading.Thread(target=health.serve, args=(health_check,), daemon=True, name="health").start()

    if not PHA_ENABLED:
        log.warning("Platform Health Agent DISABLED — no KIRO_API_KEY present. "
                    "Watcher idling (no investigations). To enable: create the "
                    "platform-health-agent-secrets Secret in ai-platform with a "
                    "KIRO_API_KEY, then restart this deployment "
                    "(kubectl rollout restart deployment event-watcher -n ai-platform).")
        while not agent.stop.is_set():
            time.sleep(1)
        return 0

    threads = [threading.Thread(target=watch.watch_loop, args=(agent, label, db_with_retry, lister, detect),
                                daemon=True, name=f"{label}s")
               for label, (lister, detect) in watch.detectors(RULES).items()]
    threads += [
        threading.Thread(target=watch.stuck_scan_loop, args=(agent, db_with_retry), daemon=True,
                         name="stuck-resources"),
        threading.Thread(target=rbac.reconcile_loop, args=(agent.stop, NAMESPACE), daemon=True, name="reconciler"),
    ]
    for t in threads:
        t.start()
    log.info("event-watcher started: cluster=%s region=%s triggers=%s",
             CLUSTER_NAME, AWS_REGION, ",".join(sorted(RULES.enabled)))
    while not agent.stop.is_set():
        time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
