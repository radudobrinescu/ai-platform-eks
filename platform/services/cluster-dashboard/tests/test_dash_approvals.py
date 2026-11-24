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
    state = {"post": None, "error": None}
    ns = types.SimpleNamespace(posted=posted, state=state, conns=[], current=None)

    def create_job(spec):
        posted.append(("/apis/batch/v1/namespaces/ai-platform/jobs", spec))
        if state["error"] is not None:
            raise state["error"]
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


def _ready(script_extra=()):
    return [("SELECT * FROM investigations", [{"status": "awaiting_approval", "created_at": _ago(minutes=5)}]),
            ("today_counters", [{"remediations": 3}]), *script_extra]


def test_approve_success_claims_before_creating_the_job(approvals):
    c = approvals.conn(_ready([("SET status='remediating'", 1)]))
    status, body = approvals.api.approve(INV, "alice@example.com")
    assert status == 200 and body["status"] == "remediating"
    assert approvals.posted[0][1]["metadata"]["name"] == f"remediator-{INV[:8]}"
    claim = [(s, p) for s, p in c.executed if "SET status='remediating'" in s]
    assert claim[0][1] == ("alice@example.com", INV)
    assert "WHERE id=%s AND status='awaiting_approval'" in claim[0][0]
    assert any("INSERT INTO daily_counters" in s for s in c.sql())


def test_concurrent_approval_loses_the_claim(approvals):
    approvals.conn(_ready([("SET status='remediating'", 0)]))
    assert approvals.api.approve(INV, "bob") == (409, {"error": "already approved"})
    assert approvals.posted == [], "the loser must not create a Job"


def test_existing_job_counts_as_success(approvals):
    c = approvals.conn(_ready([("SET status='remediating'", 1)]))
    approvals.state["post"] = (409, "jobs.batch already exists")
    assert approvals.api.approve(INV, "alice")[0] == 200
    assert not any("SET status='awaiting_approval'" in s for s in c.sql())


def test_failed_job_create_releases_the_claim(approvals):
    c = approvals.conn(_ready([("SET status='remediating'", 1)]))
    approvals.state["post"] = (500, "boom")
    status, body = approvals.api.approve(INV, "alice")
    assert status == 502 and "boom" in body["body"]
    assert any("SET status='awaiting_approval', approved_by=NULL" in s for s in c.sql())
    assert not any("INSERT INTO daily_counters" in s for s in c.sql())


class MaxRetryError(Exception):
    """Stands in for urllib3's: the kubernetes package raises it (not an API
    status) when the API server cannot be reached."""


@pytest.mark.parametrize("error", [MaxRetryError("Max retries exceeded with url: /apis/batch/v1/jobs"),
                                   TimeoutError("timed out")])
def test_job_create_failing_without_an_api_status_releases_the_claim(approvals, error):
    c = approvals.conn(_ready([("SET status='remediating'", 1)]))
    approvals.state["error"] = error
    status, body = approvals.api.approve(INV, "alice")
    assert (status, body["error"], body["status"]) == (502, "job create failed", None)
    assert str(error) in body["body"] and type(error).__name__ in body["body"]
    assert any("SET status='awaiting_approval', approved_by=NULL" in s for s in c.sql()), "claim must be released"
    assert not any("INSERT INTO daily_counters" in s for s in c.sql())


def test_failing_to_build_the_job_spec_releases_the_claim(approvals):
    c = approvals.conn(_ready([("SET status='remediating'", 1)]))

    def broken_spec(_inv_id):
        raise KeyError("python_image")

    approvals.api.remediator_job = broken_spec
    assert approvals.api.approve(INV, "alice")[0] == 502
    assert any("SET status='awaiting_approval', approved_by=NULL" in s for s in c.sql())
    assert approvals.posted == []


def test_db_unavailable(approvals):
    u = approvals.unavailable
    assert [u.approve(INV, "a")[0], u.dismiss(INV, "a")[0], u.delete(INV)[0], u.delete(None)[0]] == [503] * 4
    assert (u.pending(), u.recent(5), u.get(INV)) == ([], [], None)


# --- dismiss -----------------------------------------------------------------

def test_dismiss_is_one_conditional_update(approvals):
    c = approvals.conn([("SET status='dismissed'", 1)])
    assert approvals.api.dismiss(INV, "admin")[0] == 200
    sql, params = c.executed[0]
    assert "status='awaiting_approval'" in sql and "status IN ('pending','running','remediating')" in sql
    assert params == ("admin", INV, 15), "stale threshold comes from STALE_INVESTIGATION_MINUTES"


@pytest.mark.parametrize("row,expected", [
    (None, (404, {"error": "not found"})),
    ({"status": "running"}, (409, {"error": "investigation still in progress"})),
    ({"status": "done"}, (409, {"error": "status is done, cannot dismiss"})),
])
def test_dismiss_refused_explains_why(approvals, row, expected):
    approvals.conn([("SET status='dismissed'", 0), ("SELECT status, created_at", [row] if row else [])])
    assert approvals.api.dismiss(INV, "admin") == expected


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
