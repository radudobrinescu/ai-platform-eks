#!/usr/bin/env python3
"""E2E harness entry point: provision a product ref, prove it works, tear it down.

  e2e.py fresh   <subject-ref>                 [--profile default|managed] [options]
  e2e.py upgrade <base-ref> <candidate-ref>    [--profile default|managed] [options]

The harness lives on the `lifecycle` branch; the product ref under test is a
separate checkout (`--subject-dir`, default ./subject) driven only through its own
`./platformctl` (design section 3.1). Every run works on a throwaway GitOps
branch `e2e/<run-id>` that Argo CD tracks; commits made by `./platformctl`
(models, teams, the upgrade merge) land there and the branch is deleted at
teardown.

fresh    branch at <ref> -> render tfvars -> `up` -> smoke -> teardown
upgrade  branch at <base> -> render tfvars -> `up` -> seed + snapshot ->
         `git merge` <candidate> (local) -> missing required tfvars from the new
         example.tfvars -> the candidate's `./platformctl upgrade --yes` ->
         survival compare -> smoke -> optional Kubernetes minor bump + `up` +
         smoke -> teardown

A failure never aborts the run; teardown always runs unless --keep. Exit code 0
only if every step and check passed. --dry-run prints the planned steps and the
rendered tfvars and touches neither git nor AWS.

Reports (Markdown + JSON) are redacted: no account ids, ARNs or AWS hostnames.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import checks  # noqa: E402
import survival  # noqa: E402
import tfvars  # noqa: E402
import versions  # noqa: E402
from common import log, now_iso, redact  # noqa: E402
from subject import Subject, SubjectError  # noqa: E402

EXAMPLE_TFVARS = "terraform/00.global/vars/example.tfvars"
BRANCH_PREFIX = "e2e/"
DEFAULT_REGION = "eu-central-1"
DRY_RUN_CIDR = "203.0.113.10/32"   # TEST-NET-3, used only to render a dry-run preview
GIT_USER = ("ai-platform-e2e", "ai-platform-e2e@users.noreply.github.com")


# --------------------------------------------------------------------------- #
# Naming (design section 3.2)
# --------------------------------------------------------------------------- #

def env_name(mode: str, profile: str, run_number: str) -> str:
    """e2e-<m><p>-<n>: m = f fresh / u upgrade, p = d default / m managed."""
    if mode not in ("fresh", "upgrade") or profile not in tfvars.PROFILES:
        raise ValueError(f"bad mode/profile: {mode}/{profile}")
    if not re.fullmatch(r"\d{1,4}", str(run_number)):
        raise ValueError(f"run number must be 1-4 digits, got {run_number!r}")
    return f"e2e-{mode[0]}{profile[0]}-{run_number}"


def resources_prefix(tfvars_text: str) -> str:
    m = re.search(r'(?m)^\s*resources_prefix\s*=\s*"([^"]+)"', tfvars.strip_comments(tfvars_text))
    return m.group(1) if m else ""


def cluster_name(prefix: str, env: str) -> str:
    return f"{prefix}-{env}" if prefix else env


def model_cache_bucket_len(cluster: str) -> int:
    """Length of the longest derived AWS name: "<cluster>-<12-digit account>-model-cache"."""
    return len(cluster) + 1 + 12 + len("-model-cache")


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

@dataclass
class Report:
    mode: str
    profile: str
    env: str
    refs: dict
    started: str = field(default_factory=now_iso)
    finished: str = ""
    versions: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    survival: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.results) and all(r.ok or r.skipped for r in self.results)


def render_markdown(rep: Report) -> str:
    icon = "PASSED" if rep.passed else "FAILED"
    refs = ", ".join(f"{k} `{v}`" for k, v in rep.refs.items())
    lines = [f"## E2E {rep.mode} / {rep.profile} - `{rep.env}` - {icon}", "",
             f"Refs: {refs}  ", f"Started {rep.started}, finished {rep.finished or '-'}", ""]
    if rep.versions:
        cols = list(rep.versions)
        keys = sorted({k for v in rep.versions.values() for k in v})
        lines += ["### Component versions", "", "| Component | " + " | ".join(cols) + " |",
                  "|---|" + "---|" * len(cols)]
        for k in keys:
            lines.append(f"| {k} | " + " | ".join(f"`{rep.versions[c].get(k, '-')}`" for c in cols) + " |")
        lines.append("")
    lines += ["### Steps and checks", "", "| Domain | Check | Result | Time | Detail |", "|---|---|---|---|---|"]
    for r in rep.results:
        res = "skipped" if r.skipped else ("pass" if r.ok else "FAIL")
        detail = redact(r.detail).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r.domain} | {r.name} | {res} | {r.seconds:.0f}s | {detail} |")
    if rep.survival:
        lines += ["", "### Seeded state (upgrade survival)", "", "```json",
                  redact(json.dumps(rep.survival, indent=2, sort_keys=True)), "```"]
    lines += ["", "<sub>Account ids, ARNs and AWS hostnames are redacted.</sub>"]
    return "\n".join(lines) + "\n"


def to_json(rep: Report) -> str:
    d = {"mode": rep.mode, "profile": rep.profile, "env": rep.env, "refs": rep.refs,
         "started": rep.started, "finished": rep.finished, "passed": rep.passed,
         "versions": rep.versions, "survival": rep.survival,
         "results": [{**r.__dict__, "detail": r.detail} for r in rep.results]}
    return redact(json.dumps(d, indent=2))


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

class Runner:
    def __init__(self, a: argparse.Namespace):
        self.a = a
        self.dry = a.dry_run
        self.env = env_name(a.mode, a.profile, a.run_number)
        self.branch = BRANCH_PREFIX + self.env
        self.subject = Subject(a.subject_dir, self.env, a.region, dry_run=self.dry)
        refs = {"subject": a.ref} if a.mode == "fresh" else {"base": a.base, "candidate": a.candidate}
        self.report = Report(a.mode, a.profile, self.env, refs)
        self.provisioned = False
        self.branch_pushed = False
        self.cluster = ""
        self.ctx: checks.Ctx | None = None

    # -- bookkeeping --------------------------------------------------------- #

    def step(self, name: str, fn) -> bool:
        """Run one lifecycle step; record it like a check. Returns success."""
        if self.dry:
            print(f"\n- step {name}")
        else:
            log(f"[lifecycle] {name} ...")
        t0 = time.time()
        try:
            res = fn()
            ok, detail = res if isinstance(res, tuple) else (res is not False, "")
        except (Exception, SubjectError) as e:  # noqa: BLE001 - recorded, teardown still runs
            ok, detail = False, f"{type(e).__name__}: {e}"
        if not self.dry:
            self.report.results.append(checks.CheckResult("lifecycle", name, ok, str(detail), time.time() - t0))
            log(f"[lifecycle] {name}: {'PASS' if ok else 'FAIL'} {redact(str(detail))}")
        return ok

    def _platformctl_ok(self, *args: str, input_: str | None = None) -> tuple[bool, str]:
        rc = self.subject.platformctl(*args, input_=input_)
        return rc == 0, f"./platformctl {' '.join(args)} exited {rc}"

    # -- steps --------------------------------------------------------------- #

    def prepare_branch(self, ref: str) -> tuple[bool, str]:
        g = self.subject.git
        if not self.dry and not os.path.isdir(self.subject.path(".git")) and not os.path.isfile(self.subject.path(".git")):
            raise SubjectError(f"{self.subject.root} is not a git checkout of the product")
        if not self.dry and g("ls-remote", "--exit-code", "--heads", self.a.remote, self.branch, check=False).returncode == 0:
            raise SubjectError(f"remote branch {self.branch} already exists (left by a kept run?); delete it first")
        g("fetch", "--quiet", self.a.remote, ref)
        g("switch", "--quiet", "-C", self.branch, "FETCH_HEAD")
        g("config", "user.name", GIT_USER[0])
        g("config", "user.email", GIT_USER[1])
        g("push", "--quiet", "-u", self.a.remote, f"{self.branch}:refs/heads/{self.branch}")
        self.branch_pushed = True
        sha = "<sha>" if self.dry else self.subject.head()
        return True, f"{self.branch} at {ref} ({sha[:12]}) pushed; Argo CD will track it"

    def render_tfvars(self) -> tuple[bool, str]:
        idc = {"instance_arn": os.environ.get("E2E_IDC_INSTANCE_ARN", ""),
               "region": os.environ.get("E2E_IDC_REGION", ""),
               "group_id": os.environ.get("E2E_IDC_GROUP_ID", "")}
        if self.dry and self.a.profile == "managed" and not all(idc.values()):
            idc = {"instance_arn": idc["instance_arn"] or "arn:aws:sso:::instance/ssoins-EXAMPLE",
                   "region": idc["region"] or "<E2E_IDC_REGION>", "group_id": idc["group_id"] or "<E2E_IDC_GROUP_ID>"}
        text = tfvars.render(self.subject.read(EXAMPLE_TFVARS), region=self.a.region, repo_url=self.a.repo_url,
                             revision=self.branch, operator_cidr=self.a.operator_cidr or DRY_RUN_CIDR,
                             profile=self.a.profile, idc=idc)
        self.cluster = cluster_name(resources_prefix(text), self.env)
        if model_cache_bucket_len(self.cluster) > 63:
            raise ValueError(f"cluster name {self.cluster} makes the model-cache bucket name exceed 63 chars")
        self.subject.write_tfvars(text)
        if self.dry:
            print(f"\n  ---- {self.subject.tfvars_rel} (effective lines; comments omitted; redacted) ----")
            body = [ln.rstrip() for ln in tfvars.strip_comments(text).splitlines() if ln.strip()]
            print("\n".join("  " + ln for ln in redact("\n".join(body)).splitlines()))
            print("  ---- end ----")
        return True, f"{self.subject.tfvars_rel} rendered from the subject's example.tfvars (profile {self.a.profile})"

    def provision(self) -> tuple[bool, str]:
        self.provisioned = True
        ok, detail = self._platformctl_ok("up", self.env)
        if ok:
            self.subject.run("aws", "eks", "update-kubeconfig", "--name", self.cluster, "--region", self.a.region,
                             "--alias", self.env)
        return ok, detail

    def make_ctx(self) -> checks.Ctx:
        if self.ctx is None:
            self.ctx = checks.Ctx(self.subject, self.cluster, self.a.profile, self.a.with_llmd)
        return self.ctx

    def smoke(self, prefix: str = "") -> None:
        if self.dry:
            print(f"\n- smoke suite{f' ({prefix.rstrip(chr(47))})' if prefix else ''}: {len(checks.CHECKS)} checks")
            c = checks.Ctx(self.subject, self.cluster or "<cluster>", self.a.profile, self.a.with_llmd)
            for domain, name, _fn, when in checks.CHECKS:
                reason = when(c) if when else None
                print(f"    {domain:9} {prefix}{name}" + (f"  (skipped: {reason})" if reason else ""))
            return
        self.report.results += checks.run_suite(self.make_ctx(), prefix)

    def teardown(self) -> None:
        if self.a.keep:
            log(f"--keep: env {self.env} (cluster {self.cluster}) and branch {self.branch} are left running. "
                f"Tear down later with: ./platformctl down {self.env}")
            return
        if self.provisioned:
            self.step("teardown", lambda: self._platformctl_ok("down", self.env, input_=f"{self.env}\n"))
            self.step("orphan-sweep", lambda: self.subject.run(
                "python3", "ops/lib/sweep_env.py", "--env", self.env, "--cluster", self.cluster,
                "--region", self.a.region, check=False).returncode == 0)
            self.step("teardown-verify", self.verify_gone)
        if self.branch_pushed:
            self.step("delete-branch", lambda: self.subject.git(
                "push", "--quiet", self.a.remote, "--delete", self.branch, check=False).returncode == 0)

    def verify_gone(self) -> tuple[bool, str]:
        """Check AWS, not exit codes: `./platformctl down` on `main` exits 0 even
        when its destroy keeps failing, which once left two clusters billing
        behind a teardown PASS. The cluster and the env's VPC must be gone."""
        cluster_cmd = ("aws", "eks", "describe-cluster", "--name", self.cluster,
                       "--query", "cluster.status", "--output", "text")
        vpc_cmd = ("aws", "ec2", "describe-vpcs", "--filters", f"Name=tag:Name,Values={self.cluster}",
                   "--query", "Vpcs[].VpcId", "--output", "text")
        if self.dry:
            self.subject.run(*cluster_cmd)
            self.subject.run(*vpc_cmd)
            return True, ""
        left = []
        r = self.subject.run(*cluster_cmd, check=False, quiet=True)
        if r.returncode == 0:
            left.append(f"EKS cluster {self.cluster} ({r.stdout.strip()})")
        elif "ResourceNotFoundException" not in r.stderr:
            left.append(f"cluster check failed: {r.stderr.strip()[:200]}")
        r = self.subject.run(*vpc_cmd, check=False, quiet=True)
        if r.returncode != 0:
            left.append(f"VPC check failed: {r.stderr.strip()[:200]}")
        elif r.stdout.split():
            left.append(f"VPC {' '.join(r.stdout.split())}")
        if left:
            return False, ("still present, still billing: " + "; ".join(left)
                           + f". Re-run ./platformctl down {self.env} from a host allowed on the EKS API.")
        return True, f"cluster {self.cluster} and its VPC are gone"

    # -- modes --------------------------------------------------------------- #

    def run_fresh(self) -> None:
        if not self.step("branch", lambda: self.prepare_branch(self.a.ref)):
            return
        if not self.dry:
            self.report.versions["subject"] = versions.read_versions(self.subject.root)
        if not self.step("tfvars", self.render_tfvars):
            return
        if self.step("provision", self.provision):
            self.smoke()

    def run_upgrade(self) -> None:
        if not self.step("branch", lambda: self.prepare_branch(self.a.base)):
            return
        if not self.dry:
            self.report.versions["base"] = versions.read_versions(self.subject.root)
        if not self.step("tfvars", self.render_tfvars) or not self.step("provision", self.provision):
            return
        state: dict = {}
        if self.dry:
            print("\n- step seed (base's own commands)")
            for cmd in (f"./platformctl onboard-team {survival.TEAM} --budget {survival.TEAM_BUDGET} --deploy -y",
                        f"./platformctl new-model --source bedrock {survival.SEED_BEDROCK} --deploy --yes --region {self.a.region}",
                        f"./platformctl new-model {survival.SEED_GPU_MODEL} --tier vllm --users 1 --seq 2048 --deploy --yes --region {self.a.region}",
                        f"{survival.SEED_BEDROCK_CHATS} chats on {survival.SEED_BEDROCK} + 1 on the GPU model with the team key",
                        "SSO login of the 3 seed users into Open WebUI"):
                self.subject.plan(cmd)
            print("- step snapshot (team, key, spend, spend logs, models, Langfuse traces, Open WebUI ids, Cognito)")
        else:
            def do_seed():
                state["seed"] = survival.seed(self.make_ctx())
                state["before"] = survival.snapshot(self.make_ctx(), state["seed"])
                self.report.survival["before"] = state["before"]
                return True, "seeded and snapshotted"
            if not self.step("seed", do_seed):
                return
        if not self.step("merge-candidate", self.merge_candidate):
            return
        if not self.dry:
            self.report.versions["candidate"] = versions.read_versions(self.subject.root)
        if not self.step("required-tfvars", self.fill_required_tfvars):
            return
        upgraded = self.step("platformctl-upgrade", lambda: self._platformctl_ok("upgrade", self.env, "--yes"))
        if upgraded and not self.dry:
            def do_compare():
                after = survival.snapshot(self.make_ctx(), state["seed"])
                self.report.survival["after"] = after
                for name, ok, detail in survival.compare(state["before"], after):
                    self.report.results.append(checks.CheckResult("survival", name, ok, detail))
                return True, "snapshot after upgrade taken"
            self.step("survival-snapshot", do_compare)
        elif self.dry:
            print("- step survival-compare: " + ", ".join(n for n, _, _ in survival.compare({}, {})))
        if upgraded:
            self.smoke()
            self.kubernetes_bump()

    def merge_candidate(self) -> tuple[bool, str]:
        g = self.subject.git
        g("fetch", "--quiet", self.a.remote, self.a.candidate)
        r = g("merge", "--no-edit", "FETCH_HEAD", check=False)
        if r.returncode != 0:
            g("merge", "--abort", check=False)
            return False, f"git merge of {self.a.candidate} failed: {redact(r.stdout + r.stderr)[-300:]}"
        rel, _ = versions.current_release(self.subject.root)
        return True, f"merged {self.a.candidate} locally (not pushed); target release {rel}"

    def _manifest(self) -> tuple[str, dict | None]:
        return versions.current_release(self.subject.root)

    def fill_required_tfvars(self) -> tuple[bool, str]:
        rel, man = self._manifest()
        names = list((man or {}).get("required_tfvars") or [])
        if self.dry:
            self.subject.plan(f"copy missing required_tfvars of release {rel} from the new example.tfvars: {names or 'none'}")
            return True, ""
        text, added = tfvars.add_required(self.subject.read(self.subject.tfvars_rel),
                                          self.subject.read(EXAMPLE_TFVARS), names)
        if added:
            self.subject.write_tfvars(text)
        return True, f"release {rel}: required_tfvars {names or 'none'}; added {added or 'none'}"

    def kubernetes_bump(self) -> None:
        _rel, man = self._manifest()
        rec = ((man or {}).get("kubernetes") or {}).get("recommended")
        if self.dry:
            print(f"\n- step kubernetes-bump: if the release recommends a newer Kubernetes (now: {rec or 'n/a'}), "
                  "raise kubernetes_version one minor, ./platformctl up, smoke again (prefix k8s-bump/)")
            return
        cur = tfvars.kubernetes_version(self.subject.read(self.subject.tfvars_rel))
        if not rec or not cur or versions.k8s_key(rec) <= versions.k8s_key(cur):
            return

        def bump():
            text, new = tfvars.bump_kubernetes_minor(self.subject.read(self.subject.tfvars_rel))
            self.subject.write_tfvars(text)
            ok, detail = self._platformctl_ok("up", self.env)
            return ok, f"kubernetes_version {cur} -> {new}; {detail}"
        if self.step("kubernetes-bump", bump):
            self.smoke("k8s-bump/")

    # -- entry --------------------------------------------------------------- #

    def run(self) -> int:
        if self.dry:
            print(f"E2E dry run: mode {self.a.mode}, profile {self.a.profile}, env {self.env}, "
                  f"branch {self.branch}, region {self.a.region}, subject {self.subject.root}")
            print("Nothing below is executed; no git, AWS or cluster calls are made.")
        try:
            (self.run_fresh if self.a.mode == "fresh" else self.run_upgrade)()
        finally:
            if self.dry:
                print("\n- teardown" + (" (skipped: --keep)" if self.a.keep else ""))
            self.teardown()
        self.report.finished = now_iso()
        if self.dry:
            print(f"\n{len(self.subject.planned)} commands planned; dry run complete.")
            return 0
        os.makedirs(self.a.report_dir, exist_ok=True)
        base = os.path.join(self.a.report_dir, f"{self.env}")
        with open(base + ".md", "w", encoding="utf-8") as fh:
            fh.write(render_markdown(self.report))
        with open(base + ".json", "w", encoding="utf-8") as fh:
            fh.write(to_json(self.report))
        print(render_markdown(self.report))
        return 0 if self.report.passed else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="mode", required=True)
    f = sub.add_parser("fresh", help="provision <ref>, smoke, teardown")
    f.add_argument("ref")
    u = sub.add_parser("upgrade", help="provision <base>, seed, upgrade to <candidate>, verify, teardown")
    u.add_argument("base")
    u.add_argument("candidate")
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "")
    for p in (f, u):
        p.add_argument("--profile", choices=tfvars.PROFILES, default="default")
        p.add_argument("--run-number", default=os.environ.get("GITHUB_RUN_NUMBER", "0"))
        p.add_argument("--region", default=os.environ.get("E2E_REGION") or DEFAULT_REGION)
        p.add_argument("--repo-url", default=f"https://github.com/{gh_repo}.git" if gh_repo else "",
                       help="GitOps repo URL Argo CD syncs (default: this GitHub repository)")
        p.add_argument("--remote", default="origin", help="git remote of the subject to push e2e/<run-id> to")
        p.add_argument("--operator-cidr", default="", help="runner egress IP as a.b.c.d/32 (EKS API allowlist)")
        p.add_argument("--subject-dir", default=os.path.join(os.getcwd(), "subject"))
        p.add_argument("--report-dir", default=os.path.join(os.getcwd(), "reports"))
        p.add_argument("--keep", action="store_true", help="leave the env and branch running (billing continues)")
        p.add_argument("--with-llmd", action="store_true", help="also exercise the llm-d tier (second GPU node)")
        p.add_argument("--dry-run", action="store_true", help="print planned steps and rendered tfvars only")
    a = ap.parse_args(argv)
    if not a.dry_run:
        missing = [n for n, v in (("--repo-url", a.repo_url), ("--operator-cidr", a.operator_cidr)) if not v]
        if missing:
            ap.error(f"required for a real run: {', '.join(missing)}")
    if not a.repo_url:
        a.repo_url = "https://github.com/OWNER/REPO.git"
    return a


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    # A run lasts hours and the checks use kubectl port-forwards, which die when a
    # laptop sleeps. On macOS hold a sleep assertion for the run (v1 behaviour).
    if (not a.dry_run and platform.system() == "Darwin" and not os.environ.get("E2E_CAFFEINATED")
            and os.path.exists("/usr/bin/caffeinate")):
        os.environ["E2E_CAFFEINATED"] = "1"
        os.execv("/usr/bin/caffeinate", ["caffeinate", "-ims", sys.executable, *sys.argv])
    return Runner(a).run()


if __name__ == "__main__":
    sys.exit(main())
