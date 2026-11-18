#!/usr/bin/env python3
"""Decide what the release factory does next.

Run every day by .github/workflows/release-candidate.yaml. Only the decision is
here (pure, testable); the workflow carries it out with tools/release.py and gh.

The factory works in cycles. A cycle cuts a candidate, release/<version> =
main + the Renovate group branches + the notes and manifest of the next
release, and runs E2E on it: a fresh install, and an upgrade from the last
PUBLISHED release (the newest tag). When E2E is green the candidate is merged
into main. Most cycles stop there (an integration: main moved forward, proven);
a cycle that carries the label `publish` is also tagged and published. So main
always holds a state that was tested as one upgrade from the last published
release, and publishing is only a tag.

Nobody watches this, so every state has a way forward. plan() returns one
action:

  cut      start a cycle (or `resume`: a branch that was cut and never got a PR)
  wait     a cycle is in flight and its E2E is not decided yet
  finish   its E2E is green (or its PR is merged): start release-finish
  retry    E2E failed for the first time: run the failed jobs once more
  abandon  E2E failed again; or nobody approved it for `wait_days`; or main
           moved since the cut (the candidate can no longer be merged as
           tested); or it is green and still not merged after `wait_days`; or
           the PR was closed: mark the suspects, close the PR, delete the branch
  none     nothing to do (and why)

Rules
  * One cycle at a time: while a release/<v> newer than the newest tag exists,
    nothing is cut.
  * A new cycle starts at most every `cycle_days` (--force ignores this). A
    recovery cycle (below) does not wait.
  * Groups: every open Renovate PR (branch renovate/<group>) goes in, except:
    label `hold`; a branch that does not contain the newest tag (Renovate has
    not rebased it); a branch with no change against main; a branch whose head
    failed alone (it comes back when Renovate pushes a newer version); and all
    but one of the "anchor" updates (vLLM, the EKS Kubernetes version): at most
    one of those per cycle, so a release carries one big change at a time.
    Majors and other ticked updates exist as PRs only after a tick on the
    Dependency Dashboard, so an open PR is the approval.
  * Recovery. When a candidate with several suspects fails twice, each group in
    it (and main, if it had commits that no cycle had tested) is marked
    `candidate/combined=failure` on its head commit. The next cycles then test
    one suspect at a time, main first: a suspect that passes alone is merged,
    one that fails alone is marked `candidate/solo=failure` and left out until
    its head moves. If main itself fails alone, cycles stop until main moves.
    The marks are commit statuses, so the state survives between runs and
    disappears by itself when a branch gets a new commit. A mark also expires
    after `mark_days`: two failures may have been an outage, not the update,
    so nothing is left out (and cycles never stop) for good.
  * Publish: a cycle is a release when --publish is given, when the newest tag
    is `publish_days` old, or when it includes a PR labelled `security`.
  * Version: vYYYY.MM.N, the year.month of today or of the newest tag,
    whichever is later; N counts up within that month. Until a cycle is
    published, the following cycles reuse the name (the notes and manifest of an
    unpublished version are regenerated, not kept).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timezone

import release

TAG_RX = re.compile(r"^v(\d{4})\.(\d{2})\.(\d+)$")
RELEASE_MERGE_RX = re.compile(r"^Merge pull request #\d+ from [^/\s]+/release/")
HOLD_LABEL = "hold"
SECURITY_LABEL = "security"
PUBLISH_LABEL = release.PUBLISH_LABEL
SOLO = "candidate/solo"            # commit status: this commit failed E2E as the only suspect
COMBINED = "candidate/combined"    # commit status: this commit was in a candidate that failed with other suspects
RETRY = "candidate/retry"          # commit status on a release head: the failed E2E jobs were dispatched again
ANCHOR_MARKS = ("renovate/vllm", "eks-kubernetes")   # branch name parts of the anchor updates


def git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check)


def newest_tag(repo: str, ref: str) -> str | None:
    tags = git(repo, "tag", "--merged", ref, "--list", "v*").stdout.split()
    parsed = [(tuple(int(x) for x in TAG_RX.match(t).groups()), t) for t in tags if TAG_RX.match(t)]
    return max(parsed)[1] if parsed else None


def _key(tag: str) -> tuple[int, ...]:
    return tuple(int(x) for x in TAG_RX.match(tag).groups())


def _release_branches(repo: str, remote: str) -> list[str]:
    refs = git(repo, "for-each-ref", "--format=%(refname:lstrip=3)", f"refs/remotes/{remote}/release/").stdout.split()
    return [r for r in refs if r.startswith("release/") and TAG_RX.match(r[8:])]


def release_in_flight(repo: str, tag: str, remote: str = "origin") -> str | None:
    """A remote release/<v> branch newer than the newest tag (a cycle that is not finished)."""
    newer = [r for r in _release_branches(repo, remote) if _key(r[8:]) > _key(tag)]
    return max(newer, key=lambda r: _key(r[8:])) if newer else None


def finishable(repo: str, ref: str = "origin/main", remote: str = "origin") -> str | None:
    """The release `release.py finish` should look at: the newest remote
    release/<v> branch that is not older than the newest tag. Newer = cut, E2E
    running or done; equal = tagged, but the finish stopped before it deleted
    the branch (it resumes: GitHub Release, branch). None when there is none."""
    tag = newest_tag(repo, ref)
    live = [r[8:] for r in _release_branches(repo, remote) if tag is None or _key(r[8:]) >= _key(tag)]
    return max(live, key=_key) if live else None


def tag_date(repo: str, tag: str) -> date:
    ts = git(repo, "for-each-ref", "--format=%(creatordate:unix)", f"refs/tags/{tag}").stdout.strip()
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()


def merged_group_heads(repo: str, branch: str, remote: str = "origin") -> dict[str, str]:
    """{renovate/<group>: the commit of it that was merged} for an already-cut release branch."""
    log = git(repo, "log", "--merges", "--format=%H %s", f"{remote}/main..{remote}/{branch}").stdout
    rx = re.compile(rf"Merge (?:remote-tracking )?branch '(?:{re.escape(remote)}/)?(renovate/[^']+)'")
    heads: dict[str, str] = {}
    for line in log.splitlines():
        sha, _, subject = line.partition(" ")
        m = rx.search(subject)
        if m and m.group(1) not in heads:
            heads[m.group(1)] = git(repo, "rev-parse", f"{sha}^2").stdout.strip()
    return heads


def merged_groups(repo: str, branch: str, remote: str = "origin") -> list[str]:
    return sorted(merged_group_heads(repo, branch, remote))


def manifest_previous(repo: str, branch: str, version: str, remote: str = "origin") -> str | None:
    r = git(repo, "show", f"{remote}/{branch}:releases/{version}.json", check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("previous")
    except ValueError:
        return None


def next_version(latest: str | None, today: date) -> str:
    ym = (today.year, today.month)
    if latest:
        y, m, n = (int(x) for x in TAG_RX.match(latest).groups())
        if (y, m) >= ym:
            return f"v{y:04d}.{m:02d}.{n + 1}"
    return f"v{ym[0]:04d}.{ym[1]:02d}.0"


def untested_commits(repo: str, tag: str, ref: str) -> list[str]:
    """Commits on `ref` (first parent) that no cycle has tested: everything after
    the newest merge of a release branch, or after the tag when there is none."""
    out = []
    for line in git(repo, "log", "--first-parent", "--format=%h %s", f"{tag}..{ref}").stdout.splitlines():
        if RELEASE_MERGE_RX.match(line.partition(" ")[2]):
            break
        if line.strip():
            out.append(line)
    return out


def last_cycle_date(repo: str, tag: str, ref: str) -> date:
    """UTC date of the newest merged cycle on `ref`, or of the tag when none was merged since."""
    for line in git(repo, "log", "--first-parent", "--format=%ct %s", f"{tag}..{ref}").stdout.splitlines():
        ts, _, subject = line.partition(" ")
        if RELEASE_MERGE_RX.match(subject):
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
    return tag_date(repo, tag)


def _labels(pr: dict) -> set[str]:
    return {(lbl.get("name") if isinstance(lbl, dict) else lbl) for lbl in pr.get("labels") or []}


def _day(stamp: str | None) -> date | None:
    """Date of an ISO timestamp as GitHub writes them (2026-11-17T19:04:49Z)."""
    if not stamp:
        return None
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).date()


# --------------------------------------------------------------------------- #
# E2E state of the candidate in flight
# --------------------------------------------------------------------------- #

def e2e_state(statuses: list[dict], previous: str) -> dict:
    """What the commit statuses of a release head say. `statuses` is newest
    first, each {context, state, description, created_at}.

      green              all four e2e/* are success and are the release's own run
      failed             e2e contexts whose newest status is failure/error
      retried            a retry was dispatched (candidate/retry exists)
      failed_after_retry a context failed after the retry was dispatched
      pending            e2e contexts whose newest status is pending (jobs running)
      last_event         date of the newest retry mark (None without one)
      decided_on         date of the newest e2e/* status (None without one)
    """
    latest: dict[str, dict] = {}
    for st in statuses:
        latest.setdefault(st.get("context", ""), st)
    retry = latest.get(RETRY)
    details = {c: latest[c] for c in release.E2E_CONTEXTS if c in latest}
    try:
        release.require_all_green(details, previous)
        green = True
    except release.ReleaseError:
        green = False
    failed = sorted(c for c, st in details.items() if st.get("state") in ("failure", "error"))
    after = bool(retry) and any((details[c].get("created_at") or "") > (retry.get("created_at") or "") for c in failed)
    pending = sorted(c for c, st in details.items() if st.get("state") == "pending")
    newest = max((st.get("created_at") or "" for st in details.values()), default="")
    return {"green": green, "failed": failed, "retried": bool(retry), "failed_after_retry": after,
            "pending": pending, "last_event": _day(retry.get("created_at")) if retry else None,
            "decided_on": _day(newest)}


def retry_inputs(failed: list[str]) -> dict:
    """e2e.yaml inputs that rerun the failed jobs in ONE run (one approval): the
    modes and profiles that failed. With two failures in different modes and
    profiles this also reruns jobs that passed; cheaper than a second approval."""
    pairs = [c.split("/", 1)[1].split("-", 1) for c in failed]
    modes = [m for m in ("fresh", "upgrade") if any(p[0] == m for p in pairs)]
    profiles = [x for x in ("default", "managed") if any(p[1] == x for p in pairs)]
    return {"modes": ",".join(modes), "profiles": ",".join(profiles)}


def failure_marks(repo: str, branch: str, tag: str, remote: str = "origin") -> list[dict]:
    """Commit statuses to post when the candidate `branch` failed E2E twice:
    which commits are suspects. One suspect: it failed alone. Several: each is
    marked as part of a failed combination and is tested alone next."""
    heads = merged_group_heads(repo, branch, remote)
    base = git(repo, "merge-base", f"{remote}/main", f"{remote}/{branch}").stdout.strip()
    main_untested = bool(untested_commits(repo, tag, base))
    if not heads:
        return [{"sha": base, "context": SOLO, "what": "main"}]
    if len(heads) == 1 and not main_untested:
        (group, sha), = heads.items()
        return [{"sha": sha, "context": SOLO, "what": group}]
    marks = [{"sha": sha, "context": COMBINED, "what": group} for group, sha in sorted(heads.items())]
    if main_untested:
        marks.append({"sha": base, "context": COMBINED, "what": "main"})
    return marks


def marked(marks: dict, sha: str, context: str, today: date, mark_days: int) -> bool:
    """`sha` carries a `context` failure mark that has not expired. A mark is
    {state, at} (or a bare state, without an age)."""
    mark = (marks.get(sha) or {}).get(context)
    if isinstance(mark, str):
        mark = {"state": mark, "at": None}
    if not mark or mark.get("state") != "failure":
        return False
    at = _day(mark.get("at"))
    return at is None or (today - at).days < mark_days


def conflicts_with(repo: str, base: str, ref: str) -> bool:
    """`ref` does not merge cleanly into `base` (nothing is checked out)."""
    return git(repo, "merge-tree", "--write-tree", base, ref, check=False).returncode == 1


def is_release(prs: list[dict], branches: list[str], repo: str, tag: str, today: date, publish: bool,
               publish_days: int) -> bool:
    """A cycle is a release when asked for, when the newest tag is `publish_days`
    old, or when one of its groups is a security update."""
    security = any(SECURITY_LABEL in _labels(pr) for pr in prs if pr.get("headRefName") in branches)
    return bool(publish or security or (today - tag_date(repo, tag)).days >= publish_days)


# --------------------------------------------------------------------------- #
# The decision
# --------------------------------------------------------------------------- #

def _blank() -> dict:
    return {"action": "none", "release": False, "resume": False, "version": None, "base": None, "branches": [],
            "kind": None, "publish": False, "stale": [], "held": [], "unchanged": [], "failed_alone": [],
            "deferred": [], "conflicts": [], "product_commits": [], "reason": "", "pr": None, "pr_created": None,
            "why": None, "retry": None, "marks": [], "attention": False, "leftover": False, "security": []}


def plan(repo: str, prs: list[dict], today: date, *, force: bool = False, publish: bool = False,
         cycle_days: int = 7, publish_days: int = 90, wait_days: int = 5, mark_days: int = 14,
         remote: str = "origin", release_prs: list[dict] | None = None, statuses: list[dict] | None = None,
         marks: dict[str, dict] | None = None) -> dict:
    """release_prs: PRs from release/* branches, any state {headRefName, headRefOid, state, number, createdAt}.
    statuses: commit statuses of the release head in flight (newest first), None = unknown.
    marks: {commit sha: {candidate/solo|candidate/combined: {state, at}}} for main's head and the group heads."""
    main = f"{remote}/main"
    out = _blank()
    tag = newest_tag(repo, main)
    if not tag:
        out["reason"] = "no release tag on main; cut the first release by hand"
        return out
    out["base"] = tag
    # A release that was tagged but whose finish stopped before the release page
    # or the branch delete: release-finish resumes it. Never in the way of a cycle.
    out["leftover"] = f"release/{tag}" in _release_branches(repo, remote)
    inflight = release_in_flight(repo, tag, remote)
    if inflight:
        return _plan_in_flight(repo, out, prs, inflight, tag, today, publish, publish_days, wait_days, remote,
                               release_prs, statuses)
    return _plan_cut(repo, out, prs, tag, today, force, publish, cycle_days, publish_days, mark_days, remote,
                     marks or {})


def _plan_in_flight(repo, out, prs, inflight, tag, today, publish, publish_days, wait_days, remote,
                    release_prs, statuses) -> dict:
    version = inflight[len("release/"):]
    out["version"] = version
    previous = manifest_previous(repo, inflight, version, remote) or tag
    if release_prs is None:
        out.update(action="wait", reason=f"{inflight} is in flight; the state of its PR is unknown")
        return out
    # A version name is reused by every cycle until one is published, so only a
    # PR at this branch's head is this candidate's (older ones are other cycles).
    head = git(repo, "rev-parse", f"{remote}/{inflight}").stdout.strip()
    mine = [p for p in release_prs if p.get("headRefName") == inflight and p.get("headRefOid", head) == head]
    if not mine:
        groups = merged_groups(repo, inflight, remote)
        out.update(action="cut", release=True, resume=True, base=previous, branches=groups,
                   publish=is_release(prs, groups, repo, tag, today, publish, publish_days),
                   reason=f"{inflight} was cut but has no PR: open it and dispatch E2E")
        return out
    merged = [p for p in mine if p.get("state") == "MERGED"]
    opened = [p for p in mine if p.get("state", "OPEN") == "OPEN"]
    if merged:
        out.update(action="finish", pr=merged[0].get("number"),
                   reason=f"{inflight} is merged but not finished (tag, release page or branch): finish resumes")
        return out
    if not opened:
        out.update(action="abandon", why="closed", pr=mine[0].get("number"),
                   reason=f"the PR of {inflight} was closed without a merge: delete the branch")
        return out
    pr = opened[0]
    out.update(pr=pr.get("number"), pr_created=pr.get("createdAt"))
    if git(repo, "merge-base", "--is-ancestor", f"{remote}/main", head, check=False).returncode != 0:
        # finish only merges a candidate that contains main: otherwise the merge
        # result is a tree E2E never saw. Waiting cannot fix that.
        out.update(action="abandon", why="superseded",
                   reason=f"main moved since {inflight} was cut, so it can no longer be merged as tested; "
                          "the next run cuts a fresh candidate")
        return out
    if statuses is None:
        out.update(action="wait", reason=f"{inflight} is in flight; its E2E state is unknown")
        return out
    st = e2e_state(statuses, previous)
    if st["green"]:
        if st["decided_on"] and (today - st["decided_on"]).days >= wait_days:
            # release-finish has had days to merge it and could not (it opened the issue).
            out.update(action="abandon", why="stuck", attention=True,
                       reason=f"{inflight} is green since {st['decided_on'].isoformat()} and still not merged; "
                              "see the release-finish runs. The next run cuts a fresh candidate")
            return out
        out.update(action="finish", reason=f"E2E is green on {inflight}")
        return out
    since = st["last_event"] or _day(pr.get("createdAt")) or today
    overdue = (today - since).days >= wait_days
    if st["failed"] and not st["retried"] and not st["pending"]:
        out.update(action="retry", retry=retry_inputs(st["failed"]), base=previous,
                   reason="E2E failed (" + ", ".join(st["failed"]) + "): run the failed jobs once more")
        return out
    if st["failed"] and st["failed_after_retry"]:
        out.update(action="abandon", why="failed", attention=True, marks=failure_marks(repo, inflight, tag, remote),
                   reason="E2E failed again after the retry (" + ", ".join(st["failed"]) + ")")
        return out
    if overdue:
        out.update(action="abandon", why="stale", attention=True,
                   reason=f"E2E of {inflight} was not approved or did not finish within {wait_days} days; "
                          "the next run cuts a fresh candidate")
        return out
    state = "retry dispatched" if st["retried"] else "still running" if st["pending"] else "not finished"
    out.update(action="wait", reason=f"{inflight}: E2E {state} (since {since.isoformat()}; abandoned after "
                                     f"{wait_days} days)")
    return out


def _plan_cut(repo, out, prs, tag, today, force, publish, cycle_days, publish_days, mark_days, remote,
              marks) -> dict:
    main = f"{remote}/main"
    head = git(repo, "rev-parse", main).stdout.strip()
    out["product_commits"] = untested_commits(repo, tag, main)
    if marked(marks, head, SOLO, today, mark_days):
        # Said once, when the candidate was abandoned; not on every run.
        out["reason"] = (f"main ({head[:12]}) failed E2E with nothing else in the candidate; cycles resume when "
                         f"main moves, or {mark_days} days after that failure")
        return out

    eligible: list[tuple[str, str, set[str]]] = []
    for pr in sorted(prs, key=lambda p: p.get("headRefName", "")):
        name = pr.get("headRefName", "")
        if not name.startswith("renovate/"):
            continue
        labels = _labels(pr)
        ref = f"{remote}/{name}"
        if HOLD_LABEL in labels:
            out["held"].append(name)
        elif git(repo, "merge-base", "--is-ancestor", tag, ref, check=False).returncode != 0:
            out["stale"].append(name)
        elif not git(repo, "diff", "--quiet", f"{main}...{ref}", check=False).returncode:
            out["unchanged"].append(name)
        elif conflicts_with(repo, main, ref):
            out["conflicts"].append(name)       # Renovate rebases it; it returns in a later cycle
        else:
            sha = pr.get("headRefOid") or git(repo, "rev-parse", ref).stdout.strip()
            if marked(marks, sha, SOLO, today, mark_days):
                out["failed_alone"].append(name)
            else:
                eligible.append((name, sha, labels))

    suspects = [e for e in eligible if marked(marks, e[1], COMBINED, today, mark_days)]
    if out["product_commits"] and (suspects or marked(marks, head, COMBINED, today, mark_days)):
        # Recovery starts with main alone whenever main has commits no cycle has
        # tested (also ones that landed after the failure): a suspect tested on
        # top of unproven commits could be blamed for them.
        kind, chosen = "product", []
        out["deferred"] = [e[0] for e in eligible]
    elif suspects:
        kind, chosen = "solo", suspects[:1]
        out["deferred"] = [e[0] for e in eligible if e not in chosen]
    else:
        last = last_cycle_date(repo, tag, main)
        if not force and (today - last).days < cycle_days:
            out["reason"] = (f"the last cycle was merged on {last.isoformat()}; the next one starts "
                             f"{cycle_days} days after it")
            return out
        kind, chosen, anchor_taken = "combined", [], False
        for e in eligible:
            if any(mark in e[0] for mark in ANCHOR_MARKS):
                if anchor_taken:
                    out["deferred"].append(e[0])
                    continue
                anchor_taken = True
            chosen.append(e)
    if not chosen and not out["product_commits"]:
        out["reason"] = "nothing to integrate: no eligible group branch and no untested commit on main"
        return out
    branches = [e[0] for e in chosen]
    out.update(action="cut", release=True, kind=kind, branches=branches, version=next_version(tag, today),
               publish=is_release(prs, branches, repo, tag, today, publish, publish_days),
               security=[e[0] for e in chosen if SECURITY_LABEL in e[2]])
    return out


# --------------------------------------------------------------------------- #
# Texts
# --------------------------------------------------------------------------- #

KIND_TEXT = {"combined": "every eligible group", "solo": "one suspect alone (a candidate with it failed)",
             "product": "main alone (a candidate on top of it failed)"}


ANCHOR_COMPONENTS = ("vllm", "kubernetes (default)")


def _short(component: str) -> str:
    """'image/ghcr.io/open-webui/open-webui' -> 'open-webui'; 'kubernetes (default)' -> 'kubernetes'."""
    return component.split(" (")[0].rsplit("/", 1)[-1]


def _ordered(changes: dict) -> list[list[str]]:
    """Changed components, the ones a person cares about first: the anchors, then
    majors, then the rest; CI-only tools last."""
    majors = set(changes.get("majors") or [])

    def rank(row):
        c = row[0]
        return (0 if c in ANCHOR_COMPONENTS else 1 if c in majors else 3 if c.startswith("ci/") else 2, c)
    return sorted(changes.get("rows") or [], key=rank)


def headline(p: dict, changes: dict | None, limit: int = 100) -> str:
    """One line for the E2E run's name: what is waiting for approval."""
    kind = "release" if p.get("publish") else "cycle" if p.get("action") == "cut" else "candidate"
    head = f"{kind} {p.get('version') or '?'}"
    rows = _ordered(changes or {})
    if not rows:
        n = len(p.get("product_commits") or [])
        return f"{head}: no version changes" + (f", {n} commit(s) on main" if n else "")
    parts = [f"{_short(c)} {new}" for c, _old, new in rows]
    text = f"{head}: {len(rows)} update{'s' if len(rows) != 1 else ''}: "
    shown: list[str] = []
    for i, part in enumerate(parts):
        rest = len(parts) - i - 1
        tail = f", +{rest} more" if rest else ""
        if len(text + ", ".join(shown + [part]) + tail) > limit:
            break
        shown.append(part)
    rest = len(parts) - len(shown)
    return text + ", ".join(shown) + (f"{', ' if shown else ''}+{rest} more" if rest else "")


def approval_block(p: dict, changes: dict | None, wait_days: int = 5) -> list[str]:
    """The top of the candidate's PR: what approving its E2E run means, in a few lines."""
    rows = _ordered(changes or {})
    majors = set((changes or {}).get("majors") or [])
    lines = ["### What you are approving", ""]
    if p.get("publish"):
        lines.append(f"- **A release.** `{p['version']}` is tagged and published when E2E is green "
                     f"(label `{PUBLISH_LABEL}`).")
    else:
        lines.append("- **An integration cycle.** When E2E is green it is merged into `main`; nothing is "
                     "tagged or published.")
    if p.get("kind") and p["kind"] != "combined":
        lines.append(f"- **A recovery cycle**: {KIND_TEXT[p['kind']]}.")
    if rows:
        lines.append(f"- **{len(rows)} component version{'s' if len(rows) != 1 else ''} change** on top of `main`:")
        lines += ["", "  | Component | Now on `main` | In this candidate | |", "  |---|---|---|---|"]
        for c, old, new in rows:
            flag = "anchor" if c in ANCHOR_COMPONENTS else "major" if c in majors else "CI only" if c.startswith("ci/") else ""
            lines.append(f"  | {c} | `{old}` | `{new}` | {flag} |")
        lines.append("")
    else:
        lines.append("- **No component version changes** on top of `main`.")
    if p.get("product_commits"):
        lines.append(f"- **{len(p['product_commits'])} commit(s) on `main`** that no cycle has tested yet (listed below).")
    security = p.get("security")
    if security:
        lines.append("- **Includes a security update** (" + ", ".join(f"`{b}`" for b in security) + "): do not let it wait.")
    lines += [f"- **If you do not approve**, nothing changes: after {wait_days} days this candidate is replaced by a "
              "fresh one with whatever is newest then. Rejecting the run has the same effect.",
              "- **Cost of approving**: four clusters for about 95 minutes, roughly $5-10.", ""]
    return lines


def pr_body(p: dict, notes_md: str, changes: dict | None = None, wait_days: int = 5) -> str:
    what = "Release" if p.get("publish") else "Integration cycle"
    lines = [f"{what}: `{p['version']}` (upgrades from `{p['base']}`).", ""]
    lines += approval_block(p, changes, wait_days)
    lines += [f"Remove the label `{PUBLISH_LABEL}` to merge it without a release." if p.get("publish")
              else f"Add the label `{PUBLISH_LABEL}` to this pull request to make it a release.", ""]
    lines.append("Groups: " + (", ".join(f"`{b}`" for b in p["branches"]) or "none (product changes only)"))
    if p["product_commits"]:
        lines += ["", "Commits on `main` that no cycle has tested yet:", ""]
        lines += [f"- {c}" for c in p["product_commits"]]
    for key, why in (("stale", "left out: does not contain the last release (Renovate has not rebased it; check the dashboard)"),
                     ("held", f"left out: labelled `{HOLD_LABEL}`"),
                     ("unchanged", "left out: no change against main"),
                     ("failed_alone", "left out: this version failed E2E alone; it returns with the next version"),
                     ("deferred", "left for a later cycle (one anchor update, or one suspect, at a time)"),
                     ("conflicts", "left out: merge conflict with main or with another group in this cycle")):
        if p.get(key):
            lines += ["", f"{why}: " + ", ".join(f"`{b}`" for b in p[key])]
    lines += ["", "E2E (fresh + upgrade, both profiles) was dispatched and waits for approval of the `e2e`"
              " environment. Do not merge by hand: when all four `e2e/*` statuses are green,"
              " `release.py finish` does it. Any push to this branch needs a new E2E run.", "", "---", "", notes_md]
    return "\n".join(lines)


def abandon_comment(p: dict) -> str:
    why = {"failed": "E2E failed twice on this candidate.",
           "stale": "E2E was not approved or did not finish in time, so this candidate is out of date.",
           "superseded": "`main` moved since this candidate was cut, so it can no longer be merged as tested.",
           "stuck": "E2E is green, but the candidate could not be merged for days (see the release-finish runs).",
           "closed": "This pull request was closed without a merge."}[p["why"]]
    lines = [f"Candidate abandoned: {why}", ""]
    if p["marks"]:
        solo = [m["what"] for m in p["marks"] if m["context"] == SOLO]
        combined = [m["what"] for m in p["marks"] if m["context"] == COMBINED]
        if solo:
            lines.append(f"`{solo[0]}` was the only suspect. It is left out until it changes"
                         + (" (cycles stop until `main` moves)" if solo[0] == "main" else " (a newer version)")
                         + ", or for two weeks: after that it is tried again, in case the failures were an outage.")
        if combined:
            lines.append("Suspects, tested one at a time in the next cycles: " + ", ".join(f"`{w}`" for w in combined) + ".")
    else:
        lines.append("Nothing is marked as a suspect; the next run cuts a fresh candidate.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# GitHub (read-only) and the CLI
# --------------------------------------------------------------------------- #

def fetch_statuses(slug: str, sha: str) -> list[dict]:
    """Every commit status of `sha`, newest first."""
    out = subprocess.run(["gh", "api", f"repos/{slug}/commits/{sha}/statuses", "--paginate"],
                         capture_output=True, text=True, check=True).stdout
    return [{"context": s.get("context", ""), "state": s.get("state", ""), "description": s.get("description") or "",
             "created_at": s.get("created_at") or ""} for s in json.loads(out or "[]")]


def fetch_marks(slug: str, shas: list[str]) -> dict[str, dict]:
    marks: dict[str, dict] = {}
    for sha in dict.fromkeys(shas):
        latest: dict[str, dict] = {}
        for st in fetch_statuses(slug, sha):
            if st["context"] in (SOLO, COMBINED):
                latest.setdefault(st["context"], {"state": st["state"], "at": st["created_at"]})
        if latest:
            marks[sha] = latest
    return marks


def gather(repo: str, slug: str, prs: list[dict], remote: str = "origin") -> tuple[list[dict] | None, dict]:
    """(statuses of the release head in flight or None, marks of main's head and the group heads)."""
    main = f"{remote}/main"
    tag = newest_tag(repo, main)
    inflight = release_in_flight(repo, tag, remote) if tag else None
    if inflight:
        return fetch_statuses(slug, git(repo, "rev-parse", f"{remote}/{inflight}").stdout.strip()), {}
    shas = [git(repo, "rev-parse", main).stdout.strip()]
    shas += [p["headRefOid"] for p in prs if p.get("headRefName", "").startswith("renovate/") and p.get("headRefOid")]
    return None, fetch_marks(slug, shas)


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", required=True)
    p.add_argument("--prs", required=True, help="JSON from gh pr list --json headRefName,labels,headRefOid")
    p.add_argument("--release-prs",
                   help="JSON from gh pr list --state all --json headRefName,headRefOid,state,number,createdAt")
    p.add_argument("--github", metavar="OWNER/REPO", help="read commit statuses (E2E state, suspect marks) with gh")
    p.add_argument("--today", default=date.today().isoformat())
    p.add_argument("--force", action="store_true", help="start a cycle even if the last one is recent")
    p.add_argument("--publish", action="store_true", help="make the cycle a release")
    p.add_argument("--cycle-days", type=int, default=7)
    p.add_argument("--publish-days", type=int, default=90)
    p.add_argument("--wait-days", type=int, default=5)
    p.add_argument("--mark-days", type=int, default=14)
    for name in ("pr-body", "abandon-comment", "headline"):
        b = sub.add_parser(name)
        b.add_argument("--plan", required=True)
        if name == "pr-body":
            b.add_argument("--notes", required=True)
            b.add_argument("--wait-days", type=int, default=5)
        if name in ("pr-body", "headline"):
            b.add_argument("--changes", help="JSON from `release.py changes origin/main`")
        if name == "headline":
            b.add_argument("--prefix", default="", help="e.g. 'retry of ' for a second E2E run")
    f = sub.add_parser("finishable", help="print the release in flight (for release-finish.yaml), or nothing")
    f.add_argument("--repo", required=True)
    n = sub.add_parser("newest-tag", help="print the newest release tag on main, or nothing")
    n.add_argument("--repo", required=True)
    n.add_argument("--since", metavar="vYYYY.MM.N", help="print it only if it is this version or a later one")
    a = ap.parse_args(argv)
    if a.cmd == "newest-tag":
        tag = newest_tag(a.repo, "origin/main")
        if tag and (not a.since or (TAG_RX.match(a.since) and _key(tag) >= _key(a.since))):
            print(tag)
        return 0
    if a.cmd == "finishable":
        version = finishable(a.repo)
        if version:
            print(version)
        return 0
    if a.cmd == "plan":
        prs = _load(a.prs)
        rprs = [x for x in _load(a.release_prs) if x.get("headRefName", "").startswith("release/")] \
            if a.release_prs else None
        statuses, marks = gather(a.repo, a.github, prs) if a.github else (None, {})
        print(json.dumps(plan(a.repo, prs, date.fromisoformat(a.today), force=a.force, publish=a.publish,
                              cycle_days=a.cycle_days, publish_days=a.publish_days, wait_days=a.wait_days,
                              mark_days=a.mark_days, release_prs=rprs, statuses=statuses, marks=marks),
                         indent=2, default=str))
        return 0
    pl = _load(a.plan)
    if a.cmd == "abandon-comment":
        print(abandon_comment(pl))
        return 0
    changes = _load(a.changes) if a.changes else None
    if a.cmd == "headline":
        print(a.prefix + headline(pl, changes))
        return 0
    with open(a.notes, encoding="utf-8") as fh:
        print(pr_body(pl, fh.read(), changes, a.wait_days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
