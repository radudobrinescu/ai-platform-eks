"""Agent throttling: why an incident is skipped instead of investigated, and
what is recorded when one is investigated."""
from datetime import datetime, timedelta, timezone

import pytest


def _now_minus(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


FREE = [("status IN ('running','awaiting_approval','remediating')", []),
        ("out_of_scope = true", []),
        ("FROM debounce", []),
        ("active_investigation_count", [(0,)]),
        ("today_counters", [(0,)])]


def _script(**override):
    keys = ["inflight", "scope", "debounce", "active", "today"]
    return [(frag, override.get(k, rows)) for k, (frag, rows) in zip(keys, FREE)]


@pytest.mark.parametrize("override,reason", [
    ({}, None),
    ({"inflight": [("abc",)]}, "already_in_flight(abc)"),
    ({"scope": [(1,)]}, "out_of_scope_cooldown"),
    ({"debounce": [(_now_minus(seconds=30),)]}, "debounce"),
    ({"debounce": [(_now_minus(hours=2),)]}, None),          # window passed
    ({"active": [(3,)]}, "concurrency_cap(3/3)"),
    ({"today": [(50,)]}, "daily_budget(50/50)"),
])
def test_should_throttle(event_watcher, fake_db, override, reason):
    conn = fake_db(_script(**override))
    assert event_watcher.should_throttle(conn, "Deployment", "inference", "qwen") == reason


def test_record_trigger(event_watcher, fake_db):
    conn = fake_db()
    event_watcher.record_trigger(conn, "Deployment", "inference", "qwen")
    stmts = conn.executed
    assert "INSERT INTO debounce" in stmts[0][0]
    assert stmts[0][1] == ("Deployment", "inference", "qwen")
    assert "INSERT INTO daily_counters" in stmts[1][0]
