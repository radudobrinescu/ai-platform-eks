"""LiteLLM adapters (dashboard.litellm) on inputs produced by LiteLLM 1.108.10.

The `litellm_metrics_*.txt.in` scrapes were rendered by prometheus_client
from LiteLLM's own PrometheusLogger metric definitions (names, labels,
buckets); the `litellm_*.json.in` REST responses were dumped from LiteLLM's
own response models. Scenario of the scrapes (60 s apart):

- pod a, one process: 10 qwen requests at 0.3 s, then 5 more at 0.4 s and 5
  from a second user at 1.5 s (a new series on a known pod); 100 tokens each.
- pod b, restarted between the scrapes: 20 qwen requests at 0.3 s, then 3 at
  3.0 s; plus nova-lite (new on this pod): 3 requests at 0.7 s with 200
  tokens and $0.001 each, and 1 failed request.
"""
import base64
import json
import math
import os
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dash_testlib import FIXTURE_NOW
from dashboard import litellm as ll
from dashboard.sources import PolledSource

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
QWEN, NOVA = "qwen2-5-3b-instruct", "nova-lite"
SECRET_KEY = "sk-viewer-test-123"


def text(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def js(name):
    return json.loads(text(name))


class Key:
    """ViewerKey stand-in."""

    def __init__(self, value=SECRET_KEY):
        self.value, self.invalidated = value, 0

    def get(self):
        return self.value

    def invalidate(self):
        self.invalidated += 1


# ---------------------------------------------------------------------------
# /metrics parsing
# ---------------------------------------------------------------------------

def test_parse_real_scrape_sums_series_per_model():
    a2 = ll.parse_metrics(text("litellm_metrics_a2.txt.in"))
    assert set(a2) == {QWEN}
    q = a2[QWEN]
    assert (q["requests"], q["failed"], q["tokens"], q["spend"]) == (20, 0, 2000, 0)
    assert q["buckets"][0.5] == 15 and q["buckets"][2.0] == 20 and q["buckets"][math.inf] == 20
    b2 = ll.parse_metrics(text("litellm_metrics_b2.txt.in"))
    assert (b2[NOVA]["requests"], b2[NOVA]["failed"], b2[NOVA]["tokens"]) == (4, 1, 600)
    assert b2[NOVA]["spend"] == pytest.approx(0.003)


def test_created_and_other_families_are_ignored():
    q = ll.parse_metrics(text("litellm_metrics_a1.txt.in"))[QWEN]
    assert q["requests"] == 10          # not 10 + the *_created timestamps


@pytest.mark.parametrize("family", ll.FAMILIES)
def test_missing_family_is_an_error_not_zeros(family):
    lines = [ln for ln in text("litellm_metrics_a1.txt.in").splitlines()
             if not (ln.startswith("# TYPE " + family + " ") or ln.startswith("# TYPE " + family + "_total "))]
    with pytest.raises(ll.LiteLLMError, match="metric missing: " + family):
        ll.parse_metrics("\n".join(lines))


def test_type_lines_with_and_without_total_suffix():
    base = "".join(f"# TYPE {f} {'histogram' if f == ll.LATENCY else 'counter'}\n" for f in ll.FAMILIES)
    assert ll.missing_families(base) == []
    suffixed = base.replace("_metric counter", "_metric_total counter")
    assert ll.missing_families(suffixed) == []


def test_labels_with_quotes_and_braces_and_nan():
    head = "".join(f"# TYPE {f} counter\n" for f in ll.FAMILIES)
    body = ('litellm_proxy_total_requests_metric_total{user_agent="a \\"b\\" {c}",requested_model="m"} 2.0\n'
            'litellm_proxy_total_requests_metric_total{requested_model="m"} NaN\n'
            'litellm_proxy_total_requests_metric_total{requested_model=""} 5.0\n')
    assert ll.parse_metrics(head + body)["m"]["requests"] == 2.0


# ---------------------------------------------------------------------------
# Quantiles
# ---------------------------------------------------------------------------

def test_quantile_interpolates_inside_the_bucket():
    b = {0.5: 5, 1.0: 5, 2.0: 10, 5.0: 13, math.inf: 13}
    assert ll.quantile(0.5, b) == pytest.approx(1.3)
    assert ll.quantile(0.95, b) == pytest.approx(4.35)


def test_quantile_first_bucket_starts_at_zero():
    assert ll.quantile(0.5, {1.0: 4, math.inf: 4}) == pytest.approx(0.5)


def test_quantile_in_inf_bucket_gives_highest_finite_bound():
    assert ll.quantile(0.95, {1.0: 1, 600.0: 1, math.inf: 10}) == 600.0


def test_quantile_without_observations():
    assert ll.quantile(0.5, {}) is None
    assert ll.quantile(0.5, {1.0: 0, math.inf: 0}) is None


# ---------------------------------------------------------------------------
# Traffic window
# ---------------------------------------------------------------------------

def scenario():
    t = ll.Traffic()
    t.add("a", 0, ll.parse_metrics(text("litellm_metrics_a1.txt.in")))
    t.add("b", 0, ll.parse_metrics(text("litellm_metrics_b1.txt.in")))
    t.add("a", 60, ll.parse_metrics(text("litellm_metrics_a2.txt.in")))
    t.add("b", 60, ll.parse_metrics(text("litellm_metrics_b2.txt.in")))
    return t


def test_figures_from_two_pods_with_a_restart_and_new_series():
    f = scenario().figures()
    # qwen: pod a +10 (new user series counted), pod b reset -> +3; 1300 tokens; buckets as above
    assert f[QWEN] == {"rpm": 13.0, "p50": 1300, "p95": 4350, "tps": 21.7, "errPct": 0, "spendHr": 0.0}
    # nova: new on pod b, counted from 0
    assert f[NOVA]["rpm"] == 4.0 and f[NOVA]["errPct"] == 25.0 and f[NOVA]["tps"] == 10.0
    assert (f[NOVA]["p50"], f[NOVA]["p95"]) == (750, 975)
    assert f[NOVA]["spendHr"] == pytest.approx(0.18)


def test_first_sample_of_a_pod_is_only_a_baseline():
    t = ll.Traffic()
    t.add("b", 60, ll.parse_metrics(text("litellm_metrics_b2.txt.in")))
    assert t.figures() is None                     # warming up: nothing counted from the baseline
    t.add("b", 75, ll.parse_metrics(text("litellm_metrics_b2.txt.in")))
    assert t.figures()[NOVA]["rpm"] == 0.0


def test_gone_pod_is_dropped():
    t = scenario()
    t.retain({"a"})
    f = t.figures()
    assert NOVA not in f and f[QWEN]["rpm"] == 10.0


def _counters(requests):
    return {"m": {"requests": float(requests), "failed": 0.0, "tokens": 0.0, "spend": 0.0, "buckets": {}}}


def test_window_keeps_five_minutes_plus_the_sample_before():
    t = ll.Traffic()
    for i in range(41):                            # every 15 s for 10 min, 1 request per second
        t.add("a", i * 15, _counters(i * 15))
    samples = t._pods["a"]
    assert samples[0][0] == 300 and samples[-1][0] == 600
    assert t.figures()["m"]["rpm"] == 60.0


def test_rate_uses_elapsed_time_while_the_window_fills():
    t = ll.Traffic()
    t.add("a", 0, _counters(0))
    t.add("a", 30, _counters(30))
    assert t.figures()["m"]["rpm"] == 60.0


def test_no_requests_gives_zero_error_and_no_latency():
    t = ll.Traffic()
    t.add("a", 0, _counters(5))
    t.add("a", 15, _counters(5))
    assert t.figures()["m"] == {"rpm": 0.0, "p50": None, "p95": None, "tps": 0.0, "errPct": 0, "spendHr": 0.0}


# ---------------------------------------------------------------------------
# Pods and a metrics run
# ---------------------------------------------------------------------------

def pod(name, ns="ai-platform", app="litellm", phase="Running", ready=True, ip="10.0.0.1"):
    return {"metadata": {"name": name, "namespace": ns, "labels": {"app": app}},
            "status": {"phase": phase, "podIP": ip, "containerStatuses": [{"name": "litellm", "ready": ready}]}}


def test_only_ready_litellm_pods_are_scraped():
    pods = [pod("litellm-1"), pod("litellm-2", ready=False), pod("litellm-3", phase="Pending"),
            pod("litellm-4", ns="inference"), pod("other", app="open-webui"), pod("litellm-5", ip=None)]
    assert ll.litellm_pods(pods, "ai-platform", "app=litellm") == [("litellm-1", "10.0.0.1")]


def scraper(responses):
    calls = []

    def get(url, key, where):
        calls.append((url, key, where))
        r = responses[url]
        if isinstance(r, Exception):
            raise r
        return r.encode()
    get.calls = calls
    return get


PODS = [("litellm-a", "10.0.0.1"), ("litellm-b", "10.0.0.2")]


def test_metrics_run_warms_up_then_publishes():
    t, key = ll.Traffic(), Key()
    first = scraper({"http://10.0.0.1:4000/metrics": text("litellm_metrics_a1.txt.in"),
                     "http://10.0.0.2:4000/metrics": text("litellm_metrics_b1.txt.in")})
    assert ll.fetch_metrics(t, PODS, key, 4000, 0, first) == ({}, "warming up")
    assert {c[1] for c in first.calls} == {SECRET_KEY}
    second = scraper({"http://10.0.0.1:4000/metrics": text("litellm_metrics_a2.txt.in"),
                      "http://10.0.0.2:4000/metrics": text("litellm_metrics_b2.txt.in")})
    figures, detail = ll.fetch_metrics(t, PODS, key, 4000, 60, second)
    assert detail is None and figures[QWEN]["rpm"] == 13.0 and figures[NOVA]["rpm"] == 4.0


def test_one_pod_failing_fails_the_run_but_keeps_the_other_sample():
    t = ll.Traffic()
    get = scraper({"http://10.0.0.1:4000/metrics": text("litellm_metrics_a1.txt.in"),
                   "http://10.0.0.2:4000/metrics": ll.LiteLLMError("pod litellm-b: URLError")})
    with pytest.raises(ll.LiteLLMError, match="pod litellm-b"):
        ll.fetch_metrics(t, PODS, Key(), 4000, 0, get)
    assert list(t._pods) == ["litellm-a"]


def test_missing_metric_names_the_pod():
    get = scraper({"http://10.0.0.1:4000/metrics": "# TYPE something counter\n"})
    with pytest.raises(ll.LiteLLMError, match="pod litellm-a: metric missing"):
        ll.fetch_metrics(ll.Traffic(), PODS[:1], Key(), 4000, 0, get)


def test_rejected_key_invalidates_it():
    key = Key()
    get = scraper({"http://10.0.0.1:4000/metrics":
                   ll.LiteLLMError("viewer key rejected (HTTP 401 from pod litellm-a)", 401)})
    with pytest.raises(ll.LiteLLMError, match="viewer key rejected"):
        ll.fetch_metrics(ll.Traffic(), PODS[:1], key, 4000, 0, get)
    assert key.invalidated == 1


def test_no_ready_pod():
    with pytest.raises(ll.LiteLLMError, match="no ready LiteLLM pod"):
        ll.fetch_metrics(ll.Traffic(), [], Key(), 4000, 0, scraper({}))


# ---------------------------------------------------------------------------
# REST data
# ---------------------------------------------------------------------------

TODAY = date(2026, 11, 20)                       # FIXTURE_NOW in UTC


def test_teams_from_team_list_and_today():
    teams = ll.teams_from(js("litellm_team_list.json.in"), js("litellm_team_activity.json.in"), TODAY)
    assert teams == [
        {"id": "t1", "name": "search", "budget": 0.42, "limit": 50.0, "reqsToday": 18, "failed": 1,
         "spendToday": 0.12, "tokens": 9000, "keys": 1},
        {"id": "8f3c2a10-5b6d-4e7f-9a1b-2c3d4e5f6a7b", "name": "8f3c2a10", "budget": 0.0, "limit": None,
         "reqsToday": 0, "failed": 0, "spendToday": 0.0, "tokens": 0, "keys": 0}]
    assert ll.by_team(teams) == [["search", 0.12]]


def test_trend_lists_days_with_spend_oldest_first():
    assert ll.trend_from(js("litellm_team_activity.json.in")) == [["2026-11-19", 0.3], ["2026-11-20", 0.12]]


def test_users_totals_skip_unassigned():
    assert ll.users_from(js("litellm_customer_activity.json.in")) == [
        {"user": "developer@example.com", "requests": 7, "tokens": 5100, "spend": 0.0311},
        {"user": "admin@example.com", "requests": 1, "tokens": 400, "spend": 0.004}]


def test_users_capped_at_50():
    act = {"results": [{"date": "2026-11-20", "breakdown": {"entities": {
        f"u{i}@x": {"metrics": {"spend": i, "api_requests": 1, "total_tokens": 1}} for i in range(60)}}}]}
    users = ll.users_from(act)
    assert len(users) == 50 and users[0]["user"] == "u59@x"


def test_health_newest_check_per_model_wins():
    assert ll.health_from(js("litellm_health_latest.json.in")) == {
        QWEN: {"status": "healthy"}, NOVA: {"status": "unhealthy"}}


class FakeApi(ll.LiteLLMApi):
    def __init__(self, fail=None, key=None):
        self.fail, self.paths = fail or {}, []
        super().__init__("http://litellm:4000", key or Key(), self._fake_get)

    def _fake_get(self, url, key, where):
        self.paths.append(url)
        if where in self.fail:
            raise self.fail[where]
        name = {"/team/list": "litellm_team_list.json.in",
                "/team/daily/activity/aggregated": "litellm_team_activity.json.in",
                "/customer/daily/activity/aggregated": "litellm_customer_activity.json.in",
                "/health/latest": "litellm_health_latest.json.in"}[where]
        return text(name).encode()


def test_api_run_builds_all_inputs_with_utc_dates():
    api = FakeApi()
    out, detail = ll.fetch_api(api, FIXTURE_NOW, None)
    assert detail is None
    assert set(out) == {"teams", "byTeam", "trend", "byUser", "health"}
    assert out["teams"][0]["reqsToday"] == 18 and out["byUser"][0]["requests"] == 7
    assert "start_date=2026-11-07&end_date=2026-11-20" in api.paths[1]
    assert "start_date=2026-10-22&end_date=2026-11-20" in api.paths[2]


@pytest.mark.parametrize("path", ["/team/list", "/team/daily/activity/aggregated"])
def test_required_part_failing_fails_the_run(path):
    with pytest.raises(ll.LiteLLMError):
        ll.fetch_api(FakeApi({path: ll.LiteLLMError(f"HTTP 500 from {path}", 500)}), FIXTURE_NOW, None)


def test_optional_parts_keep_previous_values():
    previous = {"byUser": [{"user": "old"}], "health": {"m": {"status": "healthy"}}}
    api = FakeApi({"/health/latest": ll.LiteLLMError("HTTP 500 from /health/latest", 500),
                   "/customer/daily/activity/aggregated": ll.LiteLLMError("HTTP 404 from /customer", 404)})
    out, detail = ll.fetch_api(api, FIXTURE_NOW, previous)
    assert out["byUser"] == [{"user": "old"}] and out["health"] == {"m": {"status": "healthy"}}
    assert detail == "users: HTTP 404 from /customer; health: HTTP 500 from /health/latest"
    assert out["teams"]


def test_rejected_key_on_an_optional_part_fails_and_invalidates():
    key = Key()
    api = FakeApi({"/health/latest": ll.LiteLLMError("viewer key rejected (HTTP 401 from /health/latest)", 401)},
                  key)
    with pytest.raises(ll.LiteLLMError, match="viewer key rejected"):
        ll.fetch_api(api, FIXTURE_NOW, None)
    assert key.invalidated == 1


def test_invalid_json_is_an_error():
    api = ll.LiteLLMApi("http://x", Key(), lambda u, k, w: b"<html>")
    with pytest.raises(ll.LiteLLMError, match="/team/list: invalid JSON"):
        api.team_list()


# ---------------------------------------------------------------------------
# Viewer key
# ---------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Status(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def secret(value):
    return {"data": {"api-key": base64.b64encode(value.encode()).decode()}}


def test_key_read_cached_and_refreshed():
    reads, clock = [], Clock()

    def get(path):
        reads.append(path)
        return secret(f"sk-{len(reads)}")
    k = ll.ViewerKey(get, "ai-platform", "cluster-dashboard-litellm-key", clock)
    assert k.get() == "sk-1" and k.get() == "sk-1"
    assert reads == ["/api/v1/namespaces/ai-platform/secrets/cluster-dashboard-litellm-key"]
    clock.t += ll.KEY_REFRESH
    assert k.get() == "sk-2"
    k.invalidate()
    assert k.get() == "sk-3"


@pytest.mark.parametrize("obj", [{"data": {}}, {}, {"data": {"api-key": ""}}])
def test_key_empty_secret_is_missing(obj):
    with pytest.raises(ll.KeyMissing, match="viewer key not created yet"):
        ll.ViewerKey(lambda p: obj, "ns", "s", Clock()).get()


def test_key_secret_not_found_is_missing():
    def get(_path):
        raise Status(404)
    with pytest.raises(ll.KeyMissing):
        ll.ViewerKey(get, "ns", "s", Clock()).get()


def test_key_secret_forbidden_is_an_error():
    def get(_path):
        raise Status(403)
    with pytest.raises(ll.LiteLLMError, match=r"cannot read Secret s \(HTTP 403\)"):
        ll.ViewerKey(get, "ns", "s", Clock()).get()


def test_missing_key_pauses_the_source_and_it_recovers():
    k = ll.ViewerKey(lambda p: {}, "ns", "s", Clock())
    src = PolledSource("litellm-api", 30, lambda: k.get() and None)
    src.run_once()
    st = src.tracker.status()
    assert (st.state, st.detail) == ("disabled", "viewer key not created yet")
    src.fn = lambda: None
    src.run_once()
    assert src.tracker.status().state == "ok"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
def server():
    state = {"status": 200, "body": b"{}", "auth": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            state["auth"].append(self.headers.get("Authorization"))
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(state["body"])))
            self.end_headers()
            self.wfile.write(state["body"])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()
    srv.server_close()


def test_http_sends_bearer_key(server):
    assert ll.http_get(server["url"] + "/x?key=v", SECRET_KEY, "/x") == b"{}"
    assert server["auth"] == [f"Bearer {SECRET_KEY}"]


@pytest.mark.parametrize("code", [401, 403])
def test_http_rejected_key(server, code):
    server["status"] = code
    with pytest.raises(ll.LiteLLMError) as e:
        ll.http_get(server["url"] + "/team/list", SECRET_KEY, "/team/list")
    assert e.value.auth and str(e.value) == f"viewer key rejected (HTTP {code} from /team/list)"


def test_http_error_text_has_no_key_or_query(server):
    server["status"] = 500
    with pytest.raises(ll.LiteLLMError) as e:
        ll.http_get(server["url"] + "/team/list?secret=1", SECRET_KEY, "/team/list")
    assert str(e.value) == "HTTP 500 from /team/list" and e.value.status == 500


def test_http_size_cap(server, monkeypatch):
    monkeypatch.setattr(ll, "MAX_BYTES", 10)
    server["body"] = b"x" * 11
    with pytest.raises(ll.LiteLLMError, match="response too large"):
        ll.http_get(server["url"], SECRET_KEY, "pod p")


def test_http_connection_refused():
    with pytest.raises(ll.LiteLLMError, match="^pod p: URLError$"):
        ll.http_get("http://127.0.0.1:9/metrics", SECRET_KEY, "pod p")
