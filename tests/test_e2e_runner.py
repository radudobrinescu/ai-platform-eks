"""Static tests for the E2E gate (no cluster, no AWS).

  * ops/lib/e2e.py's check registry covers every upgrade domain named in
    COMPATIBILITY.md, so a domain can't silently lose its runtime proof;
  * the report redacts account ids, ARNs and AWS hostnames (it is posted on
    public PRs);
  * `recommend-instance.py --emit` writes the manifest and never reaches the
    git/deploy path;
  * platformctl advertises `e2e` and `down --yes` (shellcheck covers syntax in CI).
"""

import argparse
import os
import re
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "ops", "lib"))

import e2e


def _compat_domains():
    with open(os.path.join(REPO_ROOT, "COMPATIBILITY.md"), encoding="utf-8") as fh:
        text = fh.read()
    # rows of the "Upgrade domains" table: | `infra` | ...
    return set(re.findall(r"^\| `([a-z]+)` \|", text, flags=re.MULTILINE))


class TestRegistryCoversDomains:
    def test_every_cluster_facing_domain_has_a_check(self):
        want = _compat_domains() - {"tooling"}  # tooling is proven by ci.yaml, not on a cluster
        assert want, "could not parse domains from COMPATIBILITY.md"
        assert want <= set(e2e.DOMAINS), f"domains without an E2E check: {want - set(e2e.DOMAINS)}"

    def test_check_names_are_unique_and_callable(self):
        names = [n for _, n, _, _ in e2e.CHECKS]
        assert len(names) == len(set(names))
        assert all(callable(fn) for _, _, fn, _ in e2e.CHECKS)

    def test_gpu_check_runs_before_its_dependents(self):
        order = [n for _, n, _, _ in e2e.CHECKS]
        gpu = order.index("vllmendpoint-gpu-chat")
        assert gpu < order.index("karpenter-nodeclaim-ready")
        assert gpu < order.index("dcgm-gpu-metrics")


class TestReportRedaction:
    def test_identifiers_are_redacted(self):
        rep = e2e.Report(env="e2e", started="2026-10-19T00:00:00Z", finished="2026-10-19T01:00:00Z")
        rep.results.append(e2e.CheckResult(
            "infra", "alb", False,
            "k8s-aiplatf-abc123-1234567890.eu-central-1.elb.amazonaws.com in account 802019299867 "
            "arn:aws:iam::802019299867:role/ai-platform-e2e-github",
            12.0))
        md = e2e.render_markdown(rep)
        js = e2e.to_json(rep)
        for leak in ("802019299867", "k8s-aiplatf-abc123", "arn:aws:iam"):
            assert leak not in md, f"{leak!r} leaked into markdown"
            assert leak not in js, f"{leak!r} leaked into json"
        assert "FAILED" in md

    def test_versions_under_test_reads_repo_pins(self):
        v = e2e.versions_under_test(REPO_ROOT)
        assert v.get("vllm", "").startswith("v")
        assert "chart/gpu-operator" in v and "chart/langfuse" in v
        assert "litellm" in v

    def test_skipped_optional_checks_do_not_fail_the_report(self):
        rep = e2e.Report(env="e2e", started="x")
        rep.results.append(e2e.CheckResult("inference", "llmd", True, "skipped", 0, skipped=True))
        rep.results.append(e2e.CheckResult("gitops", "argo", True, "ok", 1))
        assert rep.passed


class TestEmitFlag:
    def test_emit_writes_manifest_and_never_deploys(self, tmp_path, monkeypatch):
        from recommend_instance import cli
        out = tmp_path / "cr.yaml"
        assert cli.emit_manifest(str(out), "kind: VLLMEndpoint\n", "demo") == 0
        assert out.read_text() == "kind: VLLMEndpoint\n"

    def test_emit_and_deploy_are_mutually_exclusive(self):
        from recommend_instance import cli
        with pytest.raises(SystemExit):
            cli.main(["Qwen/Qwen2.5-0.5B-Instruct", "--emit", "/tmp/x.yaml", "--deploy"])

    def test_bedrock_emit_short_circuits_before_deploy(self, tmp_path, monkeypatch):
        """Drive bedrock._enroll with stubbed AWS lookups; --emit must write the
        CR and return 0 without touching gitops.deploy_model."""
        from recommend_instance import bedrock, gitops
        monkeypatch.setattr(bedrock, "list_text_models", lambda region: [{"modelId": "amazon.nova-lite-v1:0", "providerName": "Amazon"}])
        monkeypatch.setattr(bedrock, "system_profiles_by_model", lambda region: {})
        monkeypatch.setattr(bedrock, "resolve", lambda region, q, fms, profiles: (fms[0], "amazon.nova-lite-v1:0", False))
        monkeypatch.setattr(bedrock, "token_prices", lambda region, fm: (0.00000006, 0.00000024))
        monkeypatch.setattr(gitops, "deploy_model", lambda *a, **k: pytest.fail("deploy_model must not be called with --emit"))
        out = tmp_path / "bedrock.yaml"
        args = argparse.Namespace(model="nova-lite", model_name=None, input_cost=None, output_cost=None,
                                  deploy=False, emit=str(out), yes=True, json=False, hf_token=None)
        assert bedrock._enroll("eu-central-1", args) == 0
        text = out.read_text()
        assert "kind: BedrockModel" in text and "name: nova-lite" in text


class TestPlatformctlSurface:
    def test_usage_lists_e2e_and_down_yes(self):
        out = subprocess.run([os.path.join(REPO_ROOT, "platformctl"), "--help"], capture_output=True, text=True, check=False).stdout
        assert re.search(r"^\s*\./platformctl e2e\s", out, flags=re.MULTILINE), out
        assert "--yes" in out

    def test_down_has_yes_guard_and_e2e_dispatch(self):
        with open(os.path.join(REPO_ROOT, "platformctl"), encoding="utf-8") as fh:
            s = fh.read()
        assert 'if [ "$yes" -eq 0 ]; then\n    read -r -p "Type the environment name' in s
        assert 'e2e)       cmd_e2e "$@" ;;' in s
