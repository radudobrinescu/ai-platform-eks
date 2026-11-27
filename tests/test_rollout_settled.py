"""After the Kubernetes bump the smoke checks wait until the node rollout is over.

`platformctl up` with a new Kubernetes version returns once the control plane and
the managed node group are updated; Karpenter then replaces its own nodes (they
drift to the new AMI) and the platform pods move with them. The k8s-bump smoke
started during that rollout, and its port-forwards hit pods that were moving: in
two of three upgrade runs (2026-11-26 and 2026-11-27) the dashboard sign-in
checks failed with refused connections while every other check passed.
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "harness"))

import checks  # noqa: E402


def _claim(name, drifted="False", deleting=False):
    meta = {"name": name}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-27T10:00:00Z"
    return {"metadata": meta, "status": {"conditions": [{"type": "Drifted", "status": drifted}]}}


def _node(name, ready="True"):
    return {"metadata": {"name": name}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}


def _deploy(name, want=2, ready=2, updated=None):
    return {"metadata": {"name": name, "namespace": "ai-platform"},
            "spec": {"replicas": want},
            "status": {"readyReplicas": ready, "updatedReplicas": want if updated is None else updated}}


def test_a_settled_cluster_has_nothing_pending():
    assert checks.rollout_pending({"items": [_claim("a")]}, {"items": [_node("n")]},
                                  {"items": [_deploy("litellm")]}) == []


def test_drifted_or_deleting_claims_mean_the_rollout_is_not_over():
    pending = checks.rollout_pending({"items": [_claim("a", drifted="True"), _claim("b", deleting=True)]},
                                     {"items": []}, {"items": []})
    assert len(pending) == 2 and "a" in pending[0] and "b" in pending[1]


def test_a_node_that_is_not_ready_or_a_platform_pod_that_moves_is_pending():
    pending = checks.rollout_pending({"items": []}, {"items": [_node("n", ready="Unknown")]},
                                     {"items": [_deploy("oauth2-proxy", 1, 0), _deploy("litellm", 2, 2, 1)]})
    assert len(pending) == 3


def test_a_deployment_scaled_to_zero_is_not_pending():
    assert checks.rollout_pending({"items": []}, {"items": []}, {"items": [_deploy("x", 0, 0)]}) == []


def test_the_bump_waits_for_the_rollout_before_the_smoke():
    with open(os.path.join(os.path.dirname(HERE), "harness", "e2e.py"), encoding="utf-8") as fh:
        text = fh.read()
    body = text[text.index("def kubernetes_bump("):text.index("# -- entry")]
    assert body.index('self.step("kubernetes-bump"') < body.index('"node-rollout-settled"') < body.index('self.smoke("k8s-bump/")')


def test_the_dashboard_health_check_retries_a_dropped_connection(monkeypatch):
    calls = {"n": 0}

    class _PF:
        base = "http://127.0.0.1:1"

        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def http(method, url, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("Remote end closed connection without response")
        return 200, "{}"
    monkeypatch.setattr(checks, "PortForward", _PF)
    monkeypatch.setattr(checks, "http", http)
    monkeypatch.setattr(checks.time, "sleep", lambda s: None)
    ok, detail = checks.chk_dashboard_health(None)
    assert ok and calls["n"] == 2 and re.search(r"HTTP 200", detail)


def test_a_drifted_node_that_karpenter_keeps_on_purpose_is_not_pending():
    """Model pods carry karpenter.sh/do-not-disrupt: Karpenter keeps a drifted
    node that runs one until the pod leaves. The rerun of 2026-11-27 waited 30
    minutes for such a node (the smoke model's) and failed the step although
    every check after it passed."""
    claim = _claim("gpu-shared-xd56c", drifted="True")
    claim["status"]["nodeName"] = "ip-10-10-1-1"
    assert checks.rollout_pending({"items": [claim]}, {"items": []}, {"items": []},
                                  protected_nodes={"ip-10-10-1-1"}) == []
    assert checks.rollout_pending({"items": [claim]}, {"items": []}, {"items": []}, protected_nodes=set()) != []


def test_protected_nodes_are_the_nodes_of_do_not_disrupt_pods():
    pods = {"items": [
        {"metadata": {"annotations": {"karpenter.sh/do-not-disrupt": "true"}}, "spec": {"nodeName": "a"}},
        {"metadata": {"annotations": {}}, "spec": {"nodeName": "b"}},
        {"metadata": {"annotations": {"karpenter.sh/do-not-disrupt": "true"}}, "spec": {}},
    ]}
    assert checks.do_not_disrupt_nodes(pods) == {"a"}
