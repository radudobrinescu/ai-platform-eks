"""Region detection and EC2 GPU pricing (AWS Pricing API with on-disk cache and a
static catalog fallback)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from .catalog import CATALOG_PRICING_REGION, INSTANCES, PRICING_CACHE_TTL_SEC


# --------------------------------------------------------------------------- #
# Region + pricing                                                            #
# --------------------------------------------------------------------------- #

def detect_region(explicit: str | None) -> str:
    """Explicit > AWS_REGION > AWS_DEFAULT_REGION > boto3 session > catalog default."""
    if explicit:
        return explicit
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        import boto3  # type: ignore
        region = boto3.Session().region_name
        if region:
            return region
    except Exception:
        pass
    return CATALOG_PRICING_REGION


def _pricing_cache_path(region: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "ai-platform" / f"pricing-{region}.json"


def _load_cached_prices(region: str, wanted: list[str] | None = None) -> dict[str, float] | None:
    """Cached prices for `region`, or None when the cache is missing, older than
    PRICING_CACHE_TTL_SEC, or did not look up every type in `wanted` (a type
    added to the catalog after the cache was written would otherwise get the
    static us-east-1 price in every region for up to 30 days). A type the
    region does not offer was looked up and has no price; that is fine."""
    path = _pricing_cache_path(region)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if time.time() - payload.get("timestamp", 0) > PRICING_CACHE_TTL_SEC:
            return None
        if wanted is not None and not set(wanted) <= set(payload.get("queried") or []):
            return None
        return {k: float(v) for k, v in payload.get("prices", {}).items()}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_cached_prices(region: str, prices: dict[str, float], queried: list[str] | None = None) -> None:
    path = _pricing_cache_path(region)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"timestamp": time.time(), "region": region, "prices": prices,
                                    "queried": sorted(queried if queried is not None else prices)}))
    except OSError:
        pass  # Non-fatal; we'll just refetch next time.


def _parse_pricelist_entry(price_list_json: str) -> float | None:
    """Extract the hourly OnDemand USD price from one AWS Pricing API record."""
    try:
        data = json.loads(price_list_json)
        for term in data.get("terms", {}).get("OnDemand", {}).values():
            for dim in term.get("priceDimensions", {}).values():
                usd = dim.get("pricePerUnit", {}).get("USD")
                if usd is not None:
                    val = float(usd)
                    if val > 0:
                        return val
    except (ValueError, AttributeError, TypeError):
        return None
    return None


def fetch_prices_from_aws(region: str, instance_types: list[str],
                          verbose: bool = False) -> tuple[dict[str, float], list[str]]:
    """Query the AWS Pricing API for on-demand Linux prices in `region`.

    Returns (prices, completed). `completed` lists the types whose lookup got an
    answer — a price, or no product in that region. A type whose lookup failed
    (throttling, network) is in neither, so the caller does not mistake "could
    not ask" for "has no price there".

    Requires boto3 + valid AWS credentials. Silent no-op if either is missing.
    """
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        if verbose:
            print("  ! boto3 not installed; falling back to static us-east-1 prices",
                  file=sys.stderr)
        return {}, []

    # Pricing API is global but only exposed in us-east-1, ap-south-1, eu-central-1.
    try:
        client = boto3.client("pricing", region_name="us-east-1")
    except Exception as e:
        if verbose:
            print(f"  ! could not initialise Pricing client: {e}", file=sys.stderr)
        return {}, []

    prices: dict[str, float] = {}
    completed: list[str] = []
    for it in instance_types:
        try:
            resp = client.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType",    "Value": it},
                    {"Type": "TERM_MATCH", "Field": "regionCode",      "Value": region},
                    {"Type": "TERM_MATCH", "Field": "tenancy",         "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw",  "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus",  "Value": "Used"},
                ],
                MaxResults=10,
            )
            for entry in resp.get("PriceList", []):
                # PriceList items are JSON strings in SDK v1, dicts in v2.
                raw = entry if isinstance(entry, str) else json.dumps(entry)
                price = _parse_pricelist_entry(raw)
                if price is not None:
                    prices[it] = price
                    break
            completed.append(it)
        except (BotoCoreError, ClientError) as e:
            if verbose:
                print(f"  ! pricing lookup failed for {it}: {e}", file=sys.stderr)
        except Exception as e:
            if verbose:
                print(f"  ! unexpected error pricing {it}: {e}", file=sys.stderr)
    return prices, completed


def _mark_unpriced(prices: dict[str, float], looked_up: list[str]) -> dict[str, float | None]:
    """`prices` plus a None entry for every type that was looked up and has no
    price in the region."""
    return {**{it: None for it in looked_up}, **prices}


def resolve_prices(region: str, refresh: bool, verbose: bool) -> tuple[dict[str, float | None], str]:
    """Return (prices_by_instance_type, source_label).

    With prices from the Pricing API or its cache, a type the API has no price
    for in `region` maps to None: very likely the type is not offered there. A
    type that is absent from the mapping was not looked up (a failed lookup);
    callers fall back to the static catalog price for both, and flag the first.
    """
    instance_types = [i.name for i in INSTANCES]
    if not refresh:
        cached = _load_cached_prices(region, instance_types)
        if cached:
            # The cache is only used when it looked up every type.
            return _mark_unpriced(cached, instance_types), f"cache ({region})"

    fetched, completed = fetch_prices_from_aws(region, instance_types, verbose=verbose)
    if fetched:
        # Record only the lookups that got an answer: a throttled one must be
        # asked again on the next run, not kept at the static price for 30 days.
        _save_cached_prices(region, fetched, completed)
        return _mark_unpriced(fetched, completed), f"AWS Pricing API ({region})"

    # Fall back to the static catalog, flagging if the user asked for a
    # different region than the catalog's baked-in one.
    static = {i.name: i.price_usd_h for i in INSTANCES}
    if region != CATALOG_PRICING_REGION:
        if verbose:
            print(f"  ! using static {CATALOG_PRICING_REGION} prices — requested {region}",
                  file=sys.stderr)
        return static, f"⚠ static catalog ({CATALOG_PRICING_REGION}, not {region})"
    return static, f"static catalog ({CATALOG_PRICING_REGION})"
