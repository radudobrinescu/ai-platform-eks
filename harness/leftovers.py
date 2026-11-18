#!/usr/bin/env python3
"""Preflight of every E2E job: environments an earlier run left behind, and
whether the account still has the Elastic IPs this job needs.

An E2E environment that was not torn down keeps billing, and holds 3 of the
account's Elastic IPs: a few leaks and no later job can build its VPC. That
failure looks like a broken candidate, and the factory would blame the updates
in it. So before anything is provisioned this check

  * lists the EKS clusters that look like E2E environments (<prefix>-e2e<f|u><d|m><n>)
    and are older than a run can take: they are leaks, reported in the job
    summary and through the job's output (the workflow opens an issue);
  * compares the Elastic IPs in use with the account quota: without room for
    this job it exits 3, and the workflow marks the job "not run" instead of
    "failed", so nothing is blamed.

With --own, after the job's teardown: what is left of the job's own environment
(its cluster, its VPC). A teardown can report success and leave both (seen on
2026-11-18), and the preflight of a later run reports a leak only once it is
older than a run can take. The workflow opens the issue at once.

Read-only. Any AWS call that fails makes the check pass with a warning: a
preflight must never be the reason a healthy run does not start.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ENV_RX = re.compile(r"-(e2e[fu][dm]\d{1,4})$")
EIPS_PER_ENV = 3                       # one NAT gateway per availability zone
EIP_QUOTA_CODE = "L-0263D0A3"          # EC2-VPC Elastic IPs
BLOCKED = 3                            # exit code: not enough Elastic IPs for this job


def e2e_env(cluster: str) -> str | None:
    """The E2E env name inside a cluster name, or None for any other cluster."""
    m = ENV_RX.search(cluster)
    return m.group(1) if m else None


def classify(clusters: list[dict], own_env: str, now: datetime, max_age_hours: float) -> dict:
    """clusters: [{name, created (datetime, aware)}]. Returns {leaked: [...], running: [...]},
    each {name, env, age_hours}. `own_env` (this job's) is never listed."""
    out: dict = {"leaked": [], "running": []}
    for c in sorted(clusters, key=lambda c: c["name"]):
        env = e2e_env(c["name"])
        if not env or env == own_env:
            continue
        age = (now - c["created"]).total_seconds() / 3600
        out["leaked" if age >= max_age_hours else "running"].append(
            {"name": c["name"], "env": env, "age_hours": round(age, 1)})
    return out


def headroom(quota: float | None, used: int | None, needed: int = EIPS_PER_ENV) -> bool | None:
    """True/False: room for this job's Elastic IPs; None when it cannot be told."""
    if quota is None or used is None:
        return None
    return quota - used >= needed


def report(found: dict, quota: float | None, used: int | None, room: bool | None) -> str:
    lines = ["### E2E preflight", ""]
    if found["leaked"]:
        lines += ["**Environments left behind by earlier runs** (still billing):", ""]
        lines += [f"- `{c['name']}`, {c['age_hours']} h old" for c in found["leaked"]]
        lines += ["", "Tear each down from a checkout of the ref it ran: `./platformctl down <env>` "
                      "(the env name is the last part of the cluster name).", ""]
    else:
        lines += ["No environment left behind by an earlier run.", ""]
    if found["running"]:
        lines += ["Other E2E environments in use right now: " + ", ".join(f"`{c['env']}`" for c in found["running"]) + ".", ""]
    if room is None:
        lines.append("Elastic IP headroom: could not be read (the job goes on).")
    else:
        lines.append(f"Elastic IPs: {used} of {int(quota)} in use; this job needs {EIPS_PER_ENV}: "
                     + ("enough." if room else "**not enough. Not provisioning.**"))
    return "\n".join(lines) + "\n"


def own_left(clusters: list[dict], vpcs: list[str]) -> str:
    """clusters: [{name, status}] and VPC ids of the job's own environment that
    still exist. One line for the log and the issue; empty when nothing is left."""
    parts = [f"EKS cluster {c['name']} ({c['status']})" for c in clusters]
    if vpcs:
        parts.append("VPC " + " ".join(vpcs))
    return "; ".join(parts)


# --------------------------------------------------------------------------- #
# AWS (read-only); every failure is "unknown", never an error
# --------------------------------------------------------------------------- #

def _aws(*args: str):
    r = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"preflight: aws {' '.join(args[:2])} failed; treated as unknown", file=sys.stderr)
        return None
    try:
        return json.loads(r.stdout or "null")
    except ValueError:
        return None


def e2e_clusters(region: str) -> list[dict]:
    names = (_aws("eks", "list-clusters", "--region", region) or {}).get("clusters") or []
    out = []
    for name in names:
        if not e2e_env(name):
            continue
        created = ((_aws("eks", "describe-cluster", "--region", region, "--name", name) or {}).get("cluster") or {}).get("createdAt")
        if created:
            out.append({"name": name, "created": datetime.fromisoformat(str(created).replace("Z", "+00:00"))})
    return out


def own_environment(region: str, env: str) -> tuple[list[dict], list[str]]:
    """The clusters and VPCs named for `env` that exist now. Unknown counts as none."""
    names = (_aws("eks", "list-clusters", "--region", region) or {}).get("clusters") or []
    clusters = []
    for name in sorted(n for n in names if e2e_env(n) == env):
        status = ((_aws("eks", "describe-cluster", "--region", region, "--name", name) or {}).get("cluster") or {}).get("status")
        clusters.append({"name": name, "status": status or "status unknown"})
    # The VPC carries the cluster's name; the cluster may be gone, so match on the env.
    vpcs = (_aws("ec2", "describe-vpcs", "--region", region, "--filters", f"Name=tag:Name,Values=*-{env}") or {}).get("Vpcs") or []
    return clusters, sorted(v["VpcId"] for v in vpcs if v.get("VpcId"))


def _write(var: str, text: str) -> None:
    if os.environ.get(var):
        with open(os.environ[var], "a", encoding="utf-8") as fh:
            fh.write(text)


def own(region: str, env: str) -> int:
    left = own_left(*own_environment(region, env))
    if left:
        print(left)
        _write("GITHUB_STEP_SUMMARY", f"### Not torn down\n\n`{env}` is still there, still billing: {left}.\n")
    _write("GITHUB_OUTPUT", f"left={left}\n")
    return 0


def eip_numbers(region: str) -> tuple[float | None, int | None]:
    quota = ((_aws("service-quotas", "get-service-quota", "--region", region, "--service-code", "ec2",
                   "--quota-code", EIP_QUOTA_CODE) or {}).get("Quota") or {}).get("Value")
    addresses = (_aws("ec2", "describe-addresses", "--region", region) or {}).get("Addresses")
    return quota, (len(addresses) if addresses is not None else None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", required=True, help="this job's E2E env name")
    ap.add_argument("--region", required=True)
    ap.add_argument("--max-age-hours", type=float, default=8, help="older E2E environments are leaks (a job runs < 6 h)")
    ap.add_argument("--own", action="store_true",
                    help="after the teardown: print what is left of this job's own environment (nothing else)")
    a = ap.parse_args(argv)
    if a.own:
        return own(a.region, a.env)
    found = classify(e2e_clusters(a.region), a.env, datetime.now(timezone.utc), a.max_age_hours)
    quota, used = eip_numbers(a.region)
    room = headroom(quota, used)
    text = report(found, quota, used, room)
    print(text)
    _write("GITHUB_STEP_SUMMARY", text)
    _write("GITHUB_OUTPUT", "leaked=" + " ".join(c["name"] for c in found["leaked"]) + "\n"
                            + f"blocked={'true' if room is False else 'false'}\n")
    return BLOCKED if room is False else 0


if __name__ == "__main__":
    sys.exit(main())
