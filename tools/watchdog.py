#!/usr/bin/env python3
"""The factory's watchdog: what would stop it without anyone noticing.

Nobody watches the factory, so once a week (.github/workflows/watchdog.yaml)
this looks for the quiet failures and says so in the issue "Release candidate
needs attention"; it also keeps one issue, "Factory status", with the whole
picture. It checks, from GitHub alone (no cloud credentials):

  * tokens: E2E_PUSH_TOKEN (and PUBLIC_PUSH_TOKEN when set) are valid and do
    not expire within `token_days`. An expired push token stops every cycle;
  * workflows: none of the factory's workflows is disabled. GitHub disables
    scheduled workflows of a repository without activity for 60 days;
  * the schedule: the factory's own scheduled run happened within `run_days`;
  * progress: a cycle was merged within `cycle_days`. Cycles that are cut and
    never approved, or always fail, show up here;
  * the attention issue is not open for longer than `issue_days`.

`findings()` is pure; the CLI gathers the facts with gh and curl.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

ATTENTION_TITLE = "Release candidate needs attention"
STATUS_TITLE = "Factory status"
FACTORY_WORKFLOWS = ("release-candidate", "release-finish", "e2e", "watchdog", "coverage", "main-schedules")


def parse_time(text: str | None) -> datetime | None:
    """GitHub's timestamps: '2026-11-17T19:04:49Z' and the token header's '2027-01-31 00:00:00 UTC' / '... +0000'."""
    if not text:
        return None
    t = text.strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
    if " " in t and "T" not in t:
        date_part, _, rest = t.partition(" ")
        t = date_part + "T" + rest.replace(" ", "")
    if len(t) > 5 and t[-5] in "+-" and ":" not in t[-5:]:
        t = t[:-2] + ":" + t[-2:]
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _days(now: datetime, then: datetime | None) -> int | None:
    return None if then is None else (now - then).days


def findings(now: datetime, facts: dict, *, token_days: int = 30, run_days: int = 3, cycle_days: int = 21,
             issue_days: int = 7) -> tuple[list[str], list[tuple[str, str]]]:
    """(problems, status rows). facts:
       tokens: {name: {"set": bool, "valid": bool, "expires": datetime|None}}
       workflows: {name: state}          last_scheduled_run / last_cycle_merged / attention_opened: datetime|None
    """
    problems: list[str] = []
    rows: list[tuple[str, str]] = []
    for name, tok in sorted((facts.get("tokens") or {}).items()):
        if not tok.get("set"):
            rows.append((f"token `{name}`", "not set"))
            continue
        if not tok.get("valid"):
            problems.append(f"The token `{name}` is rejected by GitHub (expired or revoked). Create a new one and "
                            "update the secret; until then no cycle can be cut or finished.")
            rows.append((f"token `{name}`", "**rejected**"))
            continue
        left = None if tok.get("expires") is None else (tok["expires"] - now).days
        if left is None:
            rows.append((f"token `{name}`", "valid, no expiry"))
        else:
            rows.append((f"token `{name}`", f"valid, expires in {left} days ({tok['expires'].date().isoformat()})"))
            if left <= token_days:
                problems.append(f"The token `{name}` expires in {left} days ({tok['expires'].date().isoformat()}). "
                                "Renew it and update the secret before then: the factory stops when it expires.")
    states = facts.get("workflows") or {}
    off = sorted(n for n in FACTORY_WORKFLOWS if states.get(n) not in (None, "active"))
    missing = sorted(n for n in FACTORY_WORKFLOWS if n not in states)
    rows.append(("workflows", "all active" if not off and not missing else
                 "; ".join([f"**{n}: {states[n]}**" for n in off] + [f"**{n}: missing**" for n in missing])))
    for n in off:
        problems.append(f"The workflow `{n}` is {states[n]} (GitHub disables scheduled workflows after 60 days "
                        f"without repository activity). Enable it: `gh workflow enable {n}.yaml`.")
    for n in missing:
        problems.append(f"The workflow `{n}` does not exist on the default branch.")
    age = _days(now, facts.get("last_scheduled_run"))
    rows.append(("last scheduled factory run", "never" if age is None else f"{age} days ago"))
    if age is None or age >= run_days:
        problems.append("The factory's scheduled run (release-candidate) has not run for "
                        + ("ever" if age is None else f"{age} days") + ". Check that the workflow is enabled.")
    age = _days(now, facts.get("last_cycle_merged"))
    rows.append(("last cycle merged", "never" if age is None else f"{age} days ago"))
    if age is not None and age >= cycle_days:
        problems.append(f"No cycle has been merged for {age} days. Either E2E runs wait for approval (Actions, "
                        "workflow e2e) or candidates keep failing (see the closed `cycle:` pull requests).")
    age = _days(now, facts.get("attention_opened"))
    rows.append((f"issue \"{ATTENTION_TITLE}\"", "not open" if age is None else f"open for {age} days"))
    if age is not None and age >= issue_days:
        problems.append(f"The issue \"{ATTENTION_TITLE}\" has been open for {age} days.")
    return problems, rows


def status_body(now: datetime, problems: list[str], rows: list[tuple[str, str]]) -> str:
    lines = [f"Updated {now.strftime('%Y-%m-%d %H:%M')} UTC by the weekly watchdog.", "",
             "**" + ("Nothing needs attention." if not problems else f"{len(problems)} thing(s) need attention.") + "**", "",
             "| Check | State |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rows]
    if problems:
        lines += ["", "### Needs attention", ""] + [f"- {p}" for p in problems]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Facts (GitHub only)
# --------------------------------------------------------------------------- #

def _gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def token_fact(value: str) -> dict:
    """Ask GitHub about a token without using any of its permissions (rate_limit needs none)."""
    if not value:
        return {"set": False}
    r = subprocess.run(["curl", "-sS", "-D", "-", "-o", "/dev/null", "-H", "Authorization: Bearer " + value,
                        "-H", "Accept: application/vnd.github+json", "https://api.github.com/rate_limit"],
                       capture_output=True, text=True)
    head = r.stdout.splitlines()
    ok = bool(head) and " 200" in head[0]
    expires = next((ln.split(":", 1)[1] for ln in head
                    if ln.lower().startswith("github-authentication-token-expiration:")), None)
    return {"set": True, "valid": ok, "expires": parse_time(expires)}


def gather(slug: str, token_env: dict[str, str]) -> dict:
    wf = json.loads(_gh("api", f"repos/{slug}/actions/workflows", "--paginate", "--jq", ".workflows") or "[]")
    runs = json.loads(_gh("run", "list", "--repo", slug, "--workflow", "release-candidate.yaml", "--event", "schedule",
                          "--limit", "1", "--json", "createdAt") or "[]")
    prs = json.loads(_gh("pr", "list", "--repo", slug, "--state", "merged", "--limit", "100", "--json",
                         "headRefName,mergedAt") or "[]")
    cycles = [parse_time(p["mergedAt"]) for p in prs if p.get("headRefName", "").startswith("release/")]
    issues = json.loads(_gh("issue", "list", "--repo", slug, "--state", "open", "--search",
                            f'in:title "{ATTENTION_TITLE}"', "--json", "createdAt") or "[]")
    return {
        "tokens": {name: token_fact(os.environ.get(env, "")) for name, env in token_env.items()},
        "workflows": {w["name"]: w["state"] for w in wf},
        "last_scheduled_run": parse_time(runs[0]["createdAt"]) if runs else None,
        "last_cycle_merged": max((c for c in cycles if c), default=None),
        "attention_opened": parse_time(issues[0]["createdAt"]) if issues else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--github", required=True, metavar="OWNER/REPO")
    ap.add_argument("--token-env", action="append", default=[], metavar="NAME=ENVVAR",
                    help="a token to check: its secret's name and the environment variable holding it")
    ap.add_argument("--status-file", default="status.md")
    ap.add_argument("--problems-file", default="problems.md")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    problems, rows = findings(now, gather(a.github, dict(t.split("=", 1) for t in a.token_env)))
    with open(a.status_file, "w", encoding="utf-8") as fh:
        fh.write(status_body(now, problems, rows))
    with open(a.problems_file, "w", encoding="utf-8") as fh:
        fh.write("".join(f"- {p}\n" for p in problems))
    print(status_body(now, problems, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
