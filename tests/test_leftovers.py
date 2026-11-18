"""harness/leftovers.py: the preflight that finds leaked E2E environments and
checks Elastic IP headroom. Pure logic plus the CLI with a fake aws."""

import json
import os
from datetime import datetime, timedelta, timezone

import leftovers

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def cluster(name, hours_old):
    return {"name": name, "created": NOW - timedelta(hours=hours_old)}


def test_only_e2e_environments_are_looked_at_and_never_the_job_s_own():
    assert leftovers.e2e_env("ai-platform-e2eud17") == "e2eud17"
    assert leftovers.e2e_env("ai-platform-e2efm9999") == "e2efm9999"
    for other in ("ai-platform-dev", "ai-eks-prod", "ai-platform-e2e", "ai-platform-e2exd1", "e2eud17-suffix"):
        assert leftovers.e2e_env(other) is None, other
    found = leftovers.classify([cluster("ai-platform-dev", 900), cluster("ai-platform-e2eud17", 30),
                                cluster("ai-platform-e2efd18", 0.5), cluster("ai-platform-e2efm18", 9)],
                               "e2efm18", NOW, 8)
    assert [c["env"] for c in found["leaked"]] == ["e2eud17"] and found["leaked"][0]["age_hours"] == 30.0
    assert [c["env"] for c in found["running"]] == ["e2efd18"]          # a job of this run, or a parallel one


def test_headroom_needs_three_addresses_and_unknown_is_not_a_no():
    assert leftovers.headroom(15, 12) is True and leftovers.headroom(15, 13) is False
    assert leftovers.headroom(None, 3) is None and leftovers.headroom(15, None) is None


def test_report_says_what_to_do():
    found = {"leaked": [{"name": "ai-platform-e2eud17", "env": "e2eud17", "age_hours": 30.0}], "running": []}
    text = leftovers.report(found, 15, 14, False)
    assert "`ai-platform-e2eud17`, 30.0 h old" in text and "./platformctl down <env>" in text
    assert "14 of 15 in use" in text and "**not enough. Not provisioning.**" in text
    clean = leftovers.report({"leaked": [], "running": []}, None, None, None)
    assert "No environment left behind" in clean and "could not be read (the job goes on)" in clean


FAKE_AWS = r"""#!/bin/sh
case "$1 $2" in
  "eks list-clusters") printf '%s' "$FAKE_CLUSTERS" ;;
  "eks describe-cluster") printf '{"cluster": {"createdAt": "%s", "status": "ACTIVE"}}' "$FAKE_CREATED" ;;
  "ec2 describe-vpcs") [ -n "$FAKE_VPCS" ] || exit 255; printf '%s' "$FAKE_VPCS" ;;
  "service-quotas get-service-quota") [ -n "$FAKE_QUOTA" ] || exit 255; printf '{"Quota": {"Value": %s}}' "$FAKE_QUOTA" ;;
  "ec2 describe-addresses") printf '%s' "$FAKE_ADDRESSES" ;;
esac
"""


def run_cli(tmp_path, monkeypatch, **env):
    fake = tmp_path / "aws"
    fake.write_text(FAKE_AWS)
    fake.chmod(0o755)
    out, summary = tmp_path / "out", tmp_path / "summary"
    out.write_text("")
    summary.write_text("")
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    defaults = {"FAKE_CLUSTERS": json.dumps({"clusters": ["ai-platform-dev", "ai-platform-e2eud17"]}),
                "FAKE_CREATED": "2020-01-01T00:00:00Z", "FAKE_QUOTA": "15",
                "FAKE_ADDRESSES": json.dumps({"Addresses": [{}] * 6})}
    argv = env.pop("argv", ["--env", "e2efd18", "--region", "eu-central-1"])
    for k, v in {**defaults, **env}.items():
        monkeypatch.setenv(k, v)
    rc = leftovers.main(argv)
    return rc, out.read_text(), summary.read_text()


def test_cli_reports_a_leak_and_goes_on_when_there_is_room(tmp_path, monkeypatch):
    rc, out, summary = run_cli(tmp_path, monkeypatch)
    assert rc == 0 and "leaked=ai-platform-e2eud17\n" in out and "blocked=false" in out
    assert "left behind" in summary and "6 of 15 in use" in summary and "enough." in summary


def test_cli_blocks_without_room_and_says_so_through_the_output(tmp_path, monkeypatch):
    rc, out, summary = run_cli(tmp_path, monkeypatch, FAKE_ADDRESSES=json.dumps({"Addresses": [{}] * 13}))
    assert rc == leftovers.BLOCKED == 3 and "blocked=true" in out and "Not provisioning" in summary


def test_cli_never_blocks_on_what_it_cannot_read(tmp_path, monkeypatch):
    rc, out, summary = run_cli(tmp_path, monkeypatch, FAKE_QUOTA="", FAKE_CLUSTERS="not json")
    assert rc == 0 and "leaked=\n" in out and "blocked=false" in out and "could not be read" in summary


# --- the job's own environment, after its teardown ----------------------------

def test_what_is_left_of_the_job_s_own_environment():
    assert leftovers.own_left([], []) == ""
    assert leftovers.own_left([{"name": "ai-platform-e2eud17", "status": "ACTIVE"}], ["vpc-0abc"]) \
        == "EKS cluster ai-platform-e2eud17 (ACTIVE); VPC vpc-0abc"
    assert leftovers.own_left([], ["vpc-0abc"]) == "VPC vpc-0abc"


OWN = ["--env", "e2eud17", "--region", "eu-central-1", "--own"]


def test_own_says_what_this_job_left_and_nothing_about_other_environments(tmp_path, monkeypatch, capsys):
    rc, out, summary = run_cli(tmp_path, monkeypatch, argv=OWN,
                               FAKE_CLUSTERS=json.dumps({"clusters": ["ai-platform-dev", "ai-platform-e2eud17", "ai-platform-e2efd3"]}),
                               FAKE_VPCS=json.dumps({"Vpcs": [{"VpcId": "vpc-0abc"}]}))
    printed = capsys.readouterr().out.strip()
    assert rc == 0 and printed == "EKS cluster ai-platform-e2eud17 (ACTIVE); VPC vpc-0abc"
    assert "left=EKS cluster ai-platform-e2eud17 (ACTIVE); VPC vpc-0abc\n" in out
    assert "still billing" in summary and "e2efd3" not in summary and "leaked=" not in out


def test_own_is_silent_when_the_environment_is_gone_or_cannot_be_read(tmp_path, monkeypatch, capsys):
    rc, out, summary = run_cli(tmp_path, monkeypatch, argv=OWN,
                               FAKE_CLUSTERS=json.dumps({"clusters": ["ai-platform-dev"]}),
                               FAKE_VPCS=json.dumps({"Vpcs": []}))
    assert rc == 0 and capsys.readouterr().out.strip() == "" and "left=\n" in out and summary == ""
    rc, out, _ = run_cli(tmp_path, monkeypatch, argv=OWN, FAKE_CLUSTERS="not json", FAKE_VPCS="")
    assert rc == 0 and capsys.readouterr().out.strip() == ""        # unknown is not a leak
