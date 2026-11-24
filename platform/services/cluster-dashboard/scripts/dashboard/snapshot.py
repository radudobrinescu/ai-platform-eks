"""Versioned in-memory store of dashboard sections.

Requests and streams read only from here, never from upstreams. Every change
bumps one global version (the ETag); unchanged data does not. Stored values
must not be mutated after `put` (producers build new objects each time).
"""
from __future__ import annotations

import threading
import time

# Sections whose change time is `/data.json` "ts"; the others count for "mts".
K8S_SECTIONS = frozenset({"nodes", "pods", "endpoints", "nodeAlerts", "cost", "approvals", "meta"})


class Store:
    def __init__(self, clock=time.time):
        self._cond = threading.Condition()
        self._clock = clock
        self._version = 0
        self._sections: dict[str, tuple[int, float, object]] = {}   # name -> (version, changed_at, data)

    @property
    def version(self) -> int:
        with self._cond:
            return self._version

    def put(self, name: str, data) -> bool:
        """Store `data` under `name`; True if it changed (and waiters were woken)."""
        with self._cond:
            cur = self._sections.get(name)
            if cur is not None and cur[2] == data:
                return False
            self._version += 1
            self._sections[name] = (self._version, self._clock(), data)
            self._cond.notify_all()
            return True

    def get(self) -> tuple[int, dict]:
        with self._cond:
            return self._version, {n: v[2] for n, v in self._sections.items()}

    def changed_since(self, version: int) -> tuple[int, dict]:
        """(current version, sections changed after `version`)."""
        with self._cond:
            return self._version, {n: v[2] for n, v in self._sections.items() if v[0] > version}

    def wait(self, since: int, timeout: float) -> int:
        """Block until the version moves past `since` or `timeout` passes."""
        with self._cond:
            self._cond.wait_for(lambda: self._version > since, timeout)
            return self._version

    def section(self, name: str, default=None):
        with self._cond:
            v = self._sections.get(name)
            return v[2] if v else default

    def data_json(self) -> dict:
        """The `/data.json` document: today's fields plus `sourceStatus` and `degraded`."""
        with self._cond:
            sec = {n: v[2] for n, v in self._sections.items()}
            ts = max((v[1] for n, v in self._sections.items() if n in K8S_SECTIONS), default=0)
            mts = max((v[1] for n, v in self._sections.items() if n not in K8S_SECTIONS), default=0)
        approvals = sec.get("approvals") or {}
        meta = sec.get("meta") or {}
        status = sec.get("status") or {}
        doc = {name: sec.get(name, default) for name, default in (
            ("nodes", []), ("pods", []), ("endpoints", []), ("nodeAlerts", []), ("cost", {}),
            ("models", []), ("teams", []), ("gpu", []), ("argocd", []), ("autoscaling", []),
            ("costExt", {}), ("provisioning", []))}
        doc.update({
            "approvals_available": approvals.get("available", False),
            "approvals_pending": approvals.get("pending", 0),
            "cluster": meta.get("cluster", ""), "region": meta.get("region", ""),
            "links": meta.get("links", []),
            "sources": status.get("legacy", {}),
            "sourceStatus": status.get("sources", []),
            "degraded": status.get("degraded", []),
            "ts": ts, "mts": mts,
        })
        return doc
