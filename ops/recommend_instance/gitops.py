"""GitOps deploy / undeploy: write (or delete) a model's InferenceEndpoint YAML
and commit + push it, so ArgoCD applies (or prunes) the change.

Both paths are surgical: they stage ONLY the one model file (never a blanket
`git add`), so an unrelated dirty working tree is left untouched. A push failure
leaves a local commit behind — reported, not hidden — so the user can retry.
"""

from __future__ import annotations

import os
import subprocess
import sys

from .ux import _palette, _should_use_colour

MODELS_DIR = "workloads/models"


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _repo_root() -> str:
    """Resolve the git repo root from this file's location (works regardless of
    the caller's CWD)."""
    here = os.path.dirname(os.path.abspath(__file__))
    r = _run(["git", "rev-parse", "--show-toplevel"], cwd=here)
    if r.returncode != 0:
        sys.exit(f"error: not inside a git repository ({r.stderr.strip()})")
    return r.stdout.strip()


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        # Non-interactive without --yes → refuse rather than act silently.
        sys.stderr.write("refusing to proceed without a TTY; pass --yes to confirm.\n")
        return False
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def _git_commit_push(root: str, rel_path: str, commit_msg: str, C: type) -> int:
    """Stage exactly `rel_path` (a create, modify, OR delete), commit, and push.
    Returns a process exit code. `-A` is required so a deletion stages cleanly —
    a plain `git add -- <path>` errors with 'pathspec did not match' once the
    file is gone, which is what silently broke --undeploy."""
    add = _run(["git", "add", "-A", "--", rel_path], cwd=root)
    if add.returncode != 0:
        print(f"{C.RED}git add failed:{C.RESET} {add.stderr.strip()}", file=sys.stderr)
        return 1

    # Nothing staged (e.g. file content identical / already absent) → no-op.
    staged = _run(["git", "diff", "--cached", "--quiet", "--", rel_path], cwd=root)
    if staged.returncode == 0:
        print(f"{C.YELLOW}No change to commit{C.RESET} — {rel_path} already in the "
              f"desired state. Nothing pushed.")
        return 0

    commit = _run(["git", "commit", "-m", commit_msg, "--", rel_path], cwd=root)
    if commit.returncode != 0:
        print(f"{C.RED}git commit failed:{C.RESET} {commit.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"{C.GREEN}✓ committed{C.RESET} {commit_msg}")

    push = _run(["git", "push"], cwd=root)
    if push.returncode != 0:
        print(f"{C.RED}git push failed:{C.RESET} {push.stderr.strip()}", file=sys.stderr)
        print(f"{C.DIM}The commit is saved locally — fix the remote/auth and "
              f"`git push` to apply it.{C.RESET}", file=sys.stderr)
        return 1
    print(f"{C.GREEN}✓ pushed{C.RESET} — ArgoCD applies it within ~30s.")
    return 0


def deploy_model(name: str, yaml_path: str, yaml_body: str, commit_msg: str,
                 args) -> int:
    """Write the model YAML to the repo and commit + push it."""
    C = _palette(_should_use_colour(args))
    root = _repo_root()
    abs_path = os.path.join(root, yaml_path)

    action = "Overwrite" if os.path.exists(abs_path) else "Create"
    print(f"\n{C.BOLD}{action} {yaml_path}{C.RESET} and push so ArgoCD deploys "
          f"{C.BOLD}{name}{C.RESET}.")
    if not _confirm("Proceed?", args.yes):
        print("Aborted — nothing written.")
        return 1

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w") as f:
        f.write(yaml_body)
    print(f"{C.GREEN}✓ wrote{C.RESET} {yaml_path}")

    rc = _git_commit_push(root, yaml_path, commit_msg, C)
    if rc == 0:
        print(f"\n{C.DIM}Watch it come up:{C.RESET} "
              f"kubectl get inferenceendpoints -n inference -w")
        print(f"{C.DIM}Remove it later:{C.RESET} "
              f"./ops/recommend-instance.py --undeploy {name}")
    return rc


def undeploy_model(name: str, args) -> int:
    """Delete a model's YAML from the repo and commit + push so ArgoCD prunes the
    InferenceEndpoint (which also deregisters it from LiteLLM via the finalizer).
    Needs no model lookup — operates purely on the file by name."""
    C = _palette(_should_use_colour(args))
    root = _repo_root()
    rel_path = f"{MODELS_DIR}/{name}.yaml"
    abs_path = os.path.join(root, rel_path)

    if not os.path.exists(abs_path):
        print(f"{C.RED}No such model file:{C.RESET} {rel_path}")
        existing = _list_models(root)
        if existing:
            print(f"{C.DIM}Deployed models in {MODELS_DIR}:{C.RESET}")
            for m in existing:
                print(f"  {m}")
        else:
            print(f"{C.DIM}No model YAMLs found in {MODELS_DIR}.{C.RESET}")
        return 2

    print(f"\n{C.BOLD}Delete {rel_path}{C.RESET} and push so ArgoCD removes "
          f"{C.BOLD}{name}{C.RESET} (the InferenceEndpoint is pruned and LiteLLM "
          f"deregisters it).")
    if not _confirm("Proceed?", args.yes):
        print("Aborted — file left in place.")
        return 1

    # Remove the file on disk only — staging is handled uniformly by
    # _git_commit_push (`git add -A`). Do NOT `git rm` here: that pre-stages the
    # deletion, and the subsequent add then had nothing to match → the commit was
    # skipped and the file was left deleted-but-not-committed (the bug this fixes).
    try:
        os.remove(abs_path)
    except OSError as e:
        print(f"{C.RED}could not remove {rel_path}:{C.RESET} {e}", file=sys.stderr)
        return 1

    rc = _git_commit_push(root, rel_path, f"chore: undeploy {name}", C)
    if rc == 0:
        print(f"\n{C.DIM}Confirm removal:{C.RESET} "
              f"kubectl get inferenceendpoints -n inference")
    return rc


def _list_models(root: str) -> list[str]:
    d = os.path.join(root, MODELS_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(
        f[:-5] for f in os.listdir(d)
        if f.endswith(".yaml") and not f.endswith(".example")
    )
