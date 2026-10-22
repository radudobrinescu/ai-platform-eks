"""Read every component version pin from a product tree.

Used for the E2E report ("versions under test") and by tools/release.py for the
release notes (version table old -> new). Reads files only; never runs git,
Terraform or anything that needs credentials.

The named pins below are the coverage inventory (tests/test_renovate_coverage.py
asserts the same set is detected for upgrades). On top of them, every container
image tag in platform/**/*.yaml and every Terraform module / provider constraint
is collected generically, so a release table shows anything that moved.
"""
from __future__ import annotations

import glob
import json
import os
import re

# (key, file relative to the product root, regex with one group = the version)
NAMED_PINS: list[tuple[str, str, str]] = [
    ("vllm", "terraform/30.eks/30.cluster/locals.tf", r'vllm_image_tag\s*=\s*"([^"]+)"'),
    ("karpenter", "terraform/30.eks/30.cluster/karpenter.tf",
     r'chart\s*=\s*"karpenter"[^}]*?\bversion\s*=\s*"([^"]+)"'),
    ("kro (self-managed)", "terraform/30.eks/30.cluster/capabilities-selfmanaged.tf",
     r'chart\s*=\s*"kro"[^}]*?\bversion\s*=\s*"([^"]+)"'),
    ("argo-cd (self-managed)", "terraform/30.eks/30.cluster/capabilities-selfmanaged.tf",
     r'chart\s*=\s*"argo-cd"[^}]*?\bversion\s*=\s*"([^"]+)"'),
    ("aws-load-balancer-controller", "terraform/30.eks/35.addons/main.tf",
     r'aws_load_balancer_controller\s*=\s*\{[^}]*?\bchart_version\s*=\s*"([^"]+)"'),
    ("keda", "terraform/30.eks/35.addons/keda.tf", r'\bversion\s*=\s*"([^"]+)"'),
    ("gateway-api-inference-extension (CRDs)", "platform/services/inference-gateway/kustomization.yaml",
     r"gateway-api-inference-extension/[^\s?]*\?ref=(v?[0-9][^\s\"]*)"),
    ("llm-d-router-standalone", "platform/config/kro/llmd-endpoint.yaml",
     r'routerChartVersion:\s*"string\s*\|\s*default=(v?[0-9][^"\s]*)"'),
    ("python (PYTHON_IMAGE)", "platform/services/cluster-dashboard/pha-config.yaml",
     r'PYTHON_IMAGE:\s*"python:([^"]+)"'),
    ("kubernetes (default)", "terraform/00.global/vars/example.tfvars",
     r'(?m)^\s*kubernetes_version\s*=\s*"([^"]+)"'),
    ("ci/terraform", ".github/workflows/ci.yaml", r'terraform_version:\s*"?v?([0-9][^"\s]*)"?'),
    ("ci/kubeconform", ".github/workflows/ci.yaml", r"yannh/kubeconform/releases/download/v([0-9][^/\s]*)/"),
]

_APPSET = "argocd/bootstrap/templates/platform.yaml"
_PIP_FILES = [
    "platform/services/litellm-sync/deployment.yaml",
    "platform/services/cluster-dashboard/manifests.yaml",
    "platform/services/cluster-dashboard/pha-event-watcher.yaml",
    "platform/services/cluster-dashboard/scripts/backend.py",
    "platform/services/cluster-dashboard/scripts/event_watcher.py",
]
_PIP_RX = re.compile(r"(?<![\w.-])([A-Za-z][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==([0-9][A-Za-z0-9.]*)")
_IMAGE_RX = re.compile(r"(?m)^\s*(?:-\s*)?image:\s*\"?([\w./-]+):([\w.-]+)\"?\s*$")
_TF_MODULE_RX = re.compile(r'source\s*=\s*"([^"]+)"\s*\n\s*version\s*=\s*"([^"]+)"')


def _read(root: str, rel: str) -> str | None:
    try:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def _add(v: dict, key: str, value: str) -> None:
    """Record a version; a component pinned to two different values shows both."""
    old = v.get(key)
    if old is None:
        v[key] = value
    elif value not in old.split(", "):
        v[key] = ", ".join(sorted(old.split(", ") + [value]))


def read_versions(root: str) -> dict[str, str]:
    """Return {component: version} for the product tree at `root`."""
    v: dict[str, str] = {}
    for key, rel, rx in NAMED_PINS:
        text = _read(root, rel)
        m = re.search(rx, text, re.S) if text else None
        if m:
            _add(v, key, m.group(1))

    text = _read(root, _APPSET) or ""
    for m in re.finditer(r"chart:\s*(\S+)\s*\n\s*chartRepo:\s*\S+\s*\n\s*chartVersion:\s*\"?([^\"\s]+)", text):
        _add(v, f"chart/{m.group(1)}", m.group(2))

    for rel in _PIP_FILES:
        for m in _PIP_RX.finditer(_read(root, rel) or ""):
            _add(v, f"pip/{m.group(1).lower()}", m.group(2))

    ci = _read(root, ".github/workflows/ci.yaml") or ""
    for line in re.findall(r"pip install[^\n]*", ci):
        for m in _PIP_RX.finditer(line):
            _add(v, f"ci/{m.group(1).lower()}", m.group(2))

    for path in sorted(glob.glob(os.path.join(root, "platform", "**", "*.yaml"), recursive=True)):
        with open(path, encoding="utf-8") as fh:
            for m in _IMAGE_RX.finditer(fh.read()):
                _add(v, f"image/{m.group(1)}", m.group(2))

    for path in sorted(glob.glob(os.path.join(root, "terraform", "**", "*.tf"), recursive=True)):
        if "/.terraform/" in path:
            continue
        with open(path, encoding="utf-8") as fh:
            for m in _TF_MODULE_RX.finditer(fh.read()):
                if m.group(1).startswith((".", "/")):
                    continue
                kind = "provider" if m.group(1).count("/") == 1 else "module"
                _add(v, f"terraform-{kind}/{m.group(1)}", m.group(2))
    return v


# --------------------------------------------------------------------------- #
# Release manifests (releases/<version>.json, schema owned by the product's
# `platformctl upgrade`). The release of a tree is the highest version present.
# --------------------------------------------------------------------------- #

VERSION_RX = re.compile(r"^v(\d{4})\.(0[1-9]|1[0-2])\.(\d+)$")


def version_key(version: str) -> tuple[int, int, int]:
    m = VERSION_RX.match(version)
    if not m:
        raise ValueError(f"not a release version (vYYYY.MM.N): {version!r}")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def release_manifests(root: str) -> dict[str, dict]:
    out = {}
    for path in glob.glob(os.path.join(root, "releases", "*.json")):
        name = os.path.basename(path)[:-5]
        if not VERSION_RX.match(name):
            continue
        with open(path, encoding="utf-8") as fh:
            out[name] = json.load(fh)
    return out


def current_release(root: str) -> tuple[str, dict | None]:
    """(version, manifest) of the highest release in the tree, or ('baseline', None)."""
    manifests = release_manifests(root)
    if not manifests:
        return "baseline", None
    top = max(manifests, key=version_key)
    return top, manifests[top]


def k8s_key(v: str) -> tuple[int, int]:
    major, minor = v.split(".")[:2]
    return int(major), int(minor)
