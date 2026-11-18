"""release.py finish: the one command after four green E2E runs. gh is faked; git
runs against a local bare origin, so the merge, the tag and the branch delete are
checked for real. The merge is done by the fake gh the way GitHub does it (a merge
commit on main).

finish works on the remote refs, so it runs from any clone (a workflow or a
laptop), changes nothing in the release branch after E2E, tags only a merge
commit whose tree is the tested tree, and resumes when an earlier run stopped
half way."""

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


def green(url="https://ci/run/1", base="main", llmd="true"):
    return {c: {"state": "success", "target_url": url,
                "description": release.status_description(c.split("/")[1].split("-")[0], c.split("-")[1], "passed",
                                                          base=base, llmd=llmd)}
            for c in release.E2E_CONTEXTS}


def tree_of(repo, rev):
    return git(repo, "rev-parse", f"{rev}^{{tree}}").strip()


class FakeFail(Exception):
    def __init__(self, rc):
        self.rc = rc


class FakeGh:
    """Just enough of gh: pr list / pr view / pr merge / release / api statuses."""

    def __init__(self, origin, pr=7, states=("CLEAN",), statuses=None):
        self.origin, self.pr, self.states, self.calls = origin, pr, list(states), []
        self.statuses = statuses if statuses is not None else green()
        self.merged = False
        self.merge_commit = None
        self.race = False          # another PR lands on main between the check and the merge
        self.releases = {}
        self.titles = {}
        self.labels = ["publish"]  # a release; [] = an integration cycle
        self.earlier = []          # PRs of earlier cycles that used the same branch name
        self._head = None

    def head(self):
        if self._head is None:
            self._head = git(self.origin, "rev-parse", f"release/{V}").strip()
        return self._head

    def __call__(self, *args):
        self.calls.append(args)
        a = list(args)
        if a[:2] == ["pr", "list"]:
            assert a[a.index("--state") + 1] == "all"   # finish must also find a merged PR (resume)
            return json.dumps(self.earlier + [{
                "number": self.pr, "headRefOid": self.head(), "baseRefName": "main",
                "state": "MERGED" if self.merged else "OPEN", "labels": [{"name": n} for n in self.labels],
                "mergeCommit": {"oid": self.merge_commit} if self.merged else None}])
        if a[:2] == ["pr", "view"]:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return json.dumps({"mergeStateStatus": state, "headRefOid": self.head()})
        if a[:2] == ["pr", "merge"]:
            assert a[a.index("--match-head-commit") + 1] == self.head()
            git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")  # bare repo: merge via a temp clone
            tmp = str(self.origin) + f"-clone{len(self.calls)}"
            subprocess.run(["git", "clone", "-q", str(self.origin), tmp], check=True, env=GIT_ENV)
            if self.race:
                with open(os.path.join(tmp, "late.txt"), "w") as fh:
                    fh.write("landed on main after the cut\n")
                git(tmp, "add", "-A")
                git(tmp, "commit", "-qm", "fix: lands during the merge")
            git(tmp, "merge", "--no-ff", "-m", f"Merge pull request #{self.pr}", f"origin/release/{V}")
            git(tmp, "push", "-q", "origin", "HEAD:main")
            self.merge_commit = git(tmp, "rev-parse", "HEAD").strip()
            self.merged = True
            return ""
        if a[:2] == ["release", "view"]:
            if a[2] not in self.releases:
                raise FakeFail(1)
            return ""
        if a[:2] == ["release", "create"]:
            notes = open(a[a.index("--notes-file") + 1], encoding="utf-8").read()
            assert "--verify-tag" in a
            self.releases[a[2]] = notes
            self.titles[a[2]] = a[a.index("--title") + 1]
            return ""
        if a[:1] == ["api"] and "/statuses" in a[1] and "-X" not in a:
            return json.dumps([{"context": c, **d} for c, d in self.statuses.items()])
        raise AssertionError(f"unexpected gh call: {args}")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """origin (bare) with main + release/v2026.11.0 that has notes; a clone that is NOT on the
    release branch (finish reads the remote refs)."""
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
    git(work, "switch", "-q", "main")
    fake = FakeGh(origin)
    monkeypatch.setattr(release, "gh", fake)
    monkeypatch.setattr(release, "status_details_from_github", lambda slug, sha: fake.statuses)
    monkeypatch.setattr(release.subprocess, "run", _no_real_gh(release.subprocess.run, fake))
    return work, origin, fake


def _no_real_gh(real_run, fake):
    """publish() probes `gh release view` with subprocess.run(['gh', ...]); route that to the fake."""
    def run(cmd, *a, **k):
        if cmd and cmd[0] == "gh":
            try:
                return subprocess.CompletedProcess(cmd, 0, fake(*cmd[1:]), "")
            except FakeFail as e:
                return subprocess.CompletedProcess(cmd, e.rc, "", "not found")
        return real_run(cmd, *a, **k)
    return run


def test_finish_merges_tags_the_tested_tree_and_deletes_the_branch(repos):
    work, origin, fake = repos
    tested = git(origin, "rev-parse", f"release/{V}").strip()
    res = release.finish(str(work), V, SLUG)
    assert res["tested"] == tested and res["tag"] == V
    main = git(origin, "rev-parse", "main").strip()
    assert res["merge"] == main == fake.merge_commit
    assert git(origin, "rev-parse", f"{V}^{{commit}}").strip() == main          # tag on the merge commit
    assert tree_of(origin, V) == tree_of(origin, tested)                         # ...whose tree is the tested tree
    assert git(origin, "rev-parse", f"{main}^2").strip() == tested               # nothing added after E2E
    assert f"release/{V}" not in git(origin, "branch", "--list")                    # branch deleted
    assert any(c[:2] == ("pr", "merge") and "--merge" in c for c in fake.calls)
    assert fake.releases[V].startswith(f"# Release {V}")                 # GitHub Release from the notes


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
    assert res["dry_run"] and res["pr"] == 7 and len(res["steps"]) == 6     # wait, merge, check, tag, publish, delete
    assert git(origin, "for-each-ref") == before
    assert not any(c[:2] == ("pr", "merge") for c in fake.calls) and not fake.releases


def test_refuses_without_four_green_runs(repos):
    work, origin, fake = repos
    fake.statuses = {**green(), "e2e/upgrade-managed": {"state": "failure", "target_url": "", "description": ""}}
    with pytest.raises(release.ReleaseError, match="not every E2E run passed"):
        release.finish(str(work), V, SLUG)
    assert f"release/{V}" in git(origin, "branch", "--list") and not fake.merged


def test_refuses_runs_that_tested_something_else(repos):
    """The statuses must come from the release run: upgrade from the previous
    release, llm-d tier on. A manual run with other inputs does not count."""
    work, origin, fake = repos
    git(work, "switch", "-q", f"release/{V}")
    path = work / f"releases/{V}.json"
    manifest = json.loads(path.read_text())
    manifest["previous"] = "v2026.10.3"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    git(work, "commit", "-qam", "upgrades from v2026.10.3")
    git(work, "push", "-q", "origin", f"release/{V}")
    git(work, "switch", "-q", "main")
    fake.statuses = green(base="main")
    with pytest.raises(release.ReleaseError, match="e2e/upgrade-default=upgraded from main, not v2026.10.3"):
        release.finish(str(work), V, SLUG)
    fake.statuses = green(base="v2026.10.3", llmd="false")
    with pytest.raises(release.ReleaseError, match="without the llm-d tier"):
        release.finish(str(work), V, SLUG)
    assert not fake.merged
    fake.statuses = green(base="v2026.10.3")
    assert release.finish(str(work), V, SLUG)["tag"] == V


def test_refuses_when_main_moved_since_the_cut(repos):
    """main must be an ancestor of the release head: otherwise the merge result
    is a tree that E2E never saw."""
    work, origin, fake = repos
    (work / "b.txt").write_text("b\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "fix: merged into main after the cut")
    git(work, "push", "-q", "origin", "main")
    with pytest.raises(release.ReleaseError, match="main moved since"):
        release.finish(str(work), V, SLUG)
    assert not fake.merged and not git(origin, "tag", "--list", V).strip()


def test_never_tags_a_merge_whose_tree_was_not_tested(repos):
    """Race: a commit lands on main between the check and the merge."""
    work, origin, fake = repos
    fake.race = True
    with pytest.raises(release.ReleaseError, match="not the tree E2E tested"):
        release.finish(str(work), V, SLUG)
    assert fake.merged and not git(origin, "tag", "--list", V).strip() and not fake.releases
    with pytest.raises(release.ReleaseError, match="not the tree E2E tested"):   # and a rerun does not tag it either
        release.finish(str(work), V, SLUG)


def test_resumes_after_the_merge(repos):
    """An earlier run merged the PR and died before tagging."""
    work, origin, fake = repos
    tested = fake.head()
    fake("pr", "merge", "7", "--repo", SLUG, "--merge", "--match-head-commit", tested)
    res = release.finish(str(work), V, SLUG)
    assert res["resumed"] == "merged" and res["merge"] == fake.merge_commit
    assert git(origin, "rev-parse", f"{V}^{{commit}}").strip() == fake.merge_commit
    assert sum(1 for c in fake.calls if c[:2] == ("pr", "merge")) == 1          # not merged twice
    assert V in fake.releases and f"release/{V}" not in git(origin, "branch", "--list")


def test_resumes_after_the_tag_and_is_a_no_op_when_done(repos):
    work, origin, fake = repos
    release.finish(str(work), V, SLUG)
    del fake.releases[V]                                  # died before the GitHub Release
    res = release.finish(str(work), V, SLUG)
    assert res["resumed"] == "tagged" and V in fake.releases
    again = release.finish(str(work), V, SLUG)
    assert again["resumed"] == "tagged" and again["published"] is False
    assert sum(1 for c in fake.calls if c[:2] == ("pr", "merge")) == 1


def test_dirty_pr_is_refused_not_merged(repos):
    work, origin, fake = repos
    fake.states = ["DIRTY"]
    with pytest.raises(release.ReleaseError, match="DIRTY"):
        release.finish(str(work), V, SLUG)
    assert not fake.merged


def test_publish_creates_the_release_once_from_the_notes_at_the_tag(repos):
    work, origin, fake = repos
    release.finish(str(work), V, SLUG)
    first = fake.releases[V]
    assert release.publish(str(work), V, SLUG) is False and fake.releases[V] == first   # idempotent
    del fake.releases[V]
    assert release.publish(str(work), V, SLUG) is True and fake.releases[V] == first


def test_publish_adds_platform_changes_to_pages_of_older_releases(repos, tmp_path):
    """Releases tagged before notes listed changes get the section on the page only."""
    work, origin, fake = repos
    release.finish(str(work), V, SLUG)
    nxt = "v2026.11.1"
    git(work, "fetch", "-q", "origin")
    git(work, "switch", "-q", "-c", "fix/x", "origin/main")
    (work / "x.txt").write_text("x\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "fix: something users notice")
    git(work, "switch", "-q", "-c", "m2", "origin/main")
    git(work, "merge", "-q", "--no-ff", "-m", "Merge pull request #3 from owner/fix/x", "-m", "fix: something users notice", "fix/x")
    manifest = {"version": nxt, "previous": V, "kubernetes": {"supported": ["1.36"], "recommended": "1.36"},
                "required_tfvars": [], "manual_steps": [], "acknowledged_replacements": []}
    (work / "releases").mkdir(exist_ok=True)
    (work / f"releases/{nxt}.json").write_text(json.dumps(manifest))
    (work / f"docs/releases/{nxt}.md").write_text(f"# Release {nxt}\n\n## Component versions\n\n| a | b | c |\n")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "old-style notes without changes")
    git(work, "tag", nxt)
    assert release.publish(str(work), nxt, SLUG) is True
    page = fake.releases[nxt]
    assert "## Platform changes" in page and "- Something users notice" in page
    assert page.index("## Platform changes") < page.index("## Component versions") and "#3" not in page


def test_if_green_is_quiet_while_e2e_has_not_passed_and_loud_about_everything_else(repos, capsys):
    """The workflow that runs after every E2E run: not green yet is not an error."""
    work, origin, fake = repos
    fake.statuses = {**green(), "e2e/upgrade-managed": {"state": "pending", "target_url": "", "description": ""}}
    assert release.main(["--repo", str(work), "finish", V, "--github", SLUG, "--if-green"]) == 0
    assert "not finishing" in capsys.readouterr().out and not fake.merged
    assert release.main(["--repo", str(work), "finish", V, "--github", SLUG]) == 2          # by hand: an error
    fake.statuses = green()
    fake.states = ["DIRTY"]
    assert release.main(["--repo", str(work), "finish", V, "--github", SLUG, "--if-green"]) == 2   # a real problem
    fake.states = ["CLEAN"]
    assert release.main(["--repo", str(work), "finish", V, "--github", SLUG, "--if-green"]) == 0
    assert fake.merged and git(origin, "tag", "--list", V).strip() == V


def test_a_tag_that_is_not_on_main_is_not_published(repos):
    """Resume trusts a tag only if it is where finish puts it: on main."""
    work, origin, fake = repos
    git(work, "fetch", "-q", "origin", f"release/{V}")
    git(work, "tag", "-a", V, "-m", "by hand", "FETCH_HEAD")        # the release head, never merged
    git(work, "push", "-q", "origin", V)
    with pytest.raises(release.ReleaseError, match="is not on origin/main"):
        release.finish(str(work), V, SLUG)
    assert not fake.releases and f"release/{V}" in git(origin, "branch", "--list")


# --------------------------------------------------------------------------- #
# Integration cycles: merged, never tagged
# --------------------------------------------------------------------------- #
def test_without_the_publish_label_the_candidate_is_merged_and_not_tagged(repos):
    work, origin, fake = repos
    fake.labels = []
    tested = git(origin, "rev-parse", f"release/{V}").strip()
    assert len(release.finish(str(work), V, SLUG, dry_run=True)["steps"]) == 4       # no tag, no publish
    res = release.finish(str(work), V, SLUG)
    assert res["tag"] is None and res["published"] is None and res["merge"] == fake.merge_commit
    assert tree_of(origin, "main") == tree_of(origin, tested)                         # same checks as a release
    assert not git(origin, "tag", "--list", V).strip() and not fake.releases
    assert f"release/{V}" not in git(origin, "branch", "--list")


def test_the_label_can_be_overridden_either_way(repos):
    work, origin, fake = repos
    fake.labels = []
    assert release.finish(str(work), V, SLUG, publish_release=True)["tag"] == V
    assert git(origin, "tag", "--list", V).strip() == V


def test_cli_says_integrated_or_finished(repos, capsys):
    work, origin, fake = repos
    assert release.main(["--repo", str(work), "finish", V, "--github", SLUG, "--no-publish"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"integrated {V}") and "Canary" not in out
    assert not git(origin, "tag", "--list", V).strip()


def test_the_release_page_names_the_kubernetes_version(repos):
    work, origin, fake = repos
    release.finish(str(work), V, SLUG)
    assert fake.titles[V] == f"{V} · Kubernetes 1.36"


def test_earlier_cycles_with_the_same_version_name_do_not_confuse_finish(repos):
    """Until a cycle is published the version name is reused: older merged and
    closed PRs from release/<v> exist. The open one is this candidate's."""
    work, origin, fake = repos
    fake.earlier = [{"number": 3, "headRefOid": "1" * 40, "baseRefName": "main", "state": "MERGED", "labels": [],
                     "mergeCommit": {"oid": "2" * 40}},
                    {"number": 4, "headRefOid": "3" * 40, "baseRefName": "main", "state": "CLOSED", "labels": [],
                     "mergeCommit": None}]
    assert release.release_pr(SLUG, V)["number"] == 7
    assert release.finish(str(work), V, SLUG)["pr"] == 7


def test_a_branch_without_its_own_pr_is_not_finished_as_an_earlier_cycle(repos):
    work, origin, fake = repos
    head = fake.head()
    fake.merged, fake.merge_commit, fake._head = True, git(origin, "rev-parse", "main").strip(), "4" * 40
    assert git(origin, "rev-parse", f"release/{V}").strip() == head != fake.head()
    with pytest.raises(release.ReleaseError, match="has no PR of its own"):
        release.finish(str(work), V, SLUG)
    assert f"release/{V}" in git(origin, "branch", "--list")
