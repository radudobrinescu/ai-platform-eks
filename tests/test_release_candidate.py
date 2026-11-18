"""What the release factory does next (tools/release_candidate.py): start a cycle, wait,
retry, finish or abandon it, and which groups go into it."""

import os
import subprocess
from datetime import date

import pytest
import yaml

import release
import release_candidate as rc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
           # Fixed dates: tag_date() reads them, so the tests must not depend on the wall clock.
           "GIT_AUTHOR_DATE": "2026-11-01T23:30:00+02:00", "GIT_COMMITTER_DATE": "2026-11-01T23:30:00+02:00"}
TODAY = date(2026, 11, 9)          # 8 days after the tag (2026-11-01 UTC)


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


def sha(repo, ref):
    return g(repo, "rev-parse", ref).strip()


def st(context, state, at, description=""):
    return {"context": context, "state": state, "description": description, "created_at": at}


def e2e(states=None, at="2026-11-10T10:00:00Z", base="v2026.11.1", llmd="true"):
    """Statuses of the four E2E jobs (newest first); `states` overrides per context."""
    out = []
    for ctx in release.E2E_CONTEXTS:
        mode, profile = ctx.split("/")[1].split("-")
        state = (states or {}).get(ctx, "success")
        out.append(st(ctx, state, at, release.status_description(
            mode, profile, "passed" if state == "success" else "failed", base=base, llmd=llmd)))
    return out


def test_next_version():
    assert rc.next_version("v2026.11.1", date(2026, 11, 9)) == "v2026.11.2"
    assert rc.next_version("v2026.11.1", date(2026, 10, 31)) == "v2026.11.2"  # tag month ahead of today
    assert rc.next_version("v2026.11.4", date(2026, 12, 7)) == "v2026.12.0"
    assert rc.next_version(None, date(2026, 12, 7)) == "v2026.12.0"


# --------------------------------------------------------------------------- #
# Starting a cycle
# --------------------------------------------------------------------------- #
def test_a_cycle_takes_fresh_changed_unheld_groups(repo):
    clone, _ = repo
    p = rc.plan(str(clone), PRS, TODAY)
    assert p["action"] == "cut" and p["release"] and p["kind"] == "combined" and not p["publish"]
    assert p["version"] == "v2026.11.2" and p["base"] == "v2026.11.1"
    # An open major PR exists only after a dashboard tick, so it is approved: it goes in.
    assert p["branches"] == ["renovate/gateway", "renovate/major-gateway"]
    assert p["stale"] == ["renovate/stale"] and p["held"] == ["renovate/held"] and p["unchanged"] == ["renovate/same"]


def test_hold_label_keeps_any_group_out(repo):
    clone, _ = repo
    prs = [dict(pr) for pr in PRS]
    prs[0] = {**prs[0], "labels": [{"name": "hold"}]}
    p = rc.plan(str(clone), prs, TODAY)
    assert "renovate/gateway" not in p["branches"] and "renovate/gateway" in p["held"]


def test_cycles_are_a_week_apart_unless_forced(repo):
    clone, _ = repo
    p = rc.plan(str(clone), PRS, date(2026, 11, 5))
    assert p["action"] == "none" and "2026-11-01" in p["reason"] and not p["attention"]
    assert rc.plan(str(clone), PRS, date(2026, 11, 5), force=True)["action"] == "cut"
    assert rc.plan(str(clone), PRS, date(2026, 11, 8))["action"] == "cut"          # day 7


def test_the_week_counts_from_the_last_merged_cycle_not_from_the_tag(repo):
    """Most cycles are not tagged: the newest merge of a release branch is the clock."""
    clone, origin = repo
    g(origin, "switch", "-q", "-c", "release/v2026.11.2", "main")
    commit(origin, "x.txt", "x\n", "bump")
    g(origin, "switch", "-q", "main")
    env = {**GIT_ENV, "GIT_AUTHOR_DATE": "2026-11-20T10:00:00Z", "GIT_COMMITTER_DATE": "2026-11-20T10:00:00Z"}
    subprocess.run(["git", "-C", str(origin), "merge", "-q", "--no-ff", "-m",
                    "Merge pull request #5 from o/release/v2026.11.2", "release/v2026.11.2"], check=True, env=env)
    g(origin, "branch", "-D", "release/v2026.11.2")
    commit(origin, "fix.txt", "f\n", "fix: after the cycle")
    g(clone, "fetch", "-q", "origin")
    assert rc.last_cycle_date(str(clone), "v2026.11.1", "origin/main") == date(2026, 11, 20)
    assert rc.untested_commits(str(clone), "v2026.11.1", "origin/main") == [
        f"{sha(clone, 'origin/main')[:7]} fix: after the cycle"]
    assert rc.plan(str(clone), [], date(2026, 11, 24))["action"] == "none"
    p = rc.plan(str(clone), [], date(2026, 11, 27))
    assert p["action"] == "cut" and p["version"] == "v2026.11.2"                  # the name is reused until published


def test_nothing_to_integrate(repo):
    clone, _ = repo
    p = rc.plan(str(clone), [{"headRefName": "renovate/same", "labels": []}], TODAY)
    assert p["action"] == "none" and "nothing to integrate" in p["reason"]


def test_product_commits_alone_make_a_cycle(repo):
    clone, origin = repo
    commit(origin, "fix.txt", "f\n", "fix: something")
    g(clone, "fetch", "-q", "origin")
    p = rc.plan(str(clone), [], TODAY)
    assert p["action"] == "cut" and p["branches"] == [] and any("fix: something" in c for c in p["product_commits"])


def test_a_cycle_is_a_release_when_asked_when_due_or_for_a_security_fix(repo):
    clone, _ = repo
    assert not rc.plan(str(clone), PRS, TODAY)["publish"]
    assert rc.plan(str(clone), PRS, TODAY, publish=True)["publish"]
    assert rc.plan(str(clone), PRS, date(2027, 1, 29))["publish"] is False          # 89 days after the tag
    assert rc.plan(str(clone), PRS, date(2027, 1, 30))["publish"] is True           # 90
    prs = [dict(pr) for pr in PRS]
    prs[1] = {**prs[1], "labels": [{"name": "security"}]}
    assert rc.plan(str(clone), prs, TODAY)["publish"]
    prs[1] = {**prs[1], "labels": [{"name": "security"}, {"name": "hold"}]}          # held: not in the cycle
    assert not rc.plan(str(clone), prs, TODAY)["publish"]


def test_one_anchor_update_per_cycle(repo):
    """vLLM and the EKS Kubernetes version never travel together."""
    clone, origin = repo
    for name in ("renovate/minor-1.38-eks-kubernetes", "renovate/vllm"):
        g(origin, "switch", "-q", "-c", name, "main")
        commit(origin, f"{name.split('/')[1]}.txt", "x\n", name)
    g(origin, "switch", "-q", "main")
    g(clone, "fetch", "-q", "origin", "+refs/heads/renovate/*:refs/remotes/origin/renovate/*")
    prs = PRS + [{"headRefName": "renovate/minor-1.38-eks-kubernetes", "labels": []},
                 {"headRefName": "renovate/vllm", "labels": []}]
    p = rc.plan(str(clone), prs, TODAY)
    assert p["branches"] == ["renovate/gateway", "renovate/major-gateway", "renovate/minor-1.38-eks-kubernetes"]
    assert p["deferred"] == ["renovate/vllm"]


# --------------------------------------------------------------------------- #
# Recovery: suspects are tested one at a time
# --------------------------------------------------------------------------- #
def test_a_version_that_failed_alone_stays_out_until_it_changes(repo):
    clone, origin = repo
    marks = {sha(clone, "origin/renovate/gateway"): {rc.SOLO: "failure"}}
    p = rc.plan(str(clone), PRS, TODAY, marks=marks)
    assert p["branches"] == ["renovate/major-gateway"] and p["failed_alone"] == ["renovate/gateway"]
    g(origin, "switch", "-q", "renovate/gateway")
    commit(origin, "gateway.txt", "newer\n", "a newer version")
    g(origin, "switch", "-q", "main")
    g(clone, "fetch", "-q", "origin", "+refs/heads/renovate/*:refs/remotes/origin/renovate/*")
    p = rc.plan(str(clone), PRS, TODAY, marks=marks)                                  # the mark is on the old commit
    assert "renovate/gateway" in p["branches"] and p["failed_alone"] == []


def test_suspects_of_a_failed_candidate_are_tested_one_at_a_time_without_waiting(repo):
    clone, _ = repo
    marks = {sha(clone, "origin/renovate/gateway"): {rc.COMBINED: "failure"},
             sha(clone, "origin/renovate/major-gateway"): {rc.COMBINED: "failure"}}
    p = rc.plan(str(clone), PRS, date(2026, 11, 2), marks=marks)                      # the day after: no week's wait
    assert p["action"] == "cut" and p["kind"] == "solo"
    assert p["branches"] == ["renovate/gateway"] and p["deferred"] == ["renovate/major-gateway"]


def test_main_is_tested_alone_first_and_stops_the_cycles_if_it_fails_alone(repo):
    clone, origin = repo
    commit(origin, "fix.txt", "f\n", "fix: never tested")
    g(clone, "fetch", "-q", "origin")
    head = sha(clone, "origin/main")
    marks = {head: {rc.COMBINED: "failure"}, sha(clone, "origin/renovate/gateway"): {rc.COMBINED: "failure"}}
    p = rc.plan(str(clone), PRS, date(2026, 11, 2), marks=marks)
    assert p["action"] == "cut" and p["kind"] == "product" and p["branches"] == []
    assert "renovate/gateway" in p["deferred"]
    p = rc.plan(str(clone), PRS, TODAY, marks={head: {rc.SOLO: "failure"}}, force=True)
    assert p["action"] == "none" and "cycles resume when main moves" in p["reason"]
    assert not p["attention"]                       # said once, at the abandon; not twice a day
    commit(origin, "fix2.txt", "f\n", "fix: the fix")
    g(clone, "fetch", "-q", "origin")
    assert rc.plan(str(clone), PRS, TODAY, marks={head: {rc.SOLO: "failure"}})["action"] == "cut"


def test_no_verdict_is_for_ever(repo):
    """Two failures can be an outage. A mark expires, so main never stops the
    cycles for good and a version is not left out for good."""
    clone, origin = repo
    commit(origin, "fix.txt", "f\n", "fix: never tested")
    g(clone, "fetch", "-q", "origin")
    head, gw = sha(clone, "origin/main"), sha(clone, "origin/renovate/gateway")
    old = {"state": "failure", "at": "2026-11-01T10:00:00Z"}
    marks = {head: {rc.SOLO: old}, gw: {rc.SOLO: old}}
    assert rc.plan(str(clone), PRS, date(2026, 11, 14), marks=marks)["action"] == "none"            # day 13
    p = rc.plan(str(clone), PRS, date(2026, 11, 15), marks=marks)                                    # day 14
    assert p["action"] == "cut" and "renovate/gateway" in p["branches"] and p["failed_alone"] == []
    assert rc.plan(str(clone), PRS, date(2026, 11, 15), marks=marks, mark_days=30)["action"] == "none"


def test_main_goes_first_whenever_it_has_untested_commits_even_unmarked(repo):
    """A commit lands on main after the failed candidate: the mark was on the old
    head. A suspect is still never tested on top of commits no cycle has proven."""
    clone, origin = repo
    commit(origin, "later.txt", "x\n", "fix: landed after the failure")
    g(clone, "fetch", "-q", "origin")
    marks = {sha(clone, "origin/renovate/gateway"): {rc.COMBINED: "failure"}}
    p = rc.plan(str(clone), PRS, date(2026, 11, 2), marks=marks)
    assert p["action"] == "cut" and p["kind"] == "product" and p["branches"] == []


def test_a_group_that_conflicts_with_main_waits_for_its_rebase(repo):
    clone, origin = repo
    commit(origin, "gateway.txt", "main changed the same file\n", "fix: touches what the group touches")
    g(clone, "fetch", "-q", "origin")
    p = rc.plan(str(clone), PRS, TODAY)
    assert p["conflicts"] == ["renovate/gateway"] and "renovate/gateway" not in p["branches"]
    assert p["action"] == "cut" and "merge conflict" in rc.pr_body(p, "# notes\n")
    marks = {sha(clone, "origin/renovate/gateway"): {rc.COMBINED: "failure"}}      # a suspect that conflicts
    p = rc.plan(str(clone), [PRS[0]], date(2026, 11, 2), marks=marks)              # is not picked again and again
    assert p["conflicts"] == ["renovate/gateway"] and p["branches"] == [] and p["kind"] is None
    assert p["action"] == "none"                     # no recovery cycle for it: the week's rhythm applies


# --------------------------------------------------------------------------- #
# A cycle in flight
# --------------------------------------------------------------------------- #
def _cut(clone, origin, groups=("renovate/gateway",)):
    """release/v2026.11.2 = main + --no-ff merges of the groups + its manifest. Returns its head."""
    g(origin, "switch", "-q", "-c", "release/v2026.11.2", "main")
    for group in groups:
        g(origin, "merge", "-q", "--no-ff", "-m", f"Merge remote-tracking branch 'origin/{group}' into release/v2026.11.2",
          group)
    commit(origin, "releases/v2026.11.2.json", '{"previous": "v2026.11.1"}\n', "notes")
    g(origin, "switch", "-q", "main")
    g(clone, "fetch", "-q", "origin", "+refs/heads/release/*:refs/remotes/origin/release/*")
    return sha(clone, "origin/release/v2026.11.2")


def _pr(head, state="OPEN", created="2026-11-09T08:00:00Z", number=70):
    return {"headRefName": "release/v2026.11.2", "headRefOid": head, "state": state, "number": number,
            "createdAt": created}


def test_one_cycle_at_a_time(repo):
    clone, origin = repo
    head = _cut(clone, origin)
    g(origin, "branch", "release/v2026.11.0", "main")  # older than the tag: ignored
    g(clone, "fetch", "-q", "origin", "+refs/heads/release/*:refs/remotes/origin/release/*")
    for force in (False, True):
        p = rc.plan(str(clone), PRS, date(2026, 11, 10), force=force, release_prs=[_pr(head)], statuses=[])
        assert p["action"] == "wait" and not p["release"] and p["version"] == "v2026.11.2" and p["pr"] == 70
    assert rc.plan(str(clone), PRS, date(2026, 11, 10))["action"] == "wait"            # PR state unknown
    assert rc.plan(str(clone), PRS, date(2026, 11, 10), release_prs=[_pr(head)])["action"] == "wait"  # E2E unknown


def test_a_candidate_that_can_no_longer_be_merged_is_cut_again(repo):
    """main moved since the cut: finish would refuse for ever (the merge would be
    a tree E2E never saw), also when E2E is green."""
    clone, origin = repo
    head = _cut(clone, origin)
    commit(origin, "hotfix.txt", "h\n", "fix: merged while E2E was running")
    g(clone, "fetch", "-q", "origin")
    for statuses in (e2e(), [], None):
        p = rc.plan(str(clone), PRS, date(2026, 11, 10), release_prs=[_pr(head)], statuses=statuses)
        assert p["action"] == "abandon" and p["why"] == "superseded" and p["marks"] == [] and not p["attention"]
    assert "can no longer be merged as tested" in rc.abandon_comment(p)


def test_a_green_candidate_that_stays_unmerged_is_abandoned(repo):
    clone, origin = repo
    head = _cut(clone, origin)
    args = dict(release_prs=[_pr(head)], statuses=e2e(at="2026-11-10T10:00:00Z"))
    assert rc.plan(str(clone), PRS, date(2026, 11, 14), **args)["action"] == "finish"
    p = rc.plan(str(clone), PRS, date(2026, 11, 15), **args)
    assert p["action"] == "abandon" and p["why"] == "stuck" and p["attention"] and p["marks"] == []


def test_a_release_that_is_resumed_stays_a_release(repo):
    clone, origin = repo
    _cut(clone, origin)
    prs = [dict(pr) for pr in PRS]
    assert not rc.plan(str(clone), prs, date(2026, 11, 10), release_prs=[])["publish"]
    assert rc.plan(str(clone), prs, date(2026, 11, 10), release_prs=[], publish=True)["publish"]
    assert rc.plan(str(clone), prs, date(2027, 2, 1), release_prs=[])["publish"]             # the tag is 90 days old
    prs[0] = {**prs[0], "labels": [{"name": "security"}]}                                     # renovate/gateway is in it
    assert rc.plan(str(clone), prs, date(2026, 11, 10), release_prs=[])["publish"]


def test_a_tagged_release_whose_branch_is_still_there_is_handed_to_finish(repo):
    clone, origin = repo
    assert not rc.plan(str(clone), PRS, TODAY)["leftover"]
    g(origin, "branch", "release/v2026.11.1", "main")
    g(clone, "fetch", "-q", "origin", "+refs/heads/release/*:refs/remotes/origin/release/*")
    p = rc.plan(str(clone), PRS, TODAY)
    assert p["leftover"] and p["action"] == "cut"            # and it is never in the way of a cycle


def test_cut_candidate_without_a_pr_is_resumed(repo):
    clone, origin = repo
    head = _cut(clone, origin)
    earlier = [_pr("0" * 40, state="MERGED", number=60),          # an earlier cycle with the same version name
               {"headRefName": "release/v2026.11.1", "state": "MERGED", "number": 50}]
    p = rc.plan(str(clone), PRS, date(2026, 11, 10), release_prs=earlier)
    assert p["action"] == "cut" and p["release"] and p["resume"] and p["version"] == "v2026.11.2"
    assert p["base"] == "v2026.11.1" and p["branches"] == ["renovate/gateway"]
    assert "Groups: `renovate/gateway`" in rc.pr_body(p, "# notes\n")
    assert head != "0" * 40


def test_green_e2e_finishes_and_only_the_release_s_own_run_counts(repo):
    clone, origin = repo
    head = _cut(clone, origin)
    args = dict(release_prs=[_pr(head)])
    assert rc.plan(str(clone), PRS, date(2026, 11, 10), statuses=e2e(), **args)["action"] == "finish"
    other = rc.plan(str(clone), PRS, date(2026, 11, 10), statuses=e2e(base="main"), **args)
    assert other["action"] == "wait"                                # green, but upgraded from something else
    assert rc.plan(str(clone), PRS, date(2026, 11, 10), release_prs=[_pr(head, state="MERGED")])["action"] == "finish"


def test_first_failure_is_retried_once_in_one_run(repo):
    clone, origin = repo
    head = _cut(clone, origin)
    failed = e2e({"e2e/upgrade-default": "failure"})
    p = rc.plan(str(clone), PRS, date(2026, 11, 10), release_prs=[_pr(head)], statuses=failed)
    assert p["action"] == "retry" and p["retry"] == {"modes": "upgrade", "profiles": "default"}
    assert p["base"] == "v2026.11.1" and p["marks"] == []
    assert rc.retry_inputs(["e2e/fresh-managed", "e2e/upgrade-default"]) == {
        "modes": "fresh,upgrade", "profiles": "default,managed"}
    # Other jobs of the same run are still going: wait for the run to end.
    running = e2e({"e2e/upgrade-default": "failure", "e2e/fresh-managed": "pending"})
    p = rc.plan(str(clone), PRS, date(2026, 11, 10), release_prs=[_pr(head)], statuses=running)
    assert p["action"] == "wait" and "still running" in p["reason"]
    # The retry is dispatched and waits for approval: do not dispatch it again.
    waiting = [st(rc.RETRY, "success", "2026-11-10T12:00:00Z")] + failed
    p = rc.plan(str(clone), PRS, date(2026, 11, 11), release_prs=[_pr(head)], statuses=waiting)
    assert p["action"] == "wait" and "retry dispatched" in p["reason"]
    # It ran and passed: the newest status per context wins.
    passed = [st("e2e/upgrade-default", "success", "2026-11-10T15:00:00Z",
                 release.status_description("upgrade", "default", "passed", base="v2026.11.1", llmd="true"))] + waiting
    assert rc.plan(str(clone), PRS, date(2026, 11, 11), release_prs=[_pr(head)], statuses=passed)["action"] == "finish"


def test_second_failure_abandons_and_blames_the_only_suspect(repo):
    clone, origin = repo
    head = _cut(clone, origin)
    again = [st("e2e/upgrade-default", "failure", "2026-11-10T15:00:00Z"),
             st(rc.RETRY, "success", "2026-11-10T12:00:00Z")] + e2e({"e2e/upgrade-default": "failure"})
    p = rc.plan(str(clone), PRS, date(2026, 11, 11), release_prs=[_pr(head)], statuses=again)
    assert p["action"] == "abandon" and p["why"] == "failed" and p["attention"] and p["pr"] == 70
    assert p["marks"] == [{"sha": sha(clone, "origin/renovate/gateway"), "context": rc.SOLO, "what": "renovate/gateway"}]
    text = rc.abandon_comment(p)
    assert "E2E failed twice" in text and "`renovate/gateway` was the only suspect" in text


def test_several_suspects_are_marked_for_a_test_alone(repo):
    clone, origin = repo
    commit(origin, "fix.txt", "f\n", "fix: never tested")
    g(clone, "fetch", "-q", "origin")
    base = sha(clone, "origin/main")
    head = _cut(clone, origin, groups=("renovate/gateway", "renovate/major-gateway"))
    marks = rc.failure_marks(str(clone), "release/v2026.11.2", "v2026.11.1")
    assert marks == [
        {"sha": sha(clone, "origin/renovate/gateway"), "context": rc.COMBINED, "what": "renovate/gateway"},
        {"sha": sha(clone, "origin/renovate/major-gateway"), "context": rc.COMBINED, "what": "renovate/major-gateway"},
        {"sha": base, "context": rc.COMBINED, "what": "main"}]
    p = {"why": "failed", "marks": marks}
    assert "one at a time" in rc.abandon_comment(p) and "`main`" in rc.abandon_comment(p)
    assert head


def test_a_failed_candidate_without_groups_blames_main(repo):
    clone, origin = repo
    commit(origin, "fix.txt", "f\n", "fix: never tested")
    g(clone, "fetch", "-q", "origin")
    base = sha(clone, "origin/main")
    _cut(clone, origin, groups=())
    marks = rc.failure_marks(str(clone), "release/v2026.11.2", "v2026.11.1")
    assert marks == [{"sha": base, "context": rc.SOLO, "what": "main"}]
    assert "cycles stop until `main` moves" in rc.abandon_comment({"why": "failed", "marks": marks})


def test_an_unapproved_candidate_is_abandoned_after_some_days_and_cut_again(repo):
    clone, origin = repo
    head = _cut(clone, origin)
    args = dict(release_prs=[_pr(head, created="2026-11-09T08:00:00Z")], statuses=[])
    assert rc.plan(str(clone), PRS, date(2026, 11, 13), **args)["action"] == "wait"           # day 4
    p = rc.plan(str(clone), PRS, date(2026, 11, 14), **args)                                    # day 5
    assert p["action"] == "abandon" and p["why"] == "stale" and p["marks"] == [] and p["attention"]
    assert "Nothing is marked" in rc.abandon_comment(p)
    assert rc.plan(str(clone), PRS, date(2026, 11, 14), wait_days=10, **args)["action"] == "wait"
    # A retry that nobody approves is abandoned the same way, counted from the retry.
    stale_retry = [st(rc.RETRY, "success", "2026-11-12T12:00:00Z")] + e2e({"e2e/fresh-default": "failure"})
    args = dict(release_prs=[_pr(head)], statuses=stale_retry)
    assert rc.plan(str(clone), PRS, date(2026, 11, 16), **args)["action"] == "wait"
    p = rc.plan(str(clone), PRS, date(2026, 11, 17), **args)
    assert p["action"] == "abandon" and p["why"] == "stale" and p["marks"] == []


def test_a_closed_pr_means_delete_the_branch(repo):
    clone, origin = repo
    head = _cut(clone, origin)
    p = rc.plan(str(clone), PRS, date(2026, 11, 10), release_prs=[_pr(head, state="CLOSED")])
    assert p["action"] == "abandon" and p["why"] == "closed" and p["marks"] == [] and not p["attention"]


def test_pr_body_says_what_kind_of_cycle_and_what_was_left_out(repo):
    clone, _ = repo
    p = rc.plan(str(clone), PRS, TODAY)
    body = rc.pr_body({**p, "conflicts": ["renovate/infra"]}, "# Release v2026.11.2\n")
    assert body.startswith("Integration cycle: `v2026.11.2`") and "nothing is tagged or published" in body
    assert "Add the label `publish`" in body
    assert "`renovate/stale`" in body and "`renovate/held`" in body and "# Release v2026.11.2" in body
    assert "merge conflict" in body and "`renovate/infra`" in body
    body = rc.pr_body({**p, "publish": True, "kind": "solo"}, "# notes\n")
    assert body.startswith("Release: `v2026.11.2`") and "**A release.**" in body and "**A recovery cycle**" in body
    assert "Remove the label `publish`" in body


CHANGES = {"rows": [["ci/ruff", "0.17.5", "0.17.6"], ["image/ghcr.io/open-webui/open-webui", "v0.13.4", "v0.13.5"],
                    ["vllm", "v0.33.0", "v0.34.1"], ["chart/langfuse", "2.2.5", "3.0.0"],
                    ["image/postgres", "16.16-alpine", "16.17-alpine"]], "majors": ["chart/langfuse"]}


def test_the_candidate_pr_starts_with_what_approving_it_means(repo):
    """The approver decides from this: what changes on top of main, how risky,
    and what happens if it waits."""
    clone, _ = repo
    prs = [dict(pr) for pr in PRS]
    prs[0] = {**prs[0], "labels": [{"name": "security"}]}
    p = rc.plan(str(clone), prs, TODAY)
    assert p["security"] == ["renovate/gateway"]
    body = rc.pr_body(p, "# Release v2026.11.2\n", CHANGES, wait_days=5)
    top = body[:body.index("Groups:")]
    assert "### What you are approving" in top and "**A release.**" in top            # a security update publishes
    assert "**5 component versions change** on top of `main`" in top
    rows = [ln.strip() for ln in top.splitlines() if ln.strip().startswith("| ") and "| Component" not in ln]
    assert rows[0] == "| vllm | `v0.33.0` | `v0.34.1` | anchor |"                       # what matters first
    assert rows[1] == "| chart/langfuse | `2.2.5` | `3.0.0` | major |"
    assert rows[-1] == "| ci/ruff | `0.17.5` | `0.17.6` | CI only |"
    assert "**Includes a security update** (`renovate/gateway`): do not let it wait." in top
    assert "after 5 days this candidate is replaced by a fresh one" in top and "roughly $5-10" in top
    plain = rc.pr_body({**p, "publish": False, "security": []}, "# notes\n", {"rows": [], "majors": []})
    assert "**An integration cycle.**" in plain and "**No component version changes**" in plain
    assert "security update" not in plain


def test_the_headline_names_the_important_updates_and_fits_a_run_name():
    p = {"action": "cut", "version": "v2026.11.8", "publish": False, "product_commits": ["abc fix: x"]}
    line = rc.headline(p, CHANGES)
    assert line == "cycle v2026.11.8: 5 updates: vllm v0.34.1, langfuse 3.0.0, open-webui v0.13.5, +2 more"
    assert len(line) <= 100
    assert rc.headline({**p, "publish": True}, {"rows": [["vllm", "a", "b"]], "majors": []}) == \
        "release v2026.11.8: 1 update: vllm b"
    assert rc.headline(p, {"rows": [], "majors": []}) == "cycle v2026.11.8: no version changes, 1 commit(s) on main"
    assert rc.headline({"action": "retry", "version": "v2026.11.8"}, None) == "candidate v2026.11.8: no version changes"
    many = {"rows": [[f"image/some/very-long-component-name-{i}", "1", "2.0.0-with-a-long-suffix"] for i in range(30)],
            "majors": []}
    assert len(rc.headline(p, many)) <= 100 and rc.headline(p, many).endswith("more")


def test_headline_and_pr_body_cli(repo, tmp_path, capsys):
    import json
    clone, _ = repo
    (tmp_path / "plan.json").write_text(json.dumps(rc.plan(str(clone), PRS, TODAY)))
    (tmp_path / "changes.json").write_text(json.dumps(CHANGES))
    (tmp_path / "notes.md").write_text("# Release v2026.11.2\n")
    base = ["--plan", str(tmp_path / "plan.json"), "--changes", str(tmp_path / "changes.json")]
    assert rc.main(["headline", *base, "--prefix", "retry of "]) == 0
    assert capsys.readouterr().out.strip().startswith("retry of cycle v2026.11.2: 5 updates: vllm v0.34.1")
    assert rc.main(["pr-body", *base, "--notes", str(tmp_path / "notes.md"), "--wait-days", "9"]) == 0
    out = capsys.readouterr().out
    assert "### What you are approving" in out and "after 9 days" in out and "# Release v2026.11.2" in out


def test_plan_cli_reads_the_marks_and_statuses_through_gh(repo, tmp_path, monkeypatch, capsys):
    import json
    clone, origin = repo
    prs = [{**pr, "headRefOid": sha(clone, f"origin/{pr['headRefName']}")} for pr in PRS
           if pr["headRefName"].startswith("renovate/")]
    (tmp_path / "prs.json").write_text(json.dumps(prs))
    (tmp_path / "rprs.json").write_text(json.dumps([{"headRefName": "feature/y", "state": "OPEN"}]))
    asked = []

    def fake(slug, commit_sha):
        asked.append(commit_sha)
        if commit_sha == sha(clone, "origin/renovate/gateway"):
            return [st(rc.SOLO, "failure", "2026-11-08T10:00:00Z"), st("ci", "success", "2026-11-08T09:00:00Z")]
        return []
    monkeypatch.setattr(rc, "fetch_statuses", fake)
    args = ["plan", "--repo", str(clone), "--prs", str(tmp_path / "prs.json"), "--release-prs",
            str(tmp_path / "rprs.json"), "--github", "o/r", "--today", "2026-11-09"]
    assert rc.main(args) == 0
    p = json.loads(capsys.readouterr().out)
    assert p["action"] == "cut" and p["failed_alone"] == ["renovate/gateway"]
    assert sha(clone, "origin/main") in asked
    head = _cut(clone, origin)
    monkeypatch.setattr(rc, "fetch_statuses", lambda slug, s: e2e() if s == head else [])
    (tmp_path / "rprs.json").write_text(json.dumps([_pr(head)]))
    assert rc.main(args) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "finish"


def test_finishable_is_the_release_in_flight_or_one_tagged_but_not_cleaned_up(repo, capsys):
    """What release-finish.yaml looks at after an E2E run."""
    c, o = repo
    assert rc.finishable(str(c)) is None                       # only v2026.11.1 exists, no release branch
    g(o, "branch", "release/v2026.11.0", "main")               # an old leftover: older than the newest tag
    g(o, "branch", "release/v2026.11.1", "main")               # tagged, finish died before deleting it
    g(c, "fetch", "-q", "origin", "+refs/heads/release/*:refs/remotes/origin/release/*")
    assert rc.finishable(str(c)) == "v2026.11.1"
    g(o, "branch", "release/v2026.11.2", "main")               # cut, E2E running or done
    g(c, "fetch", "-q", "origin", "+refs/heads/release/*:refs/remotes/origin/release/*")
    assert rc.finishable(str(c)) == "v2026.11.2"
    assert rc.main(["finishable", "--repo", str(c)]) == 0 and capsys.readouterr().out.strip() == "v2026.11.2"


def test_newest_tag_cli_can_ignore_releases_before_the_first_public_one(repo, capsys):
    clone, _ = repo
    for since, expected in ((None, "v2026.11.1"), ("v2026.11.1", "v2026.11.1"), ("v2026.11.0", "v2026.11.1"),
                            ("v2027.01.0", ""), ("nonsense", "")):
        args = ["newest-tag", "--repo", str(clone)] + (["--since", since] if since else [])
        assert rc.main(args) == 0 and capsys.readouterr().out.strip() == expected, since


class TestWorkflow:
    def _wf(self):
        with open(os.path.join(ROOT, ".github/workflows/release-candidate.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        return yaml.safe_load(text), text

    def _steps(self):
        wf, _ = self._wf()
        return {s.get("name", ""): s for s in wf["jobs"]["candidate"]["steps"]}

    def test_runs_every_day_off_the_hour_and_by_hand(self):
        wf, _ = self._wf()
        on = wf[True] if True in wf else wf["on"]
        # Off the hour (GitHub drops scheduled runs at the start of an hour under load), with a
        # catch-up run; the planner makes the extra runs no-ops while a cycle is in flight.
        assert on["schedule"] == [{"cron": "41 7,13 * * *"}]
        assert set(on["workflow_dispatch"]["inputs"]) == {"force", "publish", "dry_run"}
        assert all(i["type"] == "boolean" for i in on["workflow_dispatch"]["inputs"].values())
        assert wf["concurrency"] == {"group": "release-candidate", "cancel-in-progress": False}

    def test_every_action_of_the_planner_has_exactly_one_step(self):
        steps = self._steps()
        by_action = {}
        for name, s in steps.items():
            for action in ("cut", "retry", "finish", "abandon"):
                if f"steps.plan.outputs.action == '{action}'" in (s.get("if") or ""):
                    by_action.setdefault(action, []).append(name)
        assert sorted(by_action) == ["abandon", "cut", "finish", "retry"]
        assert len(by_action["cut"]) == 3 and all(len(by_action[a]) == 1 for a in ("retry", "finish", "abandon"))
        for s in steps.values():                       # nothing acts without the planner saying so
            if s.get("name", "").startswith(("Cut", "Open the release PR", "Dispatch", "Retry", "Start", "Abandon")):
                assert "steps.plan.outputs.action ==" in s["if"], s["name"]

    def test_dry_run_only_plans(self):
        plan = self._steps()["Plan"]["run"]
        assert 'if [ "$DRY_RUN" = true ] && [ "$action" != none ] && [ "$action" != wait ]; then' in plan
        assert "action=none" in plan
        assert "(!inputs.dry_run)" in self._steps()["Open or update the issue when something needs a person"]["if"]

    def test_plan_reads_github_state_and_the_settings(self):
        wf, text = self._wf()
        plan = self._steps()["Plan"]["run"]
        assert '--github "$REPO"' in plan and "--release-prs all-prs.json" in plan and "--state all" in plan
        assert "headRefName,labels,headRefOid" in plan and "headRefName,headRefOid,state,number,createdAt" in plan
        env = wf["jobs"]["candidate"]["env"]
        assert env["CYCLE_DAYS"] == "${{ vars.CYCLE_DAYS || '7' }}"
        assert env["PUBLISH_DAYS"] == "${{ vars.PUBLISH_DAYS || '90' }}" and env["WAIT_DAYS"] == "${{ vars.WAIT_DAYS || '5' }}"
        assert env["MARK_DAYS"] == "${{ vars.MARK_DAYS || '14' }}" and '--mark-days "$MARK_DAYS"' in plan

    def test_cut_skips_conflicts_resumes_and_labels_a_release(self):
        steps = self._steps()
        assert "steps.plan.outputs.resume != 'true'" in steps["Cut, notes, push"]["if"]
        cut = steps["Cut, notes, push"]["run"]
        assert "--no-push --skip-conflicts" in cut and ".conflicts += $c | .branches -= $c" in cut
        # Every group conflicted and main has nothing new: no candidate, no PR, no E2E.
        assert steps["Cut, notes, push"]["id"] == "cut" and 'echo "empty=true" >> "$GITHUB_OUTPUT"' in cut
        assert cut.index("diff --quiet origin/main") < cut.index("release.py --repo subject notes")
        for name in ("Open the release PR", "Dispatch E2E (fresh + upgrade, both profiles; waits for approval)"):
            assert "steps.cut.outputs.empty != 'true'" in steps[name]["if"], name
        pr = steps["Open the release PR"]["run"]
        assert pr.count("gh pr create") == 1 and 'GH_TOKEN="$PUSH_TOKEN" gh pr create' in pr
        assert "--label publish" in pr and '"cycle: $VERSION"' in pr and '"release: $VERSION"' in pr
        # The approver sees what the candidate changes on top of main: in the PR and in the E2E run's name.
        assert "tools/release.py --repo subject changes origin/main > changes.json" in cut
        assert "--changes changes.json" in pr and '--wait-days "$WAIT_DAYS"' in pr
        assert "changes origin/main FETCH_HEAD > changes.json" in pr                 # a resumed cut
        dispatch = steps["Dispatch E2E (fresh + upgrade, both profiles; waits for approval)"]["run"]
        assert "headline --plan plan.json --changes changes.json" in dispatch and '-f note="$note"' in dispatch
        for name in ("Retry the failed E2E jobs once",
                     "Dispatch E2E if the candidate has none (an earlier dispatch failed)"):
            assert '-f note="$note"' in steps[name]["run"], name
        assert "--prefix 'retry of '" in steps["Retry the failed E2E jobs once"]["run"]

    def test_retry_marks_the_head_so_it_is_dispatched_once(self):
        retry = self._steps()["Retry the failed E2E jobs once"]["run"]
        assert 'gh workflow run e2e.yaml --repo "$REPO" --ref lifecycle -f modes="$modes"' in retry
        assert '-f base="$BASE"' in retry                              # same base: the statuses must still count
        assert "-f state=success -f context=candidate/retry" in retry and rc.RETRY == "candidate/retry"
        assert retry.index("context=candidate/retry") < retry.index("gh workflow run e2e.yaml")   # mark, then dispatch

    def test_a_waiting_candidate_without_any_e2e_run_gets_one(self):
        step = self._steps()["Dispatch E2E if the candidate has none (an earlier dispatch failed)"]
        assert step["if"] == "steps.plan.outputs.action == 'wait' && steps.plan.outputs.pr_created != ''"
        assert 'select(.createdAt >= \\"${PR_CREATED}\\")' in step["run"] and 'if [ "$runs" = 0 ]; then' in step["run"]
        assert "-f modes=fresh,upgrade" in step["run"] and "-f profiles=default,managed" in step["run"]

    def test_a_tagged_leftover_is_handed_to_release_finish(self):
        assert self._steps()["Start release-finish"]["if"] == (
            "steps.plan.outputs.action == 'finish' || steps.plan.outputs.leftover == 'true'")

    def test_abandon_closes_cancels_deletes_and_marks(self):
        abandon = self._steps()["Abandon the candidate"]["run"]
        for needle in ("tools/release_candidate.py abandon-comment --plan plan.json", "gh pr close",
                       "--status waiting", "gh run cancel", 'push --quiet origin --delete "release/$VERSION"',
                       "jq -c '.marks[]' plan.json", "-f state=failure"):
            assert needle in abandon, needle
        assert "--status in_progress" not in abandon                  # a running E2E tears itself down
        # Marks first: once the PR is closed or the branch gone, a rerun no longer knows the suspects.
        assert abandon.index("-f state=failure") < abandon.index("gh pr close") < abandon.index("origin --delete")
        assert "| while read" not in abandon                          # a failing gh in a pipe would go unnoticed

    def test_permissions_and_no_inputs_in_run_scripts(self):
        wf, _ = self._wf()
        assert wf["permissions"] == {"contents": "read"}
        job = wf["jobs"]["candidate"]
        assert job["permissions"] == {"contents": "read", "pull-requests": "write", "issues": "write",
                                      "actions": "write", "statuses": "write"}
        for s in job["steps"]:
            assert "inputs." not in (s.get("run") or "") and "${{" not in (s.get("run") or ""), s.get("name")

    def test_uses_the_release_tools_and_dispatches_both_modes(self):
        _, text = self._wf()
        for needle in ("tools/release_candidate.py plan", "tools/release.py --repo subject cut",
                       "tools/release.py --repo subject notes", "gh pr create",
                       "gh workflow run e2e.yaml", "-f modes=fresh,upgrade", "-f profiles=default,managed",
                       "gh workflow run release-finish.yaml"):
            assert needle in text, needle
        assert "gh pr merge" not in text and "git tag" not in text     # release.py finish owns those
