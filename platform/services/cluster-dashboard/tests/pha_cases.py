"""Trigger cases for the Platform Health Agent: Kubernetes objects in their
JSON shape (as `kubectl get -o json` prints them) and the trigger each one
must produce. Shared by tests/test_pha_triggers.py.

Times are relative to "now" so the age rules can be exercised; `as_client_object`
turns a case into the attribute objects the `kubernetes` client returns
(snake_case names, datetimes, None for absent fields).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def pod(name="qwen-7d9f8c6b5d-abcde", ns="inference", owner=("ReplicaSet", "qwen-7d9f8c6b5d"),
        containers=(), init=(), phase="Running", conditions=None, labels=None, start=None, node="ip-10-0-1-1"):
    meta = {"name": name, "namespace": ns}
    if labels is not None:
        meta["labels"] = labels
    if owner:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1]}]
    status = {"phase": phase, "containerStatuses": list(containers)}
    if init:
        status["initContainerStatuses"] = list(init)
    if conditions is not None:
        status["conditions"] = conditions
    if start:
        status["startTime"] = start
    return {"metadata": meta, "spec": {"nodeName": node}, "status": status}


def cs(name="vllm", restarts=0, waiting=None, terminated=None, last_terminated=None):
    out = {"name": name, "restartCount": restarts, "state": {}}
    if waiting:
        out["state"]["waiting"] = {"reason": waiting[0], "message": waiting[1]}
    if terminated:
        out["state"]["terminated"] = terminated
    if last_terminated:
        out["lastState"] = {"terminated": last_terminated}
    return out


def event(reason, type_="Warning", kind="Pod", name="qwen-7d9f8c6b5d-abcde", ns="inference", message="m", count=3):
    return {"type": type_, "reason": reason, "message": message, "count": count,
            "involvedObject": {"kind": kind, "name": name, "namespace": ns}}


def node(conditions=(), unschedulable=None, deleting=False, name="ip-10-0-1-1.ec2.internal"):
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = ago(seconds=5)
    spec = {} if unschedulable is None else {"unschedulable": unschedulable}
    return {"metadata": meta, "spec": spec,
            "status": {"conditions": [{"type": t, "status": s, "reason": r, "message": m}
                                      for t, s, r, m in conditions]}}


def _pod_payload(container, detail, name="qwen", kind="Deployment", pod_name="qwen-7d9f8c6b5d-abcde",
                 owners=(("ReplicaSet", "qwen-7d9f8c6b5d"),), phase="Running", ns="inference"):
    return {"kind": kind, "namespace": ns, "name": name, "pod_name": pod_name, "node": "ip-10-0-1-1",
            "container": container, "detail": detail,
            "owner_references": [{"kind": k, "name": n} for k, n in owners], "phase": phase}


# ---------------------------------------------------------------------------
# Cases: (id, object, expected (trigger, payload) or None)
# The OOMKilled detail is the terminated state printed by the client; only
# its reason is checked (payload "detail" = ANY).
# ---------------------------------------------------------------------------

ANY = object()
UNREADY = [{"type": "Ready", "status": "False", "lastTransitionTime": ago(minutes=20)}]

POD_CASES = [
    ("crashloop", pod(containers=[cs(restarts=4, waiting=("CrashLoopBackOff", "back-off 5m"))]),
     ("CrashLoopBackOff", _pod_payload("vllm (main)", "back-off 5m"))),
    ("crashloop-3-restarts", pod(containers=[cs(restarts=3, waiting=("CrashLoopBackOff", "x"))]), None),
    ("init-exit", pod(phase="Pending", init=[cs("init-model", restarts=5,
                                                 terminated={"exitCode": 1, "reason": "Error"})]),
     ("CrashLoopBackOff", _pod_payload("init-model (init)", "init container exit 1 (reason=Error); restarts=5",
                                       phase="Pending"))),
    ("init-exit-low-restarts", pod(phase="Pending", init=[cs("init-model", restarts=2,
                                                              terminated={"exitCode": 1, "reason": "Error"})]), None),
    ("oomkilled", pod(containers=[cs(restarts=1, last_terminated={"exitCode": 137, "reason": "OOMKilled"})]),
     ("OOMKilled", _pod_payload("vllm (main)", ANY))),
    ("imagepull", pod(containers=[cs(waiting=("ImagePullBackOff", "not found"))]),
     ("ImagePullBackOff", _pod_payload("vllm (main)", "not found"))),
    ("errimagepull", pod(containers=[cs(waiting=("ErrImagePull", "denied"))]),
     ("ImagePullBackOff", _pod_payload("vllm (main)", "denied"))),
    ("config-error", pod(containers=[cs(waiting=("CreateContainerConfigError", 'secret "s" not found'))]),
     ("CreateContainerConfigError", _pod_payload("vllm (main)", 'secret "s" not found'))),
    ("create-error", pod(containers=[cs(waiting=("CreateContainerError", "e"))]),
     ("CreateContainerConfigError", _pod_payload("vllm (main)", "e"))),
    ("run-error", pod(containers=[cs(waiting=("RunContainerError", "exec: not found"))]),
     ("RunContainerError", _pod_payload("vllm (main)", "exec: not found"))),
    ("start-error", pod(containers=[cs(waiting=("StartError", "perm"))]),
     ("RunContainerError", _pod_payload("vllm (main)", "perm"))),
    ("not-ready-20m", pod(containers=[cs()], conditions=UNREADY),
     ("PodNotReady", _pod_payload("(pod)", "Pod Running but not Ready for >600s (readiness probe failing)"))),
    ("not-ready-2m", pod(containers=[cs()], conditions=[{"type": "Ready", "status": "False",
                                                          "lastTransitionTime": ago(minutes=2)}]), None),
    ("not-ready-start-time", pod(containers=[cs()], conditions=[{"type": "Ready", "status": "False"}],
                                 start=ago(minutes=30)),
     ("PodNotReady", _pod_payload("(pod)", "Pod Running but not Ready for >600s (readiness probe failing)"))),
    ("not-ready-pending", pod(phase="Pending", containers=[cs()], conditions=UNREADY), None),
    ("ready", pod(containers=[cs()], conditions=[{"type": "Ready", "status": "True"}]), None),
    ("container-state-wins", pod(containers=[cs(waiting=("ImagePullBackOff", "x"))], conditions=UNREADY),
     ("ImagePullBackOff", _pod_payload("vllm (main)", "x"))),
    ("statefulset-owner", pod(name="db-0", owner=("StatefulSet", "db"),
                              containers=[cs(waiting=("ImagePullBackOff", "x"))]),
     ("ImagePullBackOff", _pod_payload("vllm (main)", "x", name="db", kind="StatefulSet", pod_name="db-0",
                                       owners=(("StatefulSet", "db"),)))),
    ("orphan-pod", pod(name="debug", owner=None, containers=[cs(waiting=("ImagePullBackOff", "x"))]),
     ("ImagePullBackOff", _pod_payload("vllm (main)", "x", name="debug", kind="Pod", pod_name="debug", owners=()))),
    ("own-workload-label", pod(name="investigator-1234abcd-xyz", ns="ai-platform", owner=("Job", "investigator-1234abcd"),
                               labels={"app.kubernetes.io/part-of": "platform-health-agent"},
                               containers=[cs(waiting=("ImagePullBackOff", "x"))]), None),
    ("own-workload-name", pod(name="event-watcher-5d8f-abc", ns="ai-platform", owner=None,
                              containers=[cs(waiting=("ImagePullBackOff", "x"))]), None),
    ("same-name-other-ns", pod(name="event-watcher-5d8f-abc", ns="team-a", owner=None,
                               containers=[cs(waiting=("ImagePullBackOff", "x"))]),
     ("ImagePullBackOff", _pod_payload("vllm (main)", "x", name="event-watcher-5d8f-abc", kind="Pod",
                                       pod_name="event-watcher-5d8f-abc", owners=(), ns="team-a"))),
]

EVENT_CASES = [
    ("failed-scheduling", event("FailedScheduling", message="0/3 nodes"),
     ("FailedScheduling", {"kind": "Pod", "namespace": "inference", "name": "qwen-7d9f8c6b5d-abcde",
                           "reason": "FailedScheduling", "message": "0/3 nodes", "count": 3})),
    ("failed-mount", event("FailedMount"),
     ("FailedMount", {"kind": "Pod", "namespace": "inference", "name": "qwen-7d9f8c6b5d-abcde",
                      "reason": "FailedMount", "message": "m", "count": 3})),
    ("sandbox", event("FailedCreatePodSandBox"),
     ("FailedCreatePodSandBox", {"kind": "Pod", "namespace": "inference", "name": "qwen-7d9f8c6b5d-abcde",
                                 "reason": "FailedCreatePodSandBox", "message": "m", "count": 3})),
    ("sandbox-status", event("FailedPodSandBoxStatus"),
     ("FailedCreatePodSandBox", {"kind": "Pod", "namespace": "inference", "name": "qwen-7d9f8c6b5d-abcde",
                                 "reason": "FailedPodSandBoxStatus", "message": "m", "count": 3})),
    ("normal-event", event("FailedScheduling", type_="Normal"), None),
    ("other-reason", event("BackOff"), None),
    ("own-job-event", event("FailedScheduling", name="remediator-1234abcd", ns="ai-platform", kind="Job"), None),
]

NODE_CASES = [
    ("not-ready", node([("Ready", "False", "KubeletNotReady", "PLEG"), ("MemoryPressure", "True", "x", "y")]),
     ("NodeNotReady", {"kind": "Node", "namespace": "", "name": "ip-10-0-1-1.ec2.internal",
                       "reason": "KubeletNotReady", "message": "PLEG"})),
    ("memory-pressure", node([("Ready", "True", "", ""), ("MemoryPressure", "True", "r", "low mem")]),
     ("NodePressure", {"kind": "Node", "namespace": "", "name": "ip-10-0-1-1.ec2.internal",
                       "reason": "MemoryPressure", "message": "low mem"})),
    ("disk-pressure-no-message", node([("DiskPressure", "True", "r", "")]),
     ("NodePressure", {"kind": "Node", "namespace": "", "name": "ip-10-0-1-1.ec2.internal",
                       "reason": "DiskPressure", "message": "DiskPressure"})),
    ("pid-pressure", node([("PIDPressure", "True", "r", "pids")]),
     ("NodePressure", {"kind": "Node", "namespace": "", "name": "ip-10-0-1-1.ec2.internal",
                       "reason": "PIDPressure", "message": "pids"})),
    ("cordoned", node([("Ready", "True", "", "")], unschedulable=True),
     ("NodePressure", {"kind": "Node", "namespace": "", "name": "ip-10-0-1-1.ec2.internal",
                       "reason": "Unschedulable",
                       "message": "Node ip-10-0-1-1.ec2.internal is cordoned (unschedulable)"})),
    ("cordoned-deleting", node([("Ready", "True", "", "")], unschedulable=True, deleting=True), None),
    ("healthy", node([("Ready", "True", "", ""), ("MemoryPressure", "False", "", "")]), None),
    ("no-conditions", node([]), None),
]

STUCK_CASES = [
    ("aiteam", "AITeam", {"metadata": {"name": "t", "creationTimestamp": ago(minutes=30)}, "status": {}}, True),
    ("aiteam-active", "AITeam", {"metadata": {"name": "t", "creationTimestamp": ago(minutes=30)},
                                 "status": {"state": "ACTIVE"}}, False),
    ("aiteam-ready-cond", "AITeam", {"metadata": {"name": "t", "creationTimestamp": ago(minutes=30)},
                                     "status": {"conditions": [{"type": "Ready", "status": "True"}]}}, False),
    ("aiteam-young", "AITeam", {"metadata": {"name": "t", "creationTimestamp": ago(minutes=2)}, "status": {}}, False),
    ("aiteam-no-timestamp", "AITeam", {"metadata": {"name": "t"}, "status": {}}, False),
    ("vllm", "VLLMEndpoint", {"metadata": {"name": "e", "creationTimestamp": ago(minutes=30)},
                              "status": {"ready": "False"}}, True),
    ("vllm-ready", "VLLMEndpoint", {"metadata": {"name": "e", "creationTimestamp": ago(minutes=30)},
                                    "status": {"ready": "True"}}, False),
    ("vllm-young", "VLLMEndpoint", {"metadata": {"name": "e", "creationTimestamp": ago(minutes=1)}}, False),
    ("llmd-ready-router", "LLMDEndpoint", {"metadata": {"name": "e", "creationTimestamp": ago(minutes=30)},
                                           "status": {"ready": "True", "routerHealth": "Healthy"}}, False),
    ("llmd-router-unhealthy", "LLMDEndpoint", {"metadata": {"name": "e", "creationTimestamp": ago(minutes=30)},
                                               "status": {"ready": "True", "routerHealth": "Progressing"}}, True),
    ("llmd-not-ready", "LLMDEndpoint", {"metadata": {"name": "e", "creationTimestamp": ago(minutes=30)},
                                        "status": {"ready": "False", "routerHealth": "Healthy"}}, True),
]


# ---------------------------------------------------------------------------
# Kubernetes client object stand-in
# ---------------------------------------------------------------------------

def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


class ClientObject:
    """Attribute access like the `kubernetes` client's models: snake_case
    names, None for absent fields, datetimes for *Time/*Timestamp."""

    def __init__(self, data: dict):
        self._data = {}
        for k, v in data.items():
            key = _snake(k)
            if k == "labels":
                self._data[key] = dict(v)
            elif isinstance(v, dict):
                self._data[key] = ClientObject(v)
            elif isinstance(v, list):
                self._data[key] = [ClientObject(x) if isinstance(x, dict) else x for x in v]
            elif isinstance(v, str) and (k.endswith("Time") or k.endswith("Timestamp")):
                self._data[key] = datetime.fromisoformat(v.replace("Z", "+00:00"))
            else:
                self._data[key] = v

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._data.get(name)

    def __str__(self):
        return repr({k: str(v) for k, v in self._data.items()})


def as_client_object(data: dict) -> ClientObject:
    return ClientObject(data)
