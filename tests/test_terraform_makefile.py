"""terraform/Makefile run for real against a fake `terraform`: no AWS, no network.

Each test copies the real Makefile into a throwaway tree with one stage and
puts fake `terraform` and `aws` executables first on PATH (the same approach as
test_platformctl_upgrade.py). The fake records every call and can be told to
fail on one of them, so the tests assert what ran after a failure.

The recipes pipe Terraform into `tee`. Without `pipefail` the exit status of
such a line is tee's, so a failed command looked like a success and the next
step ran anyway. GNU make 3.81 (macOS) has neither `.SHELLFLAGS` nor
`.ONESHELL`, so the recipes must set `pipefail` themselves.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import List, Optional

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ENV_NAME = "test"
STAGE = "30.eks/30.cluster"

FAKE_TERRAFORM = r"""#!/bin/sh
echo "terraform $*" >> "$FAKE_LOG"
case " $* " in
  *" ${FAKE_FAIL_ON:-<none>} "*) echo "FAKE terraform: failing on ${FAKE_FAIL_ON}" >&2; exit 1 ;;
esac
exit 0
"""

# The account and region lookups are pre-set through the environment, so any
# AWS call is a bug.
FAKE_AWS = r"""#!/bin/sh
echo "aws $*" >> "$FAKE_LOG"
exit 1
"""


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Tree:
    """A terraform/ directory holding the real Makefile and one cluster stage."""

    def __init__(self, tmp: Path, with_tfvars: bool = True) -> None:
        self.tf = tmp / "terraform"
        self.log = tmp / "calls.log"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        write_exec(self.bin / "terraform", FAKE_TERRAFORM)
        write_exec(self.bin / "aws", FAKE_AWS)
        self.log.write_text("")

        self.stage = self.tf / STAGE
        self.stage.mkdir(parents=True)
        shutil.copy(os.path.join(REPO, "terraform", "Makefile"), str(self.tf / "Makefile"))
        (self.stage / "backend.tf").write_text('terraform {\n  backend "s3" {}\n}\n')
        # The Makefile runs the targeted first apply for the stage that declares module "eks".
        (self.stage / "main.tf").write_text('module "eks" {\n  source = "./none"\n}\n')
        vars_dir = self.tf / "00.global" / "vars"
        vars_dir.mkdir(parents=True)
        if with_tfvars:
            (vars_dir / "{}.tfvars".format(ENV_NAME)).write_text('region = "eu-central-1"\n')

    def make(self, *args: str, **env_over: str) -> "subprocess.CompletedProcess[str]":
        env = dict(os.environ)
        env.update(
            PATH="{}:{}".format(self.bin, os.environ.get("PATH", "")),
            FAKE_LOG=str(self.log),
            AWS_REGION="eu-central-1",
            AWS_ACCOUNT_ID="123456789012",
            TFSTATE_REGION="eu-central-1",
        )
        env.pop("MAKEFLAGS", None)
        env.update(env_over)
        return subprocess.run(["make", "-C", str(self.tf)] + list(args) + ["ENVIRONMENT=" + ENV_NAME],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=120)

    def terraform_calls(self, word: Optional[str] = None) -> List[str]:
        lines = [ln for ln in self.log.read_text().splitlines() if ln.startswith("terraform ")]
        return [ln for ln in lines if word is None or " {} ".format(word) in ln + " "]


@pytest.fixture
def tree(tmp_path: Path) -> Tree:
    return Tree(tmp_path)


def test_apply_runs_the_targeted_cluster_apply_and_then_the_full_apply(tree: Tree) -> None:
    p = tree.make("apply", "MODULE=./" + STAGE)
    assert p.returncode == 0, p.stdout
    applies = tree.terraform_calls("apply")
    assert len(applies) == 2, applies
    assert "-target=module.eks" in applies[0] and "-target=" not in applies[1]
    assert tree.terraform_calls("init") and tree.terraform_calls("workspace")
    assert not [ln for ln in tree.log.read_text().splitlines() if ln.startswith("aws ")]


def test_failed_targeted_cluster_apply_stops_before_the_full_apply(tree: Tree) -> None:
    p = tree.make("apply", "MODULE=./" + STAGE, FAKE_FAIL_ON="-target=module.eks")
    assert p.returncode != 0, p.stdout
    assert "targeted module.eks apply failed" in p.stdout
    applies = tree.terraform_calls("apply")
    assert len(applies) == 1 and "-target=module.eks" in applies[0], applies


def test_failed_full_apply_fails_the_target(tree: Tree) -> None:
    p = tree.make("apply", "MODULE=./" + STAGE, FAKE_FAIL_ON="-auto-approve")
    assert p.returncode != 0, p.stdout


def test_failed_init_stops_the_stage(tree: Tree) -> None:
    p = tree.make("plan", "MODULE=./" + STAGE, FAKE_FAIL_ON="init")
    assert p.returncode != 0, p.stdout
    assert tree.terraform_calls("init")
    assert not tree.terraform_calls("workspace") and not tree.terraform_calls("plan")


def test_missing_var_file_stops_before_terraform_runs(tmp_path: Path) -> None:
    tree = Tree(tmp_path, with_tfvars=False)
    p = tree.make("plan", "MODULE=./" + STAGE)
    assert p.returncode != 0, p.stdout
    assert "{}.tfvars does not exist".format(ENV_NAME) in p.stdout
    assert tree.terraform_calls() == []


def test_clean_keeps_the_committed_lock_files(tree: Tree) -> None:
    (tree.stage / ".terraform").mkdir()
    (tree.stage / ".terraform" / "terraform.tfstate").write_text("{}")
    lock = tree.stage / ".terraform.lock.hcl"
    lock.write_text("# provider lock\n")
    p = tree.make("clean")
    assert p.returncode == 0, p.stdout
    assert not (tree.stage / ".terraform").exists()
    assert lock.exists(), "make clean deleted .terraform.lock.hcl, which is committed on purpose"
