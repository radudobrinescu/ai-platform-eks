"""Planning rules of the scheduled release candidate (tools/release_candidate.py)."""

import os
import subprocess
from datetime import date

import pytest
import yaml

import release_candidate as rc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
ANCHOR = date(2026, 11, 9)


def g(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout


def commit(repo, rel, text, msg):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", msg)


@pytest.fixture()
def repo(tmp_path):
    """origin with main tagged v2026.11.1 and group branches in every state."""
    o = tmp_path / "origin"
    o.mkdir()
    g(o, "init", "-q", "-b", "main")
    commit(o, "a.txt", "a\n", "a")
    g(o, "switch", "-q", "-c", "renovate/stale")
    commit(o, "s.txt", "s\n", "stale update")
    g(o, "switch", "-q", "main")
    commit(o, "a.txt", "released\n", "release v2026.11.1")
    g(o, "tag", "v2026.11.1")
    for name in ("renovate/gateway", "renovate/major-gateway", "renovate/held", "renovate/same"):
        g(o, "switch", "-q", "-c", name, "main")
        if name != "renovate/same":
            commit(o, f"{name.split('/')[1]}.txt", "x\n", name)
    g(o, "switch", "-q", "main")
    c = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(o), str(c)], check=True, env=GIT_ENV)
    g(c, "fetch", "-q", "origin", "+refs/heads/renovate/*:refs/remotes/origin/renovate/*")
    return c, o


PRS = [{"headRefName": "renovate/gateway", "labels": []},
       {"headRefName": "renovate/major-gateway", "labels": []},
       {"headRefName": "renovate/held", "labels": [{"name": "hold"}]},
       {"headRefName": "renovate/stale", "labels": []},
       {"headRefName": "renovate/same", "labels": []},
       {"headRefName": "feature/x", "labels": []}]


def test_release_week_is_every_second_monday_from_the_anchor():
    assert rc.release_week(date(2026, 11, 9), ANCHOR)
    assert not rc.release_week(date(2026, 11, 16), ANCHOR)
    assert rc.release_week(date(2026, 11, 23), ANCHOR)
    assert not rc.release_week(date(2026, 11, 2), ANCHOR)


def test_next_version():
    assert rc.next_version("v2026.11.1", date(2026, 11, 9)) == "v2026.11.2"
    assert rc.next_version("v2026.11.1", date(2026, 10, 31)) == "v2026.11.2"  # tag month ahead of today
    assert rc.next_version("v2026.11.4", date(2026, 12, 7)) == "v2026.12.0"
    assert rc.next_version(None, date(2026, 12, 7)) == "v2026.12.0"


def test_plan_selects_fresh_changed_non_major_groups(repo):
    clone, _ = repo
    p = rc.plan(str(clone), PRS, date(2026, 11, 9), ANCHOR)
    assert p["release"] and p["version"] == "v2026.11.2" and p["base"] == "v2026.11.1"
    assert p["branches"] == ["renovate/gateway"]
    assert p["stale"] == ["renovate/stale"]
    assert set(p["held"]) == {"renovate/major-gateway", "renovate/held"}
    assert p["unchanged"] == ["renovate/same"]


def test_majors_need_the_include_label(repo):
    clone, _ = repo
    prs = [dict(pr) for pr in PRS]
    prs[1]["labels"] = [{"name": "include-in-release"}]
    assert "renovate/major-gateway" in rc.plan(str(clone), prs, date(2026, 11, 9), ANCHOR)["branches"]


def test_off_week_does_nothing_unless_forced(repo):
    clone, _ = repo
    assert not rc.plan(str(clone), PRS, date(2026, 11, 16), ANCHOR)["release"]
    assert rc.plan(str(clone), PRS, date(2026, 11, 16), ANCHOR, force=True)["release"]


def test_nothing_to_release(repo):
    clone, _ = repo
    p = rc.plan(str(clone), [{"headRefName": "renovate/same", "labels": []}], date(2026, 11, 9), ANCHOR)
    assert not p["release"] and "nothing to release" in p["reason"]


def test_product_commits_alone_make_a_release(repo):
    clone, origin = repo
    commit(origin, "fix.txt", "f\n", "fix: something")
    g(clone, "fetch", "-q", "origin")
    p = rc.plan(str(clone), [], date(2026, 11, 9), ANCHOR)
    assert p["release"] and p["branches"] == [] and any("fix: something" in c for c in p["product_commits"])


def test_pr_body_lists_what_was_left_out(repo):
    clone, _ = repo
    p = rc.plan(str(clone), PRS, date(2026, 11, 9), ANCHOR)
    body = rc.pr_body(p, "# Release v2026.11.2\n")
    assert "`renovate/stale`" in body and "`renovate/major-gateway`" in body and "# Release v2026.11.2" in body


class TestWorkflow:
    def _wf(self):
        with open(os.path.join(ROOT, ".github/workflows/release-candidate.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        return yaml.safe_load(text), text

    def test_schedule_after_renovate_and_manual_force(self):
        wf, _ = self._wf()
        on = wf[True] if True in wf else wf["on"]
        assert on["schedule"] == [{"cron": "0 7 * * 1"}]
        assert on["workflow_dispatch"]["inputs"]["force"]["type"] == "boolean"

    def test_permissions_and_no_inputs_in_run_scripts(self):
        wf, _ = self._wf()
        assert wf["permissions"] == {"contents": "read"}
        job = wf["jobs"]["candidate"]
        assert job["permissions"] == {"contents": "read", "pull-requests": "write", "issues": "write",
                                      "actions": "write"}
        for s in job["steps"]:
            assert "inputs." not in (s.get("run") or "") and "${{" not in (s.get("run") or ""), s.get("name")

    def test_uses_the_release_tools_and_dispatches_both_modes(self):
        _, text = self._wf()
        for needle in ("tools/release_candidate.py plan", "tools/release.py --repo subject cut",
                       "tools/release.py --repo subject notes", "gh pr create",
                       "gh workflow run e2e.yaml", "-f modes=fresh,upgrade", "-f profiles=default,managed"):
            assert needle in text, needle
        assert "release.py --repo subject record" not in text and "gh pr merge" not in text
