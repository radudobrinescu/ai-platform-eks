"""Static tests for the E2E harness (no cluster, no AWS, no git side effects).

Ported from v1 tests/test_e2e_runner.py and extended for v2:
  * the check registry covers every cluster-facing domain in COMPATIBILITY.md;
  * reports and streamed logs redact account ids, ARNs and AWS hostnames;
  * versions are read from the subject tree;
  * env and cluster naming fit AWS limits;
  * the harness only calls platformctl interfaces that exist on main;
  * --dry-run works offline for both modes and both profiles.
"""

import os
import re
import subprocess
import sys

import pytest

import checks
import common
import e2e
import versions

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_ACCOUNT = "123456789012"


def _compat_domains():
    with open(os.path.join(ROOT, "COMPATIBILITY.md"), encoding="utf-8") as fh:
        return set(re.findall(r"^\| `([a-z]+)` \|", fh.read(), flags=re.MULTILINE))


class TestRegistry:
    def test_every_cluster_facing_domain_has_a_check(self):
        want = _compat_domains() - {"tooling"}  # tooling is proven by CI, not on a cluster
        assert want, "could not parse domains from COMPATIBILITY.md"
        assert want <= set(checks.DOMAINS), f"domains without a check: {want - set(checks.DOMAINS)}"

    def test_twenty_one_unique_checks(self):
        names = [n for _, n, _, _ in checks.CHECKS]
        assert len(names) == 21 and len(set(names)) == 21
        assert all(callable(fn) for _, _, fn, _ in checks.CHECKS)

    def test_gpu_check_runs_before_its_dependents(self):
        order = [n for _, n, _, _ in checks.CHECKS]
        gpu = order.index("vllmendpoint-gpu-chat")
        assert gpu < order.index("karpenter-nodeclaim-ready") and gpu < order.index("dcgm-gpu-metrics")

    def test_identity_center_check_only_in_managed_profile(self):
        when = {n: w for _, n, _, w in checks.CHECKS}
        argo = when["argocd-ui-identity-center-redirect"]

        class C:
            profile = "default"
            with_llmd = False
        assert argo(C()) is not None
        C.profile = "managed"
        assert argo(C()) is None

    def test_smoke_aliases_never_collide_with_seeded_models(self):
        import survival
        assert checks.BEDROCK_SMOKE_ALIAS != survival.SEED_BEDROCK
        assert checks.BEDROCK_ATTR_ALIAS != survival.SEED_BEDROCK

    def test_model_name_matches_the_product_derivation(self, subject_root):
        sys.path.insert(0, os.path.join(subject_root, "ops", "lib"))
        try:
            from recommend_instance.render import model_name_from_id
        finally:
            sys.path.pop(0)
        assert checks.model_name_from_id(checks.GPU_SMOKE_MODEL) == model_name_from_id(checks.GPU_SMOKE_MODEL)


class TestRedaction:
    LEAKS = (FAKE_ACCOUNT, "k8s-aiplatf-abc123", "arn:aws:iam", "secret-token")

    def _detail(self):
        return (f"k8s-aiplatf-abc123-1234567890.eu-central-1.elb.amazonaws.com in account {FAKE_ACCOUNT} "
                f"arn:aws:iam::{FAKE_ACCOUNT}:role/ai-platform-e2e-github "
                "https://x-access-token:secret-token@github.com/o/r.git")

    def test_reports_are_redacted(self):
        rep = e2e.Report("fresh", "default", "e2e-fd-1", {"subject": "main"})
        rep.results.append(checks.CheckResult("infra", "alb", False, self._detail(), 12.0))
        rep.survival = {"before": {"note": self._detail()}}
        md, js = e2e.render_markdown(rep), e2e.to_json(rep)
        for leak in self.LEAKS:
            assert leak not in md, f"{leak!r} leaked into markdown"
            assert leak not in js, f"{leak!r} leaked into json"
        assert "FAILED" in md

    def test_streamed_output_is_redacted(self, capsys):
        cmd = [sys.executable, "-c", f"print({self._detail()!r})"]
        assert common.stream(cmd) == 0
        err = capsys.readouterr().err
        for leak in self.LEAKS:
            assert leak not in err

    def test_skipped_checks_do_not_fail_the_report(self):
        rep = e2e.Report("fresh", "default", "e2e-fd-1", {})
        rep.results.append(checks.CheckResult("inference", "llmd", True, "skipped", 0, skipped=True))
        rep.results.append(checks.CheckResult("gitops", "argo", True, "ok", 1))
        assert rep.passed

    def test_empty_report_is_a_failure(self):
        assert not e2e.Report("fresh", "default", "e2e-fd-1", {}).passed


class TestNaming:
    @pytest.mark.parametrize("mode,profile,want", [
        ("fresh", "default", "e2e-fd-17"), ("fresh", "managed", "e2e-fm-17"),
        ("upgrade", "default", "e2e-ud-17"), ("upgrade", "managed", "e2e-um-17"),
    ])
    def test_env_names(self, mode, profile, want):
        assert e2e.env_name(mode, profile, "17") == want

    @pytest.mark.parametrize("bad", ["", "12345", "1a", "-1"])
    def test_run_number_is_bounded(self, bad):
        with pytest.raises(ValueError):
            e2e.env_name("fresh", "default", bad)

    def test_longest_derived_name_fits_s3(self, subject_root):
        with open(os.path.join(subject_root, e2e.EXAMPLE_TFVARS), encoding="utf-8") as fh:
            prefix = e2e.resources_prefix(fh.read())
        assert prefix == "ai-platform"
        cluster = e2e.cluster_name(prefix, e2e.env_name("upgrade", "managed", "9999"))
        assert cluster == "ai-platform-e2e-um-9999"
        assert e2e.model_cache_bucket_len(cluster) == 48 <= 63

    def test_bucket_formula_matches_subject(self, subject_root):
        with open(os.path.join(subject_root, "terraform/30.eks/30.cluster/locals.tf"), encoding="utf-8") as fh:
            assert '"${local.cluster_name}-${data.aws_caller_identity.current.account_id}-model-cache"' in fh.read()


class TestVersions:
    def test_reads_subject_pins(self, subject_root):
        v = versions.read_versions(subject_root)
        assert v["vllm"].startswith("v")
        for k in ("chart/gpu-operator", "chart/langfuse", "image/litellm/litellm", "karpenter", "keda",
                  "aws-load-balancer-controller", "kro (self-managed)", "ci/terraform", "ci/ruff",
                  "ci/kubeconform", "pip/psycopg", "python (PYTHON_IMAGE)", "kubernetes (default)"):
            assert k in v, k

    def test_release_of_a_tree(self, tmp_path):
        assert versions.current_release(str(tmp_path)) == ("baseline", None)
        (tmp_path / "releases").mkdir()
        for v in ("v2026.10.0", "v2026.11.0", "v2026.11.1"):
            (tmp_path / "releases" / f"{v}.json").write_text(f'{{"version": "{v}"}}')
        (tmp_path / "releases" / "notes.json").write_text("{}")
        assert versions.current_release(str(tmp_path))[0] == "v2026.11.1"

    @pytest.mark.parametrize("bad", ["2026.11.0", "v2026.13.0", "v26.11.0", "v2026.1.0", "v2026.11"])
    def test_version_format(self, bad):
        with pytest.raises(ValueError):
            versions.version_key(bad)


class TestOnlyMainInterfaces:
    """The harness drives the subject through commands that exist on main
    (plus `upgrade` from the product's upgrade unit)."""

    def _platformctl_calls(self):
        calls = set()
        for f in ("checks.py", "survival.py", "e2e.py"):
            with open(os.path.join(ROOT, "harness", f), encoding="utf-8") as fh:
                calls |= set(re.findall(r'platformctl(?:_ok)?\(\s*"([a-z-]+)"', fh.read()))
        return calls

    def test_commands_used(self):
        assert self._platformctl_calls() == {"up", "down", "new-model", "onboard-team", "upgrade"}

    def test_commands_exist_on_main(self, subject_root):
        with open(os.path.join(subject_root, "platformctl"), encoding="utf-8") as fh:
            src = fh.read()
        for cmd in self._platformctl_calls() - {"upgrade"}:
            assert re.search(rf"^\s*{re.escape(cmd)}\)\s+cmd_", src, re.M), cmd
        assert "Type the environment name ('${env}') to confirm" in src  # down reads the env name on stdin

    def test_new_model_flags_exist_on_main(self, subject_root):
        with open(os.path.join(subject_root, "ops/lib/recommend_instance/cli.py"), encoding="utf-8") as fh:
            cli = fh.read()
        for flag in ("--deploy", "--undeploy", "--yes", "--source", "--model-name", "--tier", "--users", "--seq",
                     "--region"):
            assert f'"{flag}"' in cli, flag
        assert '"--emit"' not in cli

    def test_sweep_exists_on_main(self, subject_root):
        assert os.path.isfile(os.path.join(subject_root, "ops/lib/sweep_env.py"))


class TestDryRun:
    @pytest.mark.parametrize("profile", ["default", "managed"])
    @pytest.mark.parametrize("mode", ["fresh", "upgrade"])
    def test_dry_run_is_offline_and_complete(self, subject_root, mode, profile, tmp_path):
        refs = ["main"] if mode == "fresh" else ["main", "release/v2026.11.0"]
        before = subprocess.run(["git", "-C", subject_root, "status", "--porcelain"], capture_output=True, text=True).stdout
        env = {**os.environ, "PATH": "/usr/bin:/bin"}  # no aws/kubectl/terraform needed
        for k in ("E2E_IDC_INSTANCE_ARN", "E2E_IDC_REGION", "E2E_IDC_GROUP_ID"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "harness", "e2e.py"), mode, *refs,
                            "--profile", profile, "--run-number", "7", "--dry-run",
                            "--subject-dir", subject_root, "--report-dir", str(tmp_path)],
                           capture_output=True, text=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr
        out = r.stdout
        env_name = e2e.env_name(mode, profile, "7")
        assert f"./platformctl up {env_name}" in out and f"./platformctl down {env_name}" in out
        assert f"aws eks describe-cluster --name ai-platform-{env_name}" in out
        assert f'gitops_revision = "e2e/{env_name}"' in out
        assert "cluster_endpoint_public_access_cidrs" in out
        assert ("eks_capabilities = true" in out) == (profile == "managed")
        assert ("capabilities_config" in out) == (profile == "managed")
        if mode == "upgrade":
            assert f"./platformctl upgrade {env_name} --yes" in out and "git merge --no-edit FETCH_HEAD" in out
            assert "./platformctl onboard-team e2e" in out
        assert not os.listdir(tmp_path), "dry run must not write reports"
        after = subprocess.run(["git", "-C", subject_root, "status", "--porcelain"], capture_output=True, text=True).stdout
        assert before == after, "dry run changed the subject tree"
        assert not os.path.exists(os.path.join(subject_root, f"terraform/00.global/vars/{env_name}.tfvars"))

    def test_real_run_requires_operator_cidr_and_repo(self):
        with pytest.raises(SystemExit):
            e2e.parse_args(["fresh", "main", "--repo-url", "https://github.com/o/r.git"])


class TestTeardownVerify:
    """Teardown is judged by AWS state, not by `./platformctl down`'s exit code."""

    class _FakeSubject:
        def __init__(self, cluster, vpcs):
            self.cluster, self.vpcs = cluster, vpcs

        def run(self, *cmd, check=True, quiet=False):
            if cmd[1:3] == ("eks", "describe-cluster"):
                if self.cluster is None:
                    return subprocess.CompletedProcess(cmd, 254, "", "An error occurred (ResourceNotFoundException)")
                return subprocess.CompletedProcess(cmd, 0, f"{self.cluster}\n", "")
            if cmd[1:3] == ("ec2", "describe-vpcs"):
                return subprocess.CompletedProcess(cmd, 0, "\t".join(self.vpcs) + "\n", "")
            raise AssertionError(cmd)

    def _runner(self, cluster, vpcs):
        r = e2e.Runner.__new__(e2e.Runner)
        r.dry, r.env, r.cluster = False, "e2e-fd-94", "ai-platform-e2e-fd-94"
        r.subject = self._FakeSubject(cluster, vpcs)
        return r

    def test_gone_passes(self):
        ok, detail = self._runner(None, []).verify_gone()
        assert ok and "gone" in detail

    def test_cluster_left_fails(self):
        ok, detail = self._runner("ACTIVE", []).verify_gone()
        assert not ok and "still billing" in detail and "ai-platform-e2e-fd-94" in detail

    def test_vpc_left_fails(self):
        ok, detail = self._runner(None, ["vpc-0abc"]).verify_gone()
        assert not ok and "vpc-0abc" in detail

    def test_cluster_check_error_fails(self):
        r = self._runner(None, [])
        r.subject.run = lambda *c, **k: subprocess.CompletedProcess(c, 255, "", "AccessDenied")
        ok, detail = r.verify_gone()
        assert not ok and "check failed" in detail


class TestLiteLLMReplicaAgreement:
    """Registration counts only when every LiteLLM replica agrees (each keeps its
    own model list, and the Service may route a chat to any of them)."""

    def _with(self, monkeypatch, views):
        monkeypatch.setattr(checks, "models_per_replica", lambda key: views)

    def test_present_needs_every_replica(self, monkeypatch):
        self._with(monkeypatch, {"a": ["m"], "b": []})
        assert checks._all_replicas("k", "m", True) is None
        self._with(monkeypatch, {"a": ["m"], "b": ["m", "x"]})
        assert checks._all_replicas("k", "m", True)

    def test_absent_needs_every_replica(self, monkeypatch):
        self._with(monkeypatch, {"a": [], "b": ["m"]})
        assert checks._all_replicas("k", "m", False) is None
        self._with(monkeypatch, {"a": [], "b": ["x"]})
        assert checks._all_replicas("k", "m", False)

    def test_unreadable_replica_is_not_agreement(self, monkeypatch):
        self._with(monkeypatch, {"a": [], "b": None})
        assert checks._all_replicas("k", "m", False) is None
        self._with(monkeypatch, {})
        assert checks._all_replicas("k", "m", False) is None

    def test_no_check_holds_one_litellm_port_forward_across_a_wait(self):
        with open(os.path.join(ROOT, "harness", "checks.py"), encoding="utf-8") as fh:
            text = fh.read()
        assert "lambda: alias not in models(pf" not in text and "alias if alias in models(pf" not in text


class TestLiteLLMSettled:
    def _with(self, monkeypatch, dep, pods):
        def fake(*args):
            return dep if args[1] == "deployment" else {"items": pods}
        monkeypatch.setattr(checks, "kubectl_json", fake)

    def _dep(self, **st):
        base = {"observedGeneration": 3, "updatedReplicas": 2, "readyReplicas": 2, "availableReplicas": 2, "replicas": 2}
        return {"metadata": {"generation": 3}, "spec": {"replicas": 2}, "status": {**base, **st}}

    def test_settled(self, monkeypatch):
        self._with(monkeypatch, self._dep(), [{"metadata": {}}, {"metadata": {}}])
        assert checks.litellm_settled()

    def test_rolling_or_terminating_is_not_settled(self, monkeypatch):
        self._with(monkeypatch, self._dep(updatedReplicas=1), [])
        assert not checks.litellm_settled()
        self._with(monkeypatch, self._dep(replicas=3), [])
        assert not checks.litellm_settled()
        self._with(monkeypatch, self._dep(), [{"metadata": {"deletionTimestamp": "x"}}])
        assert not checks.litellm_settled()
