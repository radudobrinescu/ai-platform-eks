"""Dashboard side of the Platform Health Agent: list investigations and
approve, dismiss or delete them. SQL lives in pha/db.py (shared with the
agent); the Remediator Job spec in pha/jobs.py.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

from dashboard.config import Settings
from dashboard.k8s import ApiError
from pha import db as pha_db

log = logging.getLogger("dashboard.approvals")

UNAVAILABLE = (503, {"error": "approvals db unavailable"})


class Approvals:
    """`connect()` opens an agent-database connection (None when psycopg or the
    credentials are missing); `create_job(spec)` creates a Job or raises
    ApiError; `remediator_job(id)` builds the Remediator Job spec."""

    def __init__(self, settings: Settings, connect: Callable | None,
                 create_job: Callable[[dict], object], remediator_job: Callable[[str], dict],
                 clock: Callable[[], float] = time.time):
        self.s, self.connect, self.create_job, self.remediator_job = settings, connect, create_job, remediator_job
        self._clock = clock

    @property
    def available(self) -> bool:
        return self.connect is not None

    # -- reads -------------------------------------------------------------------
    def pending_count(self) -> int:
        with self.connect() as conn:
            return pha_db.pending_count(conn)

    def pending(self) -> list[dict]:
        if not self.available:
            return []
        with self.connect() as conn:
            return pha_db.pending_investigations(conn)

    def recent(self, limit: int) -> list[dict]:
        if not self.available:
            return []
        with self.connect() as conn:
            return pha_db.recent_investigations(conn, limit, self.s.stale_investigation_minutes)

    def get(self, inv_id: str) -> dict | None:
        if not self.available:
            return None
        with self.connect() as conn:
            return pha_db.get_investigation(conn, inv_id)

    # -- mutations -------------------------------------------------------------------
    def approve(self, inv_id: str, approver: str) -> tuple[int, dict]:
        """Start the Remediator Job for an investigation awaiting approval.

        The row is claimed (awaiting_approval -> remediating) before the Job is
        created, so two concurrent approvals cannot both start a Job; a failed
        Job create releases the claim so the operator can retry."""
        if not self.available:
            return UNAVAILABLE
        with self.connect() as conn, conn.cursor(row_factory=pha_db.dict_rows()) as cur:
            inv = pha_db.load_for_approval(cur, inv_id)
            if not inv:
                return 404, {"error": "not found"}
            if inv["status"] != "awaiting_approval":
                return 409, {"error": f"status is {inv['status']}, cannot approve"}
            if (self._clock() - inv["created_at"].timestamp()) / 3600 > self.s.approval_expiry_hours:
                pha_db.expire(cur, inv_id)
                return 409, {"error": "expired"}
            # Read-then-act: two approvals racing for the last slot can both
            # pass. Accepted: this is a daily budget, not a safety limit.
            today = pha_db.remediations_today(cur)
            if today >= self.s.max_remediations_per_day:
                return 429, {"error": f"daily remediation budget exceeded ({today}/{self.s.max_remediations_per_day})"}
            if not pha_db.claim_for_remediation(cur, inv_id, approver):
                return 409, {"error": "already approved"}
            try:
                self.create_job(self.remediator_job(inv_id))
            except ApiError as e:
                if e.status != 409:            # 409: the Job exists, so it is running
                    pha_db.release_claim(cur, inv_id)
                    log.warning("approval", extra={"investigation": inv_id, "action": "approve",
                                                   "approver": approver, "result": f"job create failed: {e}"})
                    return 502, {"error": "job create failed", "status": e.status, "body": str(e)[:500]}
            pha_db.count_remediation(cur)
        log.info("approval", extra={"investigation": inv_id, "action": "approve", "approver": approver,
                                    "result": "remediating"})
        return 200, {"ok": True, "investigation_id": inv_id, "status": "remediating"}

    def dismiss(self, inv_id: str, approver: str) -> tuple[int, dict]:
        """Dismiss an investigation awaiting approval, or an in-flight one whose
        Job died without a result (older than STALE_INVESTIGATION_MINUTES). A
        fresh in-flight investigation is never dismissed."""
        if not self.available:
            return UNAVAILABLE
        with self.connect() as conn, conn.cursor(row_factory=pha_db.dict_rows()) as cur:
            if not pha_db.dismiss_if_allowed(cur, inv_id, approver, self.s.stale_investigation_minutes):
                inv = pha_db.load_for_dismiss(cur, inv_id)
                if not inv:
                    return 404, {"error": "not found"}
                if inv["status"] in pha_db.IN_FLIGHT:
                    return 409, {"error": "investigation still in progress"}
                return 409, {"error": f"status is {inv['status']}, cannot dismiss"}
        log.info("approval", extra={"investigation": inv_id, "action": "dismiss", "approver": approver,
                                    "result": "dismissed"})
        return 200, {"ok": True, "investigation_id": inv_id, "status": "dismissed"}

    def delete(self, inv_id: str | None) -> tuple[int, dict]:
        """Delete one terminal investigation, or every terminal one (`inv_id` None)."""
        if not self.available:
            return UNAVAILABLE
        with self.connect() as conn:
            if inv_id is None:
                return 200, {"ok": True, "deleted": pha_db.delete_terminal(conn)}
            if not pha_db.delete_investigation(conn, inv_id):
                return 409, {"error": "not deletable (in-flight or not found)"}
        return 200, {"ok": True, "investigation_id": inv_id, "deleted": True}
