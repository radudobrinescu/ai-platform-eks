"""Trigger cases for the Platform Health Agent: Kubernetes objects in their
JSON shape (as `kubectl get -o json` prints them) and the trigger each one
must produce. Shared by tests/test_pha_triggers.py.

Times are relative to NOW so the age rules can be exercised. Objects carry
the fields the kubernetes client's models require, so the same cases also
run through the real client (tests/test_pha_triggers.py).
"""
from __future__ import annotations

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
        meta["ownerReferences"] = [{"apiVersion": "apps/v1", "kind": owner[0], "name": owner[1], "uid": "u-1"}]
    status = {"phase": phase, "containerStatuses": list(containers)}
    if init:
        status["initContainerStatuses"] = list(init)
    if conditions is not None:
        status["conditions"] = conditions
    if start:
        status["startTime"] = start
    return {"metadata": meta, "spec": {"nodeName": node, "containers": [{"name": "vllm"}]}, "status": status}


def cs(name="vllm", restarts=0, waiting=None, terminated=None, last_terminated=None):
    out = {"name": name, "restartCount": restarts, "state": {}, "image": "img", "imageID": "", "ready": False}
    if waiting:
        out["state"]["waiting"] = {"reason": waiting[0], "message": waiting[1]}
    if terminated:
        out["state"]["terminated"] = terminated
    if last_terminated:
        out["lastState"] = {"terminated": last_terminated}
    return out


def event(reason, type_="Warning", kind="Pod", name="qwen-7d9f8c6b5d-abcde", ns="inference", message="m", count=3,
          last=None, series_last=None):
    out = {"metadata": {"name": "ev-1", "namespace": ns}, "type": type_, "reason": reason, "message": message,
           "count": count, "involvedObject": {"kind": kind, "name": name, "namespace": ns}}
    if last:
        out["lastTimestamp"] = last
    if series_last:                                   # as the scheduler reports a repeated event
        out["series"] = {"count": count, "lastObservedTime": series_last}
    return out


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
    # minReplicas 0, idle at zero replicas: kro reports it Ready.
    ("llmd-scaled-to-zero", "LLMDEndpoint", {"metadata": {"name": "e", "creationTimestamp": ago(minutes=30)},
                                             "spec": {"minReplicas": 0},
                                             "status": {"ready": "True", "routerHealth": "Healthy",
                                                        "scaledToZero": True}}, False),
]


def deployment_available(status: str, since_minutes: int) -> dict:
    return {"status": {"conditions": [{"type": "Available", "status": status,
                                        "lastTransitionTime": ago(minutes=since_minutes)}]}}


# (id, LLMDEndpoint spec, its Deployment, waking from zero?)
WAKE_CASES = [
    ("starting-2-min-ago", {"minReplicas": 0}, deployment_available("False", 2), True),
    ("starting-past-the-hold", {"minReplicas": 0}, deployment_available("False", 13), False),
    ("short-hold", {"minReplicas": 0, "coldStartTimeoutSeconds": 120}, deployment_available("False", 5), False),
    ("available", {"minReplicas": 0}, deployment_available("True", 2), False),
    ("always-warm-model", {"minReplicas": 1}, deployment_available("False", 2), False),
    ("no-deployment", {"minReplicas": 0}, None, False),
]


# ---------------------------------------------------------------------------
# FailedScheduling for the vLLM pod of an LLMDEndpoint (<name>-llmd-<replicaset>-<pod>).
# A scale-to-zero model (minReplicas 0) creates a new pod on every wake, and
# that pod is unschedulable until Karpenter has added its GPU node.
# ---------------------------------------------------------------------------

LLMD_POD = "sleepy-llmd-6c8f9d7b5-x2k4q"


def llmd_endpoint(min_replicas, name="sleepy", **spec) -> dict:
    return {"metadata": {"name": name, "namespace": "inference", "creationTimestamp": ago(hours=5)},
            "spec": {"minReplicas": min_replicas, **spec}}


def _unschedulable(pod_name=LLMD_POD, **kw) -> dict:
    return event("FailedScheduling", name=pod_name, message="0/3 nodes", **kw)


def _failed_scheduling(pod_name=LLMD_POD):
    return ("FailedScheduling", {"kind": "Pod", "namespace": "inference", "name": pod_name,
                                 "reason": "FailedScheduling", "message": "0/3 nodes", "count": 3})


# (id, event, the LLMDEndpoint the caller found for the pod, its Deployment, expected trigger or None)
COLD_START_CASES = [
    ("waking-within-the-hold", _unschedulable(last=ago(minutes=2)), llmd_endpoint(0),
     deployment_available("False", 2), None),
    ("waking-past-the-hold", _unschedulable(last=ago(minutes=1)), llmd_endpoint(0),
     deployment_available("False", 13), _failed_scheduling()),
    ("waking-past-a-short-hold", _unschedulable(last=ago(minutes=1)), llmd_endpoint(0, coldStartTimeoutSeconds=120),
     deployment_available("False", 5), _failed_scheduling()),
    ("always-warm-model", _unschedulable(last=ago(minutes=2)), llmd_endpoint(1),
     deployment_available("False", 2), _failed_scheduling()),
    ("unrelated-pod", event("FailedScheduling", message="0/3 nodes", last=ago(minutes=2)), None, None,
     _failed_scheduling("qwen-7d9f8c6b5d-abcde")),
    # The endpoint handed in is not the one the pod belongs to.
    ("another-models-pod", _unschedulable("other-llmd-6c8f9d7b5-x2k4q", last=ago(minutes=2)), llmd_endpoint(0),
     deployment_available("False", 2), _failed_scheduling("other-llmd-6c8f9d7b5-x2k4q")),
    ("deployment-unreadable", _unschedulable(last=ago(minutes=2)), llmd_endpoint(0), None, _failed_scheduling()),
    # The wake succeeded; the stored event is read again when the watch restarts.
    ("woke-after-the-event", _unschedulable(last=ago(minutes=9)), llmd_endpoint(0),
     deployment_available("True", 5), None),
    ("woke-after-the-event-series", _unschedulable(series_last=ago(minutes=9)), llmd_endpoint(0),
     deployment_available("True", 5), None),
    # Available, and a pod is unschedulable now (scale-out): judged as for any model.
    ("unschedulable-after-waking", _unschedulable(last=ago(minutes=1)), llmd_endpoint(0),
     deployment_available("True", 5), _failed_scheduling()),
    ("unschedulable-after-waking-series", _unschedulable(last=ago(minutes=9), series_last=ago(minutes=1)),
     llmd_endpoint(0), deployment_available("True", 5), _failed_scheduling()),
    ("no-event-time", _unschedulable(), llmd_endpoint(0), deployment_available("True", 5), _failed_scheduling()),
]
