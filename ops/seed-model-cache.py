#!/usr/bin/env python3
"""
seed-model-cache.py — Pre-populate the ai-platform HuggingFace model cache in S3.

Downloads a HuggingFace model locally (via huggingface-cli) then uploads the
snapshot to `s3://{bucket}/hf/{model-id}/...`, mirroring the HF cache layout.
The Ray worker initContainer syncs from this prefix on pod startup, so the
next deploy of the same model skips the ~60s HuggingFace download.

Cache is opt-in: models that have not been seeded (or fail to seed) still
deploy correctly — vLLM falls back to a live HF download at pod startup.

Bucket name is read from the `platform-config` ConfigMap in the `inference`
namespace, or passed via --bucket.

Examples:
  ./ops/seed-model-cache.py HuggingFaceTB/SmolLM3-3B
  HF_TOKEN=hf_... ./ops/seed-model-cache.py google/gemma-3-4b-it
  ./ops/seed-model-cache.py --list              # show what's cached
  ./ops/seed-model-cache.py --purge org/model   # remove from cache

Requires:
  - `aws` CLI configured (credentials with s3:PutObject on the cache bucket)
  - `huggingface-cli` (`pip install huggingface_hub hf_transfer`)
  - `kubectl` configured — optional if --bucket is given
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

class C:
    """ANSI palette — disabled automatically when stdout is not a TTY."""
    _enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    RESET  = "\033[0m"  if _enabled else ""
    BOLD   = "\033[1m"  if _enabled else ""
    DIM    = "\033[2m"  if _enabled else ""
    GREEN  = "\033[32m" if _enabled else ""
    YELLOW = "\033[33m" if _enabled else ""
    RED    = "\033[31m" if _enabled else ""
    CYAN   = "\033[36m" if _enabled else ""


def die(msg: str, code: int = 1) -> None:
    print(f"{C.RED}error:{C.RESET} {msg}", file=sys.stderr)
    sys.exit(code)


def info(msg: str) -> None:
    print(f"{C.GREEN}✓{C.RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{C.YELLOW}⚠{C.RESET} {msg}")


def run(cmd: list[str], *, env: dict | None = None, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    """Thin subprocess wrapper with consistent error reporting."""
    try:
        return subprocess.run(
            cmd, check=check, env={**os.environ, **(env or {})},
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
        )
    except FileNotFoundError:
        die(f"binary not found: {cmd[0]}  (is it on $PATH?)")
    except subprocess.CalledProcessError as e:
        if capture:
            sys.stderr.write(e.stderr or "")
        die(f"command failed: {' '.join(cmd)}  (exit {e.returncode})")


def human_size(bytes_: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f} {unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f} PiB"


# --------------------------------------------------------------------------- #
# Discovery                                                                   #
# --------------------------------------------------------------------------- #

def resolve_bucket(explicit: str | None) -> str:
    """Explicit --bucket > platform-config ConfigMap > error."""
    if explicit:
        return explicit

    try:
        r = run(
            ["kubectl", "get", "configmap", "platform-config",
             "-n", "inference",
             "-o", "jsonpath={.data.modelCacheBucket}"],
            capture=True,
        )
        bucket = (r.stdout or "").strip()
        if bucket:
            return bucket
    except SystemExit:
        pass

    die("could not detect bucket — pass --bucket or configure platform-config ConfigMap "
        "(requires Terraform apply of the cluster stage).")


def resolve_region(explicit: str | None) -> str:
    if explicit:
        return explicit
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        if os.environ.get(var):
            return os.environ[var]
    r = run(["aws", "configure", "get", "region"], capture=True, check=False)
    return (r.stdout or "us-east-1").strip() or "us-east-1"


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #

def cmd_seed(args: argparse.Namespace) -> int:
    bucket = resolve_bucket(args.bucket)
    region = resolve_region(args.region)
    model  = args.model

    print(f"{C.BOLD}Model:{C.RESET}  {model}")
    print(f"{C.BOLD}Bucket:{C.RESET} s3://{bucket}/hf/{model}/  ({region})")

    # Check if already cached.
    r = run(["aws", "s3", "ls", f"s3://{bucket}/hf/{model}/", "--region", region],
            capture=True, check=False)
    if r.returncode == 0 and r.stdout.strip() and not args.force:
        warn(f"{model} already in cache — use --force to overwrite")
        return 0

    with tempfile.TemporaryDirectory(prefix="seed-hf-") as tmp:
        hf_home = Path(tmp)
        # NOTE: we deliberately do NOT use `--local-dir` here. Without it,
        # huggingface-cli downloads into $HF_HOME/hub/models--{ORG}--{MODEL}/
        # with the proper snapshots/{hash}/ + blobs/{sha256}/ + refs/main
        # layout that transformers/vLLM expect when loading from a cache.
        # Using --local-dir flattens files and produces a directory that
        # transformers does NOT recognise as an HF cache — vLLM would either
        # re-download from HF at runtime or fail with ActorDiedError.

        print(f"\n{C.BOLD}1/2  Downloading from HuggingFace...{C.RESET}")
        t0 = time.time()
        env = {"HF_HOME": str(hf_home)}
        try:
            import hf_transfer  # noqa: F401
            env["HF_HUB_ENABLE_HF_TRANSFER"] = "1"
        except ImportError:
            warn("hf_transfer not installed — using plain HF download "
                 "(pip install hf_transfer for ~5x faster downloads)")
        try:
            run([
                "huggingface-cli", "download", model,
            ], env=env)
        except SystemExit:
            raise
        t_hf = time.time() - t0

        # Locate the model directory produced by huggingface-cli.
        # HF uses `models--{ORG}--{MODEL}` with `--` as the path separator.
        model_dir_name = "models--" + model.replace("/", "--")
        model_dir = hf_home / "hub" / model_dir_name
        if not model_dir.exists():
            die(f"expected HF cache at {model_dir} but it's missing — "
                f"check huggingface-cli output above")

        size = sum(f.stat().st_size for f in model_dir.rglob("*")
                   if f.is_file() and not f.is_symlink())
        info(f"Downloaded {human_size(size)} in {t_hf:.0f}s")

        print(f"\n{C.BOLD}2/2  Uploading to s3://{bucket}/hf/{model}/...{C.RESET}")
        t0 = time.time()
        # --follow-symlinks: HF uses symlinks from snapshots/{hash}/*  ->
        # blobs/{sha256}/. S3 has no symlinks, so we dereference and upload
        # actual content. This doubles disk use on download (files appear
        # at both snapshot and blob paths), but transformers looks at
        # snapshots/{hash}/ and finds real files — which is what matters.
        run([
            "aws", "s3", "sync", str(model_dir), f"s3://{bucket}/hf/{model}/",
            "--region", region,
            "--follow-symlinks",
            "--only-show-errors",
        ])
        t_s3 = time.time() - t0
        info(f"Uploaded {human_size(size)} in {t_s3:.0f}s  "
             f"({size / max(t_s3 * 1024**2, 1e-6):.0f} MiB/s)")

    print(f"\n{C.GREEN}✓ Cache seeded.{C.RESET}  Next deploy of {C.CYAN}{model}{C.RESET} "
          f"will sync from S3 on pod startup.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    bucket = resolve_bucket(args.bucket)
    region = resolve_region(args.region)

    r = run(
        ["aws", "s3api", "list-objects-v2",
         "--bucket", bucket,
         "--prefix", "hf/",
         "--region", region,
         "--query", "Contents[].[Key, Size]",
         "--output", "text"],
        capture=True, check=False,
    )
    if r.returncode != 0 or not r.stdout.strip() or r.stdout.strip() == "None":
        print(f"{C.DIM}(cache is empty){C.RESET}")
        return 0

    # Aggregate by model-id (two path components after "hf/").
    by_model: dict[str, int] = {}
    for line in r.stdout.strip().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        key, size = parts[0], int(parts[1])
        # key = hf/org/model/.../file
        segs = key.split("/")
        if len(segs) < 3:
            continue
        model_id = f"{segs[1]}/{segs[2]}"
        by_model[model_id] = by_model.get(model_id, 0) + size

    print(f"{C.BOLD}Cached models in s3://{bucket}/hf/{C.RESET} ({region}):\n")
    print(f"  {'MODEL':<50}  SIZE")
    print(f"  {'-' * 50}  --------")
    for m, s in sorted(by_model.items()):
        print(f"  {m:<50}  {human_size(s)}")
    total = sum(by_model.values())
    print(f"  {C.DIM}{'-' * 50}  --------")
    print(f"  {'TOTAL':<50}  {human_size(total)}{C.RESET}")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    bucket = resolve_bucket(args.bucket)
    region = resolve_region(args.region)
    model  = args.model

    prefix = f"s3://{bucket}/hf/{model}/"
    print(f"{C.BOLD}Purging{C.RESET} {prefix}")
    if not args.yes:
        ans = input("Proceed? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    run(["aws", "s3", "rm", prefix, "--recursive", "--region", region])
    info(f"Removed {model} from cache.")
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    # Allow "seed" to be implicit: `./seed-model-cache.py org/model` → seed.
    # We do this BEFORE argparse so subparsers don't reject the positional.
    raw = argv if argv is not None else sys.argv[1:]
    known_subs = {"seed", "list", "purge"}
    # Find first non-flag argument.
    first_positional_idx = next(
        (i for i, a in enumerate(raw) if not a.startswith("-")), None
    )
    if first_positional_idx is not None and raw[first_positional_idx] not in known_subs:
        # Insert "seed" before the first positional.
        raw = raw[:first_positional_idx] + ["seed"] + raw[first_positional_idx:]

    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[1] if "Examples:" in (__doc__ or "") else "",
    )
    p.add_argument("--bucket", help="S3 bucket name (default: read from platform-config)")
    p.add_argument("--region", help="AWS region (default: $AWS_REGION or local aws config)")

    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("seed", help="Download a model from HF and upload it to S3 (default)")
    sp.add_argument("model", help="HuggingFace model ID (e.g. google/gemma-3-4b-it)")
    sp.add_argument("--force", action="store_true", help="Overwrite if already cached")
    sp.set_defaults(func=cmd_seed)

    sp = sub.add_parser("list", help="Show models currently in the S3 cache")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("purge", help="Remove a model from the S3 cache")
    sp.add_argument("model", help="HuggingFace model ID to purge")
    sp.add_argument("-y", "--yes", action="store_true", help="Do not prompt for confirmation")
    sp.set_defaults(func=cmd_purge)

    args = p.parse_args(raw)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
