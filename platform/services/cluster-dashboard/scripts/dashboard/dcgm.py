"""GPU utilization and framebuffer memory from NVIDIA dcgm-exporter.

Every exporter pod (one per GPU node) is scraped directly: the Service VIP
would return one node's GPUs per scrape."""
from __future__ import annotations

from dashboard import prom

METRICS = {"DCGM_FI_DEV_GPU_UTIL", "DCGM_FI_DEV_FB_USED", "DCGM_FI_DEV_FB_FREE"}


def aggregate(samples: list[prom.Sample]) -> dict:
    """{host: {"util": [pct per GPU], "mem": [pct per GPU or None]}}, GPUs in index order."""
    acc: dict = {}
    for s in samples:
        host = s.labels.get("Hostname", "")
        if host:
            acc.setdefault(host, {}).setdefault(s.labels.get("gpu", "0"), {})[s.name] = s.value
    out = {}
    for host, gpus in acc.items():
        util, mem = [], []
        for g in sorted(gpus, key=lambda x: int(x) if str(x).isdigit() else 0):
            d = gpus[g]
            util.append(round(d.get("DCGM_FI_DEV_GPU_UTIL", 0)))
            used, free = d.get("DCGM_FI_DEV_FB_USED"), d.get("DCGM_FI_DEV_FB_FREE")
            mem.append(round(used / (used + free) * 100)
                       if used is not None and free is not None and used + free > 0 else None)
        out[host] = {"util": util, "mem": mem}
    return out


def scrape(urls: list[str], fallback_url: str, fetch=prom.fetch_text) -> tuple[dict, list[str]]:
    """Scrape each exporter URL (falling back to the Service URL if none
    answer). Returns (aggregate, errors); an exporter that fails is reported,
    not fatal, so one bad node does not hide the others."""
    samples, errors = [], []
    for url in urls:
        try:
            samples.extend(prom.parse_text(fetch(url), METRICS))
        except Exception as e:  # network or HTTP error for one exporter
            errors.append(f"{url}: {e}")
    if not samples:
        try:
            samples = prom.parse_text(fetch(fallback_url), METRICS)
        except Exception as e:
            errors.append(f"{fallback_url}: {e}")
    return aggregate(samples), errors
