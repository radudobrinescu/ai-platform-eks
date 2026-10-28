"""Guards on the workflows and the OIDC Terraform: the E2E workflow runs a
provisioning role against a real account, so these properties must not regress."""

import glob
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF_DIR = os.path.join(ROOT, ".github", "workflows")
OIDC = os.path.join(ROOT, "terraform", "github-oidc")


def _load(name):
    with open(os.path.join(WF_DIR, name), encoding="utf-8") as fh:
        text = fh.read()
    wf = yaml.safe_load(text)
    return wf, text


def _on(wf):
    return wf[True] if True in wf else wf["on"]  # PyYAML parses `on:` as boolean True


def _steps(job):
    return job.get("steps", [])


def _step(job, prefix):
    return next(s for s in _steps(job) if s.get("name", "").startswith(prefix))


class TestAllWorkflows:
    def test_no_pull_request_target_anywhere(self):
        for path in glob.glob(os.path.join(WF_DIR, "*.yaml")):
            wf, text = _load(os.path.basename(path))
            assert "pull_request_target" not in _on(wf), path
            assert "pull_request_target" not in text, path

    def test_top_level_permissions_are_read_only(self):
        for name in ("ci.yaml", "e2e.yaml", "main-schedules.yaml"):
            wf, _ = _load(name)
            assert wf["permissions"] == {"contents": "read"}, name

    def test_inputs_never_interpolated_into_run_scripts(self):
        """Script injection: workflow_dispatch inputs reach run: steps through env only."""
        for path in glob.glob(os.path.join(WF_DIR, "*.yaml")):
            wf, _ = _load(os.path.basename(path))
            for job in wf["jobs"].values():
                for s in _steps(job):
                    assert "inputs." not in (s.get("run") or ""), (path, s.get("name"))


class TestE2EWorkflow:
    def test_dispatch_only_with_the_designed_inputs(self):
        wf, _ = _load("e2e.yaml")
        on = _on(wf)
        assert list(on) == ["workflow_dispatch"]
        assert set(on["workflow_dispatch"]["inputs"]) == {"mode", "subject", "base", "profiles", "keep", "with_llmd"}
        assert on["workflow_dispatch"]["inputs"]["mode"]["options"] == ["fresh", "upgrade"]

    def test_job_is_gated_and_permissions_scoped(self):
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["e2e"]
        assert job["environment"] == "e2e"
        assert job["permissions"] == {"contents": "read", "id-token": "write", "statuses": "write"}
        assert job["timeout-minutes"] == 345
        assert job["needs"] == "plan" and "needs.plan.outputs.configured == 'true'" in job["if"]
        assert "fromJSON(needs.plan.outputs.profiles)" in job["strategy"]["matrix"]["profile"]
        assert job["strategy"]["fail-fast"] is False

    def test_skip_guard_needs_no_environment_and_no_aws(self):
        wf, text = _load("e2e.yaml")
        plan = wf["jobs"]["plan"]
        assert "environment" not in plan and "permissions" not in plan
        assert not any("aws" in str(s.get("uses", "")) for s in _steps(plan))
        assert "E2E_ROLE_ARN" in text and "E2E_PUSH_TOKEN" in text and "configured=false" in text

    def test_two_checkouts_and_push_token(self):
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["e2e"]
        checkouts = [s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/checkout@")]
        assert len(checkouts) == 2
        assert "path" not in (checkouts[0].get("with") or {})
        sub = checkouts[1]["with"]
        assert sub["path"] == "subject" and sub["fetch-depth"] == 0
        assert sub["token"] == "${{ secrets.E2E_PUSH_TOKEN }}"
        assert "inputs.base" in sub["ref"] and "inputs.subject" in sub["ref"]

    def test_long_session_and_egress_ip(self):
        wf, text = _load("e2e.yaml")
        job = wf["jobs"]["e2e"]
        creds = next(s for s in _steps(job) if "configure-aws-credentials" in str(s.get("uses", "")))
        assert creds["with"]["role-duration-seconds"] == 21600
        assert creds["with"]["role-to-assume"] == "${{ vars.E2E_ROLE_ARN }}"
        assert "https://checkip.amazonaws.com" in text and "--operator-cidr" in text
        assert "vars.E2E_REGION || 'eu-central-1'" in text
        for v in ("E2E_IDC_INSTANCE_ARN", "E2E_IDC_REGION", "E2E_IDC_GROUP_ID"):
            assert f"${{{{ vars.{v} }}}}" in text

    def test_commit_statuses_pending_then_result_on_subject_sha(self):
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["e2e"]
        assert job["env"]["CONTEXT"] == "e2e/${{ inputs.mode }}-${{ matrix.profile }}"
        pending, result = _step(job, "Commit status pending"), _step(job, "Commit status result")
        for s in (pending, result):
            assert 'gh api -X POST "repos/${GITHUB_REPOSITORY}/statuses/${STATUS_SHA}"' in s["run"]
        assert "state=pending" in pending["run"]
        assert result["if"] == "always()" and "state=success" in result["run"] and "state=failure" in result["run"]
        names = [s.get("name", "") for s in _steps(job)]
        assert names.index("Commit status pending") < names.index("Run the harness") < names.index("Commit status result")

    def test_teardown_safety_net(self):
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["e2e"]
        net = _step(job, "Safety net")
        assert "always()" in net["if"] and "inputs.keep != true" in net["if"]
        assert "./platformctl down" in net["run"] and "printf '%s\\n' \"$ENV_NAME\" |" in net["run"]
        harness = _step(job, "Run the harness")
        assert harness["timeout-minutes"] < job["timeout-minutes"]
        assert "python3 harness/e2e.py" in harness["run"] and "--subject-dir subject" in harness["run"]

    def test_ecr_public_login_before_the_harness(self):
        wf, _ = _load("e2e.yaml")
        names = [s.get("name", "") for s in _steps(wf["jobs"]["e2e"])]
        login = next(i for i, n in enumerate(names) if n.startswith("Log in to ECR Public"))
        creds = next(i for i, n in enumerate(names) if n.startswith("Configure AWS credentials"))
        assert creds < login < names.index("Run the harness")
        run = _step(wf["jobs"]["e2e"], "Log in to ECR Public")["run"]
        assert "ecr-public get-login-password --region us-east-1" in run and "--password-stdin public.ecr.aws" in run

    def test_tfvars_never_echoed(self):
        _, text = _load("e2e.yaml")
        assert ".tfvars" not in re.sub(r'\[ -f "terraform/00.global/vars/\$\{ENV_NAME\}.tfvars" \]', "", text)


class TestCiAndCoverage:
    def test_ci_runs_on_lifecycle_against_main(self):
        wf, text = _load("ci.yaml")
        on = _on(wf)
        assert on["push"]["branches"] == ["lifecycle"] and on["pull_request"]["branches"] == ["lifecycle"]
        py = wf["jobs"]["python"]
        sub = next(s for s in _steps(py) if (s.get("with") or {}).get("path") == "subject")
        assert sub["with"]["ref"] == "main"
        for needle in ("python3 -m pytest", "ruff check", "shellcheck", "renovate-config-validator --strict",
                       "terraform fmt -check", "init -backend=false", "validate"):
            assert needle in text, needle
        assert 'REQUIRE_SUBJECT: "1"' in text

    def test_coverage_guard_schedule_and_issue(self):
        wf, text = _load("coverage.yaml")
        on = _on(wf)
        assert on["schedule"] and on["push"]["branches"] == ["lifecycle"]
        assert wf["permissions"] == {"contents": "read", "issues": "write"}
        assert "tests/test_renovate_coverage.py" in text and "ref: main" in text
        issue = _step(wf["jobs"]["guard"], "Open or update an issue")
        assert issue["if"] == "failure()" and "gh issue create" in issue["run"]


class TestMainSchedulesRelay:
    """`schedule` fires only on the default branch (`lifecycle`); the relay runs main's."""

    def _relay(self):
        wf, _ = _load("main-schedules.yaml")
        job = wf["jobs"]["relay"]
        step = _step(job, "Dispatch on main")
        return wf, job, step, step["env"]["WORKFLOWS"].split()

    def test_relay_shape(self):
        wf, job, step, listed = self._relay()
        on = _on(wf)
        assert on["schedule"] and "workflow_dispatch" in on
        assert job["permissions"] == {"contents": "read", "actions": "write"}
        assert listed and 'gh workflow run "$wf" --repo "$REPO" --ref main' in step["run"]

    def test_every_scheduled_workflow_on_main_is_relayed(self, subject_root):
        _, _, _, listed = self._relay()
        scheduled = set()
        for path in glob.glob(os.path.join(subject_root, ".github", "workflows", "*.y*ml")):
            with open(path, encoding="utf-8") as fh:
                wf = yaml.safe_load(fh)
            on = _on(wf)
            if isinstance(on, dict) and "schedule" in on:
                name = os.path.basename(path)
                scheduled.add(name)
                assert "workflow_dispatch" in on, f"{name} on main is scheduled but cannot be dispatched"
        assert scheduled <= set(listed), f"not relayed: {sorted(scheduled - set(listed))}"


class TestOidcModule:
    def _tf(self):
        with open(os.path.join(OIDC, "main.tf"), encoding="utf-8") as fh:
            return fh.read()

    def test_local_state_only(self):
        assert not glob.glob(os.path.join(OIDC, "backend*.tf"))
        assert "backend " not in self._tf()

    def test_trust_pinned_to_listed_repos_and_environment(self):
        tf = self._tf()
        assert 'subjects = [for r in var.github_repositories : "repo:${r}:environment:${var.github_environment}"]' in tf
        assert 'variable = "${local.oidc_host}:sub"' in tf and "values   = local.subjects" in tf
        with open(os.path.join(OIDC, "variables.tf"), encoding="utf-8") as fh:
            assert re.search(r'variable "github_repositories" \{[^}]*type\s+= list\(string\)', fh.read())

    def test_session_and_guardrails(self):
        tf = self._tf()
        assert "max_session_duration = 21600" in tf
        assert "protected_account_ids" in tf
        for sid in ("DenyIdentityAndEscalation", "DenyTouchingGitHubOidcProvider", "DenyEditingOwnTrust",
                    "DenyDestroyingTerraformState"):
            assert f'sid    = "{sid}"' in tf or f'sid       = "{sid}"' in tf or f'sid     = "{sid}"' in tf, sid

    def test_eks_irsa_provider_is_not_blocked(self):
        """The product's EKS module creates and deletes its IRSA OIDC provider;
        a blanket Deny on those actions would break every run."""
        deny = self._tf().split('sid    = "DenyIdentityAndEscalation"')[1].split("statement {")[0]
        assert "OpenIDConnectProvider" not in deny

    def test_no_real_account_id(self):
        for path in glob.glob(os.path.join(OIDC, "*.tf")):
            with open(path, encoding="utf-8") as fh:
                assert not re.search(r"\b\d{12}\b", fh.read()), path
