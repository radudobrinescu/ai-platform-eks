"""Team offboarding revokes a team only when its AITeam is missing on two runs in a row.

The team-offboarding CronJob revokes every kro-managed LiteLLM team whose alias
has no AITeam, and with it every API key of that team. In the release test of
2026-11-26 an upgrade (kro 0.9.4 -> 0.10.0, default profile) lost the seeded
team and its key: the AITeam was very probably not listed for a moment while
kro restarted, and the CronJob ran in that moment. A team that is missing once
is now only noted; it is revoked when it is still missing 15 minutes later.

The tests run the CronJob's own script against fake `kubectl` and `curl`.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "platform/config/team-offboarding.yaml"

FAKE_KUBECTL = r"""#!/bin/bash
case "$*" in
  *"get aiteams.kro.run"*)
    [ "${AITEAMS_FAIL:-}" = 1 ] && exit 1
    printf '%s' "$AITEAMS" ;;
  *"get configmap team-offboarding-state"*)
    [ -f "$STATE" ] && cat "$STATE" ;;
  *"create configmap team-offboarding-state"*)
    [ "${STATE_WRITE_FAIL:-}" = 1 ] && exit 1
    for a in "$@"; do case "$a" in --from-literal=pending=*) printf '%s' "${a#--from-literal=pending=}" > "$STATE" ;; esac; done
    echo "apiVersion: v1" ;;
  *"apply -f -"*) cat > /dev/null ;;
  *) echo "unexpected kubectl $*" >&2; exit 1 ;;
esac
"""

FAKE_CURL = r"""#!/bin/bash
url=""; data=""
while [ $# -gt 0 ]; do
  case "$1" in -d) data="$2"; shift ;; http*) url="$1" ;; esac
  shift
done
case "$url" in
  */health/liveliness) echo ok ;;
  */team/list) printf '%s' "$TEAMS" ;;
  */team/delete) printf '%s' "$data" | jq -c . >> "$DELETED"; echo '{}' ;;
  *) echo "unexpected curl $url" >&2; exit 1 ;;
esac
"""


def _script() -> str:
    docs = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
    cron = next(d for d in docs if d and d.get("kind") == "CronJob")
    return cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]


def _team(alias: str, tid: str, managed: bool = True) -> dict:
    return {"team_alias": alias, "team_id": tid, "metadata": {"managed_by": "kro"} if managed else {}}


class Cluster:
    def __init__(self, tmp_path: Path) -> None:
        self.dir = tmp_path
        for name, body in (("kubectl", FAKE_KUBECTL), ("curl", FAKE_CURL)):
            path = tmp_path / name
            path.write_text(body)
            path.chmod(0o755)
        self.state = tmp_path / "state"
        self.deleted = tmp_path / "deleted"

    def run(self, aiteams: list[str], teams: list[dict], **extra: str) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.dir}:{os.environ['PATH']}", "LITELLM_MASTER_KEY": "sk-test",
               "AITEAMS": "".join(f"{a}\n" for a in aiteams), "TEAMS": json.dumps(teams),
               "STATE": str(self.state), "DELETED": str(self.deleted), **extra}
        return subprocess.run(["bash", "-c", _script()], capture_output=True, text=True, env=env, timeout=60)

    def deleted_ids(self) -> list[str]:
        if not self.deleted.exists():
            return []
        return [i for line in self.deleted.read_text().splitlines() for i in json.loads(line)["team_ids"]]


@pytest.fixture
def cluster(tmp_path: Path) -> Cluster:
    return Cluster(tmp_path)


def test_a_team_missing_once_is_not_revoked(cluster: Cluster) -> None:
    r = cluster.run(aiteams=[], teams=[_team("search", "t-1")])
    assert r.returncode == 0, r.stdout + r.stderr
    assert cluster.deleted_ids() == []
    assert "t-1" in cluster.state.read_text()


def test_a_team_missing_twice_in_a_row_is_revoked(cluster: Cluster) -> None:
    cluster.run(aiteams=[], teams=[_team("search", "t-1")])
    r = cluster.run(aiteams=[], teams=[_team("search", "t-1")])
    assert r.returncode == 0, r.stdout + r.stderr
    assert cluster.deleted_ids() == ["t-1"]
    assert "t-1" not in cluster.state.read_text()


def test_a_team_that_comes_back_is_forgotten(cluster: Cluster) -> None:
    cluster.run(aiteams=[], teams=[_team("search", "t-1")])
    cluster.run(aiteams=["search"], teams=[_team("search", "t-1")])     # the AITeam is listed again
    r = cluster.run(aiteams=[], teams=[_team("search", "t-1")])         # missing once more: a new first time
    assert r.returncode == 0
    assert cluster.deleted_ids() == []


def test_teams_with_an_aiteam_and_teams_not_managed_by_kro_are_never_touched(cluster: Cluster) -> None:
    teams = [_team("search", "t-1"), _team("manual", "t-2", managed=False)]
    for _ in range(3):
        cluster.run(aiteams=["search"], teams=teams)
    assert cluster.deleted_ids() == []
    assert "t-2" not in cluster.state.read_text()


def test_nothing_is_revoked_when_the_state_cannot_be_saved(cluster: Cluster) -> None:
    cluster.run(aiteams=[], teams=[_team("search", "t-1")])
    r = cluster.run(aiteams=[], teams=[_team("search", "t-1")], STATE_WRITE_FAIL="1")
    assert r.returncode != 0
    assert cluster.deleted_ids() == []


def test_nothing_is_noted_or_revoked_when_aiteams_cannot_be_listed(cluster: Cluster) -> None:
    cluster.run(aiteams=[], teams=[_team("search", "t-1")])
    r = cluster.run(aiteams=[], teams=[_team("search", "t-1")], AITEAMS_FAIL="1")
    assert r.returncode != 0
    assert cluster.deleted_ids() == []


def test_the_job_may_read_and_write_its_state() -> None:
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")) if d]
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "team-offboarding-state")
    assert role["metadata"]["namespace"] == "ai-platform"
    verbs = {v for rule in role["rules"] if rule["resources"] == ["configmaps"] for v in rule["verbs"]}
    assert {"get", "create", "patch"} <= verbs
    binding = next(d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "team-offboarding-state")
    assert binding["subjects"][0]["name"] == "team-offboarding"
