"""`platformctl new-model` prices: the AWS Pricing API, its on-disk cache and the
static us-east-1 fallback (`pricing.resolve_prices`).

Offline: boto3 is replaced by a fake whose Pricing client answers from a table.
"""

import json
import sys
import types

import pytest

from recommend_instance import catalog, pricing

REGION = "eu-central-1"
ALL_TYPES = [i.name for i in catalog.INSTANCES]


class _Throttled(Exception):
    """Stands in for botocore's ClientError (ThrottlingException)."""


class _FakePricing:
    """Pricing client: `prices` maps a type to its hourly price, a type in
    `failing` raises, any other type has no product in the region."""

    def __init__(self, prices, failing=(), error=_Throttled):
        self.prices, self.failing, self.error = dict(prices), set(failing), error
        self.calls = []

    def get_products(self, **kw):
        filters = {f["Field"]: f["Value"] for f in kw["Filters"]}
        it = filters["instanceType"]
        self.calls.append(it)
        assert filters["regionCode"] == REGION
        if it in self.failing:
            raise self.error(f"Rate exceeded for {it}")
        if it not in self.prices:
            return {"PriceList": []}
        return {"PriceList": [json.dumps({"terms": {"OnDemand": {"t": {"priceDimensions": {
            "d": {"pricePerUnit": {"USD": str(self.prices[it])}}}}}}})]}


@pytest.fixture
def aws(monkeypatch, tmp_path):
    """Install a fake boto3 and an empty cache directory; returns a function
    that sets the Pricing client the next lookups will use."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    boto3 = types.ModuleType("boto3")
    botocore = types.ModuleType("botocore")
    exceptions = types.ModuleType("botocore.exceptions")
    exceptions.BotoCoreError = type("BotoCoreError", (Exception,), {})
    exceptions.ClientError = _Throttled
    botocore.exceptions = exceptions
    for name, mod in (("boto3", boto3), ("botocore", botocore), ("botocore.exceptions", exceptions)):
        monkeypatch.setitem(sys.modules, name, mod)

    def use(client):
        boto3.client = lambda service, region_name=None: client
        return client

    return use


def _cache() -> dict:
    return json.loads(pricing._pricing_cache_path(REGION).read_text())


class TestResolvePrices:
    def test_prices_come_from_the_pricing_api(self, aws):
        client = aws(_FakePricing({"g6.xlarge": 0.9, "g7e.2xlarge": 4.2}))
        prices, source = pricing.resolve_prices(REGION, refresh=False, verbose=False)[:2]
        assert prices == {"g6.xlarge": 0.9, "g7e.2xlarge": 4.2}
        assert source == f"AWS Pricing API ({REGION})"
        assert sorted(client.calls) == sorted(ALL_TYPES)

    def test_a_complete_lookup_is_cached_including_types_without_a_price(self, aws):
        aws(_FakePricing({"g6.xlarge": 0.9}))
        pricing.resolve_prices(REGION, refresh=False, verbose=False)
        saved = _cache()
        assert saved["prices"] == {"g6.xlarge": 0.9}
        assert saved["queried"] == sorted(ALL_TYPES)   # looked up, no price there: still done

    def test_the_cache_is_used_on_the_next_run(self, aws):
        aws(_FakePricing({"g6.xlarge": 0.9}))
        pricing.resolve_prices(REGION, refresh=False, verbose=False)
        second = aws(_FakePricing({"g6.xlarge": 5.0}))
        prices, source = pricing.resolve_prices(REGION, refresh=False, verbose=False)[:2]
        assert prices == {"g6.xlarge": 0.9} and source == f"cache ({REGION})"
        assert second.calls == []

    def test_refresh_ignores_the_cache(self, aws):
        aws(_FakePricing({"g6.xlarge": 0.9}))
        pricing.resolve_prices(REGION, refresh=False, verbose=False)
        aws(_FakePricing({"g6.xlarge": 5.0}))
        prices, source = pricing.resolve_prices(REGION, refresh=True, verbose=False)[:2]
        assert prices == {"g6.xlarge": 5.0} and source == f"AWS Pricing API ({REGION})"

    @pytest.mark.parametrize("error", [_Throttled, RuntimeError])
    def test_a_failed_lookup_is_not_recorded_as_done(self, aws, error):
        aws(_FakePricing({"g6.xlarge": 0.9, "g7e.2xlarge": 4.2}, failing={"g7e.2xlarge"}, error=error))
        prices = pricing.resolve_prices(REGION, refresh=False, verbose=False)[0]
        assert prices == {"g6.xlarge": 0.9}
        saved = _cache()
        assert "g7e.2xlarge" not in saved["queried"]
        assert saved["queried"] == sorted(set(ALL_TYPES) - {"g7e.2xlarge"})

    def test_a_failed_lookup_is_retried_on_the_next_run(self, aws):
        aws(_FakePricing({"g6.xlarge": 0.9, "g7e.2xlarge": 4.2}, failing={"g7e.2xlarge"}))
        pricing.resolve_prices(REGION, refresh=False, verbose=False)
        # next run: the throttling is over. The cache is incomplete, so it is not
        # used and g7e.2xlarge gets its regional price, not the us-east-1 one.
        retry = aws(_FakePricing({"g6.xlarge": 0.9, "g7e.2xlarge": 4.2}))
        prices, source = pricing.resolve_prices(REGION, refresh=False, verbose=False)[:2]
        assert "g7e.2xlarge" in retry.calls
        assert prices["g7e.2xlarge"] == 4.2 and source == f"AWS Pricing API ({REGION})"
        # ... and now the cache is complete
        third = aws(_FakePricing({}))
        assert pricing.resolve_prices(REGION, refresh=False, verbose=False)[1] == f"cache ({REGION})"
        assert third.calls == []

    def test_a_failed_lookup_is_reported_with_verbose(self, aws, capsys):
        aws(_FakePricing({"g6.xlarge": 0.9}, failing={"g7e.2xlarge"}))
        pricing.resolve_prices(REGION, refresh=False, verbose=True)
        assert "pricing lookup failed for g7e.2xlarge" in capsys.readouterr().err

    def test_without_any_price_the_static_catalog_is_used_and_nothing_is_cached(self, aws):
        aws(_FakePricing({}, failing=set(ALL_TYPES)))
        prices, source = pricing.resolve_prices(REGION, refresh=False, verbose=False)[:2]
        assert prices == {i.name: i.price_usd_h for i in catalog.INSTANCES}
        assert source == f"⚠ static catalog (us-east-1, not {REGION})"
        assert not pricing._pricing_cache_path(REGION).exists()

    def test_static_catalog_in_its_own_region_is_not_flagged(self, aws, monkeypatch):
        monkeypatch.setitem(sys.modules, "boto3", None)   # import boto3 -> ImportError
        prices, source = pricing.resolve_prices("us-east-1", refresh=False, verbose=False)[:2]
        assert source == "static catalog (us-east-1)"
        assert prices["g6.xlarge"] == 0.805
