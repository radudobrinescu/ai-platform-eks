"""Trigger rules of the Platform Health Agent: which Kubernetes state starts an
investigation, and the payload it starts with.

Pure functions on Kubernetes objects in their JSON shape (camelCase, as the
API returns them and `ApiClient.sanitize_for_serialization` produces them);
the current time is passed in. The payload's kind/namespace/name is the
debounce key: every pod of a Deployment maps to the Deployment, so a failing
rollout starts one investigation, not one per pod.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Trigger name -> environment variable that turns it off ("false").
TRIGGER_ENV = {
    "CrashLoopBackOff": "TRIGGER_CRASHLOOP",
    "OOMKilled": "TRIGGER_OOMKILLED",
    "ImagePullBackOff": "TRIGGER_IMAGEPULL",
    "FailedScheduling": "TRIGGER_FAILEDSCHED",
    "NodeNotReady": "TRIGGER_NODENOTREADY",
    "FailedMount": "TRIGGER_FAILEDMOUNT",
    "StuckResource": "TRIGGER_STUCKRESOURCE",
    # Missing Secret/ConfigMap referenced as an env (envFrom/valueFrom): the
    # kubelet cannot build the container config. A volume mount failure is
    # FailedMount, a separate event.
    "CreateContainerConfigError": "TRIGGER_CONFIGERROR",
    # Container cannot start (bad command/entrypoint, missing binary, perms).
    "RunContainerError": "TRIGGER_RUNCONTAINERERROR",
    # Node resource pressure (Memory/Disk/PID) or cordoned.
    "NodePressure": "TRIGGER_NODEPRESSURE",
    # Pod sandbox / CNI setup failure: pod stuck ContainerCreating.
    "FailedCreatePodSandBox": "TRIGGER_PODSANDBOX",
    # Running but persistently not Ready (readiness probe failing past the
    # threshold, which is generous so slow model loads do not fire).
    "PodNotReady": "TRIGGER_PODNOTREADY",
}

# The agent's own workloads (event-watcher, Investigator and Remediator Jobs)
# share the ai-platform namespace with services it watches. Pods carry this
# label; events carry no labels, so their names are matched instead.
PART_OF = "platform-health-agent"
_OWN_NAME_RE = re.compile(r"^(investigator-[0-9a-f]{8}|remediator-[0-9a-f]{8}|event-watcher-)")
# ReplicaSet name: <deployment-name>-<6 to 10 hex characters>.
_RS_HASH_RE = re.compile(r"-[a-f0-9]{6,10}$")


@dataclass(frozen=True)
class Rules:
    enabled: frozenset[str] = frozenset(TRIGGER_ENV)
    watch_namespaces: str = "*"                       # "*" or a comma-separated list
    exclude_namespaces: frozenset[str] = field(default_factory=frozenset)
    pod_not_ready_sec: int = 600
    stuck_sec: int = 600
    own_namespace: str = "ai-platform"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Rules:
        return cls(
            enabled=frozenset(t for t, var in TRIGGER_ENV.items() if env.get(var, "true").lower() == "true"),
            watch_namespaces=env.get("WATCH_NAMESPACES", "*"),
            exclude_namespaces=frozenset(filter(None, env.get("EXCLUDE_NAMESPACES", "").split(","))),
            pod_not_ready_sec=int(env.get("POD_NOT_READY_THRESHOLD_SEC", "600")),
            stuck_sec=int(env.get("STUCK_RESOURCE_THRESHOLD_SEC", "600")),
        )

    def on(self, trigger: str) -> bool:
        return trigger in self.enabled


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def age_seconds(obj: dict, now: datetime) -> float:
    """Seconds since metadata.creationTimestamp; 0 when it is missing or invalid."""
    created = _parse_time((obj.get("metadata") or {}).get("creationTimestamp"))
    return (now - created).total_seconds() if created else 0.0


def namespace_watched(ns: str, rules: Rules) -> bool:
    if ns in rules.exclude_namespaces:
        return False
    if rules.watch_namespaces == "*":
        return True
    return ns in rules.watch_namespaces.split(",")


def is_own_workload(namespace: str | None, name: str | None, labels: dict | None, rules: Rules) -> bool:
    if namespace != rules.own_namespace:
        return False
    if labels and labels.get("app.kubernetes.io/part-of") == PART_OF:
        return True
    return bool(_OWN_NAME_RE.match(name or ""))


def owner_root(pod: dict) -> tuple[str, str]:
    """(kind, name) of the pod's top-level controller: a ReplicaSet maps to its
    Deployment (hash stripped); StatefulSet, DaemonSet and Job are kept; an
    orphan pod is itself."""
    meta = pod.get("metadata") or {}
    for ref in meta.get("ownerReferences") or []:
        if ref.get("kind") == "ReplicaSet":
            return "Deployment", _RS_HASH_RE.sub("", ref.get("name", ""))
        if ref.get("kind") in ("StatefulSet", "DaemonSet", "Job"):
            return ref["kind"], ref.get("name", "")
    return "Pod", meta.get("name", "")


def _pod_payload(pod: dict, container: str, detail: str) -> dict:
    meta, status = pod.get("metadata") or {}, pod.get("status") or {}
    kind, name = owner_root(pod)
    return {
        "kind": kind,
        "namespace": meta.get("namespace"),
        "name": name,
        "pod_name": meta.get("name"),
        "node": (pod.get("spec") or {}).get("nodeName"),
        "container": container,
        "detail": detail,
        "owner_references": [{"kind": o.get("kind"), "name": o.get("name")}
                             for o in meta.get("ownerReferences") or []],
        "phase": status.get("phase"),
    }


def _describe(state: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in state.items() if v is not None)


# ---------------------------------------------------------------------------
# Pods
# ---------------------------------------------------------------------------

def detect_pod(pod: dict, rules: Rules, now: datetime) -> tuple[str, dict] | None:
    """(trigger, payload) for an actionable pod, else None.

    Init containers are checked first: while one fails, the main containers
    sit in PodInitializing, which is not a trigger. Container states take
    precedence over PodNotReady."""
    meta, status = pod.get("metadata") or {}, pod.get("status")
    ns = meta.get("namespace")
    if ns and not namespace_watched(ns, rules):
        return None
    if is_own_workload(ns, meta.get("name"), meta.get("labels"), rules):
        return None
    if not status:
        return None

    for statuses, label in ((status.get("initContainerStatuses") or [], "init"),
                            (status.get("containerStatuses") or [], "main")):
        for cs in statuses:
            container = f"{cs.get('name')} ({label})"
            restarts = cs.get("restartCount") or 0
            state = cs.get("state") or {}
            last = (cs.get("lastState") or {}).get("terminated")
            if last and last.get("reason") == "OOMKilled" and rules.on("OOMKilled"):
                return "OOMKilled", _pod_payload(pod, container, _describe(last))
            # Init containers between retries may show terminated rather than
            # waiting/CrashLoopBackOff.
            term = state.get("terminated")
            if (label == "init" and term and term.get("exitCode") != 0 and restarts > 3
                    and rules.on("CrashLoopBackOff")):
                return "CrashLoopBackOff", _pod_payload(
                    pod, container,
                    f"init container exit {term.get('exitCode')} (reason={term.get('reason')}); restarts={restarts}")
            waiting = state.get("waiting") or {}
            reason, message = waiting.get("reason"), waiting.get("message") or ""
            if reason == "CrashLoopBackOff" and restarts > 3 and rules.on("CrashLoopBackOff"):
                return "CrashLoopBackOff", _pod_payload(pod, container, message)
            if reason in ("ImagePullBackOff", "ErrImagePull") and rules.on("ImagePullBackOff"):
                return "ImagePullBackOff", _pod_payload(pod, container, message)
            # Persistent states (the kubelet retries forever): no restart gate.
            if (reason in ("CreateContainerConfigError", "CreateContainerError")
                    and rules.on("CreateContainerConfigError")):
                return "CreateContainerConfigError", _pod_payload(pod, container, message)
            if reason in ("RunContainerError", "StartError") and rules.on("RunContainerError"):
                return "RunContainerError", _pod_payload(pod, container, message)

    if rules.on("PodNotReady") and _persistently_unready(status, rules, now):
        return "PodNotReady", _pod_payload(
            pod, "(pod)",
            f"Pod Running but not Ready for >{rules.pod_not_ready_sec}s (readiness probe failing)")
    return None


def _persistently_unready(status: dict, rules: Rules, now: datetime) -> bool:
    """Running, Ready=False, and that for longer than the threshold (since the
    Ready condition last changed, else since the pod started)."""
    if status.get("phase") != "Running":
        return False
    ready = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), None)
    if not ready or ready.get("status") != "False":
        return False
    since = _parse_time(ready.get("lastTransitionTime")) or _parse_time(status.get("startTime"))
    if not since:
        return False
    return (now - since).total_seconds() >= rules.pod_not_ready_sec


# ---------------------------------------------------------------------------
# Events and nodes
# ---------------------------------------------------------------------------

def detect_event(event: dict, rules: Rules) -> tuple[str, dict] | None:
    """Warning events: FailedScheduling, FailedMount, pod sandbox failures."""
    if event.get("type") != "Warning":
        return None
    obj = event.get("involvedObject") or {}
    ns = obj.get("namespace")
    if ns and not namespace_watched(ns, rules):
        return None
    if is_own_workload(ns, obj.get("name"), None, rules):
        return None
    reason = event.get("reason")
    payload = {"kind": obj.get("kind"), "namespace": ns or "", "name": obj.get("name"),
               "reason": reason, "message": event.get("message"), "count": event.get("count")}
    if reason == "FailedScheduling" and rules.on("FailedScheduling"):
        return "FailedScheduling", payload
    if reason == "FailedMount" and rules.on("FailedMount"):
        return "FailedMount", payload
    if reason in ("FailedCreatePodSandBox", "FailedPodSandBoxStatus") and rules.on("FailedCreatePodSandBox"):
        return "FailedCreatePodSandBox", payload
    return None


def detect_node(node: dict, rules: Rules) -> tuple[str, dict] | None:
    """NotReady first (most severe), then Memory/Disk/PID pressure, then
    cordoned (ignored while the node is being deleted: Karpenter drains)."""
    conditions = (node.get("status") or {}).get("conditions")
    if not conditions:
        return None
    meta = node.get("metadata") or {}
    name = meta.get("name")

    def payload(reason, message):
        return {"kind": "Node", "namespace": "", "name": name, "reason": reason, "message": message}

    conds = {c.get("type"): c for c in conditions}
    ready = conds.get("Ready")
    if rules.on("NodeNotReady") and ready and ready.get("status") == "False":
        return "NodeNotReady", payload(ready.get("reason"), ready.get("message"))
    if rules.on("NodePressure"):
        for ctype in ("MemoryPressure", "DiskPressure", "PIDPressure"):
            c = conds.get(ctype)
            if c and c.get("status") == "True":
                return "NodePressure", payload(ctype, c.get("message") or ctype)
        if (node.get("spec") or {}).get("unschedulable") and not meta.get("deletionTimestamp"):
            return "NodePressure", payload("Unschedulable", f"Node {name} is cordoned (unschedulable)")
    return None


# ---------------------------------------------------------------------------
# Stuck kro resources (not healthy STUCK_RESOURCE_THRESHOLD_SEC after creation)
# ---------------------------------------------------------------------------

def stuck_aiteam(team: dict, rules: Rules, now: datetime) -> bool:
    # kro reports state ACTIVE and a Ready condition when the team is healthy.
    status = team.get("status") or {}
    if status.get("state") == "ACTIVE":
        return False
    if any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or []):
        return False
    return age_seconds(team, now) >= rules.stuck_sec


def stuck_vllm(ep: dict, rules: Rules, now: datetime) -> bool:
    # VLLMEndpoint: healthy once its Deployment is available (status.ready "True").
    if str((ep.get("status") or {}).get("ready", "False")) == "True":
        return False
    return age_seconds(ep, now) >= rules.stuck_sec


def stuck_llmd(ep: dict, rules: Rules, now: datetime) -> bool:
    # LLMDEndpoint and LLMDDisaggEndpoint: replicas ready AND the router (EPP
    # and InferencePool, an Argo CD Application) Healthy.
    status = ep.get("status") or {}
    if str(status.get("ready", "False")) == "True" and status.get("routerHealth", "") == "Healthy":
        return False
    return age_seconds(ep, now) >= rules.stuck_sec


# A scale-to-zero LLMDEndpoint (minReplicas 0) is not Ready while its first
# replica starts; that is a cold start, not a stuck resource, for as long as the
# llm-d router holds requests (coldStartTimeoutSeconds) plus this margin.
WAKE_MARGIN_SEC = 120


def waking_from_zero(ep: dict, deployment: dict | None, now: datetime) -> bool:
    """True while a minReplicas-0 LLMDEndpoint starts a replica from zero: its
    Deployment's Available condition turned False (0 -> 1 replicas) less than
    coldStartTimeoutSeconds + WAKE_MARGIN_SEC ago."""
    spec = ep.get("spec") or {}
    if str(spec.get("minReplicas", 1)) != "0" or not deployment:
        return False
    cond = next((c for c in (deployment.get("status") or {}).get("conditions") or []
                 if c.get("type") == "Available"), None)
    if not cond or cond.get("status") != "False":
        return False
    since = _parse_time(cond.get("lastTransitionTime"))
    if since is None:
        return False
    try:
        budget = int(spec.get("coldStartTimeoutSeconds", 600)) + WAKE_MARGIN_SEC
    except (TypeError, ValueError):
        budget = 600 + WAKE_MARGIN_SEC
    return (now - since).total_seconds() < budget


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
