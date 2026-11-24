"""Cost engine: $/hr per node, totals, spot discount, unpriced instances, the
operator price-override ConfigMap and live prices."""
import json

import pytest

from dashboard import cost
from dashboard.pricing import LivePrices


def _node(instance, capacity="on-demand", gpu=0, region="eu-central-1"):
    return {"instance": instance, "capacity": capacity, "gpu": gpu, "region": region}


def _cost(nodes, table_json=None, live=None):
    region = cost.detect_region(nodes, "")
    prices = dict(cost.merge_prices(table_json).get(region, {}), **(live or {}))
    return cost.compute_cost(nodes, region, prices, bool(live))


def test_on_demand_spot_and_gpu_split():
    nodes = [_node("m6g.large"), _node("g6.4xlarge", "spot", gpu=1)]
    out = _cost(nodes)
    assert nodes[0]["hourly"] == 0.092
    assert nodes[1]["hourly"] == round(1.6547 * cost.SPOT_FACTOR, 4)
    assert out["gpuHourly"] == nodes[1]["hourly"]
    raw = 0.092 + 1.6547 * cost.SPOT_FACTOR
    assert out["hourly"] == round(raw, 4)
    assert out["monthly"] == round(raw * 730, 2)   # projected from the unrounded rate
    assert (out["priceSource"], out["unpriced"]) == ("table", 0)


def test_unknown_instance_is_counted_not_guessed():
    nodes = [_node("x9.huge")]
    out = _cost(nodes)
    assert nodes[0]["hourly"] == 0.0
    assert (out["unpriced"], out["hourly"]) == (1, 0.0)


def test_price_configmap_adds_regions():
    out = _cost([_node("m6g.large", region="us-east-1")], json.dumps({"us-east-1": {"m6g.large": 0.077}}))
    assert (out["region"], out["hourly"]) == ("us-east-1", 0.077)


def test_malformed_price_configmap_is_an_error_for_the_caller():
    with pytest.raises(ValueError):
        cost.merge_prices("{not json")


@pytest.mark.parametrize("table,problem", [
    ("[]", "expected an object of regions, got list"),
    ('"eu-west-1"', "expected an object of regions, got str"),
    ('{"eu-west-1": 3}', "eu-west-1: expected an object of instance prices, got int"),
    ('{"eu-west-1": ["g5.xlarge"]}', "eu-west-1: expected an object of instance prices, got list"),
    ('{"eu-west-1": {"g5.xlarge": null}}', "eu-west-1/g5.xlarge: price must be a number"),
    ('{"eu-west-1": {"g5.xlarge": {"usd": 1}}}', "eu-west-1/g5.xlarge: price must be a number"),
    ('{"eu-west-1": {"g5.xlarge": true}}', "eu-west-1/g5.xlarge: price must be a number"),
    ('{"eu-west-1": {"g5.xlarge": "cheap"}}', "eu-west-1/g5.xlarge: price must be a number"),
    ('{"eu-west-1": {"g5.xlarge": NaN}}', "eu-west-1/g5.xlarge: price must be a finite number >= 0"),
    ('{"eu-west-1": {"g5.xlarge": -1}}', "eu-west-1/g5.xlarge: price must be a finite number >= 0"),
])
def test_wrong_shape_price_configmap_is_an_error_for_the_caller(table, problem):
    """Valid JSON of the wrong shape is reported like malformed JSON: with a
    ValueError that says what is wrong, never an AttributeError or TypeError."""
    with pytest.raises(cost.InvalidPriceTable, match=problem) as raised:
        cost.merge_prices(table)
    assert isinstance(raised.value, ValueError), "callers catch ValueError"


def test_price_configmap_overrides_one_instance_and_keeps_the_rest():
    merged = cost.merge_prices(json.dumps({"eu-central-1": {"m6g.large": "0.5", "x9.huge": 2}}))
    assert merged["eu-central-1"]["m6g.large"] == 0.5 and merged["eu-central-1"]["x9.huge"] == 2.0
    assert merged["eu-central-1"]["m5.large"] == cost.DEFAULT_PRICES_BY_REGION["eu-central-1"]["m5.large"]
    assert "x9.huge" not in cost.DEFAULT_PRICES_BY_REGION["eu-central-1"], "the shipped table is not modified"
    assert cost.merge_prices("") == cost.merge_prices(None) == cost.DEFAULT_PRICES_BY_REGION


@pytest.mark.parametrize("table", ["{not json", "[]", '{"eu-central-1": 3}', '{"eu-central-1": {"m6g.large": null}}'])
def test_invalid_price_configmap_falls_back_to_the_shipped_table(table, caplog):
    """The dashboard keeps deriving every section; the problem is a note in
    the status section and one log line, not a stopped derive loop."""
    from dash_testlib import fixture_app
    app = fixture_app()
    app.derive_once()
    _, good = app.store.get()
    app.caches["cm-node-prices"]._items = {"ai-platform/cluster-dashboard-node-prices": {
        "metadata": {"name": "cluster-dashboard-node-prices"}, "data": {"prices.json": table}}}
    with caplog.at_level("WARNING", logger="dashboard.app"):
        app.derive_once()
        app.derive_once()
    _, sections = app.store.get()
    assert sections["cost"] == good["cost"] and sections["cost"]["priceSource"] == "table"
    (note,) = sections["status"]["notes"]
    assert note.startswith("cluster-dashboard-node-prices: invalid prices.json (")
    logged = [r for r in caplog.records if r.getMessage() == "derive.notes"]
    assert len(logged) == 1 and logged[0].notes == [note], "logged once, not on every recompute"


def test_live_prices_win_and_are_reported():
    out = _cost([_node("m6g.large")], live={"m6g.large": 0.1})
    assert (out["hourly"], out["priceSource"]) == (0.1, "aws-price-list")


def test_region_is_the_most_common_node_region():
    nodes = [_node("m6g.large", region="eu-west-1")] * 2 + [_node("m6g.large")]
    assert cost.detect_region(nodes, "x") == "eu-west-1"
    assert cost.detect_region([], "fallback") == "fallback"


def test_model_cost_shares_a_node_by_gpu():
    nodes = {"n": {"hourly": 2.0, "gpu": 4}}
    pods = [{"name": "m-1", "namespace": "inference", "node": "n", "gpu": 1},
            {"name": "m-2", "namespace": "inference", "node": "n", "gpu": 2},
            {"name": "m-3", "namespace": "other", "node": "n", "gpu": 1}]
    assert cost.model_cost("m", pods, nodes) == 1.5


class _Pricing:
    def __init__(self, prices):
        self.prices, self.calls = prices, []

    def get_products(self, **kw):
        it = next(f["Value"] for f in kw["Filters"] if f["Field"] == "instanceType")
        self.calls.append(it)
        p = self.prices.get(it)
        if p is None:
            return {"PriceList": []}
        term = {"priceDimensions": {"d": {"pricePerUnit": {"USD": str(p)}}}}
        return {"PriceList": [json.dumps({"terms": {"OnDemand": {"t": term}}})]}


def test_live_prices_fetch_only_missing_types_and_report_failures():
    client = _Pricing({"m6g.large": 0.09})
    lp = LivePrices(lambda: client)
    assert lp.refresh("eu-central-1", ("m6g.large", "x9.huge")) == ["x9.huge"]
    assert lp.get("eu-central-1") == {"m6g.large": 0.09}
    lp.refresh("eu-central-1", ("m6g.large", "x9.huge"))
    assert client.calls == ["m6g.large", "x9.huge", "x9.huge"], "known prices are not fetched again"


class _Ticking:
    """A monotonic clock that advances `step` seconds on every reading."""

    def __init__(self, step):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def test_live_price_run_stops_at_its_time_budget_and_continues_next_run():
    """Many instance types on a slow endpoint must not make one run long:
    what is not reached is looked up on the next run."""
    types = tuple(f"m{i}.large" for i in range(10))
    client = _Pricing({t: 0.1 for t in types})
    lp = LivePrices(lambda: client, budget_s=20, monotonic=_Ticking(6))     # each lookup "takes" 6 s
    assert lp.refresh("eu-central-1", types) == []
    assert 0 < len(client.calls) < len(types) and set(lp.get("eu-central-1")) == set(client.calls)
    for _ in range(5):
        lp.refresh("eu-central-1", types)
    assert set(lp.get("eu-central-1")) == set(types) and len(client.calls) == len(types)


def test_pricing_run_bound_is_far_below_the_liveness_limit():
    from dashboard import pricing
    assert pricing.CONNECT_TIMEOUT_S <= 5 and pricing.READ_TIMEOUT_S <= 5, "sources.py: timeouts of 5 s or less"
    one_call = pricing.MAX_ATTEMPTS * (pricing.CONNECT_TIMEOUT_S + pricing.READ_TIMEOUT_S + 1)   # +1 s backoff
    assert pricing.RUN_BUDGET_S + one_call <= 60, "a run must fit well inside the 60 s interval"


def test_price_list_client_gives_up_quickly_on_a_silent_endpoint(monkeypatch, tmp_path):
    """The real boto3 client, as the backend creates it, against an endpoint
    that accepts connections and never answers. boto3's defaults (60 s
    timeouts, 5 attempts) would block the pricing loop for about 5 minutes."""
    import socket
    import threading
    import time

    pytest.importorskip("boto3")
    from botocore.exceptions import BotoCoreError
    from dashboard import pricing
    monkeypatch.setattr(pricing, "CONNECT_TIMEOUT_S", 0.3)
    monkeypatch.setattr(pricing, "READ_TIMEOUT_S", 0.3)
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_ENDPOINT_URL", "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS"):
        monkeypatch.delenv(name, raising=False)
    for k, v in {"AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test", "AWS_EC2_METADATA_DISABLED": "true",
                 "AWS_CONFIG_FILE": str(tmp_path / "no-config"),             # not the developer's own
                 "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-credentials")}.items():
        monkeypatch.setenv(k, v)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    monkeypatch.setenv("AWS_ENDPOINT_URL_PRICING", f"http://127.0.0.1:{srv.getsockname()[1]}")
    held = []

    def accept_and_hold():
        try:
            while True:
                held.append(srv.accept()[0])
        except OSError:               # the listening socket was closed: the test is over
            return

    threading.Thread(target=accept_and_hold, daemon=True).start()
    client = pricing.client_factory("us-east-1")()
    cfg = client.meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (0.3, 0.3)
    assert cfg.retries["total_max_attempts"] == pricing.MAX_ATTEMPTS == 2 and cfg.retries["mode"] == "standard"
    result = {}

    def call():
        started = time.monotonic()
        try:
            pricing.fetch_ondemand(client, "eu-central-1", "m6g.large")
        except BotoCoreError as e:    # expected: ReadTimeoutError, checked below
            result["error"] = type(e).__name__
        result["seconds"] = time.monotonic() - started

    th = threading.Thread(target=call, daemon=True)
    th.start()
    th.join(10)
    attempts = len(held)
    srv.close()
    for conn in held:
        conn.close()
    assert not th.is_alive(), "the lookup is still blocked"
    assert result["error"] == "ReadTimeoutError" and result["seconds"] < 8
    assert attempts == pricing.MAX_ATTEMPTS, "one connection per attempt, and no more attempts than configured"
