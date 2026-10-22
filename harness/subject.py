"""The subject: a checkout of the product ref under test, driven only through its
stable interfaces (design section 3.1): `./platformctl`, git (as a forker uses
it), and the tfvars file a forker writes. Nothing here edits tracked files.

In dry-run mode every side effect is recorded as a planned step instead of run.
"""
from __future__ import annotations

import glob
import os
import subprocess

from common import log, redact, stream


class SubjectError(RuntimeError):
    pass


class Subject:
    def __init__(self, root: str, env: str, region: str, *, dry_run: bool = False):
        self.root = os.path.abspath(root)
        self.env, self.region, self.dry_run = env, region, dry_run
        self.planned: list[str] = []

    # -- execution ----------------------------------------------------------- #

    def _env(self) -> dict:
        e = dict(os.environ)
        e.update({"AWS_REGION": self.region, "AWS_DEFAULT_REGION": self.region,
                  "TF_IN_AUTOMATION": "1", "PLATFORM_ENV": self.env})
        return e

    def plan(self, step: str) -> None:
        self.planned.append(step)
        print(f"  [dry-run] {redact(step)}")

    def platformctl(self, *args: str, input_: str | None = None) -> int:
        cmd = ["./platformctl", *args]
        shown = " ".join(cmd) + (" < (stdin: env name)" if input_ else "")
        if self.dry_run:
            self.plan(shown)
            return 0
        log(f"$ {shown}")
        return stream(cmd, cwd=self.root, env=self._env(), input_=input_, prefix="    | ")

    def run(self, *cmd: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess:
        """Run a non-platformctl command in the subject (git, aws, python)."""
        if self.dry_run:
            self.plan(" ".join(cmd))
            return subprocess.CompletedProcess(list(cmd), 0, "", "")
        if not quiet:
            log(f"$ {' '.join(cmd)}")
        r = subprocess.run(list(cmd), cwd=self.root, env=self._env(), capture_output=True, text=True)
        if check and r.returncode != 0:
            raise SubjectError(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed ({r.returncode}): "
                               f"{redact(r.stderr.strip() or r.stdout.strip())[:400]}")
        return r

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return self.run("git", *args, check=check)

    # -- files a forker touches ---------------------------------------------- #

    def path(self, rel: str) -> str:
        return os.path.join(self.root, rel)

    def read(self, rel: str) -> str:
        with open(self.path(rel), encoding="utf-8") as fh:
            return fh.read()

    @property
    def tfvars_rel(self) -> str:
        return f"terraform/00.global/vars/{self.env}.tfvars"

    def write_tfvars(self, text: str) -> None:
        if self.dry_run:
            self.plan(f"write {self.tfvars_rel} (rendered below; gitignored on the subject)")
            return
        with open(self.path(self.tfvars_rel), "w", encoding="utf-8") as fh:
            fh.write(text)
        ignored = subprocess.run(["git", "check-ignore", "-q", self.tfvars_rel], cwd=self.root).returncode == 0
        if not ignored:
            raise SubjectError(f"{self.tfvars_rel} is not gitignored in the subject; refusing to continue "
                               "(it would make the tree dirty and could be committed)")

    def find_model_file(self, name: str, under: str = "workloads") -> str | None:
        hits = glob.glob(os.path.join(self.root, under, "**", f"{name}.yaml"), recursive=True)
        return os.path.relpath(hits[0], self.root) if hits else None

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()
