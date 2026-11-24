"""Approval flow: every outcome of approve, dismiss and delete, against a fake
agent database and a fake Kubernetes Job API."""
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

INV = str(uuid.UUID(int=0x1234abcd))


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


@pytest.fixture
def approvals(fake_db):
    """dashboard.approvals.Approvals over a scripted FakeConn and a fake Job API;
    `conn(script)` installs the connection the next call uses."""
    from dashboard import config
    from dashboard.approvals import Approvals
    from dashboard.k8s import ApiError

    posted = []
    state = {"post": None}
    ns = types.SimpleNamespace(posted=posted, state=state, conns=[], current=None)

    def create_job(spec):
        posted.append(("/apis/batch/v1/namespaces/ai-platform/jobs", spec))
        if state["post"] is not None:
            raise ApiError(*state["post"])

    def conn(script):
        ns.current = fake_db(script)
        ns.conns.append(ns.current)
        return ns.current

    settings = config.load({})
    ns.api = Approvals(settings, lambda: ns.current, create_job,
                       lambda i: {"metadata": {"name": f"remediator-{i[:8]}"}})
    ns.max_per_day = settings.max_remediations_per_day
    ns.conn = conn
    ns.unavailable = Approvals(settings, None, create_job, lambda i: {})
    return ns


# --- approve -----------------------------------------------------------------

def test_approve_not_found(approvals):
    approvals.conn([("SELECT * FROM investigations", [])])
    assert approvals.api.approve(INV, "admin")[0] == 404


def test_approve_wrong_status(approvals):
    approvals.conn([("SELECT * FROM investigations", [{"status": "done", "created_at": _ago(minutes=1)}])])
    status, body = approvals.api.approve(INV, "admin")
    assert status == 409 and "done" in body["error"]


def test_approve_expired_marks_row_expired(approvals):
    c = approvals.conn([("SELECT * FROM investigations",
                         [{"status": "awaiting_approval", "created_at": _ago(hours=30)}])])
    status, body = approvals.api.approve(INV, "admin")
    assert (status, body["error"]) == (409, "expired")
    assert any("SET status='expired'" in s for s in c.sql())
    assert approvals.posted == []


def test_approve_daily_cap(approvals):
    approvals.conn([
        ("SELECT * FROM investigations", [{"status": "awaiting_approval", "created_at": _ago(minutes=5)}]),
        ("today_counters", [{"remediations": approvals.max_per_day}]),
    ])
    assert approvals.api.approve(INV, "admin")[0] == 429
    assert approvals.posted == []


def test_approve_job_create_failure_leaves_row_untouched(approvals):
    c = approvals.conn([
        ("SELECT * FROM investigations", [{"status": "awaiting_approval", "created_at": _ago(minutes=5)}]),
        ("today_counters", [{"remediations": 0}]),
    ])
    approvals.state["post"] = (500, "boom")
    status, body = approvals.api.approve(INV, "admin")
    assert status == 502 and "boom" in body["body"]
    assert not any("remediating" in s for s in c.sql())


def test_approve_success(approvals):
    c = approvals.conn([
        ("SELECT * FROM investigations", [{"status": "awaiting_approval", "created_at": _ago(minutes=5)}]),
        ("today_counters", [{"remediations": 3}]),
    ])
    status, body = approvals.api.approve(INV, "alice@example.com")
    assert status == 200 and body["status"] == "remediating"
    _path, job = approvals.posted[0]
    assert job["metadata"]["name"] == f"remediator-{INV[:8]}"
    update = [(s, p) for s, p in c.executed if "SET status='remediating'" in s]
    assert update and update[0][1] == ("alice@example.com", INV)
    assert any("INSERT INTO daily_counters" in s for s in c.sql())


def test_db_unavailable(approvals):
    u = approvals.unavailable
    assert [u.approve(INV, "a")[0], u.dismiss(INV, "a")[0], u.delete(INV)[0], u.delete(None)[0]] == [503] * 4
    assert (u.pending(), u.recent(5), u.get(INV)) == ([], [], None)


# --- dismiss -----------------------------------------------------------------

@pytest.mark.parametrize("status,age,expected", [
    ("awaiting_approval", {"minutes": 1}, 200),
    ("running", {"minutes": 2}, 409),        # still in progress
    ("running", {"minutes": 20}, 200),       # stuck past STALE_INVESTIGATION_MINUTES
    ("remediating", {"minutes": 20}, 200),
    ("done", {"minutes": 1}, 409),
])
def test_dismiss(approvals, status, age, expected):
    c = approvals.conn([("SELECT status, created_at", [{"status": status, "created_at": _ago(**age)}])])
    assert approvals.api.dismiss(INV, "admin")[0] == expected
    assert any("SET status='dismissed'" in s for s in c.sql()) == (expected == 200)


def test_dismiss_not_found(approvals):
    approvals.conn([("SELECT status, created_at", [])])
    assert approvals.api.dismiss(INV, "admin")[0] == 404


# --- delete and lists ----------------------------------------------------------

def test_delete_one(approvals):
    approvals.conn([("DELETE FROM investigations", 1)])
    assert approvals.api.delete(INV)[0] == 200
    approvals.conn([("DELETE FROM investigations", 0)])
    assert approvals.api.delete(INV)[0] == 409


def test_delete_all_keeps_in_flight(approvals):
    c = approvals.conn([("DELETE FROM investigations", 3)])
    status, body = approvals.api.delete(None)
    assert (status, body["deleted"]) == (200, 3)
    assert "NOT IN ('running','remediating','awaiting_approval')" in c.sql()[0]


def test_lists_serialize_ids_and_flag_stale_rows(approvals):
    uid = uuid.UUID(int=7)
    approvals.conn([("FROM investigations", [
        {"id": uid, "created_at": _ago(minutes=30), "status": "running",
         "completed_at": None, "approved_at": None},
        {"id": uid, "created_at": _ago(minutes=1), "status": "running",
         "completed_at": None, "approved_at": None},
    ])])
    rows = approvals.api.recent(20)
    assert [r["stale"] for r in rows] == [True, False]
    assert rows[0]["id"] == str(uid) and isinstance(rows[0]["created_at"], str)
    approvals.conn([("FROM investigations", [{"id": uid, "created_at": _ago(minutes=1)}])])
    assert approvals.api.pending()[0]["id"] == str(uid)
