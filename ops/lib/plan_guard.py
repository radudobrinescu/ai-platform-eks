#!/usr/bin/env python3
"""Plan guard: refuse Terraform plans that destroy long-lived or stateful resources.

Run by `platformctl upgrade` on the JSON form of every saved plan
(`terraform show -json <plan>`), before anything is applied. A change is
GUARDED when its actions include `delete` (a plain delete, or a replace in
either order) and its resource type is in GUARDED_TYPES. A guarded change
passes only when it is acknowledged:

  * in the release manifest (`acknowledged_replacements`, matched on stage +
    address), or
  * on the command line with `--allow-replace ADDR` (any stage) or
    `--allow-replace STAGE:ADDR` (that stage only).

Exit status: 0 = no unacknowledged guarded change, 3 = at least one guarded
change, 2 = bad input (unreadable plan or manifest).

Python 3.9 compatible, standard library only.
"""
import argparse
import json
import re
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

EXIT_OK = 0
EXIT_INPUT = 2
EXIT_GUARDED = 3

# Resource types whose delete or replace loses data, identity, or running
# workloads. Kind -> types; flattened into GUARDED_TYPES below.
GUARDED_KINDS = (
    ("cluster and nodes", (
        "aws_eks_cluster", "aws_eks_node_group", "aws_eks_addon", "aws_eks_capability",
    )),
    ("network", (
        "aws_vpc", "aws_subnet", "aws_nat_gateway", "aws_eip", "aws_internet_gateway",
    )),
    ("data and keys", (
        "aws_s3_bucket", "aws_dynamodb_table", "aws_kms_key", "aws_cognito_user_pool",
    )),
    ("identity", (
        "aws_iam_openid_connect_provider",
    )),
    # kubectl_manifest holds the Argo CD bootstrap Application and the
    # Karpenter NodePools: deleting either removes running workloads.
    ("in-cluster", (
        "kubernetes_namespace", "kubernetes_persistent_volume_claim", "helm_release",
        "kubectl_manifest",
    )),
)
GUARDED_TYPES = {t: kind for kind, types in GUARDED_KINDS for t in types}

# A stage-scoped --allow-replace value: "<stage>:<address>". Stages are the
# Terraform directories (e.g. "30.eks/30.cluster") and start with two digits;
# Terraform addresses start with a letter, so the two forms never overlap.
_STAGE_SCOPED = re.compile(r"^(\d\d\.[^:]+):(.+)$")


class GuardInputError(Exception):
    """The plan or manifest could not be read."""


def normalize_stage(stage: str) -> str:
    """'./30.eks/30.cluster/' -> '30.eks/30.cluster' (the Makefile prints './' paths)."""
    s = stage.strip()
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/")


def classify(actions: Sequence[str]) -> str:
    """Map a Terraform `change.actions` list to one word."""
    acts = list(actions)
    if "delete" in acts and "create" in acts:
        return "replace"
    if "delete" in acts:
        return "delete"
    if acts == ["create"]:
        return "create"
    if acts == ["update"]:
        return "update"
    if acts == ["read"]:
        return "read"
    if not acts or acts == ["no-op"]:
        return "no-op"
    return "-".join(acts)  # e.g. "forget" (Terraform 1.7+ removed blocks)


def parse_allow_replace(
        values: Optional[Sequence[str]]) -> Tuple[Set[str], Set[Tuple[str, str]]]:
    """Split --allow-replace values into any-stage addresses and (stage, address) pairs."""
    anywhere: Set[str] = set()
    scoped: Set[Tuple[str, str]] = set()
    for v in values or ():
        v = v.strip()
        if not v:
            continue
        m = _STAGE_SCOPED.match(v)
        if m:
            scoped.add((normalize_stage(m.group(1)), m.group(2)))
        else:
            anywhere.add(v)
    return anywhere, scoped


def manifest_acknowledgements(manifest: Optional[dict]) -> Dict[Tuple[str, str], str]:
    """(stage, address) -> reason from a release manifest's acknowledged_replacements."""
    acks: Dict[Tuple[str, str], str] = {}
    if not manifest:
        return acks
    for entry in manifest.get("acknowledged_replacements") or []:
        if not isinstance(entry, dict):
            continue
        stage = normalize_stage(str(entry.get("stage", "")))
        address = str(entry.get("address", ""))
        if stage and address:
            acks[(stage, address)] = str(entry.get("reason", "")).strip()
    return acks


def evaluate(plan: dict, stage: str, manifest: Optional[dict] = None,
             allow_replace: Optional[Sequence[str]] = None) -> dict:
    """Classify every resource change of one stage's plan.

    Returns {"stage", "counts": {create, update, delete, replace},
    "guarded": [ {address, type, action, kind, acknowledged, reason} ],
    "blocked": int}. Data sources and no-op changes are ignored.
    """
    if not isinstance(plan, dict):
        raise GuardInputError("plan JSON must be an object (output of 'terraform show -json PLAN')")
    stage = normalize_stage(stage)
    acks = manifest_acknowledgements(manifest)
    version = (manifest or {}).get("version", "the release manifest")
    anywhere, scoped = parse_allow_replace(allow_replace)

    counts = {"create": 0, "update": 0, "delete": 0, "replace": 0}
    guarded: List[dict] = []
    for rc in plan.get("resource_changes") or []:
        if rc.get("mode") == "data":
            continue
        action = classify((rc.get("change") or {}).get("actions") or [])
        if action in counts:
            counts[action] += 1
        if action not in ("delete", "replace"):
            continue
        rtype = rc.get("type", "")
        kind = GUARDED_TYPES.get(rtype)
        if kind is None:
            continue
        address = rc.get("address", "")
        if (stage, address) in acks:
            ok, reason = True, "acknowledged by {}: {}".format(version, acks[(stage, address)] or "no reason given")
        elif address in anywhere or (stage, address) in scoped:
            ok, reason = True, "allowed with --allow-replace"
        else:
            ok, reason = False, "{} ({})".format(kind, rtype)
        guarded.append({
            "address": address, "type": rtype, "action": action, "kind": kind,
            "acknowledged": ok, "reason": reason,
        })
    return {
        "stage": stage,
        "counts": counts,
        "guarded": guarded,
        "blocked": sum(1 for g in guarded if not g["acknowledged"]),
    }


def format_summary(result: dict) -> str:
    c = result["counts"]
    lines = ["stage {}: {} to create, {} to update, {} to delete, {} to replace".format(
        result["stage"], c["create"], c["update"], c["delete"], c["replace"])]
    for g in result["guarded"]:
        tag = "allowed" if g["acknowledged"] else "GUARDED"
        lines.append("  {:<8} {:<8} {}  {}".format(tag, g["action"], g["address"], g["reason"]))
    if result["blocked"]:
        lines.append("  {} guarded change(s) not acknowledged. If the change is intended, rerun with".format(
            result["blocked"]))
        for g in result["guarded"]:
            if not g["acknowledged"]:
                lines.append("    --allow-replace '{}:{}'".format(result["stage"], g["address"]))
        lines.append("  (see UPGRADING.md, 'When the plan guard stops').")
    return "\n".join(lines)


def load_json(path: str, what: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise GuardInputError("cannot read {} {}: {}".format(what, path, exc))
    if not isinstance(data, dict):
        raise GuardInputError("{} {} is not a JSON object".format(what, path))
    return data


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="plan_guard.py",
        description="Fail on delete or replace of long-lived or stateful resources in a Terraform plan.")
    ap.add_argument("plan_json", metavar="PLAN_JSON", help="output of 'terraform show -json PLAN'")
    ap.add_argument("--stage", required=True, help="Terraform stage directory, e.g. 30.eks/30.cluster")
    ap.add_argument("--manifest", help="release manifest (releases/<version>.json)")
    ap.add_argument("--allow-replace", action="append", default=[], metavar="[STAGE:]ADDR",
                    help="accept a guarded change at this address (repeatable)")
    args = ap.parse_args(argv)
    try:
        plan = load_json(args.plan_json, "plan")
        manifest = load_json(args.manifest, "manifest") if args.manifest else None
        result = evaluate(plan, args.stage, manifest, args.allow_replace)
    except GuardInputError as exc:
        print("plan_guard: {}".format(exc), file=sys.stderr)
        return EXIT_INPUT
    print(format_summary(result))
    return EXIT_GUARDED if result["blocked"] else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
