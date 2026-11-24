"""LiteLLM data through LiteLLM's own interfaces, never its database.

Two sources use the dashboard's read-only key (LiteLLM role
`proxy_admin_viewer`, Secret `cluster-dashboard-litellm-key`, kept current by
the viewer-key CronJob):

- `litellm-api`: the REST API for teams, today's team usage, the 14-day spend
  trend, 30-day per-user totals and the latest model health checks.
- `litellm-metrics`: `/metrics` on every LiteLLM pod (the Service would answer
  from one replica only). Per-model requests/min, latency p50/p95, tokens/s,
  error rate and spend/hour over the last 5 minutes are computed from counter
  increases between scrapes, the way Prometheus `increase()` does.

Rules: aidlc-docs/construction/dashboard-u3-litellm/functional-design/
business-rules.md (L1 to L13). The key is never logged or returned: errors
carry only an HTTP status and a path.
"""
from __future__ import annotations

import base64
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from dashboard import prom
from dashboard.sources import SourceUnavailable

TIMEOUT = 5                       # every LiteLLM call (s)
MAX_BYTES = 16 * 1024 * 1024      # response size cap
WINDOW = 300                      # traffic window (s)
KEY_REFRESH = 30                  # re-read the key Secret at most this often (s)

REQUESTS = "litellm_proxy_total_requests_metric"
FAILED = "litellm_proxy_failed_requests_metric"
LATENCY = "litellm_request_total_latency_metric"
TOKENS = "litellm_total_tokens_metric"
SPEND = "litellm_spend_metric"
FAMILIES = (REQUESTS, FAILED, LATENCY, TOKENS, SPEND)
_COUNTERS = {REQUESTS + "_total": "requests", FAILED + "_total": "failed",
             TOKENS + "_total": "tokens", SPEND + "_total": "spend"}
_BUCKET = LATENCY + "_bucket"
_SAMPLE_NAMES = set(_COUNTERS) | {_BUCKET}
FIELDS = ("requests", "failed", "tokens", "spend")


class LiteLLMError(Exception):
    """A LiteLLM call failed. `status` is the HTTP status (0 for network errors)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status

    @property
    def auth(self) -> bool:
        return self.status in (401, 403)


# ---------------------------------------------------------------------------
# Viewer key
# ---------------------------------------------------------------------------

class KeyMissing(SourceUnavailable):
    pass


class ViewerKey:
    """The read-only key from its Secret, read through the Kubernetes API at
    most every KEY_REFRESH seconds and shared by both LiteLLM sources."""

    def __init__(self, get_json: Callable[[str], dict], namespace: str, secret: str,
                 clock: Callable[[], float] = time.time):
        self._get, self._clock = get_json, clock
        self._path = f"/api/v1/namespaces/{namespace}/secrets/{secret}"
        self.secret = secret
        self._lock = threading.Lock()
        self._value: str | None = None
        self._error: Exception | None = None
        self._read_at = -math.inf

    def get(self) -> str:
        """The key. Raises KeyMissing (Secret not found) or LiteLLMError
        (Secret unreadable)."""
        with self._lock:
            if self._clock() - self._read_at >= KEY_REFRESH:
                self._read()
            if self._error is not None:
                raise self._error
            return self._value  # type: ignore[return-value]

    def invalidate(self) -> None:
        """Re-read on the next get (LiteLLM rejected the key; the CronJob may
        have just replaced it)."""
        with self._lock:
            self._read_at = -math.inf

    def _read(self) -> None:
        self._read_at = self._clock()
        self._value, self._error = None, None
        try:
            obj = self._get(self._path)
        except Exception as e:
            status = getattr(e, "status", 0)
            self._error = (KeyMissing("viewer key not created yet") if status == 404 else
                           LiteLLMError(f"cannot read Secret {self.secret} (HTTP {status})", status))
            return
        raw = (obj.get("data") or {}).get("api-key")
        try:
            self._value = base64.b64decode(raw).decode().strip() if raw else None
        except ValueError:
            self._value = None
        if not self._value:
            self._error = KeyMissing("viewer key not created yet")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def http_get(url: str, key: str, where: str) -> bytes:
    """GET with the key as bearer token, TIMEOUT and MAX_BYTES. `where` names
    the call in errors (a path or pod name, never the URL query or the key)."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        e.close()
        if e.code in (401, 403):
            raise LiteLLMError(f"viewer key rejected (HTTP {e.code} from {where})", e.code) from None
        raise LiteLLMError(f"HTTP {e.code} from {where}", e.code) from None
    except (OSError, ValueError) as e:  # connection refused, timeout, bad URL
        raise LiteLLMError(f"{where}: {type(e).__name__}") from None
    if len(body) > MAX_BYTES:
        raise LiteLLMError(f"{where}: response too large")
    return body


class LiteLLMApi:
    def __init__(self, base_url: str, key: ViewerKey, get: Callable[[str, str, str], bytes] = http_get):
        self.base, self.key, self._get = base_url.rstrip("/"), key, get

    def call(self, path: str, query: dict | None = None):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        try:
            return json.loads(self._get(url, self.key.get(), path))
        except LiteLLMError as e:
            if e.auth:
                self.key.invalidate()
            raise
        except ValueError:
            raise LiteLLMError(f"{path}: invalid JSON") from None

    def team_list(self) -> list:
        return self.call("/team/list")

    def team_activity(self, start: date, end: date) -> dict:
        return self.call("/team/daily/activity/aggregated",
                         {"start_date": start.isoformat(), "end_date": end.isoformat()})

    def customer_activity(self, start: date, end: date) -> dict:
        return self.call("/customer/daily/activity/aggregated",
                         {"start_date": start.isoformat(), "end_date": end.isoformat()})

    def health_latest(self) -> dict:
        return self.call("/health/latest")


# ---------------------------------------------------------------------------
# REST data -> dashboard inputs (pure)
# ---------------------------------------------------------------------------

def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def teams_from(team_list: list, activity: dict, today: date) -> list[dict]:
    """Teams with budget and key count from /team/list, today's usage from
    the per-team breakdown of today's activity (L9)."""
    day = next((r for r in activity.get("results") or [] if str(r.get("date")) == today.isoformat()), {})
    entities = (day.get("breakdown") or {}).get("entities") or {}
    out = []
    for t in team_list:
        tid = str(t.get("team_id") or "")
        mt = (entities.get(tid) or {}).get("metrics") or {}
        limit = _num(t.get("max_budget"))
        out.append({"id": tid, "name": t.get("team_alias") or tid[:8],
                    "budget": round(_num(t.get("spend")), 2), "limit": limit or None,
                    "reqsToday": int(_num(mt.get("api_requests"))), "failed": int(_num(mt.get("failed_requests"))),
                    "spendToday": round(_num(mt.get("spend")), 2), "tokens": int(_num(mt.get("total_tokens"))),
                    "keys": len(t.get("keys") or [])})
    return out


def by_team(teams: list[dict]) -> list:
    return sorted([[t["name"], t["spendToday"]] for t in teams if t["spendToday"] > 0], key=lambda x: -x[1])


def trend_from(activity: dict) -> list:
    rows = sorted((str(r.get("date")), round(_num((r.get("metrics") or {}).get("spend")), 2))
                  for r in activity.get("results") or [])
    return [[d, s] for d, s in rows if s > 0]


def users_from(activity: dict, limit: int = 50) -> list[dict]:
    """Per end user (the e-mail Open WebUI forwards) totals over the range."""
    acc: dict[str, dict] = {}
    for r in activity.get("results") or []:
        for user, ent in ((r.get("breakdown") or {}).get("entities") or {}).items():
            if not user or user == "Unassigned":
                continue
            mt = ent.get("metrics") or {}
            e = acc.setdefault(user, {"user": user, "requests": 0, "tokens": 0, "spend": 0.0})
            e["requests"] += int(_num(mt.get("api_requests")))
            e["tokens"] += int(_num(mt.get("total_tokens")))
            e["spend"] += _num(mt.get("spend"))
    users = [dict(e, spend=round(e["spend"], 4)) for e in acc.values()]
    return sorted(users, key=lambda x: (-x["spend"], x["user"]))[:limit]


def health_from(latest: dict) -> dict:
    """Model name -> {status} of its newest health check."""
    newest: dict[str, dict] = {}
    for c in (latest.get("latest_health_checks") or {}).values():
        name = c.get("model_name")
        if name and str(c.get("checked_at") or "") >= str((newest.get(name) or {}).get("checked_at") or ""):
            newest[name] = c
    return {n: {"status": c.get("status") or ""} for n, c in newest.items()}


def fetch_api(api: LiteLLMApi, now: float, previous: dict | None) -> tuple[dict, str | None]:
    """One `litellm-api` run. Team list and team activity are required (an
    error raises, the source keeps its previous data as stale); per-user
    totals and health are optional and keep their previous value on error."""
    today = datetime.fromtimestamp(now, timezone.utc).date()
    team_list = api.team_list()
    activity = api.team_activity(today - timedelta(days=13), today)
    teams = teams_from(team_list if isinstance(team_list, list) else [], activity, today)
    out = {"teams": teams, "byTeam": by_team(teams), "trend": trend_from(activity)}
    previous, notes = previous or {}, []
    for key, fn in (("byUser", lambda: users_from(api.customer_activity(today - timedelta(days=29), today))),
                    ("health", lambda: health_from(api.health_latest()))):
        try:
            out[key] = fn()
        except LiteLLMError as e:
            if e.auth:
                raise
            out[key] = previous.get(key, [] if key == "byUser" else {})
            notes.append(f"{'users' if key == 'byUser' else 'health'}: {e}")
    return out, ("; ".join(notes) or None)


# ---------------------------------------------------------------------------
# /metrics -> per-model counters (pure)
# ---------------------------------------------------------------------------

def missing_families(text: str) -> list[str]:
    """Families of FAMILIES without a `# TYPE` line. prometheus_client names
    counters with or without the `_total` suffix depending on its version."""
    seen = set()
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            name = line.split(" ", 3)[2]
            seen.add(name[:-6] if name.endswith("_total") else name)
    return [f for f in FAMILIES if f not in seen]


def parse_metrics(text: str) -> dict[str, dict]:
    """{requested_model: {requests, failed, tokens, spend, buckets: {le: count}}},
    summed over all series of the model. Raises LiteLLMError if a family is
    missing (renamed by a LiteLLM upgrade): zeros would look like no traffic."""
    missing = missing_families(text)
    if missing:
        raise LiteLLMError("metric missing: " + ", ".join(missing))
    out: dict[str, dict] = {}
    for s in prom.parse_text(text, _SAMPLE_NAMES):
        model = s.labels.get("requested_model")
        if not model or math.isnan(s.value):
            continue
        c = out.setdefault(model, {"requests": 0.0, "failed": 0.0, "tokens": 0.0, "spend": 0.0, "buckets": {}})
        if s.name == _BUCKET:
            try:
                le = float(s.labels.get("le", ""))
            except ValueError:
                continue
            c["buckets"][le] = c["buckets"].get(le, 0.0) + s.value
        else:
            c[_COUNTERS[s.name]] += s.value
    return out


# ---------------------------------------------------------------------------
# Traffic window (L7, L8)
# ---------------------------------------------------------------------------

def _increase(prev: float, cur: float) -> float:
    return cur if cur < prev else cur - prev     # lower value: LiteLLM restarted, count from 0


def quantile(q: float, buckets: dict[float, float]) -> float | None:
    """`histogram_quantile` on cumulative bucket counts: linear interpolation
    inside the bucket holding the rank; a rank in +Inf gives the highest
    finite bound; no observations gives None."""
    if not buckets:
        return None
    bounds = sorted(buckets)
    cum, run = [], 0.0
    for b in bounds:                       # cumulative counts must not decrease
        run = max(run, buckets[b])
        cum.append(run)
    total = cum[-1]
    if total <= 0:
        return None
    rank = q * total
    prev_b, prev_c = 0.0, 0.0
    for b, c in zip(bounds, cum):
        if c >= rank:
            if math.isinf(b):
                return prev_b
            if c == prev_c:
                return b
            return prev_b + (b - prev_b) * (rank - prev_c) / (c - prev_c)
        prev_b, prev_c = b, c
    return prev_b


class Traffic:
    """Per-pod scrape windows and the per-model figures computed from them.
    Thread-safe; used by the `litellm-metrics` source only."""

    def __init__(self, window: float = WINDOW):
        self.window = window
        self._pods: dict[str, list[tuple[float, dict]]] = {}
        self._lock = threading.Lock()

    def add(self, pod: str, at: float, per_model: dict) -> None:
        with self._lock:
            samples = self._pods.setdefault(pod, [])
            samples.append((at, per_model))
            # keep the window plus the newest sample before it
            while len(samples) > 2 and samples[1][0] <= at - self.window:
                samples.pop(0)

    def retain(self, pods: set[str]) -> None:
        with self._lock:
            for p in set(self._pods) - pods:
                del self._pods[p]

    def figures(self) -> dict[str, dict] | None:
        """{model: {rpm, p50, p95, tps, errPct, spendHr}}, or None while no pod
        has two samples yet (warming up)."""
        with self._lock:
            pods = {p: list(s) for p, s in self._pods.items() if len(s) >= 2}
        if not pods:
            return None
        elapsed = max(s[-1][0] - s[0][0] for s in pods.values())
        if elapsed <= 0:
            return None
        inc: dict[str, dict] = {}
        for samples in pods.values():
            for (_, prev), (_, cur) in zip(samples, samples[1:]):
                for model, c in cur.items():
                    p = prev.get(model) or {}          # new model on a known pod counts from 0
                    acc = inc.setdefault(model, {f: 0.0 for f in FIELDS} | {"buckets": {}})
                    for f in FIELDS:
                        acc[f] += _increase(p.get(f, 0.0), c[f])
                    pb = p.get("buckets") or {}
                    for le, v in c["buckets"].items():
                        acc["buckets"][le] = acc["buckets"].get(le, 0.0) + _increase(pb.get(le, 0.0), v)
        out = {}
        for model, a in inc.items():
            p50, p95 = quantile(0.5, a["buckets"]), quantile(0.95, a["buckets"])
            out[model] = {
                "rpm": round(a["requests"] / elapsed * 60, 1),
                "p50": round(p50 * 1000) if p50 is not None else None,
                "p95": round(p95 * 1000) if p95 is not None else None,
                "tps": round(a["tokens"] / elapsed, 1),
                "errPct": round(a["failed"] / a["requests"] * 100, 1) if a["requests"] > 0 else 0,
                "spendHr": a["spend"] / elapsed * 3600,
            }
        return out


def litellm_pods(pods: list[dict], namespace: str, selector: str) -> list[tuple[str, str]]:
    """(name, IP) of ready LiteLLM pods (L5). The name identifies the pod
    process: a replaced pod gets a new name, a container restart keeps it
    and shows as a counter reset."""
    key, _, val = selector.partition("=")
    out = []
    for p in pods:
        meta, st = p["metadata"], p.get("status") or {}
        cs = st.get("containerStatuses") or []
        if (meta.get("namespace") == namespace and (meta.get("labels") or {}).get(key) == val
                and st.get("phase") == "Running" and st.get("podIP") and cs and all(c.get("ready") for c in cs)):
            out.append((meta["name"], st["podIP"]))
    return out


def fetch_metrics(traffic: Traffic, pods: list[tuple[str, str]], key: ViewerKey, port: int,
                  now: float, get: Callable[[str, str, str], bytes] = http_get) -> tuple[dict | None, str | None]:
    """One `litellm-metrics` run: scrape every ready pod, record the samples,
    return (figures or None while warming up, detail). Any pod failing fails
    the run (half the traffic missing would look like a real drop), after the
    other pods' samples are recorded."""
    if not pods:
        raise LiteLLMError("no ready LiteLLM pod")
    secret = key.get()
    errors = []
    for name, ip in pods:
        try:
            traffic.add(name, now, parse_metrics(get(f"http://{ip}:{port}/metrics", secret, f"pod {name}")
                                                .decode("utf-8", "replace")))
        except LiteLLMError as e:
            if e.auth:
                key.invalidate()
            errors.append(str(e) if str(e).startswith(("pod ", "viewer key")) else f"pod {name}: {e}")
    traffic.retain({name for name, _ip in pods})
    if errors:
        raise LiteLLMError("; ".join(errors)[:300])
    figures = traffic.figures()
    return (figures, None) if figures is not None else ({}, "warming up")
