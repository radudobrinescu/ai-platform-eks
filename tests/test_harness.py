"""Static tests for the E2E harness (no cluster, no AWS, no git side effects).

Ported from v1 tests/test_e2e_runner.py and extended for v2:
  * the check registry covers every cluster-facing domain in COMPATIBILITY.md;
  * reports and streamed logs redact account ids, ARNs and AWS hostnames;
  * versions are read from the subject tree;
  * env and cluster naming fit AWS limits;
  * the harness only calls platformctl interfaces that exist on main;
  * --dry-run works offline for both modes and both profiles.
"""

import json
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

    def test_twenty_five_unique_checks(self):
        names = [n for _, n, _, _ in checks.CHECKS]
        assert len(names) == 25 and len(set(names)) == 25
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
        ("fresh", "default", "e2efd17"), ("fresh", "managed", "e2efm17"),
        ("upgrade", "default", "e2eud17"), ("upgrade", "managed", "e2eum17"),
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
        assert cluster == "ai-platform-e2eum9999"
        assert e2e.model_cache_bucket_len(cluster) <= 63

    def test_longest_node_group_prefix_fits_eks(self, subject_root):
        """EKS: node_group_name_prefix ("<cluster>-criticaladdons-") <= 37 chars."""
        with open(os.path.join(subject_root, "terraform/30.eks/30.cluster/main.tf"), encoding="utf-8") as fh:
            assert '"${local.cluster_name}-criticaladdons" = {' in fh.read()
        with open(os.path.join(subject_root, e2e.EXAMPLE_TFVARS), encoding="utf-8") as fh:
            prefix = e2e.resources_prefix(fh.read())
        longest = e2e.env_name("upgrade", "managed", "9999")
        assert len(longest) <= e2e.MAX_ENV_LEN
        assert len(f"{e2e.cluster_name(prefix, longest)}-criticaladdons-") <= 37

    def test_workflow_env_name_matches_harness(self):
        with open(os.path.join(ROOT, ".github/workflows/e2e.yaml"), encoding="utf-8") as fh:
            assert 'env_name="e2e${MODE:0:1}${PROFILE:0:1}$((GITHUB_RUN_NUMBER % 10000))"' in fh.read()

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


# --------------------------------------------------------------------------- #
# KEDA autoscaling check: the model-file edit and the wiring                  #
# --------------------------------------------------------------------------- #
GENERATED_LLMD = """apiVersion: kro.run/v1alpha1
kind: LLMDEndpoint
metadata:
  name: qwen2-5-0-5b-instruct
  namespace: inference
spec:
  model: "Qwen/Qwen2.5-0.5B-Instruct"
  gpuCount: 1
  maxModelLen: 2048
  minReplicas: 1
  maxReplicas: 3
  targetQueueDepth: 20
  routingProfile: balanced   # EPP KV/prefix/load-aware weights for this workload
"""


def test_tune_llmd_sets_the_keda_test_knobs_and_keeps_the_rest():
    import yaml
    import checks
    out = checks.tune_llmd_for_autoscale(GENERATED_LLMD)
    spec = yaml.safe_load(out)["spec"]
    assert spec["minReplicas"] == 1
    assert spec["maxReplicas"] == checks.KEDA_MAX_REPLICAS == 2
    assert spec["targetQueueDepth"] == checks.KEDA_TARGET_PER_REPLICA
    assert "targetInflight" not in spec   # a subject without it: keep to the field it has
    assert spec["extraArgs"] == ["--max-num-seqs", str(checks.KEDA_MAX_NUM_SEQS)]
    assert spec["routingProfile"] == "balanced" and spec["maxModelLen"] == 2048
    # enough parallel streams to keep a queue above the target on one replica
    assert checks.LOAD_STREAMS - checks.KEDA_MAX_NUM_SEQS > checks.KEDA_TARGET_PER_REPLICA * checks.KEDA_MAX_REPLICAS


GENERATED_LLMD_SCALE_TO_ZERO = GENERATED_LLMD.replace(
    "  minReplicas: 1\n", "  minReplicas: 0   # scale to zero when idle\n"
                          "  scaleToZeroAfterSeconds: 600   # idle time before the last replica stops\n"
                          "  coldStartTimeoutSeconds: 600\n").replace(
    "  targetQueueDepth: 20\n", "  targetInflight: 16   # requests in flight per replica before KEDA adds one\n")


def test_tune_llmd_sets_targetinflight_when_the_subject_has_it():
    import yaml
    import checks
    spec = yaml.safe_load(checks.tune_llmd_for_autoscale(GENERATED_LLMD_SCALE_TO_ZERO))["spec"]
    assert spec["targetInflight"] == checks.KEDA_TARGET_PER_REPLICA and "targetQueueDepth" not in spec
    assert spec["minReplicas"] == 0 and spec["maxReplicas"] == checks.KEDA_MAX_REPLICAS


def test_scale_to_zero_tune_shortens_only_the_idle_time():
    import yaml
    import checks
    out = checks.tune_llmd_for_scale_to_zero(checks.tune_llmd_for_autoscale(GENERATED_LLMD_SCALE_TO_ZERO))
    spec = yaml.safe_load(out)["spec"]
    assert spec["scaleToZeroAfterSeconds"] == checks.SCALE_TO_ZERO_IDLE_SEC == 60
    assert spec["coldStartTimeoutSeconds"] == 600 and spec["maxReplicas"] == checks.KEDA_MAX_REPLICAS
    assert out.count("scaleToZeroAfterSeconds") == 1


def test_tune_llmd_adds_missing_keys_and_refuses_existing_extra_args():
    import pytest
    import yaml
    import checks
    bare = "\n".join(ln for ln in GENERATED_LLMD.splitlines() if "Replicas" not in ln and "QueueDepth" not in ln)
    spec = yaml.safe_load(checks.tune_llmd_for_autoscale(bare))["spec"]
    assert spec["maxReplicas"] == 2 and spec["targetQueueDepth"] == 2
    with pytest.raises(ValueError):
        checks.tune_llmd_for_autoscale(GENERATED_LLMD + '  extraArgs: ["--foo"]\n')


def test_keda_check_runs_after_the_llmd_check_with_the_same_gate():
    import checks
    names = [n for _d, n, _f, _w in checks.CHECKS]
    i, z, j = (names.index("llmdendpoint-routed-chat"), names.index("llmd-scales-to-zero-and-back"),
               names.index("keda-autoscales-llmd-on-queue-depth"))
    # scale to zero runs on the warm model, before the KEDA check removes it
    assert i < z < j and j == z + 1 == i + 2
    assert checks.CHECKS[j][3] is checks.CHECKS[i][3] is checks._only_llmd
    assert checks.CHECKS[z][3] is checks._only_scale_to_zero


class _Subject:
    def __init__(self, rgd: str | None):
        self.rgd = rgd

    def read(self, rel):
        if self.rgd is None:
            raise FileNotFoundError(rel)
        return self.rgd


def test_scale_to_zero_check_skips_a_subject_without_it():
    import checks
    old = checks.Ctx(subject=_Subject("      minReplicas: \"integer | default=1 minimum=1\""), cluster="x", with_llmd=True)
    new = checks.Ctx(subject=_Subject("      scaleToZeroAfterSeconds: \"integer | default=600\""), cluster="x",
                     with_llmd=True)
    assert "no llm-d scale to zero" in checks._only_scale_to_zero(old)
    assert checks._only_scale_to_zero(new) is None
    assert checks._only_scale_to_zero(checks.Ctx(subject=_Subject(None), cluster="x", with_llmd=True))
    assert "llm-d tier not requested" in checks._only_scale_to_zero(checks.Ctx(subject=new.subject, cluster="x"))


def test_cold_request_script_is_valid_python_with_the_model_inlined():
    import ast
    import checks
    code = checks.COLD_REQUEST_PY.replace("MODEL", '"qwen2-0-5b-instruct"').replace("TIMEOUT", "900")
    ast.parse(code)
    assert '"model": "qwen2-0-5b-instruct"' in code and "timeout=900" in code


def test_keda_check_reports_a_missing_llmd_endpoint_instead_of_crashing():
    import checks
    c = checks.Ctx(subject=None, cluster="x", with_llmd=True)
    ok, detail = checks.chk_keda_autoscaling(c)
    assert not ok and "no llm-d endpoint" in detail


def test_e2e_runs_the_llmd_tier_and_keda_by_default():
    import os
    import yaml
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    wf = yaml.safe_load(open(os.path.join(root, ".github/workflows/e2e.yaml"), encoding="utf-8"))
    on = wf[True] if True in wf else wf["on"]
    assert on["workflow_dispatch"]["inputs"]["with_llmd"]["default"] is True


# --------------------------------------------------------------------------- #
# Diagnostics on timeouts, and KEDA cleanup on every path                      #
# --------------------------------------------------------------------------- #
STUCK = {
    "applications": {"items": [
        {"metadata": {"name": "litellm"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Progressing"},
         "resources": [{"kind": "Deployment", "name": "litellm", "health": {"status": "Progressing"}}]}},
        {"metadata": {"name": "teams"}, "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"}}}]},
    "pods": {"items": [
        {"metadata": {"namespace": "ai-platform", "name": "platform-db-0"},
         "status": {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "message": "0/3 nodes are available"}]}},
        {"metadata": {"namespace": "ai-platform", "name": "langfuse-web-1"},
         "status": {"phase": "Pending", "conditions": [{"type": "Ready", "status": "False"}],
                    "containerStatuses": [{"name": "web", "state": {"waiting": {"reason": "ImagePullBackOff",
                                           "message": "toomanyrequests: You have reached your unauthenticated pull rate limit"}}}]}},
        {"metadata": {"namespace": "ai-platform", "name": "ok-1"}, "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}},
        {"metadata": {"namespace": "team-x", "name": "ignored"}, "status": {"phase": "Pending"}}]},
    "events": {"items": [{"involvedObject": {"namespace": "ai-platform", "name": "langfuse-web-1"}, "reason": "Failed",
                          "message": "Failed to pull image", "lastTimestamp": "2026-11-11T15:00:00Z"}]},
    "vllmendpoints": {"items": [
        {"metadata": {"name": "qwen"}, "status": {"state": "IN_PROGRESS", "ready": "False", "conditions": [
            {"type": "ResourcesReady", "status": "False", "message": "waiting for deployment qwen-vllm"},
            {"type": "InstanceManaged", "status": "True"}]}},
        {"metadata": {"name": "fine"}, "status": {"state": "ACTIVE", "ready": "True"}}]},
    "nodeclaims": {"items": [
        {"metadata": {"name": "gpu-shared-abc", "labels": {"karpenter.sh/nodepool": "gpu-shared",
                                                            "node.kubernetes.io/instance-type": "g7.4xlarge"}},
         "status": {"conditions": [{"type": "Ready", "status": "False", "message": "Node not registered with cluster"}]}},
        {"metadata": {"name": "default-xyz", "labels": {"karpenter.sh/nodepool": "default",
                                                         "node.kubernetes.io/instance-type": "m7i.xlarge"}},
         "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]},
}


def _fake_kubectl_json(*args):
    return STUCK.get(args[1], {}) if args and args[0] == "get" else {}


def test_diagnose_names_unhealthy_apps_stuck_pods_and_warnings(monkeypatch):
    import checks
    monkeypatch.setattr(checks, "kubectl_json", _fake_kubectl_json)
    monkeypatch.setattr(checks, "log", lambda *_: None)
    out = checks.diagnose()
    assert "app litellm: Synced/Progressing [Deployment/litellm=Progressing]" in out and "teams" not in out
    assert "pod ai-platform/platform-db-0 Pending: unscheduled: 0/3 nodes are available" in out
    assert "web:ImagePullBackOff toomanyrequests" in out
    assert "ok-1" not in out and "team-x" not in out
    assert "event langfuse-web-1: Failed Failed to pull image" in out
    # A model that does not come up: what kro says, and which GPU node Karpenter picked for it.
    assert "vllmendpoint qwen: IN_PROGRESS ready=False (ResourcesReady: waiting for deployment qwen-vllm)" in out
    assert "vllmendpoint fine" not in out
    assert "nodeclaim gpu-shared-abc (gpu-shared, g7.4xlarge) Ready=False: Node not registered with cluster" in out
    assert "default-xyz" not in out


def test_a_failed_upgrade_reports_what_is_stuck(monkeypatch):
    """2026-11-19: the upgrade from the public state applied every stage and then
    waited 30 minutes for one application. The cluster was torn down, and the
    report said only "exited 1"."""
    monkeypatch.setattr(e2e.checks, "diagnose", lambda: "app models-inference: Synced/Progressing")
    r = e2e.Runner.__new__(e2e.Runner)
    r.env, r.dry = "e2eud1", False

    class Subject:
        rc = 1
        @classmethod
        def platformctl(cls, *a, **k): return cls.rc
    r.subject = Subject
    ok, detail = r.upgrade_platform()
    assert not ok and detail == "./platformctl upgrade e2eud1 --yes exited 1 || stuck: app models-inference: Synced/Progressing"
    Subject.rc = 3                                   # the plan guard stopped it: nothing was applied, nothing is stuck
    assert r.upgrade_platform() == (False, "./platformctl upgrade e2eud1 --yes exited 3")
    Subject.rc = 0
    assert r.upgrade_platform() == (True, "./platformctl upgrade e2eud1 --yes exited 0")


def test_a_timed_out_check_reports_what_is_stuck(monkeypatch):
    import checks
    monkeypatch.setattr(checks, "kubectl_json", _fake_kubectl_json)
    monkeypatch.setattr(checks, "log", lambda *_: None)

    def times_out(_c):
        raise TimeoutError("timed out after 1800s waiting for Argo CD apps Synced+Healthy")
    r = checks.run_check("gitops", "argocd-applications-converged", times_out, None)
    assert not r.ok and "|| stuck: app litellm" in r.detail
    r2 = checks.run_check("gitops", "x", lambda _c: (False, "HTTP 500"), None)
    assert "stuck:" not in r2.detail  # only timeouts are diagnosed


def test_keda_check_cleans_up_and_waits_even_when_scaling_fails(monkeypatch):
    import checks
    calls = {"undeploy": 0, "gets": 0}
    monkeypatch.setattr(checks, "log", lambda *_: None)
    monkeypatch.setattr(checks, "diagnose", lambda: "nothing")

    def boom(*_a):
        raise TimeoutError("timed out after 600s waiting for KEDA scales x-llmd to 2")
    monkeypatch.setattr(checks, "_keda_scale", boom)
    monkeypatch.setattr(checks, "undeploy_model", lambda c, n: calls.__setitem__("undeploy", calls["undeploy"] + 1))

    def kj(*args):
        calls["gets"] += 1
        return {"metadata": {"name": args[-1]}} if calls["gets"] <= 3 else {}  # present once, then gone
    monkeypatch.setattr(checks, "kubectl_json", kj)
    monkeypatch.setattr(checks, "wait_for", lambda desc, fn, timeout, interval=15: fn() or fn())
    c = checks.Ctx(subject=None, cluster="x", with_llmd=True)
    c.llmd_model, c.llmd_created = "x", True
    ok, detail = checks.chk_keda_autoscaling(c)
    assert not ok and "timed out" in detail and "stuck: nothing" in detail and "endpoint removed" in detail
    assert calls["undeploy"] == 1 and c.llmd_model is None and not c.llmd_created


def test_llmd_checks_never_reuse_a_model_that_upgrade_mode_seeds():
    import checks
    import survival
    seeded = {checks.model_name_from_id(survival.SEED_GPU_MODEL), checks.model_name_from_id(checks.GPU_SMOKE_MODEL)}
    assert checks.model_name_from_id(checks.LLMD_SMOKE_MODEL) not in seeded


# --------------------------------------------------------------------------- #
# GPU models start up front, after the GitOps checks                          #
# --------------------------------------------------------------------------- #
def test_gpu_models_start_after_gitops_checks_and_before_the_rest(monkeypatch):
    import checks
    order = []
    monkeypatch.setattr(checks, "start_gpu_models", lambda c: order.append("START"))
    monkeypatch.setattr(checks, "log", lambda *_: None)
    fake = [(d, n, (lambda c, n=n: (order.append(n), (True, "ok"))[1]), None) for d, n in
            [("gitops", "argocd-applications-converged"), ("gitops", "kro-rgds-active"), ("infra", "eks-cluster-version"),
             ("gateway", "litellm-ready"), ("inference", "vllmendpoint-gpu-chat"), ("inference", "llmdendpoint-routed-chat")]]
    monkeypatch.setattr(checks, "CHECKS", fake)
    checks.run_suite(checks.Ctx(subject=None, cluster="x", with_llmd=True))
    assert order == ["argocd-applications-converged", "kro-rgds-active", "START", "eks-cluster-version",
                     "litellm-ready", "vllmendpoint-gpu-chat", "llmdendpoint-routed-chat"]


def test_no_gpu_start_when_every_gpu_check_is_skipped(monkeypatch):
    import checks
    started = []
    monkeypatch.setattr(checks, "start_gpu_models", lambda c: started.append(1))
    monkeypatch.setattr(checks, "CHECKS", [("gitops", "a", lambda c: (True, ""), None),
                                           ("inference", "vllmendpoint-gpu-chat", lambda c: (True, ""), lambda c: "skip")])
    checks.run_suite(checks.Ctx(subject=None, cluster="x"))
    assert started == []


def test_a_failed_up_front_start_is_reported_by_the_check_that_needs_it(monkeypatch):
    import checks
    monkeypatch.setattr(checks, "log", lambda *_: None)

    def boom(*a, **k):
        raise RuntimeError("platformctl new-model --deploy failed (exit 1)")
    monkeypatch.setattr(checks, "deploy_model", boom)
    monkeypatch.setattr(checks, "start_llmd_model", boom)
    c = checks.Ctx(subject=None, cluster="x", with_llmd=True)
    checks.start_gpu_models(c)
    ok, detail = checks.chk_gpu_model_roundtrip(c)
    assert not ok and "starting qwen2-5-0-5b-instruct at the beginning of the suite failed" in detail
    ok, detail = checks.chk_llmd_roundtrip(c)
    assert not ok and "starting the llm-d model at the beginning of the suite failed" in detail


# --------------------------------------------------------------------------- #
# e2e.py check: smoke checks against an env that already runs                 #
# --------------------------------------------------------------------------- #
def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _env_checkout(tmp_path, env="dev", upstream=True):
    """A checkout like .worktrees/dev: <env>.tfvars (gitignored) and a tracked branch."""
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    _git(tmp_path, "init", "-q", "-b", f"env/{env}", str(work))
    (work / ".gitignore").write_text("*.tfvars\n")
    vars_dir = work / "terraform" / "00.global" / "vars"
    vars_dir.mkdir(parents=True)
    (vars_dir / f"{env}.tfvars").write_text('resources_prefix = "ai-platform"\n')
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "init")
    if upstream:
        _git(work, "remote", "add", "origin", str(remote))
        _git(work, "push", "-qu", "origin", f"env/{env}")
    return work


def _check_args(work, tmp_path, *extra):
    return e2e.parse_args(["check", "--env", "dev", "--subject-dir", str(work),
                           "--region", "eu-central-1", "--report-dir", str(tmp_path / "reports"), *extra])


def test_check_mode_needs_no_repo_url_or_operator_cidr():
    a = e2e.parse_args(["check", "--env", "dev", "--subject-dir", "."])
    assert a.mode == "check" and a.dry_run is False and a.only == "" and not a.survival


def test_check_mode_rejects_unknown_check_names_before_touching_aws(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(e2e.Subject, "run", lambda self, *cmd, **k: calls.append(cmd))
    a = _check_args(tmp_path, tmp_path, "--only", "litellm-ready,no-such-check")
    with pytest.raises(e2e.SubjectError, match="unknown check.*no-such-check"):
        e2e.run_check_only(a)
    assert calls == []


def test_check_mode_refuses_a_checkout_without_the_env_tfvars(tmp_path):
    work = _env_checkout(tmp_path, env="other")
    with pytest.raises(e2e.SubjectError, match="dev.tfvars not found"):
        e2e.run_check_only(_check_args(work, tmp_path))


def test_check_mode_refuses_uncommitted_changes(tmp_path):
    work = _env_checkout(tmp_path)
    (work / ".gitignore").write_text("changed\n*.tfvars\n")
    with pytest.raises(e2e.SubjectError, match="uncommitted changes"):
        e2e.run_check_only(_check_args(work, tmp_path))


def test_check_mode_refuses_a_branch_without_upstream(tmp_path):
    work = _env_checkout(tmp_path, upstream=False)
    with pytest.raises(e2e.SubjectError, match="no upstream branch"):
        e2e.run_check_only(_check_args(work, tmp_path))


def test_check_mode_runs_only_the_named_checks_and_writes_a_report(tmp_path, monkeypatch):
    work = _env_checkout(tmp_path)
    aws, seen = [], {}
    real_run = e2e.Subject.run

    def fake_run(self, *cmd, **k):
        if cmd[0] == "aws":
            aws.append(cmd)
            return subprocess.CompletedProcess(list(cmd), 0, "", "")
        return real_run(self, *cmd, **k)

    def fake_suite(ctx, prefix="", only=None):
        seen.update(cluster=ctx.cluster, only=only, with_llmd=ctx.with_llmd)
        return [checks.CheckResult("gateway", "litellm-ready", True, "ok")]

    monkeypatch.setattr(e2e.Subject, "run", fake_run)
    monkeypatch.setattr(e2e.checks, "run_suite", fake_suite)
    monkeypatch.setattr(e2e, "log", lambda *_: None)
    rc = e2e.run_check_only(_check_args(work, tmp_path, "--only", "litellm-ready"))
    assert rc == 0
    assert aws == [("aws", "eks", "update-kubeconfig", "--name", "ai-platform-dev", "--region", "eu-central-1",
                    "--alias", "dev")]
    assert seen == {"cluster": "ai-platform-dev", "only": {"litellm-ready"}, "with_llmd": False}
    report = (tmp_path / "reports" / "dev-check.md").read_text()
    assert "litellm-ready" in report


def test_check_mode_fails_when_a_check_fails(tmp_path, monkeypatch):
    work = _env_checkout(tmp_path)
    real_run = e2e.Subject.run
    monkeypatch.setattr(e2e.Subject, "run", lambda self, *cmd, **k: subprocess.CompletedProcess(list(cmd), 0, "", "")
                        if cmd[0] == "aws" else real_run(self, *cmd, **k))
    monkeypatch.setattr(e2e.checks, "run_suite",
                        lambda ctx, prefix="", only=None: [checks.CheckResult("gateway", "litellm-ready", False, "down")])
    monkeypatch.setattr(e2e, "log", lambda *_: None)
    assert e2e.run_check_only(_check_args(work, tmp_path)) == 1


def test_run_suite_only_runs_the_selected_checks(monkeypatch):
    ran = []
    monkeypatch.setattr(checks, "log", lambda *_: None)
    monkeypatch.setattr(checks, "start_gpu_models", lambda c: ran.append("START"))
    monkeypatch.setattr(checks, "CHECKS", [(d, n, (lambda c, n=n: (ran.append(n), (True, ""))[1]), None) for d, n in
                                           [("gitops", "a"), ("gateway", "b"), ("inference", "vllmendpoint-gpu-chat")]])
    results = checks.run_suite(checks.Ctx(subject=None, cluster="x"), only={"b"})
    assert ran == ["b"] and [r.name for r in results] == ["b"]


# --------------------------------------------------------------------------- #
# Langfuse trace counts on Langfuse 3 and 4                                   #
# --------------------------------------------------------------------------- #
def test_trace_count_reads_langfuse_4_and_follows_the_cursor():
    import json as _json
    pages = {"": {"data": [{"traceId": "t1"}, {"traceId": "t1"}, {"traceId": "t2"}], "meta": {"cursor": "c1"}},
             "c1": {"data": [], "meta": {"cursor": "c2"}},
             "c2": {"data": [{"traceId": "t3"}, {"id": "o9"}], "meta": {"cursor": None}}}
    seen = []

    def get(path):
        seen.append(path)
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(path).query)
        assert urlparse(path).path == "/api/public/v2/observations" and q["fromStartTime"] == ["2026-11-16T10:00:00Z"]
        return 200, _json.dumps(pages[q.get("cursor", [""])[0]])
    assert checks._count_traces_v2(get, "2026-11-16T10:00:00Z") == 3 and len(seen) == 3


def test_trace_count_falls_back_to_langfuse_3():
    import json as _json

    def get(path):
        if path.startswith("/api/public/v2/"):
            return 404, "not found"
        assert path == "/api/public/traces?limit=1"
        return 200, _json.dumps({"data": [{}], "meta": {"totalItems": 7}})
    assert checks._count_traces_v2(get, None) is None
    assert checks._count_traces_v1(get, None) == 7


def test_versions_read_digest_pinned_images_and_split_helm_values(tmp_path):
    (tmp_path / "platform" / "x").mkdir(parents=True)
    (tmp_path / "platform" / "x" / "a.yaml").write_text(
        "      containers:\n        - image: clickhouse/clickhouse-server:26.8.38.9@sha256:" + "a" * 64 + "\n"
        "redis:\n  image:\n    registry: docker.io\n    repository: valkey/valkey\n"
        '    tag: "8.0.12-alpine@sha256:' + "b" * 64 + '"\n')
    v = versions.read_versions(str(tmp_path))
    assert v["image/clickhouse/clickhouse-server"] == "26.8.38.9" and v["image/valkey/valkey"] == "8.0.12-alpine"


def test_the_harness_may_upgrade_to_an_untagged_candidate():
    """`./platformctl upgrade` refuses a release without its tag; the candidate
    under test gets its tag only after E2E, so the harness opts out."""
    from subject import Subject
    assert Subject(".", "e2eud1", "eu-central-1", dry_run=True)._env()["UPGRADE_ALLOW_UNTAGGED"] == "1"


class TestOldBase:
    """An upgrade test whose base predates the first release (the public
    repository's state before any release reached it)."""

    def _seed(self, monkeypatch, fail):
        import survival
        calls = []
        monkeypatch.setattr(survival, "wait_for", lambda *a, **k: "sk-team")
        monkeypatch.setattr(survival.checks, "deploy_bedrock", lambda c: ("nova-lite", None))
        monkeypatch.setattr(survival.checks, "wait_alias", lambda c, alias, t: calls.append(("alias", alias)))
        monkeypatch.setattr(survival.checks, "deploy_model", lambda c, *a: calls.append(("deploy", a[0])))

        def ready(c, kind, name):
            if fail:
                raise TimeoutError("timed out after 1500s waiting for vllmendpoint")
        monkeypatch.setattr(survival.checks, "wait_endpoint_ready", ready)
        monkeypatch.setattr(survival.checks, "diagnose", lambda: "app models-inference: OutOfSync/Progressing")
        monkeypatch.setattr(survival.checks, "chat", lambda pf, key, model: (calls.append(("chat", model)) or (200, "ok")))
        monkeypatch.setattr(survival, "openwebui_ids", lambda c: {})

        class PF:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(survival, "PortForward", PF)

        class Ctx:
            timeouts = {"bedrock": 1}
            class subject:
                @staticmethod
                def platformctl(*a, **k): return 0
        return survival, Ctx, calls

    def test_a_base_that_cannot_bring_up_a_model_is_still_seeded_with_the_rest(self, monkeypatch):
        survival, Ctx, calls = self._seed(monkeypatch, fail=True)
        s = survival.seed(Ctx, gpu_optional=True)
        assert s.models == ["nova-lite"] and "TimeoutError" in s.gpu_skipped
        # The cluster is gone when somebody reads the report: what was stuck is in it.
        assert "stuck: app models-inference: OutOfSync/Progressing" in s.gpu_skipped
        chats = [m for kind, m in calls if kind == "chat"]
        assert chats and set(chats) == {"nova-lite"}                 # no chat on the model that is not there

    def test_on_a_released_base_the_same_failure_fails_the_seed(self, monkeypatch):
        survival, Ctx, _ = self._seed(monkeypatch, fail=True)
        with pytest.raises(TimeoutError):
            survival.seed(Ctx)

    def test_when_the_model_comes_up_nothing_is_skipped(self, monkeypatch):
        survival, Ctx, calls = self._seed(monkeypatch, fail=False)
        s = survival.seed(Ctx, gpu_optional=True)
        assert len(s.models) == 2 and s.gpu_skipped is None and ("chat", s.models[1]) in calls

    def test_a_run_that_stopped_before_the_merge_tears_down_with_the_candidate_s_code(self):
        r = e2e.Runner.__new__(e2e.Runner)
        steps = []

        class A:
            keep, mode, region, remote = False, "upgrade", "eu-central-1", "origin"
        r.a, r.dry, r.env, r.cluster, r.branch = A, False, "e2eud1", "ai-platform-e2eud1", "e2e/e2eud1"
        r.provisioned, r.branch_pushed, r.candidate_merged = True, False, False
        r.step = lambda name, fn: steps.append(name) or True
        r.teardown()
        assert steps == ["teardown-code", "teardown", "orphan-sweep", "teardown-verify"]
        steps.clear()
        r.candidate_merged = True                                     # the usual case: already on the candidate
        r.teardown()
        assert steps == ["teardown", "orphan-sweep", "teardown-verify"]
        steps.clear()
        A.mode, r.candidate_merged = "fresh", False
        r.teardown()
        assert steps == ["teardown", "orphan-sweep", "teardown-verify"]


# --------------------------------------------------------------------------- #
# Port-forwards
# --------------------------------------------------------------------------- #
FAKE_PF_KUBECTL = r'''#!/usr/bin/env python3
"""kubectl port-forward stand-in: gives up FAIL_TIMES times, then listens."""
import os, socket, sys, time
count = os.environ["PF_COUNT"]
n = int(open(count).read() or 0) + 1
open(count, "w").write(str(n))
if n <= int(os.environ.get("FAIL_TIMES", "0")):
    sys.stderr.write("error: unable to forward port because pod is not running. Current status=Pending\n")
    sys.exit(1)
local = int(sys.argv[-1].split(":")[0])
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", local)); s.listen(5)
time.sleep(60)
'''


def _fake_pf(tmp_path, monkeypatch, fail_times):
    import common
    fake = tmp_path / "kubectl"
    fake.write_text(FAKE_PF_KUBECTL)
    fake.chmod(0o755)
    count = tmp_path / "count"
    count.write_text("0")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("PF_COUNT", str(count))
    monkeypatch.setenv("FAIL_TIMES", str(fail_times))
    monkeypatch.setattr(common.PortForward, "RESTART_PAUSE", 0.1)
    return common, count


def test_a_port_forward_that_kubectl_gave_up_on_is_started_again(tmp_path, monkeypatch):
    """Release v2026.12.0, upgrade/managed: minutes after the upgrade the service had
    no running pod for a moment, kubectl exited at once, and the harness waited 60
    seconds for a port that nothing would open. The job failed on that alone."""
    common, count = _fake_pf(tmp_path, monkeypatch, fail_times=2)
    with common.PortForward("ai-platform", "svc/langfuse-web", 3000) as pf:
        assert pf._open() and count.read_text() == "3"


def test_a_port_forward_that_never_opens_says_what_kubectl_said(tmp_path, monkeypatch):
    common, _ = _fake_pf(tmp_path, monkeypatch, fail_times=1000)
    monkeypatch.setattr(common.PortForward, "OPEN_TIMEOUT", 1.5)
    with pytest.raises(TimeoutError) as e:
        common.PortForward("ai-platform", "svc/langfuse-web", 3000).__enter__()
    assert "port-forward svc/langfuse-web" in str(e.value) and "pod is not running" in str(e.value)


# --------------------------------------------------------------------------- #
# KEDA check and a cloud without GPU capacity
# --------------------------------------------------------------------------- #
def _keda_fakes(monkeypatch, capacity_events, scales_after_extension=True):
    import checks
    waits = []
    monkeypatch.setattr(checks, "log", lambda *_: None)

    def kj(*args):
        if args[1] == "scaledobject":
            return {"spec": {"maxReplicaCount": checks.KEDA_MAX_REPLICAS},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}], "hpaName": "hpa"}}
        if args[1] == "events":
            return {"items": capacity_events}
        return {"status": {"currentMetrics": []}}
    monkeypatch.setattr(checks, "kubectl_json", kj)
    monkeypatch.setattr(checks, "llmd_deployment", lambda name: {"spec": {"replicas": 1}, "status": {"readyReplicas": 2}})

    class FakeLoad:
        ok, sent = 9, 10
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(checks, "Load", FakeLoad)

    def wait_for(desc, fn, timeout, interval=15):
        waits.append((desc, timeout))
        if desc.startswith("KEDA scales"):
            first = sum(1 for d, _ in waits if d.startswith("KEDA scales")) == 1
            if first or not scales_after_extension:
                raise TimeoutError(f"timed out after {timeout}s waiting for {desc}")
            return {"spec": {"replicas": 2}}
        return fn()
    monkeypatch.setattr(checks, "wait_for", wait_for)

    class Ctx:
        timeouts = {"gpu": 1500}
        @staticmethod
        def litellm_key(): return "sk"
    return checks, Ctx, waits


CAPACITY_EVENT = {"involvedObject": {"name": "qwen-llmd-8545df8955-52kgd"}, "reason": "InsufficientCapacityError",
                  "lastTimestamp": "2099-01-01T00:00:00Z",
                  "message": "NodeClaim gpu-inference-hp572 nominated for this pod failed to launch: creating instance, insufficient capacity"}


def test_keda_check_waits_longer_when_the_cloud_had_no_gpu_to_give(monkeypatch):
    """Release v2026.12.0 (recut), upgrade/managed: the replica the test loads needed a
    new GPU node, EC2 answered "insufficient capacity", and the replacement node was
    still starting when the 10 minutes for KEDA's decision ran out. The check failed
    the job; forty minutes later, in the same job, it passed in two."""
    checks, Ctx, waits = _keda_fakes(monkeypatch, [CAPACITY_EVENT])
    ok, detail = checks._keda_scale(Ctx, "qwen", "qwen-llmd")
    assert ok and "waited for GPU capacity" in detail and "gpu-inference-hp572" in detail
    keda = [(d, t) for d, t in waits if d.startswith("KEDA scales")]
    assert [t for _, t in keda] == [checks.SCALE_DECISION_TIMEOUT, 1500] and "capacity" in keda[1][0]


def test_keda_check_still_fails_when_nothing_says_the_cloud_was_short(monkeypatch):
    other_pod = {**CAPACITY_EVENT, "involvedObject": {"name": "some-other-model-llmd-abc"}}
    old = {**CAPACITY_EVENT, "lastTimestamp": "2020-01-01T00:00:00Z"}
    for events in ([], [other_pod], [old]):
        checks, Ctx, waits = _keda_fakes(monkeypatch, events)
        with pytest.raises(TimeoutError) as e:
            checks._keda_scale(Ctx, "qwen", "qwen-llmd")
        assert "capacity" not in str(e.value) and len([d for d, _ in waits if d.startswith("KEDA scales")]) == 1


def test_keda_check_fails_and_says_so_when_capacity_never_came(monkeypatch):
    checks, Ctx, _ = _keda_fakes(monkeypatch, [CAPACITY_EVENT], scales_after_extension=False)
    with pytest.raises(TimeoutError) as e:
        checks._keda_scale(Ctx, "qwen", "qwen-llmd")
    assert "the cloud had no GPU capacity" in str(e.value) and "gpu-inference-hp572" in str(e.value)


# --------------------------------------------------------------------------- #
# Dashboard data sources and Prometheus scrape targets
# --------------------------------------------------------------------------- #
def test_dashboard_problems_names_every_source_that_is_not_ok():
    import checks
    ok = [{"name": "kubernetes", "state": "ok"}, {"name": "litellm-api", "state": "ok"}]
    sections = {"nodes": [{"name": "n"}], "nodePools": [{"name": "default"}]}
    assert checks.dashboard_problems(200, ok, sections) == []
    bad = [{"name": "kubernetes", "state": "ok"},
           {"name": "litellm-metrics", "state": "error", "lastError": "401 from http://10.0.0.1:4000/metrics"},
           {"name": "dcgm", "state": "stale", "detail": "last success 600 s ago"}]
    got = checks.dashboard_problems(503, bad, {"nodes": [], "nodePools": []})
    assert got == ["/api/v1/health/ready answered HTTP 503",
                   "source litellm-metrics is error (401 from http://10.0.0.1:4000/metrics)",
                   "source dcgm is stale (last success 600 s ago)",
                   "the snapshot has no nodes", "the snapshot has no node pools"]


def test_scrape_problems_lists_targets_that_are_down_and_a_missing_litellm_job():
    import checks
    up = {"labels": {"job": "litellm"}, "health": "up", "scrapeUrl": "http://litellm:4000/metrics"}
    assert checks.scrape_problems([up]) == []
    down = {"labels": {"job": "litellm"}, "health": "down", "scrapeUrl": "http://litellm:4000/metrics",
            "lastError": "server returned HTTP status 401 Unauthorized"}
    assert checks.scrape_problems([down]) == [
        "litellm http://litellm:4000/metrics is down (server returned HTTP status 401 Unauthorized)"]
    vllm = {"labels": {"job": "llmd-vllm"}, "health": "up", "scrapeUrl": "http://10.0.0.2:8000/metrics"}
    assert checks.scrape_problems([vllm]) == ["no target for the job litellm (the request rate that wakes a pool from zero)"]


def _fake_http(monkeypatch, answers):
    import checks

    class PF:
        base = "http://pf"
        def __init__(self, *a): self.args = a
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(checks, "PortForward", PF)
    monkeypatch.setattr(checks, "http", lambda method, url, **k: answers[url[len("http://pf"):]])
    monkeypatch.setattr(checks, "wait_for", lambda desc, fn, timeout, interval=15: fn() or (_ for _ in ()).throw(
        TimeoutError(f"timed out after {timeout}s waiting for {desc}")))
    return checks


def test_dashboard_sources_check_passes_on_a_healthy_dashboard_and_says_why_it_fails(monkeypatch):
    sources = [{"name": n, "state": "ok"} for n in ("kubernetes", "litellm-api", "litellm-metrics", "dcgm")]
    snapshot = {"version": 3, "sections": {"nodes": [{}, {}], "nodePools": [{}, {}, {}]}}
    checks = _fake_http(monkeypatch, {"/api/v1/health/ready": (200, "{}"), "/api/v1/sources": (200, json.dumps(sources)),
                                      "/api/v1/snapshot": (200, json.dumps(snapshot))})
    assert checks.chk_dashboard_sources(None) == (True, "4 data sources ok; snapshot has 2 nodes and 3 node pools")
    sources[2] = {"name": "litellm-metrics", "state": "error", "lastError": "no viewer key yet"}
    checks = _fake_http(monkeypatch, {"/api/v1/health/ready": (200, "{}"), "/api/v1/sources": (200, json.dumps(sources)),
                                      "/api/v1/snapshot": (200, json.dumps(snapshot))})
    ok, detail = checks.chk_dashboard_sources(None)
    assert not ok and detail == "source litellm-metrics is error (no viewer key yet)"
    checks = _fake_http(monkeypatch, {"/api/v1/health/ready": (404, ""), "/api/v1/sources": (404, "not found"),
                                      "/api/v1/snapshot": (404, "not found")})
    ok, detail = checks.chk_dashboard_sources(None)
    assert not ok and "/api/v1/sources answered HTTP 404" in detail


def test_scrape_targets_check(monkeypatch):
    up = {"status": "success", "data": {"activeTargets": [
        {"labels": {"job": "litellm"}, "health": "up", "scrapeUrl": "u"},
        {"labels": {"job": "llmd-vllm"}, "health": "up", "scrapeUrl": "v"}]}}
    checks = _fake_http(monkeypatch, {"/api/v1/targets": (200, json.dumps(up))})
    assert checks.chk_scrape_targets(None) == (True, "2 scrape targets up (litellm, llmd-vllm)")
    up["data"]["activeTargets"][0].update(health="down", lastError="401")
    checks = _fake_http(monkeypatch, {"/api/v1/targets": (200, json.dumps(up))})
    ok, detail = checks.chk_scrape_targets(None)
    assert not ok and detail == "litellm u is down (401)"


def test_the_two_checks_are_registered_and_the_dashboard_one_runs_last():
    import checks
    names = [n for _d, n, _f, _w in checks.CHECKS]
    assert names[-1] == "cluster-dashboard-sources-ok", "last: its LiteLLM key comes from a job that runs every 15 minutes"
    assert names.index("autoscaling-scrape-targets-up") > names.index("litellm-ready")
