"""Guards on the workflows and the OIDC Terraform: the E2E workflow runs a
provisioning role against a real account, so these properties must not regress."""

import glob
import json
import os
import re
import subprocess

import pytest
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
        for name in ("ci.yaml", "e2e.yaml", "main-schedules.yaml", "release-finish.yaml", "watchdog.yaml"):
            wf, _ = _load(name)
            assert wf["permissions"] == {"contents": "read"}, name

    def test_inputs_never_interpolated_into_run_scripts(self):
        """Script injection: workflow_dispatch inputs reach run: steps through env only."""
        for path in glob.glob(os.path.join(WF_DIR, "*.yaml")):
            wf, _ = _load(os.path.basename(path))
            for job in wf["jobs"].values():
                for s in _steps(job):
                    assert "inputs." not in (s.get("run") or ""), (path, s.get("name"))


class TestWatchdogWorkflow:
    def test_weekly_off_the_hour_github_only(self):
        wf, text = _load("watchdog.yaml")
        on = _on(wf)
        assert on["schedule"] == [{"cron": "23 6 * * 1"}] and "workflow_dispatch" in on
        job = wf["jobs"]["check"]
        assert job["permissions"] == {"contents": "read", "issues": "write", "actions": "read", "pull-requests": "read"}
        assert "environment" not in job and "aws" not in text.lower()
        check = _step(job, "Check tokens")
        assert "--token-env E2E_PUSH_TOKEN=E2E_PUSH_TOKEN" in check["run"]
        assert check["env"]["E2E_PUSH_TOKEN"] == "${{ secrets.E2E_PUSH_TOKEN }}"
        for s in _steps(job):
            assert "${{" not in (s.get("run") or ""), s.get("name")

    def test_it_watches_every_factory_workflow_by_its_real_name(self):
        import watchdog
        names = set()
        for path in glob.glob(os.path.join(WF_DIR, "*.yaml")):
            wf, _ = _load(os.path.basename(path))
            names.add(wf["name"])
        assert set(watchdog.FACTORY_WORKFLOWS) <= names, set(watchdog.FACTORY_WORKFLOWS) - names
        scheduled = set()
        for path in glob.glob(os.path.join(WF_DIR, "*.yaml")):
            wf, _ = _load(os.path.basename(path))
            if "schedule" in _on(wf):
                scheduled.add(wf["name"])
        assert scheduled <= set(watchdog.FACTORY_WORKFLOWS), scheduled - set(watchdog.FACTORY_WORKFLOWS)

    def test_issue_titles_match_the_tool(self):
        import watchdog
        _, text = _load("watchdog.yaml")
        assert f'title="{watchdog.STATUS_TITLE}"' in text and f'title="{watchdog.ATTENTION_TITLE}"' in text


class TestReleaseFinishWorkflow:
    """The only human action in a release is the e2e approval: this workflow
    finishes the release when (and only when) its E2E is green."""

    def test_started_by_e2e_and_by_hand_never_by_workflow_run(self):
        """`on: workflow_run` did not fire for an e2e run that the release candidate
        dispatched with GITHUB_TOKEN (v2026.11.7). e2e.yaml dispatches explicitly."""
        wf, _ = _load("release-finish.yaml")
        on = _on(wf)
        assert list(on) == ["workflow_dispatch"]
        assert set(on["workflow_dispatch"]["inputs"]) == {"version", "dry_run"}
        assert all(not i.get("required") for i in on["workflow_dispatch"]["inputs"].values())   # dispatched bare
        assert "if" not in wf["jobs"]["finish"]
        assert wf["concurrency"] == {"group": "release-finish", "cancel-in-progress": False}

    def test_e2e_starts_it_only_when_every_job_passed(self):
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["finish"]
        assert job["needs"] == ["plan", "e2e"]
        assert job["if"] == "needs.plan.outputs.configured == 'true' && needs.e2e.result == 'success'"
        assert job["permissions"] == {"actions": "write"} and "environment" not in job
        step = _steps(job)[0]
        assert 'gh workflow run release-finish.yaml --repo "$GITHUB_REPOSITORY" --ref lifecycle' in step["run"]
        assert step["env"] == {"GH_TOKEN": "${{ github.token }}"}       # a dispatch with GITHUB_TOKEN starts a run
        assert os.path.exists(os.path.join(WF_DIR, "release-finish.yaml"))

    def test_permissions_and_no_expressions_in_run_scripts(self):
        wf, _ = _load("release-finish.yaml")
        job = wf["jobs"]["finish"]
        assert job["permissions"] == {"contents": "read", "issues": "write"}
        for s in _steps(job):
            assert "${{" not in (s.get("run") or ""), s.get("name")

    def test_finishes_through_the_release_tool_only_when_green(self):
        wf, text = _load("release-finish.yaml")
        job = wf["jobs"]["finish"]
        finish = _step(job, "Finish the release")
        assert '--repo subject finish "$VERSION" --github "$REPO" --if-green' in finish["run"]
        assert "args+=(--dry-run)" in finish["run"]
        assert "gh pr merge" not in text and "git tag" not in text     # release.py owns the checks
        assert finish["env"]["GH_TOKEN"] == "${{ secrets.E2E_PUSH_TOKEN }}"
        assert "tools/release_candidate.py finishable --repo subject" in _step(job, "Find the release")["run"]
        subject = next(s for s in _steps(job) if s.get("with", {}).get("path") == "subject")
        assert subject["with"]["ref"] == "main" and subject["with"]["fetch-depth"] == 0
        issue = _step(job, "Open or update an issue")
        assert issue["if"].startswith("failure()") and issue["env"]["GH_TOKEN"] == "${{ github.token }}"

    def test_publishes_the_newest_release_to_the_public_repository_only_when_configured(self):
        wf, _ = _load("release-finish.yaml")
        step = _step(wf["jobs"]["finish"], "Publish the newest release to the public repository")
        assert "vars.PUBLIC_REPO != ''" in step["if"] and "vars.PUBLIC_SINCE != ''" in step["if"]
        assert "(!inputs.dry_run)" in step["if"]
        run = step["run"]
        assert 'newest-tag --repo subject --since "$PUBLIC_SINCE"' in run          # never an older, internal release
        assert 'publish-public "$tag"' in run and "--url-env PUBLIC_URL" in run and '--github "$PUBLIC_REPO"' in run
        assert "${{" not in run and "PUBLIC_URL" not in step["env"]                 # the token never sits in the YAML
        # actions/checkout makes git send this repository's token to github.com; it must not win.
        assert step["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
        assert step["env"]["GIT_CONFIG_VALUE_0"] == "" and step["env"]["GIT_CONFIG_COUNT"] == "1"
        names = [s.get("name", "") for s in _steps(wf["jobs"]["finish"])]
        assert names.index("Finish the release (merge, tag, publish) if E2E is green") < names.index(step["name"])

    def test_the_factory_restarts_a_publication_that_did_not_happen(self):
        wf, _ = _load("release-candidate.yaml")
        step = _step(wf["jobs"]["candidate"], "Hand a release that is not public yet to release-finish")
        assert "vars.PUBLIC_REPO != ''" in step["if"] and "vars.PUBLIC_SINCE != ''" in step["if"]
        assert 'git ls-remote "https://github.com/${PUBLIC_REPO}.git" "refs/tags/${tag}"' in step["run"]
        assert "gh workflow run release-finish.yaml" in step["run"]

    def test_unconfigured_repo_skips_green(self):
        wf, _ = _load("release-finish.yaml")
        steps = _steps(wf["jobs"]["finish"])
        assert steps[0]["id"] == "cfg"
        for s in steps[1:]:
            assert "steps.cfg.outputs.configured == 'true'" in s.get("if", "") or s["if"].startswith("failure()"), s


class TestE2EWorkflow:
    def test_dispatch_only_with_the_designed_inputs(self):
        wf, _ = _load("e2e.yaml")
        on = _on(wf)
        assert list(on) == ["workflow_dispatch"]
        assert set(on["workflow_dispatch"]["inputs"]) == {"modes", "subject", "base", "profiles", "keep", "with_llmd",
                                                          "note"}
        note = on["workflow_dispatch"]["inputs"]["note"]
        assert note["type"] == "string" and note["default"] == "" and not note.get("required")
        assert on["workflow_dispatch"]["inputs"]["modes"]["options"] == ["fresh", "upgrade", "fresh,upgrade"]

    def test_run_name_says_what_is_tested(self):
        wf, _ = _load("e2e.yaml")
        rn = wf["run-name"]
        for needle in ("inputs.modes", "inputs.subject", "inputs.base", "inputs.profiles", "inputs.keep"):
            assert needle in rn, needle
        assert rn.startswith("${{ inputs.note && format('{0} · ', inputs.note) || '' }}e2e ")   # what changes, first

    def test_the_run_page_says_what_is_waiting_for_approval(self, tmp_path):
        """A job without the environment, so it runs before the approval: it puts
        the candidate's pull request (what changes, what waiting means) on the
        run's summary page. Run for real with a fake gh."""
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["about"]
        assert "environment" not in job and "needs" not in job
        assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
        assert not any("aws" in str(s.get("uses", "")) for s in _steps(job))
        script = _steps(job)[0]["run"]
        fake = tmp_path / "gh"
        fake.write_text('#!/bin/sh\nprintf \'%s\' "$FAKE_PRS"\n')
        fake.chmod(0o755)
        summary = tmp_path / "summary.md"
        env = {"PATH": f"{tmp_path}:{os.environ['PATH']}", "GITHUB_REPOSITORY": "o/r", "GITHUB_STEP_SUMMARY": str(summary),
               "SUBJECT_REF": "release/v2026.11.8", "BASE_REF": "v2026.11.7", "MODES": "fresh,upgrade",
               "PROFILES": "default,managed", "WAIT_DAYS": "5"}
        pr = [{"number": 80, "title": "cycle: v2026.11.8", "url": "https://x/pull/80",
               "body": "### What you are approving\n\n- **An integration cycle.**\n| vllm | `a` | `b` | anchor |"}]
        subprocess.run(["bash", "-c", script], check=True, cwd=tmp_path, env={**env, "FAKE_PRS": json.dumps(pr)})
        text = summary.read_text()
        assert text.startswith("## What this run tests") and "[#80 cycle: v2026.11.8](https://x/pull/80)" in text
        assert "### What you are approving" in text and "| vllm | `a` | `b` | anchor |" in text
        assert "**Not approving** changes nothing" in text and "after 5 days" in text and "$5-10" in text
        summary.write_text("")
        subprocess.run(["bash", "-c", script], check=True, cwd=tmp_path, env={**env, "FAKE_PRS": "[]"})
        text = summary.read_text()
        assert "Ref `release/v2026.11.8`, upgraded from `v2026.11.7`" in text and "started by hand" in text

    def test_one_run_one_approval_for_every_mode_and_profile(self):
        wf, _ = _load("e2e.yaml")
        run = wf["jobs"]["plan"]["steps"][0]["run"]
        assert '"mode\\":\\"$m\\",\\"profile\\":\\"$p\\"' in run and "matrix=" in run
        job = wf["jobs"]["e2e"]
        assert job["name"] == "${{ matrix.mode }} / ${{ matrix.profile }}"
        assert job["env"]["MODE"] == "${{ matrix.mode }}"

    def test_job_is_gated_and_permissions_scoped(self):
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["e2e"]
        assert job["environment"] == "e2e"
        assert job["permissions"] == {"contents": "read", "id-token": "write", "statuses": "write"}
        assert job["timeout-minutes"] == 345
        assert job["needs"] == "plan" and "needs.plan.outputs.configured == 'true'" in job["if"]
        assert job["strategy"]["matrix"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
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
        assert job["env"]["CONTEXT"] == "e2e/${{ matrix.mode }}-${{ matrix.profile }}"
        pending, result = _step(job, "Commit status pending"), _step(job, "Commit status result")
        for s in (pending, result):
            assert 'gh api -X POST "repos/${GITHUB_REPOSITORY}/statuses/${STATUS_SHA}"' in s["run"]
        assert "state=pending" in pending["run"]
        assert result["if"] == "always()" and "state=success" in result["run"] and "state=failure" in result["run"]
        names = [s.get("name", "") for s in _steps(job)]
        assert names.index("Commit status pending") < names.index("Run the harness") < names.index("Commit status result")

    @pytest.mark.parametrize("mode,profile,rc,base,llmd", [
        ("upgrade", "managed", "0", "v2026.11.5", "true"),
        ("upgrade", "default", "1", "main", "false"),
        ("fresh", "default", "0", "main", "true"),
        ("fresh", "managed", "", "v2026.11.5", "true"),       # harness killed: no rc
    ])
    def test_result_status_says_what_was_tested_in_the_format_finish_reads(self, tmp_path, mode, profile, rc,
                                                                            base, llmd):
        """Run the step's own script with a fake gh and compare with release.status_description."""
        import release
        wf, _ = _load("e2e.yaml")
        script = _step(wf["jobs"]["e2e"], "Commit status result")["run"]
        out = tmp_path / "args"
        fake = tmp_path / "gh"
        fake.write_text(f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done > "{out}"\n')
        fake.chmod(0o755)
        env = {"PATH": f"{tmp_path}:{os.environ['PATH']}", "MODE": mode, "PROFILE": profile, "RC": rc,
               "BASE_REF": base, "WITH_LLMD": llmd, "GITHUB_REPOSITORY": "o/r", "STATUS_SHA": "abc",
               "CONTEXT": f"e2e/{mode}-{profile}", "RUN_URL": "https://ci/run/1"}
        subprocess.run(["bash", "-euo", "pipefail", "-c", script], check=True, env=env)
        args = out.read_text().splitlines()
        passed = rc == "0"
        assert f"state={'success' if passed else 'failure'}" in args
        desc = next(a for a in args if a.startswith("description="))[len("description="):]
        assert desc == release.status_description(mode, profile, "passed" if passed else "failed", base=base, llmd=llmd)
        fields = release.status_fields(desc)
        assert fields.get("llmd") == llmd and fields.get("base") == (base if mode == "upgrade" else None)

    def test_preflight_runs_before_the_harness_and_a_full_account_is_not_a_failed_candidate(self, tmp_path):
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["e2e"]
        names = [s.get("name", "") for s in _steps(job)]
        pre = "Preflight (leftover environments, Elastic IP headroom)"
        assert names.index("Configure AWS credentials (OIDC -> E2E provisioning role)") < names.index(pre) \
            < names.index("Run the harness")
        step = _step(job, pre)
        assert step["id"] == "preflight" and 'harness/leftovers.py --env "$ENV_NAME" --region "$E2E_REGION"' in step["run"]
        assert job["outputs"] == {"leaked": "${{ steps.preflight.outputs.leaked }}"}
        result = _step(job, "Commit status result")
        assert result["env"]["BLOCKED"] == "${{ steps.preflight.outputs.blocked }}"
        # Run the status step as it would after a blocked preflight: the harness never ran (no rc).
        out = tmp_path / "args"
        fake = tmp_path / "gh"
        fake.write_text(f'#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done > "{out}"\n')
        fake.chmod(0o755)
        env = {"PATH": f"{tmp_path}:{os.environ['PATH']}", "MODE": "upgrade", "PROFILE": "default", "RC": "",
               "BLOCKED": "true", "BASE_REF": "v2026.11.7", "WITH_LLMD": "true", "GITHUB_REPOSITORY": "o/r",
               "STATUS_SHA": "abc", "CONTEXT": "e2e/upgrade-default", "RUN_URL": "https://ci/run/1"}
        subprocess.run(["bash", "-euo", "pipefail", "-c", result["run"]], check=True, env=env)
        args = out.read_text().splitlines()
        assert "state=pending" in args and "state=failure" not in args          # nothing ran, nothing failed
        assert any(a.startswith("description=E2E upgrade (default) not run (no free Elastic IPs)") for a in args)
        subprocess.run(["bash", "-euo", "pipefail", "-c", result["run"]], check=True, env={**env, "BLOCKED": "false"})
        assert "state=failure" in out.read_text().splitlines()                  # a killed harness is still a failure

    def test_leftover_environments_are_reported_in_an_issue(self):
        wf, _ = _load("e2e.yaml")
        job = wf["jobs"]["leftovers"]
        assert job["needs"] == "e2e" and job["if"] == "always() && needs.e2e.outputs.leaked != ''"
        assert job["permissions"] == {"issues": "write"} and "environment" not in job
        run = _steps(job)[0]["run"]
        assert 'title="E2E environments left behind"' in run and "for c in $LEAKED" in run and "${{" not in run

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


def test_e2e_terraform_matches_the_subject_ci(subject_root):
    """The harness's Terraform is not Renovate-managed (lifecycle is not a base
    branch); keep it equal to what main's CI pins so E2E applies with the same
    Terraform the product is validated with."""
    _, e2e_text = _load("e2e.yaml")
    with open(os.path.join(subject_root, ".github/workflows/ci.yaml"), encoding="utf-8") as fh:
        ci = fh.read()
    want = re.search(r'terraform_version: "([^"]+)"', ci).group(1)
    assert f'terraform_version: "{want}"' in e2e_text, f"e2e.yaml should pin Terraform {want} like main's ci.yaml"
