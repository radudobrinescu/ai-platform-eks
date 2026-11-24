"""Live on-demand EC2 prices from the AWS Price List API (Pod Identity role
`<cluster>-dashboard-pricing`, needs pricing:GetProducts). Without boto3 or
access, the shipped and ConfigMap tables in dashboard.cost are used."""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

# boto3's defaults (60 s connect and read timeouts, up to 5 attempts) would
# block the pricing loop for minutes when the endpoint is unreachable. These
# follow the rule in dashboard.sources: every upstream call has a timeout of
# 5 s or less. One lookup takes at most about MAX_ATTEMPTS x 9 s.
CONNECT_TIMEOUT_S = 3
READ_TIMEOUT_S = 5
MAX_ATTEMPTS = 2             # the first try and one retry (throttling, a dropped connection)
RUN_BUDGET_S = 20            # lookups in one run stop after this; the next run continues


def client_factory(endpoint_region: str) -> Callable[[], object]:
    """A function that creates the Price List client (boto3 is imported when
    it is called, so this module loads without it)."""
    def create():
        import boto3
        from botocore.config import Config
        return boto3.client("pricing", region_name=endpoint_region, config=Config(
            connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S,
            retries={"total_max_attempts": MAX_ATTEMPTS, "mode": "standard"}))
    return create


def fetch_ondemand(client, region: str, instance_type: str) -> float | None:
    """One instance type's on-demand Linux, shared-tenancy $/hr, or None."""
    resp = client.get_products(ServiceCode="AmazonEC2", MaxResults=1, Filters=[
        {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
        {"Type": "TERM_MATCH", "Field": "instanceType", "Value": instance_type},
        {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
        {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
        {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
        {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
    ])
    for item in resp.get("PriceList", []):
        terms = (json.loads(item).get("terms", {}) or {}).get("OnDemand", {}) or {}
        for term in terms.values():
            for dim in (term.get("priceDimensions", {}) or {}).values():
                usd = (dim.get("pricePerUnit", {}) or {}).get("USD")
                if usd and float(usd) > 0:
                    return round(float(usd), 4)
    return None


class LivePrices:
    """Prices fetched so far, per region. `refresh` looks up only instance
    types not yet known; a failed lookup is retried on the next run. One run
    starts no new lookup after `budget_s`, so its duration is bounded however
    many instance types there are and however slow the endpoint is."""

    def __init__(self, client_factory, budget_s: float = RUN_BUDGET_S,
                 monotonic: Callable[[], float] = time.monotonic):
        self._client_factory = client_factory
        self._budget_s, self._monotonic = budget_s, monotonic
        self._client = None
        self._prices: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def refresh(self, region: str, instances: tuple[str, ...]) -> list[str]:
        """Fetch missing prices; returns the instance types the Price List
        has no price for. Types not reached within the budget are neither in
        the result nor in `get(region)`."""
        if not region or not instances:
            return []
        if self._client is None:
            self._client = self._client_factory()
        known = self.get(region)
        failed = []
        deadline = self._monotonic() + self._budget_s
        for it in instances:
            if it in known:
                continue
            if self._monotonic() >= deadline:
                break
            p = fetch_ondemand(self._client, region, it)
            if p is None:
                failed.append(it)
                continue
            with self._lock:
                self._prices.setdefault(region, {})[it] = p
        return failed

    def get(self, region: str) -> dict[str, float]:
        with self._lock:
            return dict(self._prices.get(region, {}))
