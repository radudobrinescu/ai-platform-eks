"""`down` and `upgrade` act on a cluster through the kubectl context named after
the environment. The name alone proves nothing: another fork's environment, or
the same one in another region, can own a context called `dev`. `down` deletes
every Application, model and Ingress through that context, so it must point at
this environment's cluster or the in-cluster steps are skipped.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLUSTER = "ai-platform-dev"
ARN = "arn:aws:eks:eu-central-1:123456789012:cluster/"

FAKE_KUBECTL = """#!/bin/sh
# `kubectl config view -o jsonpath=...` for the context named in the query.
case "$*" in
  *"config view"*) printf '%s' "$CONTEXT_CLUSTER" ;;
esac
"""


def _function(name: str) -> str:
    text = (ROOT / "platformctl").read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
    assert match, name
    return match.group(0)


def _points_at(tmp_path: Path, context_cluster: str, region: str = "eu-central-1") -> bool:
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(0o755)
    script = _function("_context_is_cluster") + f'_context_is_cluster dev "{CLUSTER}"\n'
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CONTEXT_CLUSTER": context_cluster,
           "AWS_REGION": region}
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    return result.returncode == 0


def test_the_environments_own_cluster_is_accepted(tmp_path: Path) -> None:
    assert _points_at(tmp_path, ARN + CLUSTER)


@pytest.mark.parametrize("other", [
    ARN + "ai-platform-dev2",                                         # a sibling environment
    ARN + "ai-platform-dev-eu",
    ARN + "my-ai-platform-dev",                                       # another fork's prefix
    ARN + "other-dev",
    "arn:aws:eks:us-east-1:123456789012:cluster/" + CLUSTER,          # same name, another region
    "",                                                               # no context of that name
])
def test_any_other_cluster_is_refused(tmp_path: Path, other: str) -> None:
    assert not _points_at(tmp_path, other)


def test_down_checks_the_context_before_it_deletes_anything() -> None:
    body = _function("_predown_drain")
    guard = body.index('_context_is_cluster "$env" "$cluster"')
    assert guard < body.index("delete applicationset")
    assert guard < body.index("get namespace argocd")


def test_the_teardown_watchdog_only_touches_the_right_cluster() -> None:
    body = _function("_teardown_watchdog")
    guard = body.index('_context_is_cluster "$env" "$cluster"')
    assert guard < body.index('_strip_teardown_finalizers "$env"')
    assert guard < body.index('_force_delete_stranded_pods "$env"')


def test_upgrade_refuses_a_context_that_points_elsewhere() -> None:
    body = _function("cmd_upgrade")
    assert body.index("_context_is_cluster") < body.index("get --raw /readyz"), \
        "the context must be checked before the upgrade talks to the cluster"
