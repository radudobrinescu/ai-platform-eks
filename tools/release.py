#!/usr/bin/env python3
"""Release tooling for the product repo (design section 7).

  release.py cut    <vYYYY.MM.N> <branch>...    [--repo DIR] [--no-push]
  release.py notes  <vYYYY.MM.N> --base <ref>   [--repo DIR] [options] [--no-commit]
  release.py finish <vYYYY.MM.N> --github OWNER/REPO [--repo DIR] [--dry-run]
  release.py publish <vYYYY.MM.N> --github OWNER/REPO [--repo DIR]

`cut` creates release/<version> from origin/main, merges the listed branches
(update branches, fixes) with --no-ff, and pushes it. Coupled fixes that need
code changes are cherry-picked by hand afterwards. With --skip-conflicts a
branch that does not merge cleanly is left out (and reported) instead of
stopping the cut: it returns in a later cycle, after Renovate rebased it.

A candidate is a release only when its PR carries the label `publish`. Without
it, `finish` merges the candidate into main and stops: an integration cycle.
The notes and manifest of a version that was never tagged are drafts: the next
`notes` run replaces them (also when the month, and so the version, changed).

`notes` runs on the checked-out release branch. It reads every pin from the base
ref and from the working tree (harness/versions.py) and writes three product
files, then commits them to the release branch:

  docs/releases/<version>.md    version table old -> new, majors, manual steps,
                                supported Kubernetes versions
  releases/<version>.json       release manifest read by `./platformctl upgrade`
  docs/validated-versions.md    one row per release (a release reaches main, and
                                so this table, only after E2E passed)

These files ship to the public product repo, so they name components and
versions only: nothing about how updates are found, and no account identifiers
(a test enforces both).

`notes` also lists the platform changes since the base release: the pull
requests merged into main in that range (release and update branches
excluded), grouped by type from their conventional-commit titles. PR numbers
are left out on purpose: the notes travel to the public repo, where numbers
differ.

`finish` is the one command after the four E2E runs are green. Nothing is
committed after E2E: the release head that was tested is what gets merged. It
works on the remote refs (run it from any clone, a workflow or a laptop) and
  1. requires the four `e2e/*` statuses green on the release head, from runs
     that upgraded from the previous release with the llm-d tier on (a run with
     other inputs on the same commit does not count);
  2. requires main to be an ancestor of the release head, so the merge result
     is exactly the tested tree;
  3. waits for the PR's required checks, merges it (only at that head);
  4. tags the merge commit vYYYY.MM.N, after checking again that its tree is
     the tested tree; pushes the tag;
  5. creates the GitHub Release from the notes, deletes the release branch.
It resumes: when an earlier run stopped after the merge or after the tag, the
next run continues from there. --dry-run shows the steps and changes nothing.

`publish` creates the GitHub Release for an existing tag from the notes at that
tag (idempotent; used for releases tagged before `finish` did it).

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
PUBLISH_LABEL = "publish"     # on the candidate's PR: when E2E is green, also tag and publish (a release)
FORBIDDEN_RX = re.compile(r"renovate|lifecycle|\b\d{12}\b|arn:aws", re.I)
VALIDATED_INTRO = (
    "# Validated versions\n\n"
    "Each release is a set of component versions proven together on real clusters: a fresh\n"
    "install and an upgrade from the previous release, each with the default profile\n"
    "(self-managed Argo CD and kro) and with EKS Managed Capabilities. Every release listed\n"
    "here passed all four runs before it was tagged. Upgrade with `./platformctl upgrade`\n"
    "(see UPGRADING.md).\n\n"
)
VALIDATED_COLUMNS = ("Release", "Date", "Kubernetes", "vLLM", "LiteLLM", "Karpenter")
# Components whose major version moving needs attention in the notes.
_MAJOR_IGNORE_PREFIXES = ("ci/",)


class ReleaseError(RuntimeError):
    pass


class NotGreen(ReleaseError):
    """The release's E2E has not (all) passed on the release head. Not a fault of
    the tooling: `finish --if-green` (run after every E2E run) treats it as
    'nothing to do yet'."""


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

def _latest_release_tag(repo: str, ref: str) -> str | None:
    r = subprocess.run(["git", "-C", repo, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*.[0-9]*.[0-9]*", ref],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _stale_branches(repo: str, refs: list[str], main: str) -> str:
    """Group branches that do not contain the newest release tag on main. Renovate
    rebases a group branch whenever main moves (rebaseWhen: behind-base-branch),
    but only in its Monday window; a branch still behind the last release would
    merge stale pins, so cut refuses it. Empty string when all are fresh."""
    tag = _latest_release_tag(repo, main)
    if not tag:
        return ""
    bad = [r for r in refs if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", tag, r],
                                             capture_output=True).returncode != 0]
    if not bad:
        return ""
    return (f"{', '.join(bad)} do(es) not contain {tag}: Renovate has not rebased them onto the last release. "
            "Leave them out of this release, or tick 'rebase' for them on the Dependency Dashboard and retry.")


def cut(repo: str, version: str, branches: list[str], push: bool = True, remote: str = "origin",
        conflicts: list[str] | None = None) -> str:
    """`conflicts`: when a list is given, a branch that does not merge cleanly is
    appended to it and left out; without it, the first conflict is an error."""
    validate_version(version)
    if git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise ReleaseError("working tree has uncommitted changes")
    name = f"release/{version}"
    git(repo, "fetch", "--quiet", remote)
    if git(repo, "branch", "--list", name).strip() or git(repo, "branch", "-r", "--list", f"{remote}/{name}").strip():
        raise ReleaseError(f"{name} already exists")
    refs = [b if b.startswith(f"{remote}/") else f"{remote}/{b}" for b in branches]
    stale = _stale_branches(repo, refs, f"{remote}/main")
    if stale:
        raise ReleaseError(stale)
    git(repo, "switch", "--quiet", "-c", name, f"{remote}/main")
    for ref in refs:
        r = subprocess.run(["git", "-C", repo, "merge", "--no-ff", "--no-edit", ref], capture_output=True, text=True)
        if r.returncode != 0:
            git(repo, "merge", "--abort", check=False)
            if conflicts is None:
                raise ReleaseError(f"merge of {ref} conflicts; resolve by hand on {name}:\n{r.stdout}{r.stderr}")
            conflicts.append(ref[len(remote) + 1:])
    if push:
        git(repo, "push", "--quiet", "-u", remote, f"{name}:refs/heads/{name}")
    return name


# --------------------------------------------------------------------------- #
# notes
# --------------------------------------------------------------------------- #

def _manifest_versions(directory: str) -> set[str]:
    """Versions that have a releases/<version>.json in `directory`."""
    if not os.path.isdir(directory):
        return set()
    out = set()
    for name in os.listdir(directory):
        try:
            versions.version_key(name[:-5])
        except ValueError:
            continue
        if name.endswith(".json"):
            out.add(name[:-5])
    return out


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


MERGE_RX = re.compile(r"^Merge pull request #\d+ from [^/\s]+/(\S+)$")
CHANGE_GROUPS = (("fix", "Fixes"), ("feat", "Improvements"))


def platform_changes(repo: str, base: str, head: str = "HEAD") -> dict[str, list[str]]:
    """Titles of the pull requests merged into main between `base` and `head`,
    grouped as {"Fixes": [...], "Improvements": [...], "Other changes": [...]}.
    Release and update branches are skipped; the type prefix is dropped."""
    log = git(repo, "log", "--first-parent", "--merges", "--format=%s%x1f%b%x1e", f"{base}..{head}")
    groups: dict[str, list[str]] = {}
    for entry in log.split("\x1e"):
        if "\x1f" not in entry:
            continue
        subject, body = (x.strip() for x in entry.split("\x1f", 1))
        m = MERGE_RX.match(subject)
        if not m or m.group(1).startswith(("release/", "renovate/")):
            continue
        title = (body.splitlines() or [""])[0].strip()
        if not title:
            continue
        cm = re.match(r"^(\w+)(?:\([^)]*\))?!?:\s*(.+)$", title)
        kind, text = (cm.group(1), cm.group(2)) if cm else ("", title)
        label = dict(CHANGE_GROUPS).get(kind, "Other changes")
        if FORBIDDEN_RX.search(text):
            print(f"warning: change left out of the notes (not product-safe): {text}", file=sys.stderr)
            continue
        groups.setdefault(label, []).append(text[:1].upper() + text[1:])
    return groups


def render_notes(manifest: dict, rows: list, majors: list[str], unchanged: int, date: str,
                 changes: dict[str, list[str]] | None = None) -> str:
    k = manifest["kubernetes"]
    out = [f"# Release {manifest['version']}", "",
           f"Released {date}. Upgrades from: `{manifest['previous']}`.", "",
           "To upgrade an environment:", "", "```bash",
           "./platformctl upgrade <env>", "```", "",
           "Releases are applied one at a time, in order. See UPGRADING.md.", "",
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
    if changes:
        out += ["## Platform changes", ""]
        for label in [g for _, g in CHANGE_GROUPS] + ["Other changes"]:
            if changes.get(label):
                out += [f"{label}:", ""] + [f"- {t}" for t in changes[label]] + [""]
    out += ["## Component versions", "", "| Component | Before | After |", "|---|---|---|"]
    out += [f"| {c} | `{o}` | `{n}` |" for c, o, n in rows]
    if not rows:
        out.append("| (no version changes) | | |")
    out += ["", f"{unchanged} other pinned components are unchanged.", ""]
    return "\n".join(out)


def validated_row(version: str, date: str, new: dict, manifest: dict) -> str:
    return (f"| [{version}](releases/{version}.md) | {date} | {', '.join(manifest['kubernetes']['supported'])} "
            f"| `{new.get('vllm', '-')}` | `{new.get('image/litellm/litellm', '-')}` | `{new.get('karpenter', '-')}` |")


def _table_row(line: str) -> str:
    """A release row cut to the current columns (older files had a seventh, E2E)."""
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    return "| " + " | ".join(cells[:len(VALIDATED_COLUMNS)]) + " |"


def upsert_row(text: str, version: str, row: str, published: set[str] | None = None) -> str:
    """docs/validated-versions.md with `row` for `version`: replaced in place, or
    added on top (newest first). The file is generated as a whole: intro, header,
    one row per release. Rows written by older versions of this tool lose their
    E2E column (it only ever said 'pass': a release is tagged after four green
    runs or not at all). With `published`, rows of other versions that are not
    in it (drafts of a release that was never tagged) are dropped."""
    rows = [_table_row(ln) for ln in text.split("\n") if ln.startswith("| [v")]
    if published is not None:
        rows = [r for r in rows if r[len("| ["):r.index("]")] in published | {version}]
    key = f"| [{version}]("
    if any(r.startswith(key) for r in rows):
        rows = [row if r.startswith(key) else r for r in rows]
    else:
        rows.insert(0, row)
    header = "| " + " | ".join(VALIDATED_COLUMNS) + " |\n" + "|" + "---|" * len(VALIDATED_COLUMNS) + "\n"
    return VALIDATED_INTRO + header + "\n".join(rows) + "\n"


def status_details_from_github(repo_slug: str, sha: str) -> dict:
    """Read-only: newest status per E2E context on `sha` -> {state, target_url, description}."""
    out = subprocess.run(["gh", "api", f"repos/{repo_slug}/commits/{sha}/statuses", "--paginate"],
                         capture_output=True, text=True, check=True).stdout
    latest: dict = {}
    for st in json.loads(out or "[]"):  # the API returns newest first
        if st.get("context") in E2E_CONTEXTS:
            latest.setdefault(st["context"], {"state": st.get("state", ""), "target_url": st.get("target_url") or "",
                                              "description": st.get("description") or ""})
    return latest


def status_description(mode: str, profile: str, outcome: str, *, base: str = "", llmd: str = "") -> str:
    """Text of an E2E commit status. Besides the outcome it says what the run
    tested (upgrade: from which base; llm-d tier on or off), so `finish` can tell
    the release's own run from any other run on the same commit. e2e.yaml writes
    the same text (tests/test_workflows.py keeps the two in step)."""
    parts = [f"E2E {mode} ({profile}) {outcome}"]
    if mode == "upgrade" and base:
        parts.append(f"base={base}")
    if llmd:
        parts.append(f"llmd={llmd}")
    return "; ".join(parts)[:140]


def status_fields(description: str) -> dict:
    """{'base': ..., 'llmd': ...} as far as an E2E status description carries them.
    Strict: after the outcome, every '; '-separated part must be one known
    key=value, each key once. Anything else (a field given twice, free text)
    yields {} and the status counts as not saying what it tested."""
    out: dict = {}
    for part in (description or "").split("; ")[1:]:
        m = re.fullmatch(r"(base|llmd)=(\S+)", part)
        if not m or m.group(1) in out:
            return {}
        out[m.group(1)] = m.group(2)
    return out


def assert_product_safe(*texts: str) -> None:
    for t in texts:
        m = FORBIDDEN_RX.search(t)
        if m:
            raise ReleaseError(f"generated product file would contain {m.group(0)!r}; refusing to write it")


def notes(repo: str, version: str, base: str, *, supported: list[str] | None = None, recommended: str | None = None,
          required_tfvars: list[str] | None = None, manual_steps: list[str] | None = None,
          date: str | None = None, commit: bool = True) -> dict:
    validate_version(version)
    date = date or _dt.date.today().isoformat()
    with tempfile.TemporaryDirectory() as tmp:
        export_tree(repo, base, tmp)
        old = versions.read_versions(tmp)
        prev_version, prev = versions.current_release(tmp)
        published = _manifest_versions(os.path.join(tmp, "releases"))
    new = versions.read_versions(repo)
    # Manifests in the working tree that the base (the last published release)
    # does not have are drafts of a version that was never tagged. This run
    # replaces them; what was written into the newest one by hand is kept.
    drafts = sorted(_manifest_versions(os.path.join(repo, "releases")) - published - {version},
                    key=versions.version_key)
    carried = None
    for draft in drafts:
        with open(os.path.join(repo, "releases", f"{draft}.json"), encoding="utf-8") as fh:
            carried = json.load(fh)
        for rel in (f"releases/{draft}.json", f"docs/releases/{draft}.md"):
            if os.path.exists(os.path.join(repo, rel)):
                os.remove(os.path.join(repo, rel))
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
    existing = carried
    if os.path.exists(man_path):
        with open(man_path, encoding="utf-8") as fh:
            existing = json.load(fh)
    manifest = build_manifest(version, prev_version, supported, recommended,
                              required_tfvars or [], manual_steps or [], existing)
    rows, majors = version_table(old, new)
    unchanged = sum(1 for k in set(old) & set(new) if old[k] == new[k])
    notes_md = render_notes(manifest, rows, majors, unchanged, date, platform_changes(repo, base))
    vv_path = os.path.join(repo, "docs", "validated-versions.md")
    vv_old = open(vv_path, encoding="utf-8").read() if os.path.exists(vv_path) else ""
    vv_new = upsert_row(vv_old, version, validated_row(version, date, new, manifest), published)
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
        git(repo, "add", "-A", "--", "releases", "docs/releases", "docs/validated-versions.md")
        if git(repo, "diff", "--cached", "--name-only").strip():
            git(repo, "commit", "--quiet", "-m", f"docs(release): notes and manifest for {version}")
    return manifest


# --------------------------------------------------------------------------- #
# finish: the one command after E2E (merge, tag, publish). Nothing is committed
# after E2E; the tag is put on a merge commit whose tree is the tested tree.
# --------------------------------------------------------------------------- #

def require_all_green(details: dict, previous: str) -> None:
    """Every E2E context is `success`, and the runs are the release's own:
    upgrade jobs started from `previous` (the release this one upgrades from;
    'baseline' = first release, any base) and every job ran the llm-d tier."""
    bad = []
    for ctx in E2E_CONTEXTS:
        d = details.get(ctx) or {}
        state = d.get("state", "missing")
        if state != "success":
            bad.append(f"{ctx}={state}")
            continue
        fields = status_fields(d.get("description", ""))
        if "llmd" not in fields:
            bad.append(f"{ctx}=does not say what it tested (posted by an older workflow)")
        elif fields["llmd"] != "true":
            bad.append(f"{ctx}=ran without the llm-d tier")
        elif "/upgrade-" in ctx and previous != "baseline" and fields.get("base") != previous:
            bad.append(f"{ctx}=upgraded from {fields.get('base', 'an unknown base')}, not {previous}")
    if bad:
        raise NotGreen("not every E2E run passed on the tested commit: " + ", ".join(bad)
                           + ". Fix and rerun E2E (fresh,upgrade from the previous release, both profiles, "
                             "llm-d on); nothing merged or tagged.")


def gh(*args: str) -> str:
    """Run gh and return stdout; tests replace this."""
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout


def release_pr(repo_slug: str, version: str) -> dict:
    """The release PR: the open one, else the newest merged one (a finish that
    stopped half way resumes). A version name is reused by every cycle until one
    is published, so earlier cycles left merged PRs from the same branch name;
    closed, unmerged PRs are abandoned candidates. Neither counts."""
    out = gh("pr", "list", "--repo", repo_slug, "--state", "all", "--head", f"release/{version}",
             "--json", "number,state,headRefOid,baseRefName,mergeCommit,labels")
    prs = json.loads(out or "[]")
    opened = [p for p in prs if p.get("state") == "OPEN"]
    merged = sorted((p for p in prs if p.get("state") == "MERGED"), key=lambda p: p["number"])
    if len(opened) > 1:
        raise ReleaseError(f"{len(opened)} open PRs from release/{version}; close all but one")
    if not opened and not merged:
        raise ReleaseError(f"no open or merged PR from release/{version}")
    pr = opened[0] if opened else merged[-1]
    if pr["baseRefName"] != "main":
        raise ReleaseError(f"PR #{pr['number']} targets {pr['baseRefName']}, not main")
    return pr


def wait_mergeable(repo_slug: str, number: int, head: str, timeout: int = 900, poll: int = 20) -> None:
    """Wait until GitHub reports the PR CLEAN (required checks green on `head`)."""
    import time
    deadline = time.monotonic() + timeout
    while True:
        pr = json.loads(gh("pr", "view", str(number), "--repo", repo_slug, "--json", "mergeStateStatus,headRefOid"))
        if pr["headRefOid"] != head:
            raise ReleaseError(f"PR #{number} head moved to {pr['headRefOid'][:12]} while waiting; rerun E2E")
        state = pr["mergeStateStatus"]
        if state == "CLEAN":
            return
        if state in ("DIRTY", "BEHIND"):
            raise ReleaseError(f"PR #{number} is {state}: bring main into the release branch and rerun E2E")
        if time.monotonic() > deadline:
            raise ReleaseError(f"PR #{number} is still {state} after {timeout}s; check its required checks")
        time.sleep(poll)


def _is_ancestor(repo: str, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", older, newer],
                          capture_output=True).returncode == 0


def _tree(repo: str, rev: str) -> str:
    return git(repo, "rev-parse", f"{rev}^{{tree}}").strip()


def _tag_and_push(repo: str, version: str, commit: str, remote: str) -> None:
    """Annotated tag `version` on `commit`, pushed. A local tag left by a run that
    died before the push is reused when it points at the same commit."""
    if git(repo, "tag", "--list", version).strip():
        at = git(repo, "rev-parse", f"{version}^{{commit}}").strip()
        if at != commit:
            raise ReleaseError(f"local tag {version} points at {at[:12]}, not at the merge commit {commit[:12]}; "
                               f"delete it (git tag -d {version}) and rerun")
    else:
        git(repo, "tag", "-a", version, "-m", f"Release {version}", commit)
    git(repo, "push", "--quiet", remote, f"refs/tags/{version}")


def finish(repo: str, version: str, repo_slug: str, *, dry_run: bool = False, remote: str = "origin",
           poll: int = 20, publish_release: bool | None = None) -> dict:
    """`publish_release`: True = merge, tag and publish; False = merge only (an
    integration cycle); None = decided by the label `publish` on the PR."""
    validate_version(version)
    branch = f"release/{version}"
    res = {"dry_run": dry_run, "resumed": None, "pr": None, "tested": None, "merge": None, "tag": version,
           "published": None, "steps": []}

    # Already tagged on the remote: only the GitHub Release and the branch may be left.
    if git(repo, "ls-remote", "--tags", remote, f"refs/tags/{version}").strip():
        git(repo, "fetch", "--quiet", remote, f"+refs/tags/{version}:refs/tags/{version}",
            f"+refs/heads/main:refs/remotes/{remote}/main")
        if not _is_ancestor(repo, f"refs/tags/{version}", f"{remote}/main"):
            raise ReleaseError(f"tag {version} exists on {remote} but is not on {remote}/main: it was not put there "
                               "by `finish`. Not publishing it; remove the tag or fix main by hand.")
        res.update(resumed="tagged", merge=git(repo, "rev-parse", f"{version}^{{commit}}").strip(),
                   steps=[f"publish: GitHub Release {version} from docs/releases/{version}.md (if missing)",
                          f"delete: {remote}/{branch} (if still there)"])
        if not dry_run:
            res["published"] = publish(repo, version, repo_slug)
            git(repo, "push", "--quiet", remote, "--delete", branch, check=False)
        return res

    pr = release_pr(repo_slug, version)
    tested = pr["headRefOid"]
    res.update(pr=pr["number"], tested=tested)
    if publish_release is None:
        publish_release = PUBLISH_LABEL in {(lb.get("name") if isinstance(lb, dict) else lb)
                                            for lb in pr.get("labels") or []}
    if not publish_release:
        res["tag"] = None
    git(repo, "fetch", "--quiet", remote, f"+refs/heads/main:refs/remotes/{remote}/main")
    on_remote = (git(repo, "ls-remote", remote, f"refs/heads/{branch}").split() or [""])[0]
    if pr["state"] == "OPEN":
        git(repo, "fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
        if git(repo, "rev-parse", f"{remote}/{branch}").strip() != tested:
            raise ReleaseError(f"{remote}/{branch} moved while reading PR #{pr['number']}; rerun")
    elif on_remote and on_remote != tested:
        # The merged PR belongs to an earlier cycle that used the same version name.
        raise ReleaseError(f"{remote}/{branch} ({on_remote[:12]}) has no PR of its own: the newest PR from that "
                           f"branch name, #{pr['number']}, is merged at {tested[:12]}. Open a PR for the branch.")
    previous = json.loads(git(repo, "show", f"{tested}:releases/{version}.json")).get("previous", "")
    require_all_green(status_details_from_github(repo_slug, tested), previous)

    steps = [f"check: the merge commit's tree is the tested tree {tested[:12]}"]
    if publish_release:
        steps += [f"tag: {version} on the merge commit, pushed to {remote}",
                  f"publish: GitHub Release {version} from docs/releases/{version}.md"]
    steps.append(f"delete: {remote}/{branch}")
    if pr["state"] == "OPEN":
        main = git(repo, "rev-parse", f"{remote}/main").strip()
        if not _is_ancestor(repo, main, tested):
            raise ReleaseError(
                f"main moved since {branch} was cut ({remote}/main {main[:12]} is not in the release head "
                f"{tested[:12]}), so the merge would be a tree E2E never tested. Bring main into {branch} and "
                "rerun E2E, or cut the release again.")
        steps = [f"wait: PR #{pr['number']} required checks on {tested[:12]}",
                 f"merge: PR #{pr['number']} into main (merge commit, only at {tested[:12]})"] + steps
    else:
        res["resumed"] = "merged"
    res["steps"] = steps
    if dry_run:
        return res

    if pr["state"] == "OPEN":
        wait_mergeable(repo_slug, pr["number"], tested, poll=poll)
        gh("pr", "merge", str(pr["number"]), "--repo", repo_slug, "--merge", "--match-head-commit", tested,
           "--delete-branch=false")
        pr = release_pr(repo_slug, version)
    merge = (pr.get("mergeCommit") or {}).get("oid")
    if pr["state"] != "MERGED" or not merge:
        raise ReleaseError(f"PR #{pr['number']} is not merged yet (state {pr['state']}); rerun in a moment")
    git(repo, "fetch", "--quiet", remote, f"+refs/heads/main:refs/remotes/{remote}/main")
    if not _is_ancestor(repo, merge, f"{remote}/main"):
        raise ReleaseError(f"merge commit {merge[:12]} of PR #{pr['number']} is not on {remote}/main; not tagging")
    if _tree(repo, merge) != _tree(repo, tested):
        raise ReleaseError(
            f"merge commit {merge[:12]} is not the tree E2E tested ({tested[:12]}): main moved while the release "
            f"was being merged. {version} is NOT tagged. Delete {remote}/{branch} and cut the release again; "
            "its E2E then covers what is on main.")
    res["merge"] = merge
    if publish_release:
        _tag_and_push(repo, version, merge, remote)
        res["published"] = publish(repo, version, repo_slug)
    git(repo, "push", "--quiet", remote, "--delete", branch, check=False)
    return res


def publish(repo: str, version: str, repo_slug: str) -> bool:
    """Create the GitHub Release for an existing tag, with the release notes at that
    tag as its body. Returns False when it already exists (idempotent)."""
    validate_version(version)
    exists = subprocess.run(["gh", "release", "view", version, "--repo", repo_slug], capture_output=True, text=True)
    if exists.returncode == 0:
        return False
    notes_md = git(repo, "show", f"{version}:docs/releases/{version}.md")
    if "## Platform changes" not in notes_md:
        # Releases tagged before notes listed them: add the section to the page only.
        prev = json.loads(git(repo, "show", f"{version}:releases/{version}.json")).get("previous", "")
        if prev and prev != "baseline" and git(repo, "tag", "--list", prev).strip():
            ch = platform_changes(repo, prev, version)
            if ch:
                lines = ["## Platform changes", ""]
                for label in [g for _, g in CHANGE_GROUPS] + ["Other changes"]:
                    if ch.get(label):
                        lines += [f"{label}:", ""] + [f"- {t}" for t in ch[label]] + [""]
                notes_md = notes_md.replace("## Component versions", "\n".join(lines) + "\n## Component versions", 1)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
        fh.write(notes_md)
        path = fh.name
    k8s = (json.loads(git(repo, "show", f"{version}:releases/{version}.json")).get("kubernetes") or {}).get("recommended")
    title = f"{version} · Kubernetes {k8s}" if k8s else version
    try:
        gh("release", "create", version, "--repo", repo_slug, "--verify-tag", "--title", title, "--notes-file", path)
    finally:
        os.unlink(path)
    return True


def canary_hint(version: str) -> str:
    return ("Canary (in the long-lived environment's checkout, branch env/<env>):\n"
            f"  git fetch origin --tags && git merge {version}\n"
            "  ./platformctl upgrade <env> --plan-only   # review, then:\n"
            "  ./platformctl upgrade <env> --yes\n"
            "  make -C terraform plan-all ENVIRONMENT=<env>   # expect no changes")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cut a release branch or generate its notes.")
    ap.add_argument("--repo", default=os.path.join(os.getcwd(), "subject"), help="product repo checkout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cut")
    c.add_argument("version")
    c.add_argument("branches", nargs="*", help="group branches to merge; none for a product-only release")
    c.add_argument("--no-push", action="store_true")
    c.add_argument("--skip-conflicts", action="store_true",
                   help="leave out a branch that does not merge cleanly (printed as 'conflict: <branch>')")
    n = sub.add_parser("notes")
    n.add_argument("version")
    n.add_argument("--base", required=True, help="ref of the previous release (or main for the first)")
    n.add_argument("--kubernetes-supported", default=None, help="comma list, e.g. 1.36,1.37")
    n.add_argument("--kubernetes-recommended", default=None)
    n.add_argument("--required-tfvar", action="append", default=[])
    n.add_argument("--manual-step", action="append", default=[])
    n.add_argument("--no-commit", action="store_true")
    pb = sub.add_parser("publish", help="create the GitHub Release of an existing tag from its notes")
    pb.add_argument("version")
    pb.add_argument("--github", required=True, metavar="OWNER/REPO")
    f = sub.add_parser("finish", help="after 4 green E2E runs: merge the release PR, tag, publish, delete the "
                                      "branch (resumes a finish that stopped half way)")
    f.add_argument("version")
    f.add_argument("--github", required=True, metavar="OWNER/REPO")
    f.add_argument("--dry-run", action="store_true", help="show the steps; change nothing")
    f.add_argument("--publish", dest="publish_release", action="store_const", const=True, default=None,
                   help="tag and publish, whatever the PR's labels say")
    f.add_argument("--no-publish", dest="publish_release", action="store_const", const=False,
                   help="merge only (integration cycle), whatever the PR's labels say")
    f.add_argument("--if-green", action="store_true",
                   help="exit 0 without doing anything when E2E has not passed on the release head (for automation "
                        "that runs after every E2E run)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "publish":
            made = publish(a.repo, a.version, a.github)
            print(f"{'created' if made else 'already exists:'} GitHub Release {a.version}")
            return 0
        if a.cmd == "finish":
            try:
                res = finish(a.repo, a.version, a.github, dry_run=a.dry_run, publish_release=a.publish_release)
            except NotGreen as e:
                if not a.if_green:
                    raise
                print(f"not finishing {a.version} yet: {e}")
                return 0
            resumed = {"merged": " (PR already merged)", "tagged": " (already tagged)"}.get(res["resumed"], "")
            if res["dry_run"]:
                print(f"finish {a.version} would{resumed}:")
                print("\n".join(f"  - {st}" for st in res["steps"]))
            elif res["tag"]:
                print(f"finished {a.version}{resumed}: tag {res['tag']} on {res['merge'][:12]}, GitHub Release "
                      f"{'created' if res['published'] else 'already there'}, release branch deleted")
            else:
                print(f"integrated {a.version}{resumed}: merged as {res['merge'][:12]}, not tagged (no `{PUBLISH_LABEL}` "
                      "label), release branch deleted")
            if res["tag"]:
                print(canary_hint(a.version))
            return 0
        if a.cmd == "cut":
            conflicts: list[str] | None = [] if a.skip_conflicts else None
            print(cut(a.repo, a.version, a.branches, push=not a.no_push, conflicts=conflicts))
            for branch in conflicts or []:
                print(f"conflict: {branch}")
            return 0
        sup = [s.strip() for s in a.kubernetes_supported.split(",")] if a.kubernetes_supported else None
        m = notes(a.repo, a.version, a.base, supported=sup, recommended=a.kubernetes_recommended,
                  required_tfvars=a.required_tfvar, manual_steps=a.manual_step, commit=not a.no_commit)
        print(json.dumps(m, indent=2))
        return 0
    except ReleaseError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
