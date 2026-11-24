"""Optional Python packages and what the dashboard loses without each.

The init container installs the packages at pod start and writes
<pydeps>/INSTALL_FAILED with pip's error when that fails. The dashboard then
starts anyway (degraded) and says what is missing instead of failing silently.
"""
from __future__ import annotations

import importlib
import logging
import os
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
    install_error: str | None = None

    def has(self, name: str) -> bool:
        return name not in self.missing

    def degraded(self) -> list[dict]:
        out = [{"package": EFFECTS[m][0], "effect": EFFECTS[m][1]} for m in sorted(self.missing)]
        if self.install_error and not out:
            out.append({"package": "(install)", "effect": "the package install reported an error; all imports work"})
        return out


def check(pydeps_dir: str, importer=importlib.import_module) -> Deps:
    missing = set()
    for name in EFFECTS:
        try:
            importer(name)
        except ImportError:
            missing.add(name)
    error = None
    marker = os.path.join(pydeps_dir, "INSTALL_FAILED")
    if os.path.exists(marker):
        with open(marker, encoding="utf-8", errors="replace") as f:
            error = f.read(2000).strip() or "unknown error"
    deps = Deps(frozenset(missing), error)
    if deps.degraded():
        log.warning("degraded", extra={"degraded": deps.degraded(), "install_error": error})
    return deps
