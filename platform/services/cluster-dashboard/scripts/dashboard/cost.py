"""Cost engine: instance prices, node and cluster $/hr, tokens per dollar.

Pure functions; the AWS Price List lookup is in dashboard.pricing.
"""
from __future__ import annotations

import json
import math

# On-demand $/hr (Linux, shared tenancy) by region and instance type. Ships with
# eu-central-1 (the reference deployment). Other regions come from the optional
# `cluster-dashboard-node-prices` ConfigMap or the live Price List, so the
# estimate works in any region without a code change. Instance types with no
# price are counted as `unpriced` instead of guessed.
DEFAULT_PRICES_BY_REGION = {
    "eu-central-1": {
        "g6.xlarge": 1.0064, "g6.2xlarge": 1.2225, "g6.4xlarge": 1.6547, "g6.8xlarge": 2.519,
        "g6.12xlarge": 5.7543, "g6.16xlarge": 4.2477, "g6.24xlarge": 8.3473, "g6.48xlarge": 16.6946,
        "g5.xlarge": 1.258, "g5.2xlarge": 1.5156, "g5.4xlarge": 2.0308, "g5.8xlarge": 3.0612,
        "g5.12xlarge": 7.0928, "g5.48xlarge": 20.3681,
        "c5.large": 0.097, "c5.xlarge": 0.194, "c5.2xlarge": 0.388,
        "c5a.large": 0.087, "c5a.xlarge": 0.174,
        "c7i.large": 0.1018, "c7i.xlarge": 0.2037, "c7a.large": 0.1171, "c7a.xlarge": 0.2343,
        "m6g.large": 0.092, "m6g.xlarge": 0.184, "m5.large": 0.115, "m5.xlarge": 0.23, "r5.large": 0.152,
    },
}
SPOT_FACTOR = 0.35        # rough spot discount vs on-demand; real spot prices vary
HOURS_PER_MONTH = 730


class InvalidPriceTable(ValueError):
    """The operator price table is valid JSON but not {region: {instance: $/hr}}."""


def _price(region: str, instance: str, value) -> float:
    """One table entry as $/hr. InvalidPriceTable unless it is a finite
    number >= 0 (a numeric string is accepted, as before the shape was checked)."""
    where = f"{region}/{instance}"
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise InvalidPriceTable(f"{where}: price must be a number, got {type(value).__name__}")
    try:
        price = float(value)
    except ValueError:
        raise InvalidPriceTable(f"{where}: price must be a number, got {value[:40]!r}") from None
    if not math.isfinite(price) or price < 0:
        raise InvalidPriceTable(f"{where}: price must be a finite number >= 0, got {price}")
    return price


def parse_price_table(prices_json: str | None) -> dict[str, dict[str, float]]:
    """The operator table from the ConfigMap (data['prices.json'] =
    {"<region>": {"<instance>": usd_per_hour}}); {} when there is none.
    Raises ValueError: json.JSONDecodeError for malformed JSON,
    InvalidPriceTable for JSON of any other shape."""
    if not prices_json:
        return {}
    doc = json.loads(prices_json)          # json.JSONDecodeError is a ValueError
    if not isinstance(doc, dict):
        raise InvalidPriceTable(f"expected an object of regions, got {type(doc).__name__}")
    out = {}
    for region, table in doc.items():
        if not isinstance(table, dict):
            raise InvalidPriceTable(f"{region}: expected an object of instance prices, got {type(table).__name__}")
        out[region] = {instance: _price(region, instance, v) for instance, v in table.items()}
    return out


def merge_prices(prices_json: str | None) -> dict:
    """Shipped defaults plus the operator table from the ConfigMap; an
    operator price wins. Raises ValueError when the table is not valid (see
    parse_price_table) so the caller can report it and use the defaults."""
    operator = parse_price_table(prices_json)
    regions = dict.fromkeys((*DEFAULT_PRICES_BY_REGION, *operator))
    return {region: {**DEFAULT_PRICES_BY_REGION.get(region, {}), **operator.get(region, {})} for region in regions}


def detect_region(nodes: list[dict], fallback: str = "") -> str:
    """Most common node region label, else `fallback`."""
    counts: dict[str, int] = {}
    for n in nodes:
        r = n.get("region")
        if r:
            counts[r] = counts.get(r, 0) + 1
    return max(counts, key=counts.get) if counts else fallback


def priced_instances(nodes: list[dict]) -> tuple[str, ...]:
    return tuple(sorted({n.get("instance") for n in nodes
                         if n.get("instance") and n.get("instance") != "unknown"}))


def compute_cost(nodes: list[dict], region: str, prices: dict[str, float], live: bool) -> dict:
    """Sum the nodes' hourly rates into realtime $/hr and a 730 h monthly
    projection, split GPU vs platform. Sets `hourly` on each node.
    `prices` is {instance: on-demand $/hr} for `region`; `live` says whether
    any of it came from the AWS Price List."""
    hourly = gpu_hourly = 0.0
    unpriced = 0
    for n in nodes:
        base = prices.get(n["instance"])
        if base is None:
            unpriced += 1
            n["hourly"] = 0.0
            continue
        rate = base * (SPOT_FACTOR if n.get("capacity") == "spot" else 1.0)
        n["hourly"] = round(rate, 4)
        hourly += rate
        if n["gpu"] > 0:
            gpu_hourly += rate
    return {
        "hourly": round(hourly, 4),
        "monthly": round(hourly * HOURS_PER_MONTH, 2),
        "gpuHourly": round(gpu_hourly, 4),
        "platformHourly": round(hourly - gpu_hourly, 4),
        "unpriced": unpriced,
        "region": region,
        "priceSource": "aws-price-list" if live else ("table" if prices else "none"),
    }


def tok_per_dollar(tps, cost_hr):
    """Tokens produced per US dollar of serving cost: tok/s x 3600 / ($/hr).

    `cost_hr` is the GPU node cost attributed to a self-hosted model, or the
    token spend rate for Bedrock. Returns None when there is no cost basis
    (ratio undefined) and 0 for a model that costs money but produced no
    tokens: an idle but billing model, which the Cost view flags."""
    try:
        c = float(cost_hr or 0)
        t = float(tps or 0)
    except (TypeError, ValueError):
        return None
    if c <= 0:
        return None
    return round(t * 3600.0 / c)


def model_cost(name: str, pods: list[dict], nodes_by_name: dict[str, dict]) -> float:
    """$/hr of a self-hosted model: for each of its GPU pods in `inference`,
    the node's rate times the pod's share of the node's GPUs."""
    c = 0.0
    for p in pods:
        if p.get("namespace") == "inference" and p["name"].startswith(name + "-") and p.get("gpu", 0) > 0:
            nd = nodes_by_name.get(p.get("node"))
            if nd and nd.get("gpu"):
                c += nd.get("hourly", 0) * p["gpu"] / nd["gpu"]
    return round(c, 2)
