#!/usr/bin/env python3
"""Karpenter NodeClaims that an upgrade has to remove itself.

A node whose pool no longer allows it is drifted, and Karpenter replaces it.
But only after the node initialized: a node that joined the cluster and never
became ready (for example a GPU instance family whose GPU the node image cannot
drive, so the GPU is never reported) is drifted for ever, and Karpenter keeps
counting its capacity for the pods that wait for it. Nothing starts a
replacement until the NodeClaim is deleted.

Drift is not required. A node of the old release that never became ready stays
stuck when the new release allows its instance type again: nothing marks it,
and it still cannot run anything, while a new node of the same type can. Such a
claim is given longer, then removed as well.

Reads `kubectl get nodeclaims -o json` on stdin and prints one line per such
claim: `<name> <instance type> <pool>`. Read-only; input it cannot read means
"none".
"""
import json
import os
import sys
from datetime import datetime, timezone

MIN_AGE_SECONDS = 300   # a node that is still booting is not stuck (a GPU node reports its GPU within about 2 minutes)
MIN_AGE_NOT_DRIFTED_SECONDS = 900   # without drift nothing says the node is of the old release: wait three times as long


def _condition(claim, kind):
    for cond in (claim.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return cond.get("status")
    return None


def stuck(doc, now, min_age_seconds=MIN_AGE_SECONDS):
    """[(name, instance type, pool)] of the claims that never initialized, are
    not being deleted, and are older than `min_age_seconds` when drifted, or
    than three times that when not."""
    out = []
    for claim in (doc or {}).get("items") or []:
        meta = claim.get("metadata") or {}
        if meta.get("deletionTimestamp"):
            continue
        if _condition(claim, "Initialized") == "True":
            continue
        drifted = _condition(claim, "Drifted") == "True"
        patience = min_age_seconds if drifted else max(MIN_AGE_NOT_DRIFTED_SECONDS, 3 * min_age_seconds)
        try:
            created = datetime.fromisoformat(str(meta.get("creationTimestamp", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if (now - created).total_seconds() < patience:
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
