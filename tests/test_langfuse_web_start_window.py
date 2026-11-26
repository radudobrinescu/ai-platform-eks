"""langfuse-web must be allowed to finish its first-start migrations.

The web container migrates Postgres and ClickHouse in its entrypoint, before it
serves `/api/public/health`. The chart has no startup probe, and its default
liveness probe (first check after 20 s, 3 failures, 10 s apart) kills a container
that has not answered about 50 s after it started. A kill in the middle of a
migration leaves that migration marked as failed, and every later start exits on
it: the pod crash-loops until someone repairs the database by hand.

On a fresh install the migrations are at their longest and the cluster is at its
busiest, so the liveness probe has to wait long enough for them.
"""
from __future__ import annotations

from pathlib import Path

import yaml

VALUES = Path(__file__).resolve().parents[1] / "platform/services/langfuse/helm-values.yaml"
MIN_START_WINDOW_S = 300


def _web() -> dict:
    values = yaml.safe_load(VALUES.read_text(encoding="utf-8"))
    return values["langfuse"]["web"]


def test_liveness_probe_waits_for_first_start_migrations() -> None:
    probe = _web().get("livenessProbe") or {}
    assert probe.get("initialDelaySeconds", 0) >= MIN_START_WINDOW_S, (
        "langfuse.web.livenessProbe.initialDelaySeconds must be at least "
        f"{MIN_START_WINDOW_S} s, so the kubelet does not kill the container during its migrations"
    )


def test_readiness_probe_still_reports_early() -> None:
    """Readiness only keeps traffic away; it must not be delayed with liveness."""
    probe = _web().get("readinessProbe") or {}
    assert probe.get("initialDelaySeconds", 20) <= 30
