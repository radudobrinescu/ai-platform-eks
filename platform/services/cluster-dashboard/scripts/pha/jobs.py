"""Kubernetes Job specs for the agent's Investigator and Remediator.

Both Jobs share one shape: an init container downloads kubectl and kiro-cli,
a second installs the Python packages, and the main container runs a script
from the agent ConfigMap. They differ in ServiceAccount (read-only reader vs
writer scoped to `inference` and `team-*`), script, and the env they get.

Callers pass `pip_packages`: the version pins live in the entry-point files
(backend.py, event_watcher.py), where automated dependency updates look for
them.
"""
from __future__ import annotations

import json

AGENT_CONFIGMAP = "platform-health-agent-config"
SCRIPTS_CONFIGMAP = "platform-health-agent-scripts"
PART_OF = "platform-health-agent"
TOOLS_IMAGE = "alpine:3.20"
PATH = "/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

_NON_ROOT = {"allowPrivilegeEscalation": False, "runAsNonRoot": True, "runAsUser": 65532,
             "capabilities": {"drop": ["ALL"]}}


def _init_containers(python_image: str, kubectl_version: str, pip_packages: str) -> list[dict]:
    return [
        {
            "name": "install-tools",
            "image": TOOLS_IMAGE,
            "command": ["/bin/sh", "-c"],
            "args": [
                f"set -eu; cd /tools; "
                f"echo 'fetching kubectl {kubectl_version}…'; "
                f"wget -qO kubectl https://dl.k8s.io/release/{kubectl_version}/bin/linux/amd64/kubectl && chmod +x kubectl; "
                f"echo 'fetching kiro-cli installer…'; "
                f"apk add -q curl bash; "
                f"export HOME=/tools; "
                f"curl -fsSL https://cli.kiro.dev/install | bash; "
                # The installer drops three binaries (kiro-cli, kiro-cli-chat,
                # kiro-cli-term) in $HOME/.local/bin; the launcher forks to
                # kiro-cli-chat, so all three must be on PATH.
                f"mv /tools/.local/bin/* /tools/ 2>/dev/null || true; "
                f"chmod +x /tools/kiro-cli /tools/kiro-cli-chat /tools/kiro-cli-term 2>/dev/null || true; "
                f"ls -la /tools/",
            ],
            "volumeMounts": [{"name": "tools", "mountPath": "/tools"}],
            # Root only to apk-add curl/bash into the throwaway init container.
            "securityContext": {"runAsUser": 0, "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]}},
        },
        {
            "name": "install-pydeps",
            "image": python_image,
            "command": ["/bin/sh", "-c"],
            "args": [f"set -eu; pip install --no-cache-dir --target=/pydeps {pip_packages}"],
            "volumeMounts": [{"name": "pydeps", "mountPath": "/pydeps"}],
            "securityContext": dict(_NON_ROOT),
        },
    ]


def _env(inv_id: str, config_keys: list[str], extra: list[dict]) -> list[dict]:
    return [
        {"name": "INVESTIGATION_ID", "value": inv_id},
        *extra,
        {"name": "PATH", "value": PATH},
        {"name": "PYTHONPATH", "value": "/pydeps"},
        {"name": "HOME", "value": "/tmp"},
        *[{"name": k, "valueFrom": {"configMapKeyRef": {"name": AGENT_CONFIGMAP, "key": k}}}
          for k in config_keys],
        # Reuses the platform Postgres credentials; the Kiro key is the only
        # agent-specific secret.
        {"name": "DB_USER", "valueFrom": {"secretKeyRef": {"name": "platform-db-credentials", "key": "username"}}},
        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "platform-db-credentials", "key": "password"}}},
        {"name": "KIRO_API_KEY", "valueFrom": {"secretKeyRef": {"name": "platform-health-agent-secrets", "key": "KIRO_API_KEY"}}},
    ]


def _volume_mounts() -> list[dict]:
    return [
        {"name": "scripts", "mountPath": "/scripts", "readOnly": True},
        {"name": "tools", "mountPath": "/tools", "readOnly": True},
        {"name": "pydeps", "mountPath": "/pydeps", "readOnly": True},
        {"name": "results", "mountPath": "/results"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]


def _volumes() -> list[dict]:
    return [
        {"name": "scripts", "configMap": {"name": SCRIPTS_CONFIGMAP, "defaultMode": 0o755}},
        {"name": "tools", "emptyDir": {}},
        {"name": "pydeps", "emptyDir": {}},
        {"name": "results", "emptyDir": {}},
        {"name": "tmp", "emptyDir": {}},
    ]


def _job(role: str, inv_id: str, *, namespace: str, service_account: str, script: str,
         env: list[dict], python_image: str, kubectl_version: str, pip_packages: str) -> dict:
    app = f"platform-health-agent-{role}"
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": f"{role}-{inv_id[:8]}",
            "namespace": namespace,
            "labels": {"app.kubernetes.io/name": app, "app.kubernetes.io/part-of": PART_OF,
                       "investigation-id": inv_id},
        },
        "spec": {
            "ttlSecondsAfterFinished": 3600,   # Job and Pod garbage-collected after 1 h
            "activeDeadlineSeconds": 600,      # hard 10 min ceiling
            "backoffLimit": 0,                 # no retry: surface the failure in the dashboard
            "template": {
                "metadata": {"labels": {"app.kubernetes.io/name": app, "investigation-id": inv_id}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": service_account,
                    "automountServiceAccountToken": True,
                    "nodeSelector": {"kubernetes.io/arch": "amd64"},
                    "initContainers": _init_containers(python_image, kubectl_version, pip_packages),
                    "containers": [{
                        "name": role,
                        "image": python_image,
                        "command": ["/bin/sh", f"/scripts/{script}"],
                        "env": env,
                        "volumeMounts": _volume_mounts(),
                        "resources": {"requests": {"cpu": "200m", "memory": "256Mi"},
                                      "limits": {"cpu": "1000m", "memory": "1Gi"}},
                        "securityContext": dict(_NON_ROOT),
                    }],
                    "volumes": _volumes(),
                },
            },
        },
    }


def investigator_job(inv_id: str, payload: dict, *, namespace: str, python_image: str,
                     kubectl_version: str, pip_packages: str) -> dict:
    """Read-only investigation of one incident (ServiceAccount with cluster read)."""
    env = _env(inv_id,
               ["CLUSTER_NAME", "AWS_REGION", "DB_HOST", "DB_PORT", "DB_NAME",
                "KIRO_MODEL_INVESTIGATE", "KIRO_MODEL_REMEDIATE"],
               [{"name": "EVENT_PAYLOAD", "value": json.dumps(payload)}])
    return _job("investigator", inv_id, namespace=namespace,
                service_account="platform-health-agent-reader", script="investigate.sh",
                env=env, python_image=python_image, kubectl_version=kubectl_version,
                pip_packages=pip_packages)


def remediator_job(inv_id: str, *, namespace: str, python_image: str,
                   kubectl_version: str, pip_packages: str) -> dict:
    """Apply an approved fix (ServiceAccount that can write only to `inference`
    and `team-*` namespaces; RBAC is the safety boundary)."""
    env = _env(inv_id,
               ["CLUSTER_NAME", "AWS_REGION", "DB_HOST", "DB_PORT", "DB_NAME", "KIRO_MODEL_REMEDIATE"],
               [])
    return _job("remediator", inv_id, namespace=namespace,
                service_account="platform-health-agent-writer", script="remediate.sh",
                env=env, python_image=python_image, kubectl_version=kubectl_version,
                pip_packages=pip_packages)
