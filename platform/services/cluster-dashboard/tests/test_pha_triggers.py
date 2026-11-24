"""Trigger rules of the Platform Health Agent: which Kubernetes state starts an
investigation, and with which payload (the payload's kind/namespace/name is
also the debounce key). Cases are in tests/pha_cases.py; they were first run
against the pre-split event_watcher (commit "characterization tests for the
agent's trigger rules") and must keep their results."""
import importlib
import json
import sys

import pytest

from pha_cases import ANY, EVENT_CASES, NODE_CASES, NOW, POD_CASES, STUCK_CASES, cs, event, node, pod
from pha import triggers

RULES = triggers.Rules()


def _check(result, expected):
    if expected is None:
        assert result is None
        return
    assert result is not None
    trigger, payload = result
    assert trigger == expected[0]
    want = dict(expected[1])
    if want.get("detail") is ANY:
        assert "reason=OOMKilled" in payload["detail"] and "exitCode=137" in payload["detail"]
        want["detail"] = payload["detail"]
    assert payload == want


def _ids(cases):
    return [c[0] for c in cases]


# ---------------------------------------------------------------------------
# pha.triggers on objects in their JSON shape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", POD_CASES, ids=_ids(POD_CASES))
def test_pod_rules(case):
    _check(triggers.detect_pod(case[1], RULES, NOW), case[2])


@pytest.mark.parametrize("case", EVENT_CASES, ids=_ids(EVENT_CASES))
def test_event_rules(case):
    _check(triggers.detect_event(case[1], RULES), case[2])


@pytest.mark.parametrize("case", NODE_CASES, ids=_ids(NODE_CASES))
def test_node_rules(case):
    _check(triggers.detect_node(case[1], RULES), case[2])


STUCK_FNS = {"AITeam": triggers.stuck_aiteam, "VLLMEndpoint": triggers.stuck_vllm,
             "LLMDEndpoint": triggers.stuck_llmd}


@pytest.mark.parametrize("case", STUCK_CASES, ids=_ids(STUCK_CASES))
def test_stuck_rules(case):
    assert STUCK_FNS[case[1]](case[2], RULES, NOW) is case[3]


def test_disabled_trigger_is_ignored():
    rules = triggers.Rules(enabled=frozenset(triggers.TRIGGER_ENV) - {"ImagePullBackOff", "NodeNotReady"})
    assert triggers.detect_pod(pod(containers=[cs(waiting=("ImagePullBackOff", "x"))]), rules, NOW) is None
    assert triggers.detect_node(node([("Ready", "False", "r", "m")]), rules) is None


def test_excluded_and_unwatched_namespaces():
    p = pod(containers=[cs(waiting=("ImagePullBackOff", "x"))])
    e = event("FailedScheduling")
    excluded = triggers.Rules(exclude_namespaces=frozenset({"inference"}))
    assert triggers.detect_pod(p, excluded, NOW) is None and triggers.detect_event(e, excluded) is None
    assert triggers.detect_pod(p, triggers.Rules(watch_namespaces="team-a,team-b"), NOW) is None
    assert triggers.detect_pod(p, triggers.Rules(watch_namespaces="team-a,inference"), NOW)[0] == "ImagePullBackOff"


def test_rules_from_environment():
    rules = triggers.Rules.from_env({"TRIGGER_IMAGEPULL": "False", "TRIGGER_OOMKILLED": "no",
                                     "WATCH_NAMESPACES": "a,b", "EXCLUDE_NAMESPACES": "kube-system,,x",
                                     "POD_NOT_READY_THRESHOLD_SEC": "60", "STUCK_RESOURCE_THRESHOLD_SEC": "120"})
    assert not rules.on("ImagePullBackOff") and not rules.on("OOMKilled") and rules.on("CrashLoopBackOff")
    assert rules.watch_namespaces == "a,b" and rules.exclude_namespaces == {"kube-system", "x"}
    assert (rules.pod_not_ready_sec, rules.stuck_sec) == (60, 120)
    assert triggers.Rules.from_env({}) == triggers.Rules()


# ---------------------------------------------------------------------------
# Through the event watcher
# ---------------------------------------------------------------------------

def test_event_watcher_uses_the_rules(event_watcher, monkeypatch):
    p = pod(containers=[cs(restarts=4, waiting=("CrashLoopBackOff", "back-off"))])
    assert event_watcher.detect_pod_trigger(p)[0] == "CrashLoopBackOff"
    assert event_watcher.detect_event_trigger(event("FailedMount"))[0] == "FailedMount"
    assert event_watcher.detect_node_trigger(node([("Ready", "False", "r", "m")]))[0] == "NodeNotReady"
    monkeypatch.setattr(event_watcher, "RULES", triggers.Rules(enabled=frozenset()))
    assert event_watcher.detect_pod_trigger(p) is None


@pytest.fixture
def real_client(monkeypatch):
    """The real kubernetes client package when it is installed (locally with
    PYTHONPATH=<pip target>); skipped otherwise (CI installs only pytest,
    pyyaml and boto3)."""
    for name in [m for m in sys.modules if m == "kubernetes" or m.startswith("kubernetes.")]:
        monkeypatch.delitem(sys.modules, name)
    try:
        k8s = importlib.import_module("kubernetes.client")
    except ImportError:
        pytest.skip("kubernetes package not installed")
    cfg = k8s.Configuration()
    cfg.client_side_validation = False               # the cases omit fields the models require
    return k8s.ApiClient(cfg)


CLIENT_CASES = ([("V1Pod", c) for c in POD_CASES] + [("CoreV1Event", c) for c in EVENT_CASES]
                + [("V1Node", c) for c in NODE_CASES])


def test_real_client_objects_give_the_same_triggers(real_client):
    """The watch loops receive typed objects; event_watcher.as_dict turns them
    back into JSON with the client's own serializer before the rules run."""
    detect = {"V1Pod": lambda d: triggers.detect_pod(d, RULES, NOW),
              "CoreV1Event": lambda d: triggers.detect_event(d, RULES),
              "V1Node": lambda d: triggers.detect_node(d, RULES)}
    for model, (case_id, obj, expected) in CLIENT_CASES:
        typed = real_client.deserialize(json.dumps(obj), model, "application/json")
        _check(detect[model](real_client.sanitize_for_serialization(typed)), expected)
