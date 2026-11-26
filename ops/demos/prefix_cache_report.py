#!/usr/bin/env python3
"""Turn one inference-perf run into Benchmark Report v0.2 files that Prism shows.

Runs inside the benchmark Job, after inference-perf (see prefix_cache_bench.py).
inference-perf writes one BR0.2 *partial* per load stage
(inference-perf.partial.stage_<n>.yaml) with the measured results only. This
adds what Prism needs to label and compare the runs, and writes
benchmark_report_v0.2_<target>_stage_<n>.yaml next to it (Prism's local scan
picks up files with that name):

  run.eid / description / keywords   one run per target, named for the chart
  scenario.stack                     vLLM engine, model, GPU, replicas
  scenario.load.standardized         load tool, stage rate, input/output length
  results.observability              vLLM prefix-cache hit rate during the stage,
                                     read from the platform's autoscaling Prometheus

Only the Python standard library and PyYAML (both in the inference-perf image).
Configuration comes from environment variables set by the Job.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime

import yaml

INFERENCE_PERF_VERSION = "v0.7.0"
TARGET_LABELS = {
    "router": "llm-d router (prefix-cache aware)",
    "direct": "Kubernetes Service (round robin)",
}


def stage_of(path: str) -> int:
    m = re.search(r"stage_(\d+)", os.path.basename(path))
    if not m:
        raise ValueError(f"no stage number in {path}")
    return int(m.group(1))


def _seconds(start: str, end: str) -> float:
    def parse(t: str) -> datetime:
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    return (parse(end) - parse(start)).total_seconds()


def hit_rate_query(pool: str, window_s: int) -> str:
    """vLLM's prefix-cache hit rate over the window, all replicas of the pool:
    cached prompt tokens / prompt tokens looked up."""
    w = f"{max(window_s, 15)}s"
    return (f'sum(increase(vllm:prefix_cache_hits_total{{pool="{pool}"}}[{w}]))'
            f' / sum(increase(vllm:prefix_cache_queries_total{{pool="{pool}"}}[{w}]))')


def prometheus_value(prom_url: str, query: str, at: str) -> float | None:
    """One instant query; None when Prometheus has no answer (no data, NaN)."""
    ts = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    url = f"{prom_url}/api/v1/query?" + urllib.parse.urlencode({"query": query, "time": f"{ts:.3f}"})
    try:
        with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 - in-cluster Prometheus
            data = json.load(r)
    except Exception as e:  # noqa: BLE001 - the hit rate is optional on the chart
        print(f"prefix-cache hit rate unavailable: {e}", file=sys.stderr)
        return None
    result = (data.get("data") or {}).get("result") or []
    if not result:
        return None
    try:
        v = float(result[0]["value"][1])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def overlay(doc: dict, *, stage: int, target: str, run_id: str, model: str, replicas: int, accelerator: str,
            vllm_version: str, router_version: str, rate_qps: float, input_len: int, output_len: int,
            hit_rate: float | None) -> dict:
    """Add the run, scenario and observability fields Prism reads to a BR0.2
    partial. Results measured by inference-perf are left as they are."""
    run = doc.setdefault("run", {}) or {}
    doc["run"] = run
    run["eid"] = f"{run_id}-{target}"
    run["description"] = TARGET_LABELS.get(target, target)
    run["keywords"] = ["prefix-cache-demo", target]
    doc["scenario"] = {
        **(doc.get("scenario") or {}),
        # BR0.2 components: the request path in front of the replicas, then vLLM.
        "stack": [
            {"metadata": {"label": "llm-d-router" if target == "router" else "kubernetes-service",
                          "cfg_id": target},
             "standardized": {"kind": "generic",
                              "tool": "llm-d-router" if target == "router" else "kube-proxy",
                              "tool_version": router_version if target == "router" else ""},
             "native": {}},
            {"metadata": {"label": "vllm", "cfg_id": "vllm"},
             "standardized": {"kind": "inference_engine", "tool": "vllm", "tool_version": vllm_version,
                              "role": "replica", "replicas": replicas, "model": {"name": model},
                              "accelerator": {"model": accelerator, "count": replicas,
                                              "parallelism": {"tp": 1}}},
             "native": {}},
        ],
        "load": {"standardized": {"tool": "inference-perf", "tool_version": INFERENCE_PERF_VERSION,
                                  "stage": stage, "rate_qps": rate_qps,
                                  "input_seq_len": {"value": input_len},
                                  "output_seq_len": {"value": output_len}}},
    }
    if hit_rate is not None:
        results = doc.setdefault("results", {}) or {}
        doc["results"] = results
        obs = results.setdefault("observability", {}) or {}
        results["observability"] = obs
        obs["vllm_prefix_cache_hit_rate"] = {"aggregated": {"units": "fraction", "mean": round(hit_rate, 4)}}
    return doc


def stage_summary(doc: dict, rate: float) -> dict:
    """The numbers the side-by-side table shows, from a BR0.2 report (seconds)."""
    results = doc.get("results") or {}
    agg = ((results.get("request_performance") or {}).get("aggregate") or {})
    ttft = (agg.get("latency") or {}).get("time_to_first_token") or {}
    factor = 0.001 if str(ttft.get("units") or "s").lower().startswith("ms") else 1.0
    out = ((agg.get("throughput") or {}).get("output_token_rate") or {}).get("mean")
    hit = ((((results.get("observability") or {}).get("vllm_prefix_cache_hit_rate") or {})
            .get("aggregated") or {}).get("mean"))

    def sec(v):
        return None if v is None else float(v) * factor
    return {"rate": rate, "ttft_p50": sec(ttft.get("p50")), "ttft_p99": sec(ttft.get("p99")),
            "out_tok_s": out, "hit": hit}


def main() -> int:
    env = os.environ
    out_dir, target = env["OUT_DIR"], env["TARGET"]
    rates = [float(r) for r in env["RATES"].split(",")]
    partials = sorted(glob.glob(os.path.join(out_dir, "**", "inference-perf.partial.stage_*.yaml"), recursive=True))
    if not partials:
        print(f"no inference-perf BR0.2 partials under {out_dir}", file=sys.stderr)
        return 1
    for path in partials:
        stage = stage_of(path)
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        t = (doc.get("run") or {}).get("time") or {}
        hit = None
        if t.get("start") and t.get("end") and env.get("PROM_URL"):
            hit = prometheus_value(env["PROM_URL"], hit_rate_query(env["POOL"], int(_seconds(t["start"], t["end"]))),
                                   t["end"])
        overlay(doc, stage=stage, target=target, run_id=env["RUN_ID"], model=env["MODEL"],
                replicas=int(env["REPLICAS"]), accelerator=env.get("ACCELERATOR", ""),
                vllm_version=env.get("VLLM_VERSION", ""), router_version=env.get("ROUTER_VERSION", ""),
                rate_qps=rates[stage] if stage < len(rates) else 0.0,
                input_len=int(env["INPUT_LEN"]), output_len=int(env["OUTPUT_LEN"]), hit_rate=hit)
        dest = os.path.join(out_dir, f"benchmark_report_v0.2_{target}_stage_{stage}.yaml")
        with open(dest, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, sort_keys=False)
        print(f"stage {stage}: {dest}" + (f" (prefix-cache hit rate {hit:.0%})" if hit is not None else ""))
        print("RESULT " + json.dumps(stage_summary(doc, rates[stage] if stage < len(rates) else 0.0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
