"""Live on-demand EC2 prices from the AWS Price List API (Pod Identity role
`<cluster>-dashboard-pricing`, needs pricing:GetProducts). Without boto3 or
access, the shipped and ConfigMap tables in dashboard.cost are used."""
from __future__ import annotations

import json
import threading


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
    types not yet known; a failed lookup is retried on the next run."""

    def __init__(self, client_factory):
        self._client_factory = client_factory
        self._client = None
        self._prices: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def refresh(self, region: str, instances: tuple[str, ...]) -> list[str]:
        """Fetch missing prices; returns the instance types that failed."""
        if not region or not instances:
            return []
        if self._client is None:
            self._client = self._client_factory()
        known = self.get(region)
        failed = []
        for it in instances:
            if it in known:
                continue
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
