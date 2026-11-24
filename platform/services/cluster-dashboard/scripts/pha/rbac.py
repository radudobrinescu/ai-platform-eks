"""Writer RoleBindings for the Remediator.

Remediation may only change the `inference` and `team-*` namespaces. Every 5
minutes the reconciler makes sure each such namespace has a RoleBinding that
grants ClusterRole platform-health-agent-writer to the writer ServiceAccount,
and removes the bindings it created in namespaces that no longer match (a
deleted team)."""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable

from kubernetes import client  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

log = logging.getLogger("event-watcher")

WRITER = "platform-health-agent-writer"
ALLOWED_NAMESPACES_RE = re.compile(r"^(inference|team-.+)$")
MANAGED_BY = "platform-health-agent-reconciler"
INTERVAL = 300


def binding(namespace: str, sa_namespace: str) -> dict:
    """The writer RoleBinding for one namespace, as the API's JSON."""
    return {
        "metadata": {"name": WRITER, "namespace": namespace,
                     "labels": {"app.kubernetes.io/managed-by": MANAGED_BY,
                                "app.kubernetes.io/part-of": "platform-health-agent"}},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": WRITER},
        "subjects": [{"kind": "ServiceAccount", "name": WRITER, "namespace": sa_namespace}],
    }


def reconcile_once(v1, rbac, sa_namespace: str) -> tuple[list[str], list[str]]:
    """Create missing bindings and remove stale ones; (created, removed) namespaces."""
    namespaces = [n.metadata.name for n in v1.list_namespace().items]
    allowed = [n for n in namespaces if ALLOWED_NAMESPACES_RE.match(n)]
    log.info("reconciling writer RoleBindings for: %s", ",".join(allowed))
    created, removed = [], []
    for ns in allowed:
        try:
            rbac.read_namespaced_role_binding(name=WRITER, namespace=ns)
            continue
        except ApiException as e:
            if e.status != 404:
                raise
        rbac.create_namespaced_role_binding(namespace=ns, body=binding(ns, sa_namespace))
        created.append(ns)
        log.info("created writer RoleBinding in %s", ns)
    try:
        ours = rbac.list_role_binding_for_all_namespaces(
            label_selector=f"app.kubernetes.io/managed-by={MANAGED_BY}").items
    except ApiException:
        ours = []
    for rb in ours:
        if rb.metadata.namespace not in allowed:
            rbac.delete_namespaced_role_binding(name=rb.metadata.name, namespace=rb.metadata.namespace)
            removed.append(rb.metadata.namespace)
            log.info("removed stale writer RoleBinding from %s", rb.metadata.namespace)
    return created, removed


def reconcile_loop(stop: threading.Event, sa_namespace: str, sleep: Callable[[float], None] = time.sleep) -> None:
    while not stop.is_set():
        try:
            reconcile_once(client.CoreV1Api(), client.RbacAuthorizationV1Api(), sa_namespace)
        except Exception as e:  # API errors: log and retry next interval
            log.warning("reconciler error: %s", e)
        for _ in range(INTERVAL):
            if stop.is_set():
                return
            sleep(1)
