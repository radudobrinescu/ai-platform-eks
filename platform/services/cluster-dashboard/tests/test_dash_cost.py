"""Cost engine: $/hr per node, totals, spot discount, unpriced instances and
the operator price-override ConfigMap."""
import json

import pytest


@pytest.fixture
def pricing(backend, monkeypatch):
    monkeypatch.setattr(backend, "HAVE_BOTO3", False)
    monkeypatch.setattr(backend, "_price_cache", {"ts": 0.0, "data": None})
    monkeypatch.setattr(backend, "_live_price_cache", {})
    monkeypatch.setattr(backend, "k8s_get", lambda path: None)
    return backend


def _node(instance, capacity="on-demand", gpu=0, region="eu-central-1"):
    return {"instance": instance, "capacity": capacity, "gpu": gpu, "region": region}


def test_on_demand_spot_and_gpu_split(pricing):
    nodes = [_node("m6g.large"), _node("g6.4xlarge", "spot", gpu=1)]
    cost = pricing._compute_cost(nodes)
    assert nodes[0]["hourly"] == 0.092
    assert nodes[1]["hourly"] == round(1.6547 * pricing.SPOT_FACTOR, 4)
    assert cost["gpuHourly"] == nodes[1]["hourly"]
    raw = 0.092 + 1.6547 * pricing.SPOT_FACTOR
    assert cost["hourly"] == round(raw, 4)
    assert cost["monthly"] == round(raw * 730, 2)   # projected from the unrounded rate
    assert cost["priceSource"] == "table"
    assert cost["unpriced"] == 0


def test_unknown_instance_is_counted_not_guessed(pricing):
    nodes = [_node("x9.huge")]
    cost = pricing._compute_cost(nodes)
    assert nodes[0]["hourly"] == 0.0
    assert cost["unpriced"] == 1
    assert cost["hourly"] == 0.0


def test_price_configmap_adds_regions(pricing, monkeypatch):
    table = {"us-east-1": {"m6g.large": 0.077}}
    monkeypatch.setattr(pricing, "k8s_get",
                        lambda path: {"data": {"prices.json": json.dumps(table)}}
                        if path.endswith("/cluster-dashboard-node-prices") else None)
    cost = pricing._compute_cost([_node("m6g.large", region="us-east-1")])
    assert cost["region"] == "us-east-1"
    assert cost["hourly"] == 0.077


def test_region_is_the_most_common_node_region(pricing):
    nodes = [_node("m6g.large", region="eu-west-1")] * 2 + [_node("m6g.large")]
    assert pricing._detect_region(nodes) == "eu-west-1"
