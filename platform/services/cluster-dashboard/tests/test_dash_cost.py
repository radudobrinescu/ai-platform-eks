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
