"""Kubernetes access: a raw-path client on the `kubernetes` package and
list-then-watch caches.

Raw paths (not typed APIs) give one code path for core resources and CRDs, and
keep objects in their JSON shape, which the pure functions in dashboard.models
consume. Objects are projected to the fields the dashboard reads, which bounds
memory.
"""
from __future__ import annotations

import calendar
import json
import logging
import random
import threading
import time
from collections.abc import Callable, Iterator

log = logging.getLogger("dashboard.k8s")

WATCH_TIMEOUT = 300          # server-side watch timeout (s)
REQUEST_TIMEOUT = 5          # connect and read timeout for plain requests (s)
NOT_INSTALLED_RETRY = 300    # relist interval when a CRD is absent (s)


class ApiError(Exception):
    def __init__(self, status: int, reason: str):
        super().__init__(f"HTTP {status}: {reason}")
        self.status = status


class KubeClient:
    """Thin raw-path client. `configuration` is for tests; in the pod the
    in-cluster ServiceAccount config is loaded."""

    def __init__(self, configuration=None):
        from kubernetes import client, config  # optional dependency (degraded mode)
        if configuration is None:
            config.load_incluster_config()
            configuration = client.Configuration.get_default_copy()
        self._api = client.ApiClient(configuration)

    def _call(self, method: str, path: str, query: dict | None, body=None, timeout=REQUEST_TIMEOUT,
              accept="application/json"):
        """Send one request; return the raw urllib3 response (not read), or
        raise ApiError for a non-2xx status. Uses the client's request
        serialisation and BearerToken auth, so in-cluster credentials and CA
        handling are the package's."""
        m, url, headers, payload, post = self._api.param_serialize(
            method, path, query_params=list(query.items()) if query else None,
            header_params={"Accept": accept, "Content-Type": "application/json"},
            body=body, auth_settings=["BearerToken"])
        resp = self._api.call_api(m, url, header_params=headers, body=payload, post_params=post,
                                  _request_timeout=timeout)
        if not 200 <= resp.status < 300:
            raw = resp.read() or b""
            resp.response.release_conn()
            try:
                reason = json.loads(raw).get("message") or resp.reason
            except ValueError:
                reason = resp.reason
            raise ApiError(resp.status, reason or "error")
        return resp.response

    def get(self, path: str, query: dict | None = None) -> dict:
        resp = self._call("GET", path, query)
        try:
            return json.loads(resp.data or b"{}")
        finally:
            resp.release_conn()

    def get_text(self, path: str, query: dict | None = None) -> str:
        resp = self._call("GET", path, query, accept="*/*")
        try:
            return (resp.data or b"").decode("utf-8", "replace")
        finally:
            resp.release_conn()

    def post(self, path: str, body: dict) -> dict:
        resp = self._call("POST", path, None, body=body)
        try:
            return json.loads(resp.data or b"{}")
        finally:
            resp.release_conn()

    def watch(self, path: str, query: dict) -> Iterator[dict]:
        """Yield watch events ({type, object}) until the server ends the stream."""
        q = dict(query, watch="true", timeoutSeconds=str(WATCH_TIMEOUT), allowWatchBookmarks="true")
        resp = self._call("GET", path, q, timeout=(REQUEST_TIMEOUT, WATCH_TIMEOUT + 30))
        try:
            buf = b""
            for chunk in resp.stream(8192, decode_content=True):
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        yield json.loads(line)
        finally:
            resp.release_conn()


# ---------------------------------------------------------------------------
# Projections: keep the API shape, drop what the dashboard never reads.
# ---------------------------------------------------------------------------

def _meta(o: dict, *extra: str) -> dict:
    m = o.get("metadata", {}) or {}
    out = {k: m[k] for k in ("name", "namespace", "creationTimestamp", "resourceVersion", *extra) if k in m}
    return out


def project_node(o: dict) -> dict:
    st, spec = o.get("status", {}) or {}, o.get("spec", {}) or {}
    return {"metadata": _meta(o, "labels", "deletionTimestamp"),
            "spec": {"taints": [{"key": t.get("key")} for t in spec.get("taints") or []]},
            "status": {"conditions": [{"type": c.get("type"), "status": c.get("status")}
                                      for c in st.get("conditions") or []],
                       "allocatable": {k: v for k, v in (st.get("allocatable") or {}).items()
                                       if k in ("cpu", "memory", "nvidia.com/gpu")}}}


def project_pod(o: dict, label_keys: tuple[str, ...] = ("app",)) -> dict:
    """Pods keep only the labels in `label_keys` (used to find DCGM exporters)."""
    spec, st = o.get("spec", {}) or {}, o.get("status", {}) or {}
    labels = ((o.get("metadata") or {}).get("labels") or {})
    return {"metadata": dict(_meta(o), labels={k: v for k, v in labels.items() if k in label_keys}),
            "spec": {"nodeName": spec.get("nodeName", ""),
                     "containers": [{"name": c.get("name"),
                                     "resources": {"requests": {k: v for k, v in
                                                                ((c.get("resources") or {}).get("requests") or {}).items()
                                                                if k in ("cpu", "memory", "nvidia.com/gpu")}}}
                                    for c in spec.get("containers") or []]},
            "status": {"phase": st.get("phase", ""), "podIP": st.get("podIP"),
                       "containerStatuses": [
                           {"name": c.get("name"), "ready": c.get("ready", False),
                            "state": {"waiting": {k: w[k] for k in ("reason", "message") if k in w}}
                            if (w := (c.get("state") or {}).get("waiting")) else {}}
                           for c in st.get("containerStatuses") or []]}}


def project_cr(o: dict) -> dict:
    return {"metadata": _meta(o), "spec": o.get("spec", {}) or {}, "status": o.get("status", {}) or {}}


def project_karpenter(o: dict) -> dict:
    """NodePools and NodeClaims: the labels say which pool, instance type, zone
    and capacity type a claim is for, and deletionTimestamp that it is leaving."""
    return {"metadata": _meta(o, "labels", "deletionTimestamp"), "spec": o.get("spec", {}) or {},
            "status": o.get("status", {}) or {}}


def project_argo(o: dict) -> dict:
    st = o.get("status", {}) or {}
    return {"metadata": _meta(o), "status": {"sync": {"status": (st.get("sync") or {}).get("status")},
                                             "health": {"status": (st.get("health") or {}).get("status")}}}


def project_ingress(o: dict) -> dict:
    return {"metadata": _meta(o), "status": {"loadBalancer": (o.get("status") or {}).get("loadBalancer", {})}}


def project_configmap(o: dict) -> dict:
    return {"metadata": _meta(o), "data": o.get("data", {}) or {}}


def project_event(o: dict) -> dict:
    io = o.get("involvedObject", {}) or {}
    keep = ("type", "reason", "message", "count", "lastTimestamp", "eventTime", "reportingComponent")
    out = {k: o[k] for k in keep if k in o}
    out["metadata"] = _meta(o)
    out["involvedObject"] = {k: io[k] for k in ("kind", "name", "namespace") if k in io}
    out["source"] = {"component": (o.get("source") or {}).get("component", "")}
    return out


# ---------------------------------------------------------------------------
# Watch cache
# ---------------------------------------------------------------------------

class WatchCache:
    """Keeps a projected, up-to-date copy of one list (R2 in the U2 functional
    design): list, watch from the list's version, restart on stream end, relist
    on 410, back off on errors, treat 404 as "not installed"."""

    def __init__(self, kind: str, path: str, project: Callable[[dict], dict], *,
                 query: dict | None = None, on_change: Callable[[str], None] = lambda k: None,
                 retention_s: int | None = None, max_items: int | None = None,
                 clock=time.time, sleep: Callable[[float], bool] | None = None):
        self.kind, self.path, self.project, self.query = kind, path, project, dict(query or {})
        self.on_change, self.retention_s, self.max_items, self._clock = on_change, retention_s, max_items, clock
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._rv = ""
        self.installed = True
        self.synced = False            # first list finished
        self.last_success: float | None = None
        self.error: str | None = None
        self._sleep = sleep

    # -- reads ------------------------------------------------------------------
    def items(self) -> list[dict]:
        with self._lock:
            items = list(self._items.values())
        if self.retention_s:
            cutoff = self._clock() - self.retention_s
            items = [i for i in items if _event_time(i) >= cutoff]
        return items

    def get(self, name: str, namespace: str = "") -> dict | None:
        with self._lock:
            return self._items.get(f"{namespace}/{name}")

    # -- loop -------------------------------------------------------------------
    def run(self, client: KubeClient, stop: threading.Event) -> None:
        sleep = self._sleep or (lambda s: stop.wait(s))
        backoff = 1.0
        while not stop.is_set():
            try:
                self.list(client)
                backoff = 1.0
                while not stop.is_set():
                    if not self.watch_once(client):
                        break                      # 410: relist now
            except ApiError as e:
                if e.status == 404:
                    self._not_installed()
                    sleep(NOT_INSTALLED_RETRY)
                    continue
                self._failed(e)
                sleep(backoff * random.uniform(0.8, 1.2))
                backoff = min(backoff * 2, 30)
            except Exception as e:  # network errors, bad JSON: retry with backoff
                self._failed(e)
                sleep(backoff * random.uniform(0.8, 1.2))
                backoff = min(backoff * 2, 30)

    def list(self, client: KubeClient) -> None:
        data = client.get(self.path, self.query or None)
        items = {}
        for o in data.get("items", []):
            p = self.project(o)
            items[_key(p)] = p
        with self._lock:
            self._items = items
            self._trim()
        self._rv = (data.get("metadata") or {}).get("resourceVersion", "")
        self.installed, self.synced = True, True
        self._succeeded()
        self.on_change(self.kind)

    def watch_once(self, client: KubeClient) -> bool:
        """Consume one watch stream. False when the version expired (relist)."""
        for ev in client.watch(self.path, dict(self.query, resourceVersion=self._rv)):
            t, obj = ev.get("type"), ev.get("object") or {}
            if t == "ERROR":
                if obj.get("code") == 410:
                    return False
                raise ApiError(int(obj.get("code") or 0), obj.get("message", "watch error"))
            self._rv = (obj.get("metadata") or {}).get("resourceVersion", self._rv)
            if t == "BOOKMARK":
                self._succeeded()
                continue
            p = self.project(obj)
            with self._lock:
                if t == "DELETED":
                    self._items.pop(_key(p), None)
                else:
                    self._items[_key(p)] = p
                    self._trim()
            self._succeeded()
            self.on_change(self.kind)
        self._succeeded()            # normal end of stream
        return True

    # -- status -----------------------------------------------------------------
    def _succeeded(self) -> None:
        self.last_success, self.error = self._clock(), None

    def _failed(self, e: Exception) -> None:
        msg = f"{self.kind}: {e}"
        if msg != self.error:
            log.warning("watch.restart", extra={"kind": self.kind, "error": str(e)})
        self.error = msg

    def _not_installed(self) -> None:
        with self._lock:
            had = bool(self._items)
            self._items = {}
        self.installed, self.synced, self.error = False, True, None
        self.last_success = self._clock()
        if had:
            self.on_change(self.kind)

    def _trim(self) -> None:
        if self.max_items and len(self._items) > self.max_items:
            keep = sorted(self._items.items(), key=lambda kv: _event_time(kv[1]), reverse=True)[:self.max_items]
            self._items = dict(keep)


def _key(o: dict) -> str:
    m = o.get("metadata", {}) or {}
    return f"{m.get('namespace', '')}/{m.get('name', '')}"


def _event_time(o: dict) -> float:
    ts = o.get("lastTimestamp") or o.get("eventTime") or (o.get("metadata") or {}).get("creationTimestamp") or ""
    try:
        return float(calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return 0.0
