#!/usr/bin/env python3
"""Release manifests: validation, release detection, and pre-upgrade checks.

Each release ships `releases/<version>.json` (format: releases/README.md).
`platformctl upgrade` uses this module to:

  * find the deployed release (recorded in the cluster by the last upgrade; for
    installs without that record, the branch upstream, what Argo CD syncs) and
    the target release (HEAD), reading `releases/*.json` straight from git
    objects, so nothing is checked out. A tree without manifests is the
    `baseline`;
  * list the releases between the deployed one and a release tag, in the order
    they must be applied, and refuse more than one release at a time;
  * check that `<env>.tfvars` sets every variable the release requires, and that
    its `kubernetes_version` is supported by the release.

Subcommands (all print actionable errors to stderr and exit 1 on failure):

  validate FILE...
  release-path --repo DIR [--deployed REV | --deployed-release V] [--target REV] [--env ENV]  -> "DEPLOYED TARGET"
  release-of --repo DIR --rev REV                                      -> newest release in REV, or "baseline"
  upgrade-path --repo DIR --deployed-release V --target TAG            -> releases to apply, one per line
  newest-tag --repo DIR --remote NAME                                  -> newest release tag of the remote
  check-tfvars --manifest F --tfvars F [--example F]
  check-kubernetes --manifest F --tfvars F [--env ENV]
  manual-steps --manifest F
  kubernetes-hint --manifest F --tfvars F [--env ENV]

Python 3.9 compatible, standard library only.
"""
import argparse
import json
import re
import subprocess
import sys
from typing import Dict, List, Optional, Sequence, Tuple

BASELINE = "baseline"
RELEASES_DIR = "releases"
VERSION_RE = re.compile(r"^v(\d{4})\.(0[1-9]|1[0-2])\.(0|[1-9]\d*)$")
K8S_RE = re.compile(r"^1\.(\d+)$")
REQUIRED_KEYS = ("version", "previous", "kubernetes", "required_tfvars",
                 "manual_steps", "acknowledged_replacements")
TFVAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
# How to undo a release merge that was not pushed. ORIG_HEAD, not @{u}: a fork
# may have commits of its own that are not pushed yet, and those must survive.
UNDO_MERGE = "git reset --hard ORIG_HEAD     # the commit before the merge; check with: git log --oneline -3"


class ReleaseError(Exception):
    """A check failed; the message tells the operator what to do."""


# --------------------------------------------------------------------------- #
# Versions and manifest validation                                            #
# --------------------------------------------------------------------------- #
def is_version(value: object) -> bool:
    return isinstance(value, str) and VERSION_RE.match(value) is not None


def version_key(version: str) -> Tuple[int, int, int]:
    m = VERSION_RE.match(version)
    if not m:
        raise ValueError("not a release version (vYYYY.MM.N): {!r}".format(version))
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def k8s_minor(version: str) -> int:
    m = K8S_RE.match(version)
    if not m:
        raise ValueError("not a Kubernetes minor version (1.NN): {!r}".format(version))
    return int(m.group(1))


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def validate(data: object, filename: Optional[str] = None) -> List[str]:
    """Return a list of problems (empty = valid)."""
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]
    errors: List[str] = []
    for key in REQUIRED_KEYS:
        if key not in data:
            errors.append("missing key '{}'".format(key))
    version = data.get("version")
    if "version" in data and not is_version(version):
        errors.append("'version' must look like v2026.11.0 (vYYYY.MM.N), got {!r}".format(version))
    if filename is not None and is_version(version):
        base = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if base != "{}.json".format(version):
            errors.append("file name {!r} does not match version {!r} (expected {}.json)".format(
                base, version, version))
    previous = data.get("previous")
    if "previous" in data:
        if previous != BASELINE and not is_version(previous):
            errors.append("'previous' must be a release version or '{}', got {!r}".format(BASELINE, previous))
        elif is_version(previous) and is_version(version) and version_key(previous) >= version_key(version):
            errors.append("'previous' ({}) must be older than 'version' ({})".format(previous, version))
    k8s = data.get("kubernetes")
    if "kubernetes" in data:
        if not isinstance(k8s, dict):
            errors.append("'kubernetes' must be an object with 'supported' and 'recommended'")
        else:
            supported = k8s.get("supported")
            if not (_is_str_list(supported) and supported and all(K8S_RE.match(v) for v in supported)):
                errors.append("'kubernetes.supported' must be a non-empty list like [\"1.36\", \"1.37\"]")
                supported = None
            rec = k8s.get("recommended")
            if not (isinstance(rec, str) and K8S_RE.match(rec)):
                errors.append("'kubernetes.recommended' must look like \"1.37\"")
            elif supported is not None and rec not in supported:
                errors.append("'kubernetes.recommended' ({}) must be one of 'kubernetes.supported'".format(rec))
    if "required_tfvars" in data:
        req = data.get("required_tfvars")
        if not (isinstance(req, list) and all(isinstance(v, str) and TFVAR_NAME_RE.match(v) for v in req)):
            errors.append("'required_tfvars' must be a list of variable names")
    if "manual_steps" in data and not (isinstance(data.get("manual_steps"), list)
                                       and _is_str_list(data.get("manual_steps"))):
        errors.append("'manual_steps' must be a list of non-empty strings")
    if "acknowledged_replacements" in data:
        acks = data.get("acknowledged_replacements")
        if not isinstance(acks, list):
            errors.append("'acknowledged_replacements' must be a list")
        else:
            for i, a in enumerate(acks):
                if not (isinstance(a, dict)
                        and all(isinstance(a.get(k), str) and a.get(k).strip()
                                for k in ("stage", "address", "reason"))):
                    errors.append("'acknowledged_replacements[{}]' needs non-empty 'stage', 'address' "
                                  "and 'reason'".format(i))
    return errors


def load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ReleaseError("cannot read release manifest {}: {}".format(path, exc))
    problems = validate(data, filename=path)
    if problems:
        raise ReleaseError("invalid release manifest {}:\n  - {}".format(path, "\n  - ".join(problems)))
    return data


# --------------------------------------------------------------------------- #
# Release detection from git objects                                          #
# --------------------------------------------------------------------------- #
def _git(repo: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)
    if proc.returncode != 0:
        raise ReleaseError("git {} failed: {}".format(" ".join(args), proc.stderr.strip()))
    return proc.stdout


def releases_in(repo: str, rev: str) -> Dict[str, dict]:
    """version -> manifest for every releases/<version>.json in the tree of `rev`."""
    out = _git(repo, "ls-tree", "--name-only", "{}:{}".format(rev, RELEASES_DIR)) \
        if _has_releases_dir(repo, rev) else ""
    found: Dict[str, dict] = {}
    for name in out.splitlines():
        if not name.endswith(".json") or not is_version(name[:-5]):
            continue
        path = "{}/{}".format(RELEASES_DIR, name)
        raw = _git(repo, "show", "{}:{}".format(rev, path))
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ReleaseError("{} at {} is not valid JSON: {}".format(path, rev, exc))
        problems = validate(data, filename=path)
        if problems:
            raise ReleaseError("invalid release manifest {} at {}:\n  - {}".format(
                path, rev, "\n  - ".join(problems)))
        found[data["version"]] = data
    return found


def _has_releases_dir(repo: str, rev: str) -> bool:
    proc = subprocess.run(["git", "-C", repo, "cat-file", "-t", "{}:{}".format(rev, RELEASES_DIR)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    return proc.returncode == 0 and proc.stdout.strip() == "tree"


def latest(versions: Sequence[str]) -> str:
    return max(versions, key=version_key) if versions else BASELINE


def release_of(repo: str, rev: str) -> str:
    """Highest release in the tree of `rev`, or 'baseline' when it has none."""
    return latest(list(releases_in(repo, rev)))


def _check_release_name(value: str, what: str) -> str:
    if value != BASELINE and not is_version(value):
        raise ReleaseError("{} {!r} is not a release (vYYYY.MM.N or '{}').".format(what, value, BASELINE))
    return value


def release_path(repo: str, deployed_rev: str = "@{u}", target_rev: str = "HEAD",
                 env: str = "<env>", deployed_release: Optional[str] = None) -> Tuple[str, str]:
    """(deployed, target) releases; raises ReleaseError unless exactly one release apart.

    The deployed release is `deployed_release` when given (what the cluster
    recorded), else the newest release in `deployed_rev` (the branch Argo CD
    syncs). The two differ when a release was pushed before it was upgraded to,
    and when several environments track one branch."""
    target_set = releases_in(repo, target_rev)
    if deployed_release is not None:
        deployed = _check_release_name(deployed_release, "the deployed release")
    else:
        deployed = latest(list(releases_in(repo, deployed_rev)))
    if not target_set:
        raise ReleaseError(
            "HEAD has no release manifest (releases/<version>.json), so there is no release to upgrade to.\n"
            "  Merge a release tag first:  git fetch upstream --tags && git merge <tag>")
    target = latest(list(target_set))
    newer = sorted((v for v in target_set if deployed == BASELINE or version_key(v) > version_key(deployed)),
                   key=version_key)
    if deployed != BASELINE and version_key(target) < version_key(deployed):
        raise ReleaseError("HEAD ({}) is older than the deployed release ({}); downgrades are not "
                           "supported.".format(target, deployed))
    if not newer or target == deployed:
        raise ReleaseError(
            "HEAD is on the same release as the deployed one ({}); there is nothing to upgrade.\n"
            "  To upgrade, merge the next release tag. To ship your own commits, push them as usual."
            .format(deployed))
    if len(newer) > 1:
        raise ReleaseError(
            "HEAD is {n} releases ahead of the deployed one ({d}): {lst}.\n"
            "  Releases are applied one at a time. Undo this merge and let upgrade merge and apply them in order:\n"
            "    {undo}\n"
            "    ./platformctl upgrade {e}".format(n=len(newer), d=deployed, lst=", ".join(newer), e=env,
                                                   undo=UNDO_MERGE))
    previous = target_set[target]["previous"]
    if previous != deployed:
        raise ReleaseError(
            "release {t} upgrades from {p}, but the deployed release is {d}.\n"
            "  Upgrade to {p} first (one release at a time).".format(t=target, p=previous, d=deployed))
    return deployed, target


def upgrade_path(repo: str, deployed: str, target_tag: str, older_ok: bool = False) -> List[str]:
    """Releases to apply, in order, to get from `deployed` to the release tag
    `target_tag`. Read from the manifests in the tag's tree (every release keeps
    the manifests of the ones before it), following `previous`. Empty when
    `deployed` is the target. A target older than `deployed` is an error
    (downgrade) unless `older_ok`: the caller only asked for 'the newest
    published release' and the environment is already past it."""
    _check_release_name(deployed, "the deployed release")
    if not is_version(target_tag):
        raise ReleaseError("{!r} is not a release tag (vYYYY.MM.N).".format(target_tag))
    proc = subprocess.run(["git", "-C", repo, "rev-parse", "-q", "--verify",
                           "refs/tags/{}^{{commit}}".format(target_tag)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if proc.returncode != 0:
        raise ReleaseError("there is no release tag {} (fetch the releases: git fetch upstream --tags).".format(target_tag))
    manifests = releases_in(repo, "refs/tags/{}".format(target_tag))
    if target_tag not in manifests:
        raise ReleaseError("tag {t} does not carry releases/{t}.json, so it is not a release.".format(t=target_tag))
    if deployed != BASELINE and version_key(target_tag) < version_key(deployed):
        if older_ok:
            return []
        raise ReleaseError("{} is older than the deployed release ({}); downgrades are not supported.".format(
            target_tag, deployed))
    hops = []  # type: List[str]
    current = target_tag
    while current != deployed:
        if current == BASELINE or current not in manifests:
            raise ReleaseError(
                "no upgrade path from {d} to {t}: the releases before {t} are {chain}, and {d} is not one of them."
                .format(d=deployed, t=target_tag, chain=", ".join(reversed(hops[1:])) or "none"))
        hops.append(current)
        current = manifests[current]["previous"]
    return list(reversed(hops))


def newest_tag(repo: str, remote: str) -> Optional[str]:
    """Newest release tag (vYYYY.MM.N) that `remote` has, or None."""
    out = _git(repo, "ls-remote", "--tags", "--refs", remote)
    names = [line.split("refs/tags/", 1)[1].strip() for line in out.splitlines() if "refs/tags/" in line]
    versions = [n for n in names if is_version(n)]
    return max(versions, key=version_key) if versions else None


# --------------------------------------------------------------------------- #
# tfvars checks                                                               #
# --------------------------------------------------------------------------- #
def _strip_comments(text: str) -> str:
    """Drop HCL comments (# and // to end of line, /* */ blocks). Strings that
    contain '#' or '//' are cut too; harmless here, only names at line start matter."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(re.split(r"#|//", line, maxsplit=1)[0] for line in text.splitlines())


def _assign_re(name: str) -> "re.Pattern[str]":
    return re.compile(r"^[ \t]*{}[ \t]*=".format(re.escape(name)), re.M)


def tfvars_sets(text: str, name: str) -> bool:
    """True when `name = ...` appears outside comments (top level or inside an object)."""
    return _assign_re(name).search(_strip_comments(text)) is not None


def tfvar_string(text: str, name: str) -> Optional[str]:
    """Value of the first uncommented `name = "value"`, or None."""
    m = re.search(r"^[ \t]*{}[ \t]*=[ \t]*\"([^\"]*)\"".format(re.escape(name)),
                  _strip_comments(text), re.M)
    return m.group(1) if m else None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise ReleaseError("cannot read {}: {}".format(path, exc))


def example_line(example_text: str, name: str) -> Optional[Tuple[int, str]]:
    """(line number, line) of `name` in example.tfvars: an assignment first, else a commented one."""
    lines = example_text.splitlines()
    plain = _assign_re(name)
    commented = re.compile(r"^[ \t]*#[ \t]*{}[ \t]*=".format(re.escape(name)))
    stripped = _strip_comments(example_text).splitlines()
    for i, line in enumerate(stripped):
        if plain.match(line):
            return i + 1, lines[i].rstrip()
    for i, line in enumerate(lines):
        if commented.match(line):
            return i + 1, line.rstrip()
    return None


def missing_tfvars(manifest: dict, tfvars_text: str) -> List[str]:
    return [n for n in manifest.get("required_tfvars") or [] if not tfvars_sets(tfvars_text, n)]


def check_tfvars(manifest: dict, tfvars_path: str, example_path: Optional[str] = None) -> None:
    missing = missing_tfvars(manifest, _read(tfvars_path))
    if not missing:
        return
    example_text = None
    if example_path:
        try:
            example_text = _read(example_path)
        except ReleaseError:
            example_text = None
    lines = ["{} needs variables that {} does not set:".format(manifest["version"], tfvars_path)]
    for name in missing:
        hit = example_line(example_text, name) if example_text is not None else None
        if hit:
            lines.append("  - {}   copy from {}:{}:\n      {}".format(name, example_path, hit[0], hit[1].strip()))
        else:
            lines.append("  - {}".format(name))
    lines.append("  Add them (see the release notes for their meaning), then rerun.")
    raise ReleaseError("\n".join(lines))


def current_kubernetes(tfvars_path: str) -> str:
    v = tfvar_string(_read(tfvars_path), "kubernetes_version")
    if v is None:
        raise ReleaseError("{} does not set cluster_config.kubernetes_version; set it to the version the "
                           "cluster runs (e.g. kubernetes_version = \"1.36\").".format(tfvars_path))
    return v


def check_kubernetes(manifest: dict, current: str, env: str = "<env>") -> None:
    supported = manifest["kubernetes"]["supported"]
    if current in supported:
        return
    version = manifest["version"]
    try:
        cur = k8s_minor(current)
    except ValueError:
        raise ReleaseError("kubernetes_version {!r} is not a Kubernetes minor version like \"1.36\".".format(current))
    lowest = min(supported, key=k8s_minor)
    if cur < k8s_minor(lowest):
        raise ReleaseError(
            "{v} supports Kubernetes {s}; this environment runs {c}.\n"
            "  Move Kubernetes up one minor at a time on the release you run now, then upgrade:\n"
            "    {undo}\n"
            "    # set kubernetes_version = \"1.{n}\" in {e}.tfvars, then:\n"
            "    ./platformctl up {e}\n"
            "  Repeat until you reach {lo}, then merge {v} again. See UPGRADING.md, 'Kubernetes versions'."
            .format(v=version, s=", ".join(supported), c=current, n=cur + 1, lo=lowest, e=env, undo=UNDO_MERGE))
    raise ReleaseError(
        "{v} supports Kubernetes {s}; this environment runs {c}, which is newer. Kubernetes cannot be "
        "downgraded; wait for a release that supports {c}.".format(v=version, s=", ".join(supported), c=current))


def kubernetes_hint(manifest: dict, current: str, env: str = "<env>") -> Optional[str]:
    rec = manifest["kubernetes"]["recommended"]
    try:
        cur, want = k8s_minor(current), k8s_minor(rec)
    except ValueError:
        return None
    if want <= cur:
        return None
    return ("{v} recommends Kubernetes {r}; this environment runs {c}. Upgrade one minor at a time:\n"
            "  set kubernetes_version = \"1.{n}\" in {e}.tfvars, run ./platformctl up {e}, and repeat "
            "until {r}.\n  See UPGRADING.md, 'Kubernetes versions'.".format(
                v=manifest["version"], r=rec, c=current, n=cur + 1, e=env))


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def _cmd_validate(args):
    rc = 0
    for path in args.files:
        try:
            load(path)
            print("ok  {}".format(path))
        except ReleaseError as exc:
            print(str(exc), file=sys.stderr)
            rc = 1
    return rc


def _cmd_release_path(args):
    deployed, target = release_path(args.repo, args.deployed, args.target, args.env, args.deployed_release)
    print("{} {}".format(deployed, target))
    return 0


def _cmd_release_of(args):
    print(release_of(args.repo, args.rev))
    return 0


def _cmd_upgrade_path(args):
    for version in upgrade_path(args.repo, args.deployed_release, args.target, args.older_ok):
        print(version)
    return 0


def _cmd_newest_tag(args):
    tag = newest_tag(args.repo, args.remote)
    if tag:
        print(tag)
    return 0


def _cmd_check_tfvars(args):
    check_tfvars(load(args.manifest), args.tfvars, args.example)
    return 0


def _cmd_check_kubernetes(args):
    check_kubernetes(load(args.manifest), current_kubernetes(args.tfvars), args.env)
    return 0


def _cmd_manual_steps(args):
    for i, step in enumerate(load(args.manifest)["manual_steps"], 1):
        print("  {}. {}".format(i, step))
    return 0


def _cmd_kubernetes_hint(args):
    hint = kubernetes_hint(load(args.manifest), current_kubernetes(args.tfvars), args.env)
    if hint:
        print(hint)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="release_manifest.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    p = sub.add_parser("validate", help="validate release manifest files")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=_cmd_validate)

    p = sub.add_parser("release-path", help="print 'DEPLOYED TARGET' releases")
    p.add_argument("--repo", default=".")
    p.add_argument("--deployed", default="@{u}")
    p.add_argument("--deployed-release", default=None,
                   help="the release the environment runs (from the cluster); overrides --deployed")
    p.add_argument("--target", default="HEAD")
    p.add_argument("--env", default="<env>")
    p.set_defaults(func=_cmd_release_path)

    p = sub.add_parser("release-of", help="print the newest release in a revision, or 'baseline'")
    p.add_argument("--repo", default=".")
    p.add_argument("--rev", required=True)
    p.set_defaults(func=_cmd_release_of)

    p = sub.add_parser("upgrade-path", help="print the releases to apply to reach a release tag, in order")
    p.add_argument("--repo", default=".")
    p.add_argument("--deployed-release", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--older-ok", action="store_true",
                   help="a target older than the deployed release means 'nothing to apply', not an error")
    p.set_defaults(func=_cmd_upgrade_path)

    p = sub.add_parser("newest-tag", help="print the newest release tag of a remote")
    p.add_argument("--repo", default=".")
    p.add_argument("--remote", required=True)
    p.set_defaults(func=_cmd_newest_tag)

    for name, func, help_text in (
            ("check-tfvars", _cmd_check_tfvars, "check required_tfvars are set"),
            ("check-kubernetes", _cmd_check_kubernetes, "check kubernetes_version is supported"),
            ("kubernetes-hint", _cmd_kubernetes_hint, "print a bump hint if a newer version is recommended")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--manifest", required=True)
        p.add_argument("--tfvars", required=True)
        if name == "check-tfvars":
            p.add_argument("--example")
        else:
            p.add_argument("--env", default="<env>")
        p.set_defaults(func=func)

    p = sub.add_parser("manual-steps", help="print the release's manual steps")
    p.add_argument("--manifest", required=True)
    p.set_defaults(func=_cmd_manual_steps)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except ReleaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
