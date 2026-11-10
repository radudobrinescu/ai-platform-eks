"""release.py finish: the one command after four green E2E runs. gh is faked; git
runs against a local bare origin, so the merge, the tag and the branch delete are
checked for real. The merge is done by the fake gh the way GitHub does it (a merge
commit on main)."""

import json
import os
import subprocess

import pytest

import release

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
SLUG = "owner/product"
V = "v2026.11.0"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env=GIT_ENV).stdout


def green(url="https://ci/run/1"):
    return {c: {"state": "success", "target_url": url, "description": f"{c} passed"} for c in release.E2E_CONTEXTS}


class FakeGh:
    """Just enough of gh: pr list / pr view / pr merge / api statuses."""

    def __init__(self, origin, pr=7, states=("CLEAN",), statuses=None):
        self.origin, self.pr, self.states, self.calls = origin, pr, list(states), []
        self.statuses = statuses if statuses is not None else green()
        self.merged = False

    def head(self):
        return git(self.origin, "rev-parse", f"release/{V}").strip()

    def __call__(self, *args):
        self.calls.append(args)
        a = list(args)
        if a[:2] == ["pr", "list"]:
            if self.merged:
                return "[]"
            return json.dumps([{"number": self.pr, "headRefOid": self.head(), "mergeStateStatus": "BLOCKED",
                                "baseRefName": "main"}])
        if a[:2] == ["pr", "view"]:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return json.dumps({"mergeStateStatus": state, "headRefOid": self.head()})
        if a[:2] == ["pr", "merge"]:
            git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")  # bare repo: merge via a temp clone
            tmp = str(self.origin) + "-clone"
            subprocess.run(["git", "clone", "-q", str(self.origin), tmp], check=True, env=GIT_ENV)
            git(tmp, "merge", "--no-ff", "-m", f"Merge pull request #{self.pr}", f"origin/release/{V}")
            git(tmp, "push", "-q", "origin", "HEAD:main")
            self.merged = True
            return ""
        if a[:1] == ["api"] and "/statuses" in a[1] and "-X" not in a:
            return json.dumps([{"context": c, **d} for c, d in self.statuses.items()])
        if a[:1] == ["api"]:
            return ""  # posting carried statuses
        raise AssertionError(f"unexpected gh call: {args}")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """origin (bare) with main + release/v2026.11.0 that has notes; a clone on the release branch."""
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("a\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "base")
    git(work, "tag", "base")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=GIT_ENV)
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main", "base")
    git(work, "switch", "-q", "-c", f"release/{V}")
    (work / "terraform/00.global/vars").mkdir(parents=True)
    (work / "terraform/00.global/vars/example.tfvars").write_text('cluster_config = {\n  kubernetes_version = "1.36"\n}\n')
    git(work, "add", "-A")
    git(work, "commit", "-qm", "bumps")
    release.notes(str(work), V, "base", date="2026-11-02")
    git(work, "push", "-q", "-u", "origin", f"release/{V}")
    fake = FakeGh(origin)
    monkeypatch.setattr(release, "gh", fake)
    monkeypatch.setattr(release, "status_details_from_github", lambda slug, sha: fake.statuses)
    monkeypatch.setattr(release.subprocess, "run", _no_real_gh(release.subprocess.run, fake))
    return work, origin, fake


def _no_real_gh(real_run, fake):
    """record() posts carried statuses with subprocess.run(['gh', ...]); route that to the fake."""
    def run(cmd, *a, **k):
        if cmd and cmd[0] == "gh":
            fake(*cmd[1:])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *a, **k)
    return run


def test_finish_records_merges_tags_and_deletes_the_branch(repos):
    work, origin, fake = repos
    tested = git(work, "rev-parse", "HEAD").strip()
    res = release.finish(str(work), V, SLUG)
    assert res["tested"] == tested and res["tag"] == V
    main = git(origin, "rev-parse", "main").strip()
    assert res["merge"] == main
    assert git(origin, "rev-parse", f"{V}^{{commit}}").strip() == main          # tag on the merge commit
    assert git(origin, "merge-base", "--is-ancestor", res["recorded"], main) == ""  # recorded head is in main
    assert git(origin, "log", "-1", "--format=%s", res["recorded"]).startswith("docs(release): record E2E results")
    assert f"release/{V}" not in git(origin, "branch", "--list")                    # branch deleted
    assert any(c[:2] == ("pr", "merge") and "--merge" in c for c in fake.calls)


def test_finish_waits_for_clean_then_merges(repos):
    work, origin, fake = repos
    fake.states = ["BLOCKED", "BLOCKED", "CLEAN"]
    res = release.finish(str(work), V, SLUG, poll=0)
    assert res["tag"] == V and fake.merged
    assert sum(1 for c in fake.calls if c[:2] == ("pr", "view")) == 3


def test_dry_run_changes_nothing(repos):
    work, origin, fake = repos
    before = git(origin, "for-each-ref")
    res = release.finish(str(work), V, SLUG, dry_run=True)
    assert res["dry_run"] and res["pr"] == 7 and len(res["steps"]) == 5
    assert git(origin, "for-each-ref") == before
    assert not any(c[:2] == ("pr", "merge") for c in fake.calls)
    assert "not run" in (work / "docs/validated-versions.md").read_text()


def test_refuses_without_four_green_runs(repos):
    work, origin, fake = repos
    fake.statuses = {**green(), "e2e/upgrade-managed": {"state": "failure", "target_url": "", "description": ""}}
    with pytest.raises(release.ReleaseError, match="not every E2E run passed"):
        release.finish(str(work), V, SLUG)
    assert f"release/{V}" in git(origin, "branch", "--list")


def test_refuses_when_pr_head_differs_from_local(repos):
    work, origin, fake = repos
    (work / "b.txt").write_text("b\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "local only")
    with pytest.raises(release.ReleaseError, match="differs from local HEAD"):
        release.finish(str(work), V, SLUG)


def test_refuses_off_the_release_branch_or_with_an_existing_tag(repos):
    work, origin, fake = repos
    git(work, "tag", V)
    with pytest.raises(release.ReleaseError, match="already exists"):
        release.finish(str(work), V, SLUG)
    git(work, "tag", "-d", V)
    git(work, "switch", "-q", "main")
    with pytest.raises(release.ReleaseError, match="check out release/"):
        release.finish(str(work), V, SLUG)


def test_dirty_pr_is_refused_not_merged(repos):
    work, origin, fake = repos
    fake.states = ["DIRTY"]
    with pytest.raises(release.ReleaseError, match="DIRTY"):
        release.finish(str(work), V, SLUG)
    assert not fake.merged
