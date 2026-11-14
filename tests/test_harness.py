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

    def test_twenty_two_unique_checks(self):
        names = [n for _, n, _, _ in checks.CHECKS]
        assert len(names) == 22 and len(set(names)) == 22
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
    assert spec["targetQueueDepth"] == checks.KEDA_TARGET_QUEUE_DEPTH
    assert spec["extraArgs"] == ["--max-num-seqs", str(checks.KEDA_MAX_NUM_SEQS)]
    assert spec["routingProfile"] == "balanced" and spec["maxModelLen"] == 2048
    # enough parallel streams to keep a queue above the target on one replica
    assert checks.LOAD_STREAMS - checks.KEDA_MAX_NUM_SEQS > checks.KEDA_TARGET_QUEUE_DEPTH * checks.KEDA_MAX_REPLICAS


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
    i, j = names.index("llmdendpoint-routed-chat"), names.index("keda-autoscales-llmd-on-queue-depth")
    assert j == i + 1
    assert checks.CHECKS[j][3] is checks.CHECKS[i][3] is checks._only_llmd


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
