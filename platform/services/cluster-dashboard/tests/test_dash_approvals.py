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
def approvals(backend, monkeypatch, fake_db):
    """Backend with the DB enabled; `conn(script)` installs a scripted FakeConn."""
    monkeypatch.setattr(backend, "HAVE_PSYCOPG", True)
    monkeypatch.setattr(backend, "DB_USER", "u")
    monkeypatch.setattr(backend, "DB_PASSWORD", "p")
    monkeypatch.setattr(backend, "psycopg",
                        types.SimpleNamespace(rows=types.SimpleNamespace(dict_row=object())),
                        raising=False)
    posted = []
    state = {"post": (201, {})}

    def k8s_post(path, body):
        posted.append((path, body))
        return state["post"]

    monkeypatch.setattr(backend, "k8s_post", k8s_post)
    ns = types.SimpleNamespace(backend=backend, posted=posted, state=state, conns=[])

    def conn(script):
        c = fake_db(script)
        ns.conns.append(c)
        monkeypatch.setattr(backend.DB, "connect", staticmethod(lambda: c))
        return c

    ns.conn = conn
    return ns


# --- approve -----------------------------------------------------------------

def test_approve_not_found(approvals):
    approvals.conn([("SELECT * FROM investigations", [])])
    assert approvals.backend.approve_investigation(INV, "admin")[0] == 404


def test_approve_wrong_status(approvals):
    approvals.conn([("SELECT * FROM investigations", [{"status": "done", "created_at": _ago(minutes=1)}])])
    status, body = approvals.backend.approve_investigation(INV, "admin")
    assert status == 409 and "done" in body["error"]


def test_approve_expired_marks_row_expired(approvals):
    c = approvals.conn([("SELECT * FROM investigations",
                         [{"status": "awaiting_approval", "created_at": _ago(hours=30)}])])
    status, body = approvals.backend.approve_investigation(INV, "admin")
    assert (status, body["error"]) == (409, "expired")
    assert any("SET status='expired'" in s for s in c.sql())
    assert approvals.posted == []


def test_approve_daily_cap(approvals):
    approvals.conn([
        ("SELECT * FROM investigations", [{"status": "awaiting_approval", "created_at": _ago(minutes=5)}]),
        ("today_counters", [{"remediations": approvals.backend.MAX_REMEDIATIONS_PER_DAY}]),
    ])
    assert approvals.backend.approve_investigation(INV, "admin")[0] == 429
    assert approvals.posted == []


def test_approve_job_create_failure_leaves_row_untouched(approvals):
    c = approvals.conn([
        ("SELECT * FROM investigations", [{"status": "awaiting_approval", "created_at": _ago(minutes=5)}]),
        ("today_counters", [{"remediations": 0}]),
    ])
    approvals.state["post"] = (500, {"message": "boom"})
    status, body = approvals.backend.approve_investigation(INV, "admin")
    assert status == 502 and body["body"] == "boom"
    assert not any("remediating" in s for s in c.sql())


def test_approve_success(approvals):
    c = approvals.conn([
        ("SELECT * FROM investigations", [{"status": "awaiting_approval", "created_at": _ago(minutes=5)}]),
        ("today_counters", [{"remediations": 3}]),
    ])
    status, body = approvals.backend.approve_investigation(INV, "alice@example.com")
    assert status == 200 and body["status"] == "remediating"
    path, job = approvals.posted[0]
    assert path == "/apis/batch/v1/namespaces/ai-platform/jobs"
    assert job["metadata"]["name"] == f"remediator-{INV[:8]}"
    update = [(s, p) for s, p in c.executed if "SET status='remediating'" in s]
    assert update and update[0][1] == ("alice@example.com", INV)
    assert any("INSERT INTO daily_counters" in s for s in c.sql())


def test_approve_db_unavailable(approvals, monkeypatch):
    monkeypatch.setattr(approvals.backend, "HAVE_PSYCOPG", False)
    assert approvals.backend.approve_investigation(INV, "admin")[0] == 503


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
    assert approvals.backend.dismiss_investigation(INV, "admin")[0] == expected
    assert any("SET status='dismissed'" in s for s in c.sql()) == (expected == 200)


def test_dismiss_not_found(approvals):
    approvals.conn([("SELECT status, created_at", [])])
    assert approvals.backend.dismiss_investigation(INV, "admin")[0] == 404


# --- delete and lists ----------------------------------------------------------

def test_delete_one(approvals):
    approvals.conn([("DELETE FROM investigations", 1)])
    assert approvals.backend.delete_investigation(INV)[0] == 200
    approvals.conn([("DELETE FROM investigations", 0)])
    assert approvals.backend.delete_investigation(INV)[0] == 409


def test_delete_all_keeps_in_flight(approvals):
    c = approvals.conn([("DELETE FROM investigations", 3)])
    status, body = approvals.backend.delete_all_investigations()
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
    rows = approvals.backend.list_all_investigations(limit=20)
    assert [r["stale"] for r in rows] == [True, False]
    assert rows[0]["id"] == str(uid) and isinstance(rows[0]["created_at"], str)
    approvals.conn([("FROM investigations", [{"id": uid, "created_at": _ago(minutes=1)}])])
    assert approvals.backend.list_pending_investigations()[0]["id"] == str(uid)
