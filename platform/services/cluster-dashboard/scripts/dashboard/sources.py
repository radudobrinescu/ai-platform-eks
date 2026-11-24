"""Source status and the scheduler that runs polled sources.

A source is one upstream (LiteLLM, DCGM, Prometheus, pricing, ...). Each runs
in its own thread on its own interval, so a slow or failing source only
changes its own status. Every upstream call made by a source has its own
timeout (5 s or less). A loop that stops ticking shows as an error in its
status and fails the liveness probe, unless the source is optional
(`liveness=False`): a restart is not worth it for data the dashboard works
without.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger("dashboard.sources")


class SourceUnavailable(Exception):
    """Raised by a source run when a prerequisite is missing for now (for
    example a Secret not created yet). The source shows as `disabled` with
    the message and keeps running, so it recovers on its own."""


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
        self._paused: str | None = None
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
            self._paused = None
            self._last_error = None
            self._detail = detail
        self._log_transition()

    def fail(self, error: str) -> None:
        with self._lock:
            self._failing = True
            self._paused = None
            self._last_error = (error or "error")[:300]
        self._log_transition()

    def pause(self, reason: str) -> None:
        """Disabled for now; the next ok() or fail() clears it."""
        with self._lock:
            self._paused = reason
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

    def _stalled_for(self, now: float) -> float | None:
        """Seconds since the last run started, when that is longer than
        max(3 x interval, 60 s) plus one interval: a run is stuck or the loop
        stopped. None while the loop is ticking."""
        idle = now - self._last_tick
        return idle if idle > max(3 * self.interval, 60) + self.interval else None

    def stalled(self) -> bool:
        with self._lock:
            return self._stalled_for(self._clock()) is not None

    def status(self) -> SourceStatus:
        with self._lock:
            now = self._clock()
            stalled = self._stalled_for(now)
            if self._disabled is not None or self._paused is not None:
                state, detail = "disabled", self._disabled or self._paused
            elif stalled is not None:
                return SourceStatus(self.name, "error", self._last_success,
                                    f"no run started for {int(stalled)} s (a run is stuck or the loop stopped)",
                                    self._detail, self.interval, self.stale_after)
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
    """A source that runs `fn()` every `interval` seconds. `liveness` says
    whether a stalled loop fails the liveness probe."""

    def __init__(self, name: str, interval: int, fn: Callable[[], str | None],
                 stale_after: int | None = None, clock: Callable[[], float] = time.time,
                 liveness: bool = True):
        self.name, self.interval, self.fn, self.liveness = name, interval, fn, liveness
        self.tracker = Tracker(name, interval, stale_after or 3 * interval, clock)

    def run_once(self) -> None:
        """One run: `fn` returns an optional detail string, or raises."""
        self.tracker.tick()
        if self.tracker.disabled:
            return
        try:
            detail = self.fn()
        except SourceUnavailable as e:
            self.tracker.pause(str(e))
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
        """Sources that count for liveness whose loop is stalled (see
        Tracker._stalled_for). A stalled optional source only shows in its
        own status."""
        return [s.name for s in self.sources if s.liveness and s.tracker.stalled()]
