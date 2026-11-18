"""tools/watchdog.py: the quiet failures of an unattended factory."""

from datetime import datetime, timedelta, timezone

import watchdog

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
ACTIVE = {n: "active" for n in watchdog.FACTORY_WORKFLOWS}


def facts(**over):
    f = {"tokens": {"E2E_PUSH_TOKEN": {"set": True, "valid": True, "expires": NOW + timedelta(days=200)},
                    "PUBLIC_PUSH_TOKEN": {"set": False}},
         "workflows": dict(ACTIVE), "last_scheduled_run": NOW - timedelta(hours=10),
         "last_cycle_merged": NOW - timedelta(days=3), "attention_opened": None}
    f.update(over)
    return f


def test_a_healthy_factory_has_nothing_to_report():
    problems, rows = watchdog.findings(NOW, facts())
    assert problems == []
    table = dict(rows)
    assert table["token `E2E_PUSH_TOKEN`"].startswith("valid, expires in 200 days")
    assert table["token `PUBLIC_PUSH_TOKEN`"] == "not set" and table["workflows"] == "all active"
    body = watchdog.status_body(NOW, problems, rows)
    assert "**Nothing needs attention.**" in body and "| last cycle merged | 3 days ago |" in body


def test_a_token_about_to_expire_or_rejected_is_the_first_thing_said():
    soon = facts(tokens={"E2E_PUSH_TOKEN": {"set": True, "valid": True, "expires": NOW + timedelta(days=12)}})
    problems, _ = watchdog.findings(NOW, soon)
    assert len(problems) == 1 and "expires in 12 days (2026-12-02)" in problems[0]
    dead = facts(tokens={"E2E_PUSH_TOKEN": {"set": True, "valid": False, "expires": None}})
    problems, rows = watchdog.findings(NOW, dead)
    assert "rejected by GitHub" in problems[0] and dict(rows)["token `E2E_PUSH_TOKEN`"] == "**rejected**"
    forever = facts(tokens={"E2E_PUSH_TOKEN": {"set": True, "valid": True, "expires": None}})
    assert watchdog.findings(NOW, forever)[0] == []


def test_a_disabled_or_missing_workflow_and_a_silent_schedule():
    f = facts(workflows={**ACTIVE, "release-candidate": "disabled_inactivity"},
              last_scheduled_run=NOW - timedelta(days=9))
    del f["workflows"]["coverage"]
    problems, rows = watchdog.findings(NOW, f)
    text = "\n".join(problems)
    assert "`release-candidate` is disabled_inactivity" in text and "gh workflow enable release-candidate.yaml" in text
    assert "`coverage` does not exist" in text and "has not run for 9 days" in text
    assert "release-candidate: disabled_inactivity" in dict(rows)["workflows"]
    assert "has not run for ever" in "\n".join(watchdog.findings(NOW, facts(last_scheduled_run=None))[0])


def test_no_progress_and_a_stale_attention_issue():
    f = facts(last_cycle_merged=NOW - timedelta(days=30), attention_opened=NOW - timedelta(days=8))
    problems, _ = watchdog.findings(NOW, f)
    assert any("No cycle has been merged for 30 days" in p for p in problems)
    assert any("has been open for 8 days" in p for p in problems)
    assert watchdog.findings(NOW, facts(last_cycle_merged=NOW - timedelta(days=20),
                                        attention_opened=NOW - timedelta(days=6)))[0] == []


def test_timestamps_of_both_shapes_are_read():
    assert watchdog.parse_time("2026-11-17T19:04:49Z") == datetime(2026, 11, 17, 19, 4, 49, tzinfo=timezone.utc)
    assert watchdog.parse_time(" 2027-01-31 00:00:00 UTC") == datetime(2027, 1, 31, tzinfo=timezone.utc)
    assert watchdog.parse_time("2027-01-31 00:00:00 +0000") == datetime(2027, 1, 31, tzinfo=timezone.utc)
    assert watchdog.parse_time("") is None and watchdog.parse_time("soon") is None


def test_token_check_reads_validity_and_expiry_from_the_response_headers(tmp_path, monkeypatch):
    import os
    fake = tmp_path / "curl"
    fake.write_text('#!/bin/sh\nprintf "%b" "$FAKE_HEAD"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HEAD", "HTTP/2 200\\r\\ngithub-authentication-token-expiration: 2027-01-31 00:00:00 UTC\\r\\n")
    assert watchdog.token_fact("x") == {"set": True, "valid": True,
                                        "expires": datetime(2027, 1, 31, tzinfo=timezone.utc)}
    monkeypatch.setenv("FAKE_HEAD", "HTTP/2 200\\r\\nx-other: 1\\r\\n")
    assert watchdog.token_fact("x") == {"set": True, "valid": True, "expires": None}
    monkeypatch.setenv("FAKE_HEAD", "HTTP/2 401\\r\\n")
    assert watchdog.token_fact("x")["valid"] is False
    assert watchdog.token_fact("") == {"set": False}
