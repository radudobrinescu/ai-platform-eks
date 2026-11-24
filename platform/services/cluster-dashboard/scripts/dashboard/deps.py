"""Optional Python packages and what the dashboard loses without each.

The init container installs the packages at pod start; a failed install
fails that container (the kubelet retries it), so in the cluster they are all
present. Where one still cannot be imported (a local run, a broken package)
the dashboard starts anyway (degraded) and says what is missing instead of
failing silently.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field

log = logging.getLogger("dashboard.deps")

# import name -> (pip name, effect when missing)
EFFECTS = {
    "kubernetes": ("kubernetes", "no Kubernetes data: nodes, pods, models, alerts and GPU views are empty"),
    "psycopg": ("psycopg", "approvals and investigations are unavailable"),
    "jwt": ("pyjwt", "approve, dismiss and delete are refused (authentication cannot be verified)"),
    "boto3": ("boto3", "prices come from the shipped table instead of the AWS Price List"),
    "yaml": ("pyyaml", "Bedrock models declared in litellm-config are not listed"),
}


@dataclass(frozen=True)
class Deps:
    missing: frozenset = field(default_factory=frozenset)

    def has(self, name: str) -> bool:
        return name not in self.missing

    def degraded(self) -> list[dict]:
        return [{"package": EFFECTS[m][0], "effect": EFFECTS[m][1]} for m in sorted(self.missing)]


def check(importer=importlib.import_module) -> Deps:
    missing = set()
    for name in EFFECTS:
        try:
            importer(name)
        except ImportError:
            missing.add(name)
    deps = Deps(frozenset(missing))
    if deps.degraded():
        log.warning("degraded", extra={"degraded": deps.degraded()})
    return deps
