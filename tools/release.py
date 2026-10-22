#!/usr/bin/env python3
"""Release tooling for the product repo (design section 7).

  release.py cut   <vYYYY.MM.N> <branch>...    [--repo DIR] [--no-push]
  release.py notes <vYYYY.MM.N> --base <ref>   [--repo DIR] [options] [--no-commit]

`cut` creates release/<version> from origin/main, merges the listed branches
(update branches, fixes) with --no-ff, and pushes it. Coupled fixes that need
code changes are cherry-picked by hand afterwards.

`notes` runs on the checked-out release branch. It reads every pin from the base
ref and from the working tree (harness/versions.py) and writes three product
files, then commits them to the release branch:

  docs/releases/<version>.md    version table old -> new, majors, manual steps,
                                supported Kubernetes versions
  releases/<version>.json       release manifest read by `./platformctl upgrade`
  docs/validated-versions.md    one row per release, filled from the E2E statuses

These files ship to the public product repo, so they name components and
versions only: nothing about how updates are found, and no account identifiers
(a test enforces both).

Kubernetes rule (enforced here): a release supports every Kubernetes version
of the previous release plus at most one newer minor, which must be exactly one
minor above the previous release's newest, so a forker always has a path.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "harness"))

import versions  # noqa: E402

E2E_CONTEXTS = ("e2e/fresh-default", "e2e/fresh-managed", "e2e/upgrade-default", "e2e/upgrade-managed")
STATUS_ICON = {"success": "pass", "failure": "fail", "error": "fail", "pending": "pending"}
FORBIDDEN_RX = re.compile(r"renovate|lifecycle|\b\d{12}\b|arn:aws", re.I)
VALIDATED_HEADER = (
    "# Validated versions\n\n"
    "Each release is a set of component versions proven together on a real cluster: a fresh\n"
    "install and an upgrade from the previous release, each with the default profile\n"
    "(self-managed Argo CD and kro) and with EKS Managed Capabilities. Upgrade with\n"
    "`./platformctl upgrade` (see UPGRADING.md), one release at a time.\n\n"
    "| Release | Date | Kubernetes | vLLM | LiteLLM | Karpenter | E2E (fresh / upgrade) |\n"
    "|---|---|---|---|---|---|---|\n"
)
# Components whose major version moving needs attention in the notes.
_MAJOR_IGNORE_PREFIXES = ("ci/",)


class ReleaseError(RuntimeError):
    pass


def validate_version(v: str) -> str:
    try:
        versions.version_key(v)
    except ValueError as e:
        raise ReleaseError(str(e)) from None
    return v


def git(repo: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


# --------------------------------------------------------------------------- #
# cut
# --------------------------------------------------------------------------- #

def cut(repo: str, version: str, branches: list[str], push: bool = True, remote: str = "origin") -> str:
    validate_version(version)
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise ReleaseError("working tree has uncommitted changes")
    name = f"release/{version}"
    git(repo, "fetch", "--quiet", remote)
    if git(repo, "branch", "--list", name).strip() or git(repo, "branch", "-r", "--list", f"{remote}/{name}").strip():
        raise ReleaseError(f"{name} already exists")
    git(repo, "switch", "--quiet", "-c", name, f"{remote}/main")
    for b in branches:
        ref = b if b.startswith(f"{remote}/") else f"{remote}/{b}"
        r = subprocess.run(["git", "-C", repo, "merge", "--no-ff", "--no-edit", ref], capture_output=True, text=True)
        if r.returncode != 0:
            git(repo, "merge", "--abort", check=False)
            raise ReleaseError(f"merge of {ref} conflicts; resolve by hand on {name}:\n{r.stdout}{r.stderr}")
    if push:
        git(repo, "push", "--quiet", "-u", remote, f"{name}:refs/heads/{name}")
    return name


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #

def export_tree(repo: str, ref: str, dest: str) -> None:
    data = subprocess.run(["git", "-C", repo, "archive", "--format=tar", ref], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:  # Python < 3.12 (the archive is our own git tree)
            tf.extractall(dest)


def _major(v: str) -> str | None:
    m = re.search(r"(\d+)", v)
    return m.group(1) if m else None


def version_table(old: dict, new: dict) -> tuple[list[tuple[str, str, str]], list[str]]:
    """(changed rows [(component, old, new)], majors [component])."""
    rows, majors = [], []
    for k in sorted(set(old) | set(new)):
        o, n = old.get(k, "-"), new.get(k, "-")
        if o == n:
            continue
        rows.append((k, o, n))
        if o != "-" and n != "-" and _major(o) != _major(n) and not k.startswith(_MAJOR_IGNORE_PREFIXES):
            majors.append(k)
    return rows, majors


def check_kubernetes(prev: dict | None, supported: list[str], recommended: str) -> None:
    for v in supported + [recommended]:
        if not re.fullmatch(r"\d+\.\d+", v):
            raise ReleaseError(f"Kubernetes versions are MAJOR.MINOR, got {v!r}")
    if recommended not in supported:
        raise ReleaseError(f"recommended Kubernetes {recommended} is not in supported {supported}")
    if not prev:
        return
    before = list(((prev.get("kubernetes") or {}).get("supported")) or [])
    dropped = sorted(set(before) - set(supported), key=versions.k8s_key)
    if dropped:
        raise ReleaseError(f"release must keep supporting the previous release's Kubernetes versions; dropped {dropped}")
    added = sorted(set(supported) - set(before), key=versions.k8s_key)
    if len(added) > 1:
        raise ReleaseError(f"at most one newer Kubernetes minor per release; adds {added}")
    if added and before:
        top = max(before, key=versions.k8s_key)
        major, minor = versions.k8s_key(top)
        if versions.k8s_key(added[0]) != (major, minor + 1):
            raise ReleaseError(f"the added Kubernetes version must be {major}.{minor + 1} (one minor above {top}), got {added[0]}")


def build_manifest(version: str, previous: str, supported: list[str], recommended: str,
                   required_tfvars: list[str], manual_steps: list[str], existing: dict | None) -> dict:
    ex = existing or {}
    return {
        "version": version,
        "previous": previous,
        "kubernetes": {"supported": sorted(set(supported), key=versions.k8s_key), "recommended": recommended},
        "required_tfvars": required_tfvars or ex.get("required_tfvars", []),
        "manual_steps": manual_steps or ex.get("manual_steps", []),
        # Filled by hand in the release PR when a replacement is expected and safe.
        "acknowledged_replacements": ex.get("acknowledged_replacements", []),
    }


def render_notes(manifest: dict, rows: list, majors: list[str], unchanged: int, date: str) -> str:
    k = manifest["kubernetes"]
    out = [f"# Release {manifest['version']}", "",
           f"Released {date}. Upgrades from: `{manifest['previous']}`.", "",
           "Upgrade one release at a time:", "", "```bash",
           "git fetch upstream --tags",
           f"git merge {manifest['version']}",
           "./platformctl upgrade <env>", "```", "",
           "## Kubernetes", "",
           f"Supported: {', '.join(k['supported'])}. Recommended: {k['recommended']}.", ""]
    out += ["## Before you upgrade", ""]
    if manifest["required_tfvars"]:
        out.append("New required variables (copy them from `terraform/00.global/vars/example.tfvars` into your tfvars):")
        out += [f"- `{n}`" for n in manifest["required_tfvars"]]
        out.append("")
    if manifest["manual_steps"]:
        out.append("Manual steps:")
        out += [f"- {s}" for s in manifest["manual_steps"]]
        out.append("")
    if not manifest["required_tfvars"] and not manifest["manual_steps"]:
        out += ["Nothing to do beyond `./platformctl upgrade`.", ""]
    if manifest["acknowledged_replacements"]:
        out += ["Expected replacements (the upgrade's plan check allows exactly these):", ""]
        out += [f"- `{r['stage']}` `{r['address']}`: {r['reason']}" for r in manifest["acknowledged_replacements"]]
        out.append("")
    if majors:
        out += ["## Major version changes", "", "Read the upstream release notes for:", ""]
        out += [f"- {m}" for m in majors]
        out.append("")
    out += ["## Component versions", "", "| Component | Before | After |", "|---|---|---|"]
    out += [f"| {c} | `{o}` | `{n}` |" for c, o, n in rows]
    if not rows:
        out.append("| (no version changes) | | |")
    out += ["", f"{unchanged} other pinned components are unchanged.", ""]
    return "\n".join(out)


def e2e_cell(statuses: dict) -> str:
    def icon(ctx):
        return STATUS_ICON.get(statuses.get(ctx, ""), "not run")
    return (f"default {icon('e2e/fresh-default')}, managed {icon('e2e/fresh-managed')} / "
            f"default {icon('e2e/upgrade-default')}, managed {icon('e2e/upgrade-managed')}")


def validated_row(version: str, date: str, new: dict, manifest: dict, statuses: dict) -> str:
    return (f"| [{version}](releases/{version}.md) | {date} | {', '.join(manifest['kubernetes']['supported'])} "
            f"| `{new.get('vllm', '-')}` | `{new.get('image/litellm/litellm', '-')}` | `{new.get('karpenter', '-')}` "
            f"| {e2e_cell(statuses)} |")


def upsert_row(text: str, version: str, row: str) -> str:
    if not text.strip():
        text = VALIDATED_HEADER
    lines = text.rstrip("\n").split("\n")
    key = f"| [{version}]("
    for i, ln in enumerate(lines):
        if ln.startswith(key):
            lines[i] = row
            return "\n".join(lines) + "\n"
    # Newest first: insert right after the table separator.
    sep = next((i for i, ln in enumerate(lines) if ln.startswith("|---")), None)
    if sep is None:
        lines += ["", *VALIDATED_HEADER.rstrip("\n").split("\n")[-2:]]
        sep = len(lines) - 1
    lines.insert(sep + 1, row)
    return "\n".join(lines) + "\n"


def statuses_from_github(repo_slug: str, sha: str) -> dict:
    """Read-only: latest state per E2E context on the release head."""
    out = subprocess.run(["gh", "api", f"repos/{repo_slug}/commits/{sha}/statuses", "--paginate"],
                         capture_output=True, text=True, check=True).stdout
    latest: dict = {}
    for st in json.loads(out or "[]"):
        latest.setdefault(st["context"], st["state"])  # API returns newest first
    return {k: v for k, v in latest.items() if k in E2E_CONTEXTS}


def assert_product_safe(*texts: str) -> None:
    for t in texts:
        m = FORBIDDEN_RX.search(t)
        if m:
            raise ReleaseError(f"generated product file would contain {m.group(0)!r}; refusing to write it")


def notes(repo: str, version: str, base: str, *, supported: list[str] | None = None, recommended: str | None = None,
          required_tfvars: list[str] | None = None, manual_steps: list[str] | None = None,
          statuses: dict | None = None, date: str | None = None, commit: bool = True) -> dict:
    validate_version(version)
    date = date or _dt.date.today().isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        export_tree(repo, base, tmp)
        old = versions.read_versions(tmp)
        prev_version, prev = versions.current_release(tmp)
    new = versions.read_versions(repo)
    if prev and versions.version_key(version) <= versions.version_key(prev_version):
        raise ReleaseError(f"{version} is not newer than the base release {prev_version}")

    default_k8s = new.get("kubernetes (default)")
    if supported is None:
        supported = list(((prev or {}).get("kubernetes") or {}).get("supported") or [])
        if default_k8s and default_k8s not in supported:
            supported.append(default_k8s)
    recommended = recommended or default_k8s or (max(supported, key=versions.k8s_key) if supported else "")
    check_kubernetes(prev, supported, recommended)

    man_path = os.path.join(repo, "releases", f"{version}.json")
    existing = None
    if os.path.exists(man_path):
        with open(man_path, encoding="utf-8") as fh:
            existing = json.load(fh)
    manifest = build_manifest(version, prev_version, supported, recommended,
                              required_tfvars or [], manual_steps or [], existing)
    rows, majors = version_table(old, new)
    unchanged = sum(1 for k in set(old) & set(new) if old[k] == new[k])
    notes_md = render_notes(manifest, rows, majors, unchanged, date)
    vv_path = os.path.join(repo, "docs", "validated-versions.md")
    vv_old = open(vv_path, encoding="utf-8").read() if os.path.exists(vv_path) else ""
    vv_new = upsert_row(vv_old, version, validated_row(version, date, new, manifest, statuses or {}))
    manifest_json = json.dumps(manifest, indent=2) + "\n"
    assert_product_safe(notes_md, manifest_json, vv_new)

    files = {f"docs/releases/{version}.md": notes_md, f"releases/{version}.json": manifest_json,
             "docs/validated-versions.md": vv_new}
    for rel, text in files.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    if commit:
        git(repo, "add", "--", *files)
        if git(repo, "diff", "--cached", "--name-only").strip():
            git(repo, "commit", "--quiet", "-m", f"docs(release): notes and manifest for {version}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cut a release branch or generate its notes.")
    ap.add_argument("--repo", default=os.path.join(os.getcwd(), "subject"), help="product repo checkout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cut")
    c.add_argument("version")
    c.add_argument("branches", nargs="+")
    c.add_argument("--no-push", action="store_true")
    n = sub.add_parser("notes")
    n.add_argument("version")
    n.add_argument("--base", required=True, help="ref of the previous release (or main for the first)")
    n.add_argument("--kubernetes-supported", default=None, help="comma list, e.g. 1.36,1.37")
    n.add_argument("--kubernetes-recommended", default=None)
    n.add_argument("--required-tfvar", action="append", default=[])
    n.add_argument("--manual-step", action="append", default=[])
    n.add_argument("--status", action="append", default=[], metavar="CONTEXT=STATE",
                   help="E2E status, e.g. e2e/fresh-default=success (repeatable)")
    n.add_argument("--statuses-from-github", metavar="OWNER/REPO", default=None,
                   help="read the E2E statuses of HEAD from GitHub (read-only)")
    n.add_argument("--no-commit", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "cut":
            print(cut(a.repo, a.version, a.branches, push=not a.no_push))
            return 0
        statuses = dict(s.split("=", 1) for s in a.status)
        if a.statuses_from_github:
            statuses.update(statuses_from_github(a.statuses_from_github, git(a.repo, "rev-parse", "HEAD").strip()))
        sup = [s.strip() for s in a.kubernetes_supported.split(",")] if a.kubernetes_supported else None
        m = notes(a.repo, a.version, a.base, supported=sup, recommended=a.kubernetes_recommended,
                  required_tfvars=a.required_tfvar, manual_steps=a.manual_step, statuses=statuses,
                  commit=not a.no_commit)
        print(json.dumps(m, indent=2))
        return 0
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
