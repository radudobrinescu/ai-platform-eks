"""Trigger rules of the Platform Health Agent: which Kubernetes state starts an
investigation, and with which payload (the payload's kind/namespace/name is
also the debounce key). Cases are in tests/pha_cases.py."""
import pytest

from pha_cases import ANY, EVENT_CASES, NODE_CASES, POD_CASES, STUCK_CASES, as_client_object, cs, event, node, pod


def _check(result, expected):
    if expected is None:
        assert result is None
        return
    assert result is not None
    trigger, payload = result
    assert trigger == expected[0]
    want = dict(expected[1])
    if want.get("detail") is ANY:
        assert "OOMKilled" in payload["detail"]
        want["detail"] = payload["detail"]
    assert payload == want


def _ids(cases):
    return [c[0] for c in cases]


# ---------------------------------------------------------------------------
# Through the event watcher (objects as the kubernetes client returns them)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", POD_CASES, ids=_ids(POD_CASES))
def test_pod_rules(event_watcher, case):
    _check(event_watcher.detect_pod_trigger(as_client_object(case[1])), case[2])


@pytest.mark.parametrize("case", EVENT_CASES, ids=_ids(EVENT_CASES))
def test_event_rules(event_watcher, case):
    _check(event_watcher.detect_event_trigger(as_client_object(case[1])), case[2])


@pytest.mark.parametrize("case", NODE_CASES, ids=_ids(NODE_CASES))
def test_node_rules(event_watcher, case):
    _check(event_watcher.detect_node_trigger(as_client_object(case[1])), case[2])


STUCK_FNS = {"AITeam": "_is_stuck_aiteam", "VLLMEndpoint": "_is_stuck_vllmendpoint",
             "LLMDEndpoint": "_is_stuck_llmdendpoint"}


@pytest.mark.parametrize("case", STUCK_CASES, ids=_ids(STUCK_CASES))
def test_stuck_rules(event_watcher, case):
    assert getattr(event_watcher, STUCK_FNS[case[1]])(case[2]) is case[3]


def test_disabled_trigger_is_ignored(event_watcher, monkeypatch):
    monkeypatch.setitem(event_watcher.TRIGGERS, "ImagePullBackOff", False)
    p = pod(containers=[cs(waiting=("ImagePullBackOff", "x"))])
    assert event_watcher.detect_pod_trigger(as_client_object(p)) is None
    monkeypatch.setitem(event_watcher.TRIGGERS, "NodeNotReady", False)
    n = node([("Ready", "False", "r", "m")])
    assert event_watcher.detect_node_trigger(as_client_object(n)) is None


def test_excluded_and_unwatched_namespaces(event_watcher, monkeypatch):
    p = pod(containers=[cs(waiting=("ImagePullBackOff", "x"))])
    e = event("FailedScheduling")
    monkeypatch.setattr(event_watcher, "EXCLUDE_NAMESPACES", {"inference"})
    assert event_watcher.detect_pod_trigger(as_client_object(p)) is None
    assert event_watcher.detect_event_trigger(as_client_object(e)) is None
    monkeypatch.setattr(event_watcher, "EXCLUDE_NAMESPACES", set())
    monkeypatch.setattr(event_watcher, "WATCH_NAMESPACES", "team-a,team-b")
    assert event_watcher.detect_pod_trigger(as_client_object(p)) is None
    monkeypatch.setattr(event_watcher, "WATCH_NAMESPACES", "team-a,inference")
    assert event_watcher.detect_pod_trigger(as_client_object(p))[0] == "ImagePullBackOff"
