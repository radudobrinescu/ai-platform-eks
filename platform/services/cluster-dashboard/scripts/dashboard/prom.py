"""Prometheus helpers: a text exposition-format parser and an instant-query
client. Used for DCGM GPU metrics, LiteLLM metrics and the autoscaling
Prometheus."""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import NamedTuple

_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


class Sample(NamedTuple):
    name: str
    labels: dict
    value: float


def parse_text(text: str, names: set[str] | None = None) -> list[Sample]:
    """Parse Prometheus text format. `names` limits the result to those metric
    names (cheaper on large exporters). Comment lines and lines that do not
    parse are skipped; NaN values are kept (callers decide)."""
    out = []
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        brace = line.find("{")
        space = line.find(" ")
        if brace != -1 and (space == -1 or brace < space):
            name = line[:brace]
            end = line.rfind("}")
            if end < brace:
                continue
            labels = {k: v.replace('\\"', '"').replace("\\\\", "\\")
                      for k, v in _LABEL.findall(line[brace + 1:end])}
            rest = line[end + 1:].split()
        else:
            name, labels, rest = line[:space], {}, line[space + 1:].split()
        if names is not None and name not in names:
            continue
        if not rest:
            continue
        try:
            out.append(Sample(name, labels, float(rest[0])))
        except ValueError:
            continue
    return out


def fetch_text(url: str, timeout: float = 4.0) -> str:
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def query(base_url: str, promql: str, timeout: float = 4.0) -> list[dict]:
    """Instant query; returns the result vector. Raises on HTTP or API errors,
    so the calling source can report them."""
    url = base_url.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote(promql)
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "ignore"))
    if d.get("status") != "success":
        raise RuntimeError(f"prometheus query failed: {d.get('error', d.get('status'))}")
    return d.get("data", {}).get("result", [])
