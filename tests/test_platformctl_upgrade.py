"""`platformctl upgrade` end to end against fakes: no AWS, no cluster, no network.

Each test builds a throwaway fork: a bare "upstream" repo (what Argo CD
syncs) and a working clone holding a copy of platformctl, the two Python
helpers, the real terraform/Makefile and two empty stages. Fake `terraform`,
`kubectl`, `aws` and `curl` executables come first on PATH and record every
call, so the tests can assert what ran, in which order, and that nothing was
applied or pushed when the command stops.

platformctl runs under /bin/bash when present (bash 3.2 on macOS).
"""

import json
import os
import shutil
import stat
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures", "plans")
BASH = "/bin/bash" if os.path.exists("/bin/bash") else "bash"
ENV_NAME = "test"

TFVARS = """\
region = "eu-central-1"
shared_config = {
  resources_prefix = "ai-platform"
}
cluster_config = {
  kubernetes_version = "1.36"
}
"""

FAKE_TERRAFORM = r"""#!/bin/sh
echo "terraform $*" >> "$FAKE_LOG"
case " $* " in
  *" apply "*)
    if [ "${FAKE_ALLOW_APPLY:-0}" != 1 ]; then echo "FAKE terraform: apply reached" >&2; exit 97; fi
    echo "apply-while-upstream=$(git -C "$FAKE_UPSTREAM" rev-parse refs/heads/main)" >> "$FAKE_LOG"
    exit 0 ;;
  *" plan "*)
    for a in "$@"; do case "$a" in -out=*) : > "${a#-out=}" ;; esac; done
    exit 0 ;;
  *" show "*)
    case "$1" in
      *"${FAKE_GUARDED_STAGE:-<none>}") cat "$FAKE_GUARDED_PLAN" ;;
      *) cat "$FAKE_CLEAN_PLAN" ;;
    esac
    exit 0 ;;
esac
exit 0
"""

FAKE_KUBECTL = r"""#!/bin/sh
echo "kubectl $*" >> "$FAKE_LOG"
case " $* " in
  *" config current-context "*) echo "${FAKE_CONTEXT:-test}" ;;
  *" config get-contexts "*) exit 0 ;;
  *" config view "*) echo "ai-platform-test" ;;
  *" get --raw /readyz "*) echo ok ;;
  *" get application bootstrap "*) echo "$FAKE_REPO_URL" ;;
  *" get applications -n argocd -o jsonpath="*)
    head=$(git -C "$FAKE_UPSTREAM" rev-parse refs/heads/main)
    printf 'bootstrap\tSynced\tHealthy\t%s\t%s\n' "$head" "$FAKE_REPO_URL"
    printf 'litellm\tSynced\tHealthy\t1.2.3\thttps://charts.example.com\n' ;;
  *" get secret litellm-secrets "*) printf 'c2stdGVzdA==' ;;
esac
exit 0
"""

# Every AWS call fails: the upgrade path must not need any (the Makefile's
# account/region lookups are pre-set through the environment).
FAKE_AWS = r"""#!/bin/sh
echo "aws $*" >> "$FAKE_LOG"
exit 1
"""

FAKE_CURL = r"""#!/bin/sh
echo "curl $*" >> "$FAKE_LOG"
echo '{"data":[]}'
"""


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True).stdout.strip()


def manifest(version, previous, **over):
    m = {
        "version": version,
        "previous": previous,
        "kubernetes": {"supported": ["1.36", "1.37"], "recommended": "1.37"},
        "required_tfvars": [],
        "manual_steps": [],
        "acknowledged_replacements": [],
    }
    m.update(over)
    return m


def write_exec(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Fork:
    """A forker's checkout plus its upstream, with fakes on PATH."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.upstream = tmp / "upstream.git"
        self.work = tmp / "fork"
        self.log = tmp / "calls.log"
        self.bin = tmp / "bin"
        self.bin.mkdir()
        for name, text in (("terraform", FAKE_TERRAFORM), ("kubectl", FAKE_KUBECTL),
                           ("aws", FAKE_AWS), ("curl", FAKE_CURL)):
            write_exec(self.bin / name, text)
        self.log.write_text("")

        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.upstream))
        w = self.work
        (w / "ops" / "lib").mkdir(parents=True)
        shutil.copy(os.path.join(REPO, "platformctl"), str(w / "platformctl"))
        for mod in ("plan_guard.py", "release_manifest.py"):
            shutil.copy(os.path.join(REPO, "ops", "lib", mod), str(w / "ops" / "lib" / mod))
        shutil.copy(os.path.join(REPO, ".gitignore"), str(w / ".gitignore"))
        vars_dir = w / "terraform" / "00.global" / "vars"
        vars_dir.mkdir(parents=True)
        shutil.copy(os.path.join(REPO, "terraform", "Makefile"), str(w / "terraform" / "Makefile"))
        shutil.copy(os.path.join(REPO, "terraform", "00.global", "vars", "example.tfvars"),
                    str(vars_dir / "example.tfvars"))
        for stage in ("10.networking", "30.eks/30.cluster"):
            d = w / "terraform" / stage
            d.mkdir(parents=True)
            (d / "backend.tf").write_text('terraform {\n  backend "s3" {}\n}\n')
        self.tfvars = vars_dir / "{}.tfvars".format(ENV_NAME)
        self.tfvars.write_text(TFVARS)  # gitignored, like a real fork

        git(tmp, "init", "-q", "-b", "main", str(w))
        git(w, "add", "-A")
        git(w, "commit", "-q", "-m", "product")
        git(w, "remote", "add", "origin", str(self.upstream))

    def release(self, version, previous, notes=None, **over):
        d = self.work / "releases"
        d.mkdir(exist_ok=True)
        (d / "{}.json".format(version)).write_text(json.dumps(manifest(version, previous, **over), indent=2))
        if notes is not None:
            nd = self.work / "docs" / "releases"
            nd.mkdir(parents=True, exist_ok=True)
            (nd / "{}.md".format(version)).write_text(notes)
        return self.commit("release {}".format(version))

    def commit(self, message):
        (self.work / "CHANGES.txt").open("a").write(message + "\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", message)
        return git(self.work, "rev-parse", "HEAD")

    def push(self):
        git(self.work, "push", "-q", "-u", "origin", "main")

    def upstream_head(self):
        return git(self.upstream, "rev-parse", "refs/heads/main")

    def run(self, *args, stdin="", **env_over):
        env = dict(os.environ)
        env.update(
            PATH="{}:{}".format(self.bin, os.environ.get("PATH", "")),
            FAKE_LOG=str(self.log),
            FAKE_UPSTREAM=str(self.upstream),
            FAKE_REPO_URL="https://git.example.com/fork.git",
            FAKE_CLEAN_PLAN=os.path.join(FIXTURES, "no-changes.json"),
            FAKE_GUARDED_PLAN=os.path.join(FIXTURES, "cluster-replace.json"),
            AWS_ACCOUNT_ID="123456789012",
            TFSTATE_REGION="eu-central-1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
        )
        env.pop("PLATFORM_ENV", None)
        env.update(env_over)
        return subprocess.run([BASH, str(self.work / "platformctl")] + list(args), cwd=str(self.work),
                              input=stdin, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=120)

    def calls(self, tool=None):
        lines = [ln for ln in self.log.read_text().splitlines() if ln]
        return [ln for ln in lines if tool is None or ln.startswith(tool + " ")]


@pytest.fixture
def fork(tmp_path):
    """Deployed: v2026.10.0 pushed. HEAD: v2026.11.0 merged locally, not pushed."""
    f = Fork(tmp_path)
    f.release("v2026.10.0", "baseline")
    f.push()
    f.release("v2026.11.0", "v2026.10.0", notes="# v2026.11.0\n\nComponent bumps.\n",
              manual_steps=["Read the notes."])
    return f


def assert_nothing_changed(f, deployed_head):
    assert not [c for c in f.calls("terraform") if " apply " in c + " "]
    assert f.upstream_head() == deployed_head


# --------------------------------------------------------------------------- #
# Usage                                                                       #
# --------------------------------------------------------------------------- #
def test_usage_lists_upgrade():
    p = subprocess.run([BASH, os.path.join(REPO, "platformctl"), "--help"],
                       stdout=subprocess.PIPE, universal_newlines=True)
    assert p.returncode == 0
    assert "./platformctl upgrade [ENV]" in p.stdout


def test_upgrade_help():
    p = subprocess.run([BASH, os.path.join(REPO, "platformctl"), "upgrade", "--help"],
                       stdout=subprocess.PIPE, universal_newlines=True)
    assert p.returncode == 0
    assert "--plan-only" in p.stdout and "--allow-replace" in p.stdout


# --------------------------------------------------------------------------- #
# Preconditions: stop with a clear message, touch nothing                     #
# --------------------------------------------------------------------------- #
def test_dirty_tree_refused(fork):
    deployed = fork.upstream_head()
    (fork.work / "platformctl").open("a").write("# local edit\n")
    p = fork.run("upgrade", ENV_NAME, "--yes")
    assert p.returncode == 1
    assert "uncommitted changes" in p.stderr and "platformctl" in p.stderr
    assert fork.calls("terraform") == []
    assert_nothing_changed(fork, deployed)


def test_gitignored_files_do_not_make_the_tree_dirty(fork):
    (fork.work / "terraform" / "tf-logs").mkdir()
    (fork.work / "terraform" / "tf-logs" / "x.log").write_text("log")
    p = fork.run("upgrade", ENV_NAME, "--plan-only")
    assert p.returncode == 0, p.stderr


def test_nothing_to_push_refused(fork):
    fork.push()
    p = fork.run("upgrade", ENV_NAME, "--yes")
    assert p.returncode == 1
    assert "nothing to upgrade" in p.stderr
    assert "status {} --check".format(ENV_NAME) in p.stderr
    assert fork.calls("terraform") == []


def test_no_upstream_refused(tmp_path):
    f = Fork(tmp_path)
    f.release("v2026.11.0", "baseline")
    p = f.run("upgrade", ENV_NAME, "--yes")
    assert p.returncode == 1
    assert "has no upstream" in p.stderr and "--set-upstream-to" in p.stderr


def test_missing_manifest_refused(tmp_path):
    f = Fork(tmp_path)
    f.push()  # deployed = baseline (no releases)
    deployed = f.upstream_head()
    f.commit("a change without a release manifest")
    p = f.run("upgrade", ENV_NAME, "--yes")
    assert p.returncode == 1
    assert "no release manifest" in p.stderr and "git merge <tag>" in p.stderr
    assert f.calls("terraform") == []
    assert_nothing_changed(f, deployed)


def test_two_releases_at_once_refused(fork):
    fork.release("v2026.12.0", "v2026.11.0")
    p = fork.run("upgrade", ENV_NAME, "--yes")
    assert p.returncode == 1
    assert "2 releases ahead" in p.stderr
    assert "git merge v2026.11.0 && ./platformctl upgrade test" in p.stderr
    assert fork.calls("terraform") == []


def test_missing_required_tfvar_refused(fork):
    git(fork.work, "reset", "-q", "--hard", "HEAD~1")  # redo the release with a required variable
    fork.release("v2026.11.0", "v2026.10.0", required_tfvars=["region", "vpc_cidr"])
    p = fork.run("upgrade", ENV_NAME, "--yes")
    assert p.returncode == 1
    assert "vpc_cidr" in p.stderr and "example.tfvars:" in p.stderr
    assert fork.calls("terraform") == []


def test_unsupported_kubernetes_refused(fork):
    fork.tfvars.write_text(TFVARS.replace('"1.36"', '"1.34"'))
    p = fork.run("upgrade", ENV_NAME, "--yes")
    assert p.returncode == 1
    assert 'kubernetes_version = "1.35"' in p.stderr
    assert fork.calls("terraform") == []


def test_wrong_kubectl_context_refused_before_any_plan(fork):
    p = fork.run("upgrade", ENV_NAME, "--yes", FAKE_CONTEXT="some-other-cluster")
    assert p.returncode == 1
    assert "./platformctl use {}".format(ENV_NAME) in p.stderr
    assert fork.calls("terraform") == []


def test_declining_the_notes_changes_nothing(fork):
    deployed = fork.upstream_head()
    p = fork.run("upgrade", ENV_NAME, stdin="no\n")
    assert p.returncode == 1
    assert "Component bumps." in p.stdout and "Read the notes." in p.stdout
    assert "aborted" in p.stderr
    assert fork.calls("terraform") == []
    assert_nothing_changed(fork, deployed)


# --------------------------------------------------------------------------- #
# --plan-only and the guard                                                   #
# --------------------------------------------------------------------------- #
def test_plan_only_plans_every_stage_and_changes_nothing(fork):
    deployed = fork.upstream_head()
    p = fork.run("upgrade", ENV_NAME, "--plan-only")
    assert p.returncode == 0, p.stdout + p.stderr
    plans = [c for c in fork.calls("terraform") if " plan " in c]
    assert [c.split()[1] for c in plans] == ["-chdir=./10.networking", "-chdir=./30.eks/30.cluster"]
    assert all("-out=" in c and "tf-plans/" in c for c in plans)
    assert fork.calls("kubectl") == [] and fork.calls("aws") == []
    assert "--plan-only: nothing changed" in p.stdout
    assert_nothing_changed(fork, deployed)


def test_plan_only_guarded_exits_3(fork):
    deployed = fork.upstream_head()
    p = fork.run("upgrade", ENV_NAME, "--plan-only", FAKE_GUARDED_STAGE="30.cluster")
    assert p.returncode == 3, p.stdout + p.stderr
    assert "GUARDED  replace  module.eks.aws_eks_cluster.this[0]" in p.stdout
    assert "    30.eks/30.cluster\n  Nothing was applied." in p.stderr
    assert_nothing_changed(fork, deployed)


def test_allow_replace_lets_the_guard_pass(fork):
    p = fork.run("upgrade", ENV_NAME, "--plan-only",
                 "--allow-replace", "30.eks/30.cluster:module.eks.aws_eks_cluster.this[0]",
                 FAKE_GUARDED_STAGE="30.cluster")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "allowed with --allow-replace" in p.stdout


def test_guarded_change_stops_before_any_apply(fork):
    deployed = fork.upstream_head()
    p = fork.run("upgrade", ENV_NAME, "--yes", FAKE_GUARDED_STAGE="30.cluster", FAKE_ALLOW_APPLY="1")
    assert p.returncode == 3
    assert_nothing_changed(fork, deployed)


# --------------------------------------------------------------------------- #
# Full run: Terraform before push, saved plans only                           #
# --------------------------------------------------------------------------- #
def test_full_upgrade_applies_then_pushes(fork):
    deployed = fork.upstream_head()
    head = git(fork.work, "rev-parse", "HEAD")
    p = fork.run("upgrade", ENV_NAME, "--yes", FAKE_ALLOW_APPLY="1")
    assert p.returncode == 0, p.stdout + p.stderr

    tf = [c for c in fork.calls("terraform") if " plan " in c or " apply " in c]
    kinds = [("apply" if " apply " in c else "plan", c.split()[1]) for c in tf]
    net, cl = "-chdir=./10.networking", "-chdir=./30.eks/30.cluster"
    # guard pass over every stage, then per stage: plan + apply.
    assert kinds == [("plan", net), ("plan", cl), ("plan", net), ("apply", net), ("plan", cl), ("apply", cl)]
    applies = [c for c in tf if " apply " in c]
    assert all("tf-plans/" in c and c.endswith(".tfplan") and "-target" not in c and "-var-file" not in c
               for c in applies)

    # BR4: nothing pushed while Terraform applied; pushed afterwards.
    seen = [ln.split("=", 1)[1] for ln in fork.calls() if ln.startswith("apply-while-upstream=")]
    assert seen == [deployed, deployed]
    assert fork.upstream_head() == head

    log = fork.calls()
    last_apply = max(i for i, ln in enumerate(log) if ln.startswith("terraform ") and " apply " in ln)
    refresh = next(i for i, ln in enumerate(log) if "annotate application bootstrap" in ln)
    assert refresh > last_apply
    assert "argocd.argoproj.io/refresh=hard" in log[refresh]
    assert "'test' is on v2026.11.0" in p.stdout
    assert "recommends Kubernetes 1.37" in p.stderr  # step 11 hint
    assert not list((fork.work / "terraform" / "tf-plans").glob("*"))  # saved plans removed after apply


def test_failed_apply_pushes_nothing(fork):
    deployed = fork.upstream_head()
    p = fork.run("upgrade", ENV_NAME, "--yes")  # fake terraform fails on apply
    assert p.returncode == 1
    assert "terraform apply failed for 10.networking" in p.stderr
    assert "Nothing pushed" in p.stderr
    assert fork.upstream_head() == deployed
    assert not [c for c in fork.calls("kubectl") if "annotate" in c]
