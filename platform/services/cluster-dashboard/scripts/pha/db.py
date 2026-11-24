"""Agent database access, shared by the event-watcher and the dashboard.

Every function takes an open connection, so callers own connection lifetime
(the watcher keeps one per watch thread; the dashboard opens one per request)
and tests can pass a fake. Schema: ddl.sql.

psycopg is optional at import time: the dashboard must still start (degraded,
approvals off) when the package could not be installed.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

try:
    import psycopg  # type: ignore[import-not-found]
except ImportError:  # dashboard degraded mode; the agent always has it
    psycopg = None

log = logging.getLogger("pha.db")

IN_FLIGHT = ("pending", "running", "remediating")


def dict_rows():
    """Row factory for dict rows, or None (fakes ignore it)."""
    return psycopg.rows.dict_row if psycopg is not None else None


def connect(*, host: str, port: int, dbname: str, user: str, password: str,
            connect_timeout: int = 10):
    return psycopg.connect(host=host, port=port, dbname=dbname, user=user,
                           password=password, autocommit=True,
                           connect_timeout=connect_timeout)


def connect_with_retry(factory, *, max_backoff: int = 60, sleep=time.sleep):
    """Call `factory()` until it returns a connection, backing off 1 s doubling
    to `max_backoff`. Used by long-running watch loops."""
    backoff = 1
    while True:
        try:
            conn = factory()
            log.info("postgres connected")
            return conn
        except Exception as e:  # any connect failure is retried, never fatal
            log.warning("postgres connect failed: %s; retry in %ds", e, backoff)
            sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


def _iso(row: dict, *keys: str) -> dict:
    for k in keys:
        if row.get(k):
            row[k] = row[k].isoformat()
    return row


# ---------------------------------------------------------------------------
# Throttling (event-watcher)
# ---------------------------------------------------------------------------

def throttle_reason(conn, kind: str, namespace: str, name: str, *, debounce_sec: int,
                    max_concurrent: int, max_per_day: int,
                    out_of_scope_cooldown_sec: int) -> str | None:
    """Why an incident on this resource must NOT be investigated now, or None.

    Checked in order: a live investigation for the same resource; a recent
    out-of-scope verdict (re-investigating cannot change it and would drain the
    budget); the per-resource debounce window; the concurrency cap; the daily
    budget."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT id FROM investigations
                WHERE resource_kind=%s AND resource_namespace=%s AND resource_name=%s
                  AND status IN ('running','awaiting_approval','remediating')
                LIMIT 1""",
            (kind, namespace, name),
        )
        active = cur.fetchone()
        if active:
            return f"already_in_flight({active[0]})"

        cur.execute(
            """SELECT 1 FROM investigations
                WHERE resource_kind=%s AND resource_namespace=%s AND resource_name=%s
                  AND out_of_scope = true
                  AND created_at > now() - make_interval(secs => %s)
                LIMIT 1""",
            (kind, namespace, name, out_of_scope_cooldown_sec),
        )
        if cur.fetchone():
            return "out_of_scope_cooldown"

        cur.execute(
            "SELECT last_seen FROM debounce WHERE resource_kind=%s AND resource_namespace=%s AND resource_name=%s",
            (kind, namespace, name),
        )
        row = cur.fetchone()
        if row and (datetime.now(timezone.utc) - row[0]).total_seconds() < debounce_sec:
            return "debounce"

        cur.execute("SELECT n FROM active_investigation_count")
        running = (cur.fetchone() or (0,))[0] or 0
        if running >= max_concurrent:
            return f"concurrency_cap({running}/{max_concurrent})"

        cur.execute("SELECT investigations FROM today_counters")
        today = (cur.fetchone() or (0,))[0] or 0
        if today >= max_per_day:
            return f"daily_budget({today}/{max_per_day})"
    return None


def record_trigger(conn, kind: str, namespace: str, name: str) -> None:
    """Start the debounce window for this resource and count one investigation."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO debounce (resource_kind, resource_namespace, resource_name, last_seen)
               VALUES (%s, %s, %s, now())
               ON CONFLICT (resource_kind, resource_namespace, resource_name)
               DO UPDATE SET last_seen = EXCLUDED.last_seen""",
            (kind, namespace, name),
        )
        cur.execute(
            """INSERT INTO daily_counters (day, investigations) VALUES (CURRENT_DATE, 1)
               ON CONFLICT (day) DO UPDATE SET investigations = daily_counters.investigations + 1""",
        )


def insert_investigation(conn, inv_id: str, trigger_kind: str, kind: str,
                         namespace: str, name: str, payload: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO investigations
                  (id, status, trigger_kind, resource_kind, resource_namespace, resource_name, event_payload)
               VALUES (%s, 'pending', %s, %s, %s, %s, %s)""",
            (inv_id, trigger_kind, kind, namespace, name, json.dumps(payload)),
        )


def mark_failed(conn, inv_id: str, message: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE investigations SET status='failed', error_message=%s WHERE id=%s",
                    (message, inv_id))


def mark_running(conn, inv_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE investigations SET status='running' WHERE id=%s", (inv_id,))


# ---------------------------------------------------------------------------
# Reads (dashboard)
# ---------------------------------------------------------------------------

def pending_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM investigations WHERE status='awaiting_approval'")
        return int((cur.fetchone() or (0,))[0] or 0)


def pending_investigations(conn) -> list[dict]:
    """Investigations awaiting approval, newest first (at most 50)."""
    with conn.cursor(row_factory=dict_rows()) as cur:
        cur.execute(
            """SELECT id, created_at, trigger_kind, resource_kind,
                      resource_namespace, resource_name,
                      findings, fix_commands
                 FROM investigations
                WHERE status = 'awaiting_approval'
                ORDER BY created_at DESC LIMIT 50""",
        )
        rows = cur.fetchall() or []
    for r in rows:
        r["id"] = str(r["id"])
        r["created_at"] = r["created_at"].isoformat() if r["created_at"] else None
    return rows


def recent_investigations(conn, limit: int, stale_minutes: int) -> list[dict]:
    """Recent investigations of any status, newest first. An in-flight row whose
    Job should long since have finished is flagged `stale` so the UI can offer
    to dismiss it."""
    with conn.cursor(row_factory=dict_rows()) as cur:
        cur.execute(
            """SELECT id, created_at, completed_at, approved_at, status,
                      trigger_kind, resource_kind, resource_namespace, resource_name,
                      findings, fix_commands, remediation_result,
                      approved_by, error_message, out_of_scope
                 FROM investigations
                ORDER BY created_at DESC LIMIT %s""",
            (limit,),
        )
        rows = cur.fetchall() or []
    cutoff = time.time() - stale_minutes * 60
    for r in rows:
        r["id"] = str(r["id"])
        created = r.get("created_at")
        r["stale"] = bool(r["status"] in IN_FLIGHT and created and created.timestamp() < cutoff)
        _iso(r, "created_at", "completed_at", "approved_at")
    return rows


def get_investigation(conn, inv_id: str) -> dict | None:
    with conn.cursor(row_factory=dict_rows()) as cur:
        cur.execute("SELECT * FROM investigations WHERE id = %s", (inv_id,))
        r = cur.fetchone()
    if not r:
        return None
    r["id"] = str(r["id"])
    return _iso(r, "created_at", "completed_at", "approved_at")


# ---------------------------------------------------------------------------
# Approval transitions (dashboard)
# ---------------------------------------------------------------------------

def load_for_approval(cur, inv_id: str) -> dict | None:
    cur.execute("SELECT * FROM investigations WHERE id=%s", (inv_id,))
    return cur.fetchone()


def expire(cur, inv_id: str) -> None:
    cur.execute("UPDATE investigations SET status='expired', completed_at=now() WHERE id=%s", (inv_id,))


def remediations_today(cur) -> int:
    cur.execute("SELECT remediations FROM today_counters")
    return (cur.fetchone() or {}).get("remediations", 0) or 0


def mark_remediating(cur, inv_id: str, approver: str) -> None:
    cur.execute(
        """UPDATE investigations
              SET status='remediating',
                  approved_by=%s,
                  approved_at=now()
            WHERE id=%s""",
        (approver, inv_id),
    )
    cur.execute(
        """INSERT INTO daily_counters (day, remediations) VALUES (CURRENT_DATE, 1)
           ON CONFLICT (day) DO UPDATE SET remediations = daily_counters.remediations + 1""",
    )


def load_for_dismiss(cur, inv_id: str) -> dict | None:
    cur.execute("SELECT status, created_at FROM investigations WHERE id=%s", (inv_id,))
    return cur.fetchone()


def mark_dismissed(cur, inv_id: str, approver: str) -> None:
    cur.execute(
        """UPDATE investigations
              SET status='dismissed',
                  approved_by=%s,
                  approved_at=now(),
                  completed_at=now()
            WHERE id=%s""",
        (approver, inv_id),
    )


def delete_investigation(conn, inv_id: str) -> bool:
    """Delete one terminal row. In-flight and awaiting rows are kept: deleting
    them would orphan a running Job or drop a pending approval."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM investigations WHERE id=%s AND status NOT IN ('running','remediating','awaiting_approval')",
            (inv_id,),
        )
        return cur.rowcount > 0


def delete_terminal(conn) -> int:
    """Delete every terminal row; returns how many."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM investigations WHERE status NOT IN ('running','remediating','awaiting_approval')")
        return cur.rowcount
