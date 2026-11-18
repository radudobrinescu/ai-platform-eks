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
    a = ap.parse_args(argv)
    found = classify(e2e_clusters(a.region), a.env, datetime.now(timezone.utc), a.max_age_hours)
    quota, used = eip_numbers(a.region)
    room = headroom(quota, used)
    text = report(found, quota, used, room)
    print(text)
    for var, line in (("GITHUB_STEP_SUMMARY", text),
                      ("GITHUB_OUTPUT", "leaked=" + " ".join(c["name"] for c in found["leaked"]) + "\n"
                                        + f"blocked={'true' if room is False else 'false'}\n")):
        if os.environ.get(var):
            with open(os.environ[var], "a", encoding="utf-8") as fh:
                fh.write(line)
    return BLOCKED if room is False else 0


if __name__ == "__main__":
    sys.exit(main())
