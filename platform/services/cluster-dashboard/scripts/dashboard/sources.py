"""Source status and the scheduler that runs polled sources.

A source is one upstream (LiteLLM, DCGM, Prometheus, pricing, ...). Each runs
in its own thread on its own interval, so a slow or failing source only
changes its own status. Every upstream call made by a source has its own
timeout (5 s or less); a loop that stops ticking fails the liveness probe.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("dashboard.sources")


@dataclass(frozen=True)
class SourceStatus:
    name: str
    state: str                 # ok | stale | error | disabled
    lastSuccess: float | None
    lastError: str | None
    detail: str | None
    intervalSec: int
    staleAfterSec: int

    def to_dict(self) -> dict:
        return dict(vars(self))


class Tracker:
    """Status of one source. Thread-safe; logs each state change once."""

    def __init__(self, name: str, interval: int, stale_after: int, clock: Callable[[], float] = time.time):
        self.name, self.interval, self.stale_after, self._clock = name, interval, stale_after, clock
        self._lock = threading.Lock()
        self._last_success: float | None = None
        self._last_error: str | None = None
        self._failing = False
        self._disabled: str | None = None
        self._detail: str | None = None
        self._last_tick: float = clock()
        self._logged_state = "new"

    def tick(self) -> None:
        with self._lock:
            self._last_tick = self._clock()

    def ok(self, detail: str | None = None) -> None:
        with self._lock:
            self._last_success = self._clock()
            self._failing = False
            self._last_error = None
            self._detail = detail
        self._log_transition()

    def fail(self, error: str) -> None:
        with self._lock:
            self._failing = True
            self._last_error = (error or "error")[:300]
        self._log_transition()

    def disable(self, reason: str) -> None:
        with self._lock:
            self._disabled = reason
        self._log_transition()

    @property
    def disabled(self) -> bool:
        return self._disabled is not None

    def last_tick(self) -> float:
        return self._last_tick

    def status(self) -> SourceStatus:
        with self._lock:
            now = self._clock()
            if self._disabled is not None:
                state, detail = "disabled", self._disabled
            elif self._failing:
                state, detail = "error", self._detail
            elif self._last_success is None:
                state, detail = "stale", "waiting for the first successful run"
            elif now - self._last_success > self.stale_after:
                state, detail = "stale", self._detail
            else:
                state, detail = "ok", self._detail
            return SourceStatus(self.name, state, self._last_success, self._last_error, detail,
                                self.interval, self.stale_after)

    def _log_transition(self) -> None:
        st = self.status()
        if st.state == self._logged_state or st.state == "stale":
            return
        level = logging.WARNING if st.state in ("error", "disabled") else logging.INFO
        log.log(level, "source.state_changed",
                extra={"source": self.name, "from": self._logged_state, "to": st.state,
                       "error": st.lastError, "detail": st.detail})
        self._logged_state = st.state


class PolledSource:
    """A source that runs `fn()` every `interval` seconds."""

    def __init__(self, name: str, interval: int, fn: Callable[[], str | None],
                 stale_after: int | None = None, clock: Callable[[], float] = time.time):
        self.name, self.interval, self.fn = name, interval, fn
        self.tracker = Tracker(name, interval, stale_after or 3 * interval, clock)

    def run_once(self) -> None:
        """One run: `fn` returns an optional detail string, or raises."""
        self.tracker.tick()
        if self.tracker.disabled:
            return
        try:
            detail = self.fn()
        except Exception as e:  # any failure becomes this source's status
            self.tracker.fail(f"{type(e).__name__}: {e}")
        else:
            self.tracker.ok(detail)

    def loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            self.run_once()
            stop.wait(self.interval)


class Scheduler:
    def __init__(self, sources: list[PolledSource], clock: Callable[[], float] = time.time):
        self.sources, self._clock = sources, clock
        self.stop = threading.Event()

    def start(self) -> None:
        for s in self.sources:
            threading.Thread(target=s.loop, args=(self.stop,), daemon=True, name=f"src-{s.name}").start()

    def dead_loops(self) -> list[str]:
        """Sources whose loop has not completed a run for max(3 x interval, 60 s)."""
        now = self._clock()
        return [s.name for s in self.sources
                if now - s.tracker.last_tick() > max(3 * s.interval, 60) + s.interval]
