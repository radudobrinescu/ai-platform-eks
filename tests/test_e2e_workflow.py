"""Guard: the E2E workflow keeps its safety properties.

It runs a PROVISIONING role against a real account, so these must never regress:
  * trigger is `pull_request`, never `pull_request_target` (fork PRs must not
    receive secrets or the id-token);
  * the job runs in the `e2e` GitHub Environment (required-reviewer gate);
  * it skips cleanly when the repo variables/secret are unset (forks);
  * teardown has an always() safety net;
  * the OIDC Terraform pins the subject to one repo + one environment and has no
    backend.tf (so apply-all never picks it up).
"""

import os

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(REPO_ROOT, ".github", "workflows", "e2e.yaml")
OIDC = os.path.join(REPO_ROOT, "terraform", "ci", "github-oidc")


def _wf():
    with open(WF, encoding="utf-8") as fh:
        text = fh.read()
    return yaml.safe_load(text), text


class TestWorkflowSafety:
    def test_trigger_is_pull_request_not_target(self):
        wf, _ = _wf()
        on = wf[True] if True in wf else wf["on"]  # PyYAML parses `on:` as boolean True
        assert "pull_request" in on and "pull_request_target" not in on
        assert "workflow_dispatch" in on

    def test_job_uses_protected_environment_and_oidc(self):
        wf, _ = _wf()
        job = wf["jobs"]["gate"]
        assert job["environment"] == "e2e"
        assert wf["permissions"]["id-token"] == "write"
        assert wf["permissions"]["contents"] == "read"

    def test_label_gate_and_skip_guard(self):
        wf, text = _wf()
        assert "contains(github.event.pull_request.labels.*.name, 'e2e')" in wf["jobs"]["gate"]["if"]
        assert "E2E_ROLE_ARN" in text and "E2E_TFVARS" in text and "configured=false" in text

    def test_teardown_safety_net_always_runs(self):
        wf, _ = _wf()
        steps = wf["jobs"]["gate"]["steps"]
        net = next(s for s in steps if s.get("name", "").startswith("Safety net"))
        assert "always()" in net["if"]
        assert "platformctl down" in net["run"] and "--yes" in net["run"]

    def test_tfvars_never_echoed(self):
        _, text = _wf()
        assert "cat \"$f\"" not in text and "echo \"$TFVARS\"" not in text


class TestOidcModule:
    def test_outside_apply_all_discovery(self):
        assert not os.path.exists(os.path.join(OIDC, "backend.tf"))

    def test_subject_pinned_to_repo_and_environment(self):
        with open(os.path.join(OIDC, "main.tf"), encoding="utf-8") as fh:
            tf = fh.read()
        assert 'subject = "repo:${var.github_repository}:environment:${var.github_environment}"' in tf
        assert 'variable = "${local.oidc_host}:sub"' in tf and 'test     = "StringEquals"' in tf
        assert "protected_account_ids" in tf and 'sid    = "DenyIdentityAndEscalation"' in tf
