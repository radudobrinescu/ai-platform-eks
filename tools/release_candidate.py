#!/usr/bin/env python3
"""Decide whether, and what, to release this week (scheduled release candidate).

Run by .github/workflows/release-candidate.yaml every Monday after Renovate's
window. Only the planning is here (pure, testable); cutting, notes, PR and
E2E dispatch reuse tools/release.py and gh.

Rules
  * Release weeks: every second Monday counted from the anchor date
    (RELEASE_ANCHOR, a Monday). --force ignores the calendar.
  * Groups: open Renovate PRs whose branch is renovate/<group>. Majors
    (renovate/major-*) only with the label `include-in-release`; any PR with
    the label `hold` is left out.
  * A group branch must contain the newest release tag (Renovate leaves a
    branch alone while an update in it is inside the release age, so after a
    release it can still carry that release's changes). Stale branches are
    left out and listed, never merged.
  * A group branch with no change against main is left out.
  * No release when nothing is left and main has no commits since the tag.
  * Version: vYYYY.MM.N, the year.month of today or of the newest tag,
    whichever is later; N counts up within that month.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date

TAG_RX = re.compile(r"^v(\d{4})\.(\d{2})\.(\d+)$")
INCLUDE_LABEL = "include-in-release"
HOLD_LABEL = "hold"


def git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=check)


def release_week(today: date, anchor: date) -> bool:
    if today < anchor:
        return False
    return ((today - anchor).days // 7) % 2 == 0


def newest_tag(repo: str, ref: str) -> str | None:
    tags = git(repo, "tag", "--merged", ref, "--list", "v*").stdout.split()
    parsed = [(tuple(int(x) for x in TAG_RX.match(t).groups()), t) for t in tags if TAG_RX.match(t)]
    return max(parsed)[1] if parsed else None


def next_version(latest: str | None, today: date) -> str:
    ym = (today.year, today.month)
    if latest:
        y, m, n = (int(x) for x in TAG_RX.match(latest).groups())
        if (y, m) >= ym:
            return f"v{y:04d}.{m:02d}.{n + 1}"
    return f"v{ym[0]:04d}.{ym[1]:02d}.0"


def _labels(pr: dict) -> set[str]:
    return {(lbl.get("name") if isinstance(lbl, dict) else lbl) for lbl in pr.get("labels") or []}


def plan(repo: str, prs: list[dict], today: date, anchor: date, *, force: bool = False,
         remote: str = "origin") -> dict:
    main = f"{remote}/main"
    out: dict = {"release": False, "version": None, "base": None, "branches": [],
                 "stale": [], "held": [], "unchanged": [], "product_commits": [], "reason": ""}
    if not force and not release_week(today, anchor):
        out["reason"] = f"not a release week (every second Monday from {anchor.isoformat()})"
        return out
    tag = newest_tag(repo, main)
    if not tag:
        out["reason"] = "no release tag on main; cut the first release by hand"
        return out
    out["base"] = tag
    for pr in sorted(prs, key=lambda p: p.get("headRefName", "")):
        head = pr.get("headRefName", "")
        if not head.startswith("renovate/"):
            continue
        labels = _labels(pr)
        if HOLD_LABEL in labels or (head.startswith("renovate/major-") and INCLUDE_LABEL not in labels):
            out["held"].append(head)
            continue
        ref = f"{remote}/{head}"
        if git(repo, "merge-base", "--is-ancestor", tag, ref, check=False).returncode != 0:
            out["stale"].append(head)
            continue
        if not git(repo, "diff", "--quiet", f"{main}...{ref}", check=False).returncode:
            out["unchanged"].append(head)
            continue
        out["branches"].append(head)
    out["product_commits"] = [
        line for line in git(repo, "log", "--first-parent", "--format=%h %s", f"{tag}..{main}").stdout.splitlines()
        if line.strip()]
    if not out["branches"] and not out["product_commits"]:
        out["reason"] = f"nothing to release: no fresh group branch and no commits on main since {tag}"
        return out
    out["release"] = True
    out["version"] = next_version(tag, today)
    return out


def pr_body(p: dict, notes_md: str) -> str:
    lines = [f"Scheduled release candidate: `{p['version']}` (upgrades from `{p['base']}`).", ""]
    lines.append("Groups: " + (", ".join(f"`{b}`" for b in p["branches"]) or "none (product changes only)"))
    if p["product_commits"]:
        lines += ["", f"Product changes on `main` since `{p['base']}`:", ""]
        lines += [f"- {c}" for c in p["product_commits"]]
    for key, why in (("stale", "left out: does not contain the last release yet (Renovate has not rebased it)"),
                     ("held", f"left out: major without `{INCLUDE_LABEL}`, or labelled `{HOLD_LABEL}`"),
                     ("unchanged", "left out: no change against main")):
        if p[key]:
            lines += ["", f"{why}: " + ", ".join(f"`{b}`" for b in p[key])]
    lines += ["", "E2E (fresh + upgrade, both profiles) was dispatched and waits for approval of the `e2e`"
              " environment. Merge only after all four `e2e/*` statuses are green and `release.py record`"
              " ran.", "", "---", "", notes_md]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", required=True)
    p.add_argument("--prs", required=True, help="JSON from gh pr list --json headRefName,labels")
    p.add_argument("--today", default=date.today().isoformat())
    p.add_argument("--anchor", default="2026-11-09")
    p.add_argument("--force", action="store_true")
    b = sub.add_parser("pr-body")
    b.add_argument("--plan", required=True)
    b.add_argument("--notes", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        with open(a.prs, encoding="utf-8") as fh:
            prs = json.load(fh)
        print(json.dumps(plan(a.repo, prs, date.fromisoformat(a.today), date.fromisoformat(a.anchor),
                              force=a.force), indent=2))
        return 0
    with open(a.plan, encoding="utf-8") as fh:
        pl = json.load(fh)
    with open(a.notes, encoding="utf-8") as fh:
        print(pr_body(pl, fh.read()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
