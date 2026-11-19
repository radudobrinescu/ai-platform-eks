#!/usr/bin/env python3
"""Karpenter NodeClaims that an upgrade has to remove itself.

A node whose pool no longer allows it is drifted, and Karpenter replaces it.
But only after the node initialized: a node that joined the cluster and never
became ready (for example a GPU instance family whose GPU the node image cannot
drive, so the GPU is never reported) is drifted for ever, and Karpenter keeps
counting its capacity for the pods that wait for it. Nothing starts a
replacement until the NodeClaim is deleted.

Reads `kubectl get nodeclaims -o json` on stdin and prints one line per such
claim: `<name> <instance type> <pool>`. Read-only; input it cannot read means
"none".
"""
import json
import os
import sys
from datetime import datetime, timezone

MIN_AGE_SECONDS = 300   # a node that is still booting is not stuck (a GPU node reports its GPU within about 2 minutes)


def _condition(claim, kind):
    for cond in (claim.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return cond.get("status")
    return None


def stuck(doc, now, min_age_seconds=MIN_AGE_SECONDS):
    """[(name, instance type, pool)] of the claims that are drifted, never
    initialized, not being deleted, and older than `min_age_seconds`."""
    out = []
    for claim in (doc or {}).get("items") or []:
        meta = claim.get("metadata") or {}
        if meta.get("deletionTimestamp"):
            continue
        if _condition(claim, "Drifted") != "True" or _condition(claim, "Initialized") == "True":
            continue
        try:
            created = datetime.fromisoformat(str(meta.get("creationTimestamp", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - created).total_seconds() < min_age_seconds:
            continue
        labels = meta.get("labels") or {}
        out.append((meta.get("name", ""), labels.get("node.kubernetes.io/instance-type", "?"),
                    labels.get("karpenter.sh/nodepool", "?")))
    return out


def main():
    try:
        doc = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    try:
        min_age = int(os.environ.get("UPGRADE_STUCK_NODE_AGE", MIN_AGE_SECONDS))
    except ValueError:
        min_age = MIN_AGE_SECONDS
    for name, kind, pool in stuck(doc, datetime.now(timezone.utc), min_age):
        if name:
            print(name, kind, pool)
    return 0


if __name__ == "__main__":
    sys.exit(main())
