#!/usr/bin/env python3
"""
recommend-instance-v2.py — Right-size EC2 GPU instances for an LLM on this EKS platform.

V2 adds bandwidth-aware throughput modeling on top of V1's VRAM-aware sizing:
  - HBM bandwidth in the instance catalog → decode tok/s estimation
  - Per-instance single-stream + concurrent tok/s/user estimates
  - --target-tok-s SLO validation in fleet mode (escalates to faster GPUs if needed)
  - Quant ↔ hardware compatibility warnings (e.g. fp8 needs Ada/Hopper)
  - More accurate per-GPU framework overhead model
  - max_num_seqs emitted in the generated YAML
  - MoE bandwidth annotation (active vs total params)
  - Always-on pricing-fallback warning (no need for --verbose)

Reads model architecture from HuggingFace, estimates VRAM requirements, and
recommends the cheapest instance that fits — with the right parallelism strategy
(TP, PP, or TP×PP) based on vLLM best practices and GPU interconnect topology.

Supports two modes:
  1. Single-instance sizing (default): pick the best GPU for one replica
  2. Fleet scaling (--target-users N): recommend instance type + replica count
     for serving N concurrent users across multiple workers, validated against
     the per-user throughput SLO (--target-tok-s)

Parallelism strategy (per https://docs.vllm.ai/en/latest/serving/parallelism_scaling/):
  - Tensor Parallelism (TP): splits layers across GPUs on NVLink-connected nodes
  - Pipeline Parallelism (PP): splits layer groups across GPUs on PCIe (no NVLink)
  - TP×PP: combines both for large multi-GPU deployments
  - TP requires num_attention_heads to be evenly divisible by tp_degree

Throughput model (per https://blog.vllm.ai/2025/09/05/anatomy-of-vllm.html):
  - Decode is memory-bandwidth-bound: tok/s ≈ HBM_bandwidth / params_streamed_per_token
  - For MoE, only active experts are streamed per token (effective bandwidth is higher)
  - Per-user throughput decays with concurrency along a ~1/sqrt(N) curve up to
    saturation, then ~1/N. Estimates here are first-order (±25%); validate by
    running `vllm bench serve` against a real deployment.

Examples:

  # Quick check — what GPU fits a 4B model?
  ./ops/recommend-instance-v2.py google/gemma-3-4b-it

  # 8B model with 16K context, 8 concurrent users per instance
  ./ops/recommend-instance-v2.py meta-llama/Llama-3.1-8B-Instruct --seq 16384 --users 8

  # Quantised 32B model — int4 cuts VRAM in half
  ./ops/recommend-instance-v2.py Qwen/Qwen2.5-32B-Instruct --quant int4

  # Fleet sizing — how many replicas for 50 concurrent users? (VRAM-only, no SLO)
  ./ops/recommend-instance-v2.py meta-llama/Llama-3.1-8B-Instruct --target-users 50

  # Latency SLO — 25 tok/s/user (forces faster GPUs into the mix)
  ./ops/recommend-instance-v2.py Qwen/Qwen2.5-7B-Instruct --target-users 100 --target-tok-s 25

  # Pin to TP=4, only show in-cluster options, machine-readable output
  ./ops/recommend-instance-v2.py meta-llama/Llama-3.1-70B-Instruct --tp 4 --in-cluster-only --json

  # Gated model (needs HuggingFace token)
  HF_TOKEN=hf_... ./ops/recommend-instance-v2.py google/gemma-3-27b-it

  # Budget-conscious: cap at $5/hr per instance
  ./ops/recommend-instance-v2.py Qwen/Qwen2.5-32B-Instruct --quant int4 --max-price 5
"""

from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _build_ssl_context() -> ssl.SSLContext:
    """Return an SSL context that works across common macOS Python installs.

    python.org CPython on macOS ships without a linked system CA store; fall
    back to certifi if installed (standard on most dev machines) before giving
    up.
    """
    ctx = ssl.create_default_context()
    if ctx.get_ca_certs():
        return ctx
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ctx


_SSL_CONTEXT = _build_ssl_context()


# --------------------------------------------------------------------------- #
# Terminal UX helpers: colour, bars, formatting                               #
# --------------------------------------------------------------------------- #

class _AnsiPalette:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    GREEN  = "\033[32m"
    YELLOW = "\033[33m"
    RED    = "\033[31m"
    CYAN   = "\033[36m"


class _NoPalette:
    RESET = BOLD = DIM = GREEN = YELLOW = RED = CYAN = ""


def _palette(emit_colour: bool) -> type:
    """Return the active colour palette class."""
    return _AnsiPalette if emit_colour else _NoPalette


def _should_use_colour(args: argparse.Namespace) -> bool:
    """Honour --json, NO_COLOR, and TTY detection."""
    if args.json:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _bar(used: float, total: float, width: int = 24) -> str:
    """Render a Unicode horizontal bar. `used` and `total` are arbitrary floats."""
    if total <= 0:
        return "░" * width
    frac  = max(0.0, min(used / total, 1.0))
    fill  = int(round(frac * width))
    return "█" * fill + "░" * (width - fill)


def _fmt_monthly(price_hour: float) -> str:
    """Format approximate monthly cost (730 h/month) with thousand separators."""
    return f"${price_hour * 730:,.0f}"


def _ruler(width: int, palette: type) -> str:
    return f"{palette.DIM}{'═' * width}{palette.RESET}"


# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

HF_API = "https://huggingface.co/api/models/{}"
HF_RAW_CONFIG = "https://huggingface.co/{}/resolve/main/config.json"

# Bytes per element for weights, by quantisation label.
WEIGHT_BYTES = {
    "fp32": 4.0,
    "fp16": 2.0,
    "bf16": 2.0,
    "int8": 1.0,
    "fp8":  1.0,
    "int4": 0.5,
    "gptq4": 0.5,
    "awq4":  0.5,
    "nf4":   0.5,
}

# Bytes per element for KV cache.
KV_BYTES = {
    "fp16": 2.0,
    "bf16": 2.0,
    "fp8":  1.0,
    "int8": 1.0,
}

# Cluster Karpenter NodePool constraints (from terraform/30.eks/30.cluster/karpenter/).
# Both NodePools now accept any NVIDIA-backed G or P instance. `gpu-shared`
# additionally requires a single GPU per node (time-slicing precondition).
CLUSTER_INFERENCE_CATEGORIES = {"g", "p"}
CLUSTER_SHARED_CATEGORIES    = {"g"}
CLUSTER_GPU_MANUFACTURER     = "nvidia"
# Matches the `replicas: 4` in terraform/30.eks/30.cluster/karpenter/gpu-shared.yaml
# under kubelet-device-plugins.nvidia.time-slicing. When gpu-shared is used,
# NVIDIA advertises 1 physical GPU as this many logical slots. Time-slicing
# does NOT partition GPU memory — all co-located models compete for the full
# VRAM, so a model is shared-eligible only if TIME_SLICE_REPLICAS copies of it
# fit in the GPU's memory.
TIME_SLICE_REPLICAS = 4
# Fallback pricing region — matches the static catalog. Overridden when the
# Pricing API is reachable or a cached snapshot exists for the target region.
CATALOG_PRICING_REGION = "us-east-1"
PRICING_CACHE_TTL_SEC  = 30 * 24 * 3600  # 30 days

# Valid GPU counts for vLLM tensor parallelism.
TP_DEGREES = (1, 2, 4, 8)


# --------------------------------------------------------------------------- #
# Instance catalog                                                            #
#                                                                             #
# Prices are approximate us-east-1 on-demand ($/hr) and are used only when the #
# AWS Pricing API cannot be reached. With boto3 available, per-region prices   #
# are fetched at runtime and cached to ~/.cache/ai-platform/pricing-*.json.    #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Instance:
    name:                str
    gpu:                 str
    num_gpus:            int
    vram_gb:             int     # per GPU
    vcpu:                int
    mem_gb:              int     # host RAM
    price_usd_h:         float   # us-east-1 on-demand, approximate fallback
    gpu_manufacturer:    str   = "nvidia"
    has_nvlink:          bool  = False  # NVLink interconnect (affects TP vs PP choice)
    hbm_bandwidth_tb_s:  float = 0.0    # per-GPU HBM bandwidth (TB/s) — drives decode tok/s
    compute_capability:  str   = ""     # NVIDIA CUDA compute capability ("7.5", "8.0", ...)
    arch_family:         str   = ""     # "Turing" | "Ampere" | "Ada" | "Hopper" | "Blackwell"

    @property
    def family(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def size(self) -> str:
        return self.name.split(".", 1)[1]

    @property
    def category(self) -> str:
        """G or P (first letter of the family)."""
        return self.family[0]

    @property
    def total_vram_gb(self) -> int:
        return self.vram_gb * self.num_gpus

    @property
    def total_hbm_tb_s(self) -> float:
        """Aggregate HBM bandwidth across all GPUs on the instance."""
        return self.hbm_bandwidth_tb_s * self.num_gpus


# HBM bandwidth values are per-GPU, taken from NVIDIA datasheets:
#   T4   320 GB/s   (Turing,  cc 7.5,  fp16 tensor cores only — no native bf16/fp8/int4-Marlin)
#   L4   300 GB/s   (Ada,     cc 8.9,  bf16 + fp8 + int4 OK)
#   A10G 600 GB/s   (Ampere,  cc 8.6,  bf16 + int4 OK; no fp8)
#   L40S 864 GB/s   (Ada,     cc 8.9,  bf16 + fp8 + int4 OK)
#   A100-40 1555 GB/s (Ampere, cc 8.0, bf16 + int4 OK; no fp8)
#   A100-80 2039 GB/s (Ampere, cc 8.0, bf16 + int4 OK; no fp8)
#   H100  3350 GB/s (Hopper,  cc 9.0,  fp8 + bf16 + int4 OK)
#   H200  4800 GB/s (Hopper,  cc 9.0,  fp8 + bf16 + int4 OK)
INSTANCES: list[Instance] = [
    # g4dn — T4 (no NVLink, Turing — limited dtype support)
    Instance("g4dn.xlarge",   "T4",         1, 16,   4,  16,  0.526,
             hbm_bandwidth_tb_s=0.32, compute_capability="7.5", arch_family="Turing"),
    Instance("g4dn.2xlarge",  "T4",         1, 16,   8,  32,  0.752,
             hbm_bandwidth_tb_s=0.32, compute_capability="7.5", arch_family="Turing"),
    Instance("g4dn.12xlarge", "T4",         4, 16,  48, 192,  3.912,
             hbm_bandwidth_tb_s=0.32, compute_capability="7.5", arch_family="Turing"),

    # g5 — A10G (no NVLink, Ampere)
    Instance("g5.xlarge",     "A10G",       1, 24,   4,  16,  1.006,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),
    Instance("g5.2xlarge",    "A10G",       1, 24,   8,  32,  1.212,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),
    Instance("g5.4xlarge",    "A10G",       1, 24,  16,  64,  1.624,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),
    Instance("g5.12xlarge",   "A10G",       4, 24,  48, 192,  5.672,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),
    Instance("g5.48xlarge",   "A10G",       8, 24, 192, 768, 16.288,
             hbm_bandwidth_tb_s=0.60, compute_capability="8.6", arch_family="Ampere"),

    # g6 — L4 (no NVLink, Ada — supports fp8)
    Instance("g6.xlarge",     "L4",         1, 24,   4,  16,  0.805,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),
    Instance("g6.2xlarge",    "L4",         1, 24,   8,  32,  0.978,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),
    Instance("g6.4xlarge",    "L4",         1, 24,  16,  64,  1.323,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),
    Instance("g6.12xlarge",   "L4",         4, 24,  48, 192,  4.602,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),
    Instance("g6.48xlarge",   "L4",         8, 24, 192, 768, 13.350,
             hbm_bandwidth_tb_s=0.30, compute_capability="8.9", arch_family="Ada"),

    # g6e — L40S (no NVLink, Ada — supports fp8; prefer PP over TP for multi-GPU)
    Instance("g6e.xlarge",    "L40S",       1, 48,   4,  32,  1.861,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),
    Instance("g6e.2xlarge",   "L40S",       1, 48,   8,  64,  2.242,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),
    Instance("g6e.4xlarge",   "L40S",       1, 48,  16, 128,  3.004,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),
    Instance("g6e.12xlarge",  "L40S",       4, 48,  48, 384, 10.493,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),
    Instance("g6e.48xlarge",  "L40S",       8, 48, 192, 1536, 30.131,
             hbm_bandwidth_tb_s=0.86, compute_capability="8.9", arch_family="Ada"),

    # p4d / p4de — A100 (NVLink, Ampere)
    Instance("p4d.24xlarge",  "A100 40GB",  8, 40,  96, 1152, 32.773, has_nvlink=True,
             hbm_bandwidth_tb_s=1.555, compute_capability="8.0", arch_family="Ampere"),
    Instance("p4de.24xlarge", "A100 80GB",  8, 80,  96, 1152, 40.966, has_nvlink=True,
             hbm_bandwidth_tb_s=2.039, compute_capability="8.0", arch_family="Ampere"),

    # p5 / p5e / p5en — H100 / H200 (NVLink, Hopper — supports fp8)
    Instance("p5.48xlarge",   "H100 80GB",  8, 80, 192, 2048, 98.320, has_nvlink=True,
             hbm_bandwidth_tb_s=3.35, compute_capability="9.0", arch_family="Hopper"),
    Instance("p5e.48xlarge",  "H200 141GB", 8, 141, 192, 2048, 118.020, has_nvlink=True,
             hbm_bandwidth_tb_s=4.80, compute_capability="9.0", arch_family="Hopper"),
    Instance("p5en.48xlarge", "H200 141GB", 8, 141, 192, 2048, 124.000, has_nvlink=True,
             hbm_bandwidth_tb_s=4.80, compute_capability="9.0", arch_family="Hopper"),
]


# --------------------------------------------------------------------------- #
# Quant ↔ Hardware compatibility (PR 4)                                       #
# --------------------------------------------------------------------------- #

# Min compute capability required for native tensor-core support of each dtype.
# Lower compute capability falls back to slow software emulation, often defeating
# the purpose of the optimization.
QUANT_MIN_COMPUTE_CAPABILITY = {
    "fp32":  "7.5",   # universal
    "fp16":  "7.0",   # Volta+
    "bf16":  "8.0",   # Ampere+ (T4/Turing has no native bf16 tensor cores)
    "int8":  "7.5",   # Turing+ via DP4A
    "fp8":   "8.9",   # Ada+ (L4, L40S, H100, H200, B200)
    "int4":  "8.0",   # Ampere+ for Marlin/AWQ/GPTQ kernels
    "gptq4": "8.0",   # same as int4
    "awq4":  "8.0",   # same as int4
    "nf4":   "8.0",   # bitsandbytes 4-bit
}

# Same table for KV cache dtypes.
KV_QUANT_MIN_COMPUTE_CAPABILITY = {
    "fp16":  "7.0",
    "bf16":  "8.0",
    "fp8":   "8.9",
    "int8":  "7.5",
}


def _cc_lt(a: str, b: str) -> bool:
    """Compare two compute capability strings (e.g. '8.0' < '8.9')."""
    if not a or not b:
        return False
    try:
        return tuple(int(x) for x in a.split(".")) < tuple(int(x) for x in b.split("."))
    except ValueError:
        return False


def _quant_compat_warning(inst: Instance, weight_q: str, kv_q: str) -> str | None:
    """Return a human-readable warning if the requested quants don't match the GPU.

    Returns None when the combination is fully supported.
    """
    issues: list[str] = []

    min_cc_w = QUANT_MIN_COMPUTE_CAPABILITY.get(weight_q)
    if min_cc_w and inst.compute_capability and _cc_lt(inst.compute_capability, min_cc_w):
        issues.append(
            f"weights {weight_q} needs cc≥{min_cc_w}, "
            f"{inst.gpu} is cc{inst.compute_capability} ({inst.arch_family}) — "
            f"falls back to software emulation"
        )

    min_cc_k = KV_QUANT_MIN_COMPUTE_CAPABILITY.get(kv_q)
    if min_cc_k and inst.compute_capability and _cc_lt(inst.compute_capability, min_cc_k):
        issues.append(
            f"KV cache {kv_q} needs cc≥{min_cc_k}, "
            f"{inst.gpu} is cc{inst.compute_capability}"
        )

    return "; ".join(issues) if issues else None


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


def _load_cached_prices(region: str) -> dict[str, float] | None:
    path = _pricing_cache_path(region)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
        if time.time() - payload.get("timestamp", 0) > PRICING_CACHE_TTL_SEC:
            return None
        return {k: float(v) for k, v in payload.get("prices", {}).items()}
    except (OSError, ValueError, KeyError):
        return None


def _save_cached_prices(region: str, prices: dict[str, float]) -> None:
    path = _pricing_cache_path(region)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"timestamp": time.time(), "region": region, "prices": prices}))
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
                          verbose: bool = False) -> dict[str, float]:
    """Query the AWS Pricing API for on-demand Linux prices in `region`.

    Requires boto3 + valid AWS credentials. Silent no-op if either is missing.
    """
    try:
        import boto3  # type: ignore
        from botocore.exceptions import BotoCoreError, ClientError  # type: ignore
    except ImportError:
        if verbose:
            print("  ! boto3 not installed; falling back to static us-east-1 prices",
                  file=sys.stderr)
        return {}

    # Pricing API is global but only exposed in us-east-1, ap-south-1, eu-central-1.
    try:
        client = boto3.client("pricing", region_name="us-east-1")
    except Exception as e:
        if verbose:
            print(f"  ! could not initialise Pricing client: {e}", file=sys.stderr)
        return {}

    prices: dict[str, float] = {}
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
        except (BotoCoreError, ClientError) as e:
            if verbose:
                print(f"  ! pricing lookup failed for {it}: {e}", file=sys.stderr)
        except Exception as e:
            if verbose:
                print(f"  ! unexpected error pricing {it}: {e}", file=sys.stderr)
    return prices


def resolve_prices(region: str, refresh: bool, verbose: bool) -> tuple[dict[str, float], str]:
    """Return (prices_by_instance_type, source_label)."""
    if not refresh:
        cached = _load_cached_prices(region)
        if cached:
            return cached, f"cache ({region})"

    instance_types = [i.name for i in INSTANCES]
    fetched = fetch_prices_from_aws(region, instance_types, verbose=verbose)
    if fetched:
        _save_cached_prices(region, fetched)
        return fetched, f"AWS Pricing API ({region})"

    # Fall back to the static catalog, flagging if the user asked for a
    # different region than the catalog's baked-in one.
    static = {i.name: i.price_usd_h for i in INSTANCES}
    if region != CATALOG_PRICING_REGION:
        if verbose:
            print(f"  ! using static {CATALOG_PRICING_REGION} prices — requested {region}",
                  file=sys.stderr)
        return static, f"⚠ static catalog ({CATALOG_PRICING_REGION}, not {region})"
    return static, f"static catalog ({CATALOG_PRICING_REGION})"


# --------------------------------------------------------------------------- #
# Model metadata                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class ModelSpec:
    model_id:       str
    params:         int            # total parameters (all experts for MoE)
    num_layers:     int
    hidden_size:    int
    num_heads:      int
    num_kv_heads:   int            # == num_heads for MHA, smaller for GQA/MQA
    head_dim:       int
    vocab_size:     int
    max_position:   int
    architecture:   str = ""
    is_moe:         bool = False
    num_experts:    int = 0
    active_experts: int = 0
    warnings:       list[str] = field(default_factory=list)


def _http_get_json(url: str, token: str | None) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "recommend-instance/1.0"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode())


def fetch_model(model_id: str, token: str | None) -> ModelSpec:
    """Resolve model architecture + parameter count from HuggingFace."""
    warnings: list[str] = []

    # 1. config.json — canonical source of architecture.
    try:
        cfg = _http_get_json(HF_RAW_CONFIG.format(model_id), token)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            sys.exit(f"error: {model_id} is gated. Set HF_TOKEN=hf_... and retry.")
        if e.code == 404:
            sys.exit(f"error: model {model_id!r} not found on HuggingFace.")
        sys.exit(f"error: failed to fetch config.json for {model_id}: {e}")
    except urllib.error.URLError as e:
        sys.exit(f"error: network failure fetching {model_id}: {e.reason}")

    # Some multi-modal models nest the text config.
    if "text_config" in cfg and isinstance(cfg["text_config"], dict):
        warnings.append("multi-modal model; using text_config for sizing")
        cfg = {**cfg, **cfg["text_config"]}

    try:
        num_layers   = int(cfg.get("num_hidden_layers") or cfg["n_layer"])
        hidden_size  = int(cfg.get("hidden_size")       or cfg["n_embd"])
        num_heads    = int(cfg.get("num_attention_heads") or cfg["n_head"])
        vocab_size   = int(cfg.get("vocab_size", 32000))
    except (KeyError, TypeError, ValueError):
        sys.exit(f"error: {model_id} config.json missing required transformer fields.")

    num_kv_heads = int(cfg.get("num_key_value_heads", num_heads))
    head_dim     = int(cfg.get("head_dim", hidden_size // num_heads))
    max_position = int(cfg.get("max_position_embeddings", 4096))
    intermediate = int(cfg.get("intermediate_size", 4 * hidden_size))
    architecture = (cfg.get("architectures") or [""])[0]

    # MoE detection.
    num_experts    = int(cfg.get("num_local_experts") or cfg.get("num_experts") or 0)
    active_experts = int(cfg.get("num_experts_per_tok") or cfg.get("moe_k") or 0)
    is_moe         = num_experts > 1

    # 2. Parameter count — try HF's reported safetensors total first.
    params: int | None = None
    try:
        info = _http_get_json(HF_API.format(model_id), token)
        st = info.get("safetensors") or {}
        if "total" in st and isinstance(st["total"], int):
            params = st["total"]
    except Exception:
        pass

    # 3. Fall back to architectural estimate.
    if not params:
        warnings.append("safetensors param count unavailable; using architectural estimate")
        embed = vocab_size * hidden_size
        # Attention: Q (hidden * hidden) + K,V (hidden * num_kv_heads*head_dim) + O (hidden * hidden)
        attn = (2 * hidden_size * hidden_size) + (2 * hidden_size * num_kv_heads * head_dim)
        if is_moe and num_experts > 0:
            # All experts stored in VRAM, all counted in total params.
            ffn = num_experts * (3 * hidden_size * intermediate)
        else:
            ffn = 3 * hidden_size * intermediate  # gated FFN (gate + up + down)
        per_layer = attn + ffn + (2 * hidden_size)  # + layernorms (tiny)
        params = embed + num_layers * per_layer + hidden_size  # final norm

    return ModelSpec(
        model_id=model_id,
        params=params,
        num_layers=num_layers,
        hidden_size=hidden_size,
        num_heads=num_heads,
        num_kv_heads=num_kv_heads,
        head_dim=head_dim,
        vocab_size=vocab_size,
        max_position=max_position,
        architecture=architecture,
        is_moe=is_moe,
        num_experts=num_experts,
        active_experts=active_experts,
        warnings=warnings,
    )


# --------------------------------------------------------------------------- #
# VRAM estimation                                                             #
# --------------------------------------------------------------------------- #

@dataclass
class VramEstimate:
    weights_gb:     float
    kv_cache_gb:    float
    activations_gb: float
    overhead_gb:    float
    # Per-sequence KV at the model's MAX seq_len — used for the headline
    # "does it fit?" VRAM check. Sized for the worst case (every user fills the
    # context cap to the brim) so we don't OOM under peak.
    kv_per_seq_gb:  float = 0.0
    # Per-sequence KV at the EXPECTED working seq_len (avg input+output tokens).
    # Used for concurrency budget calculations. With vLLM's PagedAttention, KV
    # is allocated per-token-in-flight rather than reserved for the full
    # max_model_len — so realistic concurrency is much higher than worst-case
    # math suggests.
    kv_per_active_seq_gb: float = 0.0

    @property
    def total_gb(self) -> float:
        return self.weights_gb + self.kv_cache_gb + self.activations_gb + self.overhead_gb


def estimate_vram(
    m:                 ModelSpec,
    weight_q:          str,
    kv_q:              str,
    seq_len:           int,
    batch_size:        int,
    users:             int,
    avg_context_len:   int | None = None,
) -> VramEstimate:
    """
    Per-GPU VRAM required to serve `m`, ignoring tensor parallelism (TP=1).
    Dividing this total by the TP degree gives a first-order per-GPU estimate.

    `seq_len` is the model's max context window (max_model_len). It's used for
    the worst-case "does it fit?" VRAM check.

    `avg_context_len` is the typical input+output tokens per active sequence
    under real workload (PagedAttention only allocates KV for tokens actually
    in flight, not for the full max_model_len). It's used for concurrency
    budget calculations. Defaults to seq_len // 4 — a reasonable mid-range for
    chat workloads. Set equal to seq_len for worst-case sizing.
    """
    w_bytes = WEIGHT_BYTES[weight_q]
    k_bytes = KV_BYTES[kv_q]

    weights = m.params * w_bytes

    # KV cache per token = 2 (K+V) × layers × num_kv_heads × head_dim × bytes.
    # Effective concurrency in vLLM ≈ max(batch_size, users).
    concurrency = max(batch_size, users)
    kv_per_tok  = 2 * m.num_layers * m.num_kv_heads * m.head_dim * k_bytes

    # Worst-case KV cache (every active seq fills max_model_len) — used for
    # the headline "does it fit?" VRAM check.
    kv_cache    = kv_per_tok * seq_len * concurrency

    # Activations: rough upper bound for prefill — O(batch * seq * hidden).
    # vLLM / Flash-Attention reduces this materially; factor 4 is a safe pad.
    activations = 4 * batch_size * seq_len * m.hidden_size * 2  # fp16 activations

    # KV cache for a single sequence at MAX seq_len (used by the worst-case
    # headline VRAM check via kv_cache above).
    kv_per_seq = kv_per_tok * seq_len

    # Per-active-sequence KV at the EXPECTED working context length. With
    # PagedAttention, this is what concurrency calculations should divide into
    # the available KV budget. Default to seq_len // 4 if not specified.
    if avg_context_len is None:
        avg_context_len = max(1, seq_len // 4)
    avg_context_len = max(1, min(avg_context_len, seq_len))
    kv_per_active_seq = kv_per_tok * avg_context_len

    # 15% pad for framework, CUDA graphs, allocator fragmentation.
    subtotal = weights + kv_cache + activations
    overhead = 0.15 * subtotal

    gb = 1024 ** 3
    return VramEstimate(
        weights_gb=weights / gb,
        kv_cache_gb=kv_cache / gb,
        activations_gb=activations / gb,
        overhead_gb=overhead / gb,
        kv_per_seq_gb=kv_per_seq / gb,
        kv_per_active_seq_gb=kv_per_active_seq / gb,
    )


# --------------------------------------------------------------------------- #
# Throughput model (PR 2)                                                     #
#                                                                             #
# Decode is HBM-bandwidth-bound: each generated token requires streaming the  #
# active model parameters from HBM through compute. Single-stream tok/s ≈     #
# kernel_efficiency × HBM_bandwidth / active_param_bytes. With concurrency,   #
# vLLM batches reads via continuous batching, so per-user tok/s decays as     #
# roughly 1/sqrt(N) until the saturation batch size, then ~1/N. Estimates are #
# first-order (±25%); validate with `vllm bench serve`.                       #
# --------------------------------------------------------------------------- #

# Empirical kernel efficiency factor — accounts for non-ideal HBM utilization,
# attention compute on KV cache (not just weight read), kernel launch overhead,
# and small allocator/fragmentation losses.
#
# Validated against vllm bench on AWQ INT4 gemma-3-27b on L40S (May 2026):
# measured ~0.56 effective bandwidth utilization, matching the int4 entry below.
#
# Lower for quantized formats (dequant overhead on the kernel path); higher for
# native dtypes (FP8 on Hopper hits very high MBU per NVIDIA spec).
DECODE_KERNEL_EFFICIENCY_BY_DTYPE = {
    "fp32":  0.70,
    "fp16":  0.65,
    "bf16":  0.65,   # Calibrated from real bench (A: gemma-3-1b on L4 = 0.65, B: SmolLM3-3B on L40S = 0.67)
    "fp8":   0.80,   # Hopper/Ada native fp8 — high MBU (not yet measured)
    "int8":  0.65,
    "int4":  0.55,   # Validated: AWQ INT4 gemma-3-27b on L40S measured 0.56
    "gptq4": 0.55,
    "awq4":  0.55,
    "nf4":   0.50,   # bitsandbytes 4-bit, slightly slower kernel
}

# Backwards-compat default (used if dtype not in table).
DECODE_KERNEL_EFFICIENCY = 0.70

# Multi-GPU parallelism overhead.
#
# Tensor parallelism does an all-reduce after every layer. With N layers and
# TP=k, that's 2(k-1)/k * N synchronization points per fwd pass — significant
# cost even on fast interconnects. Validated against scenario C:
#   gemma-3-12b (48 layers) on TP=4 over A100 NVLink → measured 0.45x of
#   per-GPU bandwidth roofline. v2 was previously treating TP=4 as 1.0x,
#   over-predicting throughput by 2-3x for multi-GPU dense models.
#
# Pipeline parallelism communicates less per step (only the activations
# crossing a stage boundary, not all-reduces). The cost is the pipeline
# "bubble" at start/end of each engine step. Much smaller penalty.
#
# These are first-order estimates calibrated to one measurement (C) plus
# vLLM blog guidance. Refine with more benchmarks across TP/PP/interconnect
# combinations.
TP_OVERHEAD_NVLINK = {1: 1.00, 2: 0.85, 4: 0.50, 8: 0.35}
TP_OVERHEAD_PCIE   = {1: 1.00, 2: 0.50, 4: 0.25, 8: 0.15}
# PP_OVERHEAD calibrated against PP=4 on A100 NVLink (gemma-3-12b, May 2026):
#   single-stream measured 35 tok/s vs raw bandwidth-roofline of 47 tok/s
#   → real PP=4 effective penalty 0.75 (was 0.90 too optimistic).
# At high concurrency (N=32), PP also under-performs the simple sqrt-decay
# model — pipeline bubbles + cross-stage activation transfers add real cost.
# Numbers below are first-order; refine with more PP test points across
# different stage counts and interconnects.
PP_OVERHEAD        = {1: 1.00, 2: 0.85, 4: 0.75, 8: 0.65}


def _parallelism_efficiency(tp: int, pp: int, has_nvlink: bool) -> float:
    """Combined TP + PP overhead multiplier vs the raw bandwidth roofline."""
    tp_table = TP_OVERHEAD_NVLINK if has_nvlink else TP_OVERHEAD_PCIE
    tp_eff = tp_table.get(tp, 0.20)
    pp_eff = PP_OVERHEAD.get(pp, 0.80)
    return tp_eff * pp_eff

# Concurrency curve: per-user tok/s = single_stream × decay(N).
#
# Below the saturation batch B_sat, throughput-per-user is roughly FLAT — vLLM's
# continuous batching amortizes weight reads across the entire batch "for free"
# in the bandwidth-bound regime. Empirically (gemma-3-27b INT4 on L40S, seq=4K)
# per-user tok/s drops only ~5% from N=1 to N=16, then ~10% by N=32, ~35% by N=64.
#
# Above B_sat, decay is sub-linear at 1/sqrt(N/B_sat) — continuous batching
# still provides amortization even at high concurrency, so degradation is
# gentler than the naive 1/N model. The formula is continuous at the crossover.
#
# B_sat=24 calibrated against scenarios A/B/D — measured per-user tok/s holds
# essentially flat through N=16, very gentle drop through N=32, then degrades
# above. Was 16 (predicted-was-too-pessimistic at N=32 across all scenarios).
SATURATION_BATCH = 24


def _active_param_bytes(model: ModelSpec, weight_q: str) -> float:
    """Bytes of model parameters streamed per decoded token.

    For dense models, this is the full parameter set in the chosen weight dtype.
    For MoE models, only the active experts are streamed per token, so this is
    correspondingly smaller (e.g. Mixtral 8x7B: 2/8 of FFN params per token).
    """
    bytes_per = WEIGHT_BYTES[weight_q]
    if model.is_moe and model.num_experts > 0 and model.active_experts > 0:
        # Conservative approximation: ~85% of params live in FFN/expert layers
        # for typical MoE models; the 15% non-expert (attention, embed, norms)
        # is always streamed. This avoids the trap of assuming the full active
        # ratio applies to every byte of the model.
        active_ratio = model.active_experts / model.num_experts
        non_expert_share = 0.15
        effective_ratio = non_expert_share + (1.0 - non_expert_share) * active_ratio
        return model.params * bytes_per * effective_ratio
    return model.params * bytes_per


def single_stream_decode_tok_s(
    inst:        Instance,
    model:       ModelSpec,
    weight_q:    str,
    total_gpus:  int,
    shared_mode: bool = False,
    kv_q:        str = "fp16",
    tp_degree:   int = 1,
    pp_degree:   int = 1,
) -> float:
    """Estimated single-user decode tok/s (no concurrency contention).

    With multi-GPU sharding (TP or PP), aggregate HBM bandwidth scales linearly
    with GPU count — TP shards each layer across GPUs (parallel reads), PP
    pipelines layer groups (sequential reads, but each stage reads less). For
    first-order purposes both produce roughly the same speedup vs. single-GPU.
    With time-slicing (shared_mode), 4 tenants share the same physical HBM, so
    bandwidth per tenant is divided by TIME_SLICE_REPLICAS.

    When the requested dtype lacks native tensor-core support on this GPU
    (e.g. fp8 on Ampere, int4 on Turing), vLLM falls back to slow software
    kernels — empirically ~50% throughput of the bandwidth-roofline estimate.
    Apply that penalty so SLO checks reflect real-world performance, not the
    paper-spec ceiling.

    Multi-GPU parallelism adds all-reduce or pipeline-bubble overhead that
    reduces effective throughput vs. raw bandwidth. The TP_OVERHEAD_*  /
    PP_OVERHEAD tables encode measured penalties (calibrated against TP=4
    NVLink: 0.45x measured for gemma-3-12b on A100).
    """
    if inst.hbm_bandwidth_tb_s <= 0:
        return 0.0
    active_bytes = _active_param_bytes(model, weight_q)
    if active_bytes <= 0:
        return 0.0
    # Effective HBM bandwidth for SINGLE-STREAM decode.
    # - TP aggregates bandwidth: each layer's matmul is split across `tp_degree`
    #   GPUs working in parallel (with all-reduce after).
    # - PP does NOT aggregate bandwidth for single-stream: stages run
    #   sequentially, so total time = sum-of-per-stage-times = roughly the
    #   same as a single GPU traversing all the model's weights. PP only
    #   helps at high concurrency where stages can overlap (pipelining).
    # Validated against gemma-3-12b on A100 NVLink (May 2026):
    #   TP=4: measured 87 tok/s — predicted 94 with TP-only aggregation
    #   PP=4: measured 35 tok/s — would predict 42 with no PP aggregation
    # The pre-fix v2 (which aggregated bandwidth across PP) over-predicted
    # PP throughput by 2.5×.
    effective_hbm_bs = inst.hbm_bandwidth_tb_s * 1e12 * tp_degree
    if shared_mode:
        effective_hbm_bs /= TIME_SLICE_REPLICAS
    # Pick the kernel-efficiency factor that matches the requested weight dtype.
    efficiency = DECODE_KERNEL_EFFICIENCY_BY_DTYPE.get(
        weight_q, DECODE_KERNEL_EFFICIENCY,
    )
    base = effective_hbm_bs / active_bytes * efficiency
    # Software-emulation penalty for unsupported dtypes.
    if _quant_compat_warning(inst, weight_q, kv_q) is not None:
        base *= 0.5
    # Multi-GPU parallelism overhead (all-reduce for TP, pipeline bubble for PP).
    if tp_degree > 1 or pp_degree > 1:
        base *= _parallelism_efficiency(tp_degree, pp_degree, inst.has_nvlink)
    return base


def per_user_tok_s(single_stream: float, concurrency: int) -> float:
    """Decay per-user decode tok/s with concurrency.

    Below SATURATION_BATCH, per-user throughput is **flat** — vLLM's continuous
    batching amortizes weight reads across the entire batch in the
    bandwidth-bound regime, so adding more sequences is essentially free for
    per-user throughput up to the saturation point. Verified empirically with
    AWQ INT4 gemma-3-27b on L40S: per-user tok/s held within ±3% from N=1 to
    N=24, then began to degrade.

    Above SATURATION_BATCH, decay is sub-linear at 1/sqrt(N/B_sat) — continuous
    batching still provides partial amortization even when compute-bound, so
    degradation is gentler than the naive 1/N model. The formula is continuous
    at the crossover (returns single_stream at N=B_sat from either side).
    """
    if concurrency <= 1 or single_stream <= 0:
        return single_stream
    if concurrency <= SATURATION_BATCH:
        return single_stream                           # truly flat below sat
    return single_stream / math.sqrt(concurrency / SATURATION_BATCH)


def aggregate_decode_tok_s(single_stream: float, concurrency: int) -> float:
    """Total cluster tok/s across all concurrent users."""
    return per_user_tok_s(single_stream, concurrency) * max(1, concurrency)


# --------------------------------------------------------------------------- #
# Recommendation                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class Option:
    instance:           Instance
    tp_degree:          int
    pp_degree:          int      # pipeline-parallel stages (1 = no PP)
    per_gpu_need_gb:    float
    headroom_gb:        float
    price_usd_h:        float    # effective price (from Pricing API or fallback)
    nodepool:           str      # "gpu-inference" | "gpu-shared" | "out-of-cluster"
    shared_eligible:    bool     # can legitimately use gpu-shared time-slicing
    over_price_ceiling: bool
    # Throughput estimates (PR 2): single-stream is ceiling, per-user reflects
    # the user's --users concurrency setting.
    single_stream_tok_s: float = 0.0
    per_user_tok_s_at_users: float = 0.0
    # Quant compatibility (PR 4): None when fully supported, str warning otherwise.
    quant_warning:      str | None = None
    notes:              list[str] = field(default_factory=list)

    @property
    def total_gpus(self) -> int:
        return self.tp_degree * self.pp_degree

    @property
    def parallelism_label(self) -> str:
        if self.tp_degree > 1 and self.pp_degree > 1:
            return f"TP={self.tp_degree} × PP={self.pp_degree}"
        if self.pp_degree > 1:
            return f"PP={self.pp_degree}"
        if self.tp_degree > 1:
            return f"TP={self.tp_degree}"
        return ""

    @property
    def in_cluster(self) -> bool:
        return self.nodepool != "out-of-cluster"

    @property
    def effective_price_usd_h(self) -> float:
        return self.price_usd_h

    @property
    def amortised_price_usd_h(self) -> float | None:
        if self.shared_eligible and self.total_gpus == 1:
            return self.price_usd_h / TIME_SLICE_REPLICAS
        return None

    @property
    def sort_key(self) -> tuple:
        # Prefer: quant-compatible, below ceiling, in-cluster, lowest price, fewest GPUs.
        return (
            self.quant_warning is not None,
            self.over_price_ceiling,
            not self.in_cluster,
            self.effective_price_usd_h,
            self.total_gpus,
        )


def _tp_overhead(tp: int) -> float:
    """Multiplicative pad for tensor-parallel activation duplication and comm buffers."""
    return 1.0 if tp == 1 else 1.0 + 0.05 * math.log2(tp)


def _classify_nodepool(
    inst:              Instance,
    tp:                int,
    per_gpu_need_gb:   float,
    safety_margin:     float,
) -> tuple[str, bool]:
    """Which NodePool can schedule this instance, and is it shared-eligible?

    Returns (nodepool, shared_eligible). Mirrors the requirements in
    terraform/30.eks/30.cluster/karpenter/gpu-{inference,shared}.yaml.

    Shared eligibility requires that TIME_SLICE_REPLICAS copies of the model
    fit within the GPU's memory budget, because NVIDIA time-slicing shares
    compute but NOT memory — all co-tenants compete for the same VRAM.
    """
    if inst.gpu_manufacturer != CLUSTER_GPU_MANUFACTURER:
        return "out-of-cluster", False

    # gpu-shared: single-GPU G-family NVIDIA where TIME_SLICE_REPLICAS copies fit.
    available_per_slice = inst.vram_gb * (1.0 - safety_margin) / TIME_SLICE_REPLICAS
    shared_eligible = (
        inst.category in CLUSTER_SHARED_CATEGORIES
        and inst.num_gpus == 1
        and per_gpu_need_gb <= available_per_slice
    )

    # gpu-inference: any NVIDIA G or P instance.
    if inst.category in CLUSTER_INFERENCE_CATEGORIES:
        # When TP == num_gpus and > 1, we need the whole dedicated node;
        # gpu-inference serves both single- and multi-GPU cases.
        return "gpu-inference", shared_eligible

    return "out-of-cluster", False


def _valid_parallelism_configs(
    inst: Instance,
    num_heads: int,
    tp_pin: int | None,
) -> list[tuple[int, int]]:
    """Generate valid (tp, pp) pairs for an instance, respecting vLLM constraints.

    Rules from https://docs.vllm.ai/en/latest/serving/parallelism_scaling/:
    - TP: num_attention_heads must be evenly divisible by tp_degree
    - PP: splits model along layer boundaries (no head constraint)
    - TP × PP must equal the instance's GPU count

    Returns ALL valid configurations unsorted. Selection between configs is
    done by `_best_config_for_instance` based on predicted throughput at the
    user's target concurrency — not by a hardcoded interconnect heuristic.
    Earlier versions of this function preferred TP on NVLink and PP on PCIe;
    that was a rule of thumb. With the calibrated throughput model we can
    just compute and pick.
    """
    n = inst.num_gpus
    if n == 1:
        if tp_pin is not None and tp_pin != 1:
            return []
        return [(1, 1)]

    configs: list[tuple[int, int]] = []

    # Enumerate all factorizations of num_gpus into tp × pp
    for tp in TP_DEGREES:
        if tp > n:
            break
        if n % tp != 0:
            continue
        pp = n // tp
        # TP constraint: attention heads must be divisible by tp
        if tp > 1 and num_heads % tp != 0:
            continue
        # If user pinned TP, only allow that exact value
        if tp_pin is not None and tp != tp_pin:
            continue
        configs.append((tp, pp))

    return configs


def _best_config_for_instance(
    inst:       Instance,
    model:      ModelSpec,
    weight_q:   str,
    kv_q:       str,
    configs:    list[tuple[int, int]],
    users:      int,
) -> tuple[int, int] | None:
    """Pick the (tp, pp) config that maximizes per-user throughput at the
    target concurrency.

    This makes v2 a true throughput optimizer — it now decides between TP,
    PP, and TP×PP based on the calibrated throughput model rather than a
    hardcoded interconnect rule. For NVLink instances this typically picks
    max-TP (the old heuristic was right). For PCIe instances the choice
    depends on the model size and concurrency target — sometimes TP=4 PCIe
    with its 0.25× efficiency factor still beats PP=4 because PP doesn't
    aggregate bandwidth at all for single-stream.

    Tiebreak: prefer the lower TP × PP product (less coordination overhead),
    then prefer max TP (NVLink-style) when ties remain. None ⇒ no valid
    configs.
    """
    if not configs:
        return None
    if len(configs) == 1:
        return configs[0]

    scored: list[tuple[float, int, int]] = []
    for tp, pp in configs:
        ss = single_stream_decode_tok_s(
            inst, model, weight_q, tp * pp,
            shared_mode=False, kv_q=kv_q,
            tp_degree=tp, pp_degree=pp,
        )
        score = per_user_tok_s(ss, max(1, users))
        # Tiebreaker: smaller (tp+pp) wins (less coordination), then larger TP wins.
        scored.append((score, -(tp + pp), tp))
    scored.sort(reverse=True)
    best_idx = scored[0]
    # Find back the actual (tp, pp) tuple corresponding to the winner.
    best_tp = best_idx[2]
    for tp, pp in configs:
        if tp == best_tp and per_user_tok_s(
            single_stream_decode_tok_s(
                inst, model, weight_q, tp * pp,
                shared_mode=False, kv_q=kv_q,
                tp_degree=tp, pp_degree=pp,
            ),
            max(1, users),
        ) == best_idx[0]:
            return (tp, pp)
    return configs[0]


def find_options(
    total_need_gb:      float,
    num_heads:          int,
    tp_pin:             int | None,
    require_in_cluster: bool,
    safety_margin:      float,
    prices:             dict[str, float],
    max_price:          float,
    # Extra context needed for throughput + quant-compat (added in PR 2 / PR 4):
    model:              ModelSpec | None = None,
    weight_q:           str = "bf16",
    kv_q:               str = "fp16",
    users:              int = 1,
) -> list[Option]:
    options: list[Option] = []

    for inst in INSTANCES:
        configs = _valid_parallelism_configs(inst, num_heads, tp_pin)
        if not configs:
            continue

        # Pick the (tp, pp) config that maximizes per-user throughput at the
        # target concurrency. This is what makes v2 a true optimizer rather
        # than a heuristic-driven recommender. When `model` is None (no
        # context), fall back to the natural TP=gpuCount choice.
        if model is not None:
            selected = _best_config_for_instance(
                inst, model, weight_q, kv_q, configs, users,
            )
            if selected is None:
                continue
            tp, pp = selected
        else:
            tp, pp = configs[0]
        total_gpus = tp * pp

        # Per-GPU VRAM: weights sharded across all GPUs (TP shards layers,
        # PP shards layer groups). KV cache and activations are per-stage.
        per_gpu = (total_need_gb / total_gpus) * _tp_overhead(tp)
        available = inst.vram_gb * (1.0 - safety_margin)
        if per_gpu > available:
            continue

        nodepool, shared_eligible = _classify_nodepool(
            inst, tp, per_gpu, safety_margin,
        )
        if require_in_cluster and nodepool == "out-of-cluster":
            continue

        price = prices.get(inst.name, inst.price_usd_h)
        over_ceiling = price > max_price

        notes: list[str] = []
        if tp > 1 and pp > 1:
            notes.append(f"TP={tp} × PP={pp} across {total_gpus} GPUs")
        elif pp > 1:
            notes.append(f"pipeline-parallel across {pp} GPUs (no NVLink → PP preferred)")
        elif tp > 1:
            notes.append(f"tensor-parallel across {tp} GPUs"
                         + (" (NVLink)" if inst.has_nvlink else ""))
        if nodepool == "out-of-cluster":
            notes.append("not covered by Karpenter NodePools")
        if over_ceiling:
            notes.append(f"${price:.2f}/hr exceeds --max-price ${max_price:.2f}")

        # PR 2: throughput estimate. shared_mode_active reflects whether a
        # shared deployment would actually be picked (only true for single-GPU,
        # shared-eligible instances).
        single_stream = 0.0
        per_user_at_users = 0.0
        if model is not None:
            shared_mode_active = shared_eligible and total_gpus == 1
            single_stream = single_stream_decode_tok_s(
                inst, model, weight_q, total_gpus,
                shared_mode=shared_mode_active, kv_q=kv_q,
                tp_degree=tp, pp_degree=pp,
            )
            per_user_at_users = per_user_tok_s(single_stream, max(1, users))

        # PR 4: quant ↔ hardware compatibility check.
        quant_warn = _quant_compat_warning(inst, weight_q, kv_q)
        if quant_warn:
            notes.append(f"⚠ {quant_warn}")

        options.append(Option(
            instance=inst,
            tp_degree=tp,
            pp_degree=pp,
            per_gpu_need_gb=per_gpu,
            headroom_gb=inst.vram_gb - per_gpu,
            price_usd_h=price,
            nodepool=nodepool,
            shared_eligible=shared_eligible,
            over_price_ceiling=over_ceiling,
            single_stream_tok_s=single_stream,
            per_user_tok_s_at_users=per_user_at_users,
            quant_warning=quant_warn,
            notes=notes,
        ))

    options.sort(key=lambda o: o.sort_key)
    return options


# --------------------------------------------------------------------------- #
# Fleet scaling                                                               #
# --------------------------------------------------------------------------- #

DEFAULT_UTILIZATION_FACTOR = 0.70  # leave 30% headroom for burst traffic
DEFAULT_TARGET_TOK_S       = 0.0   # disabled by default — only enforce when user explicitly sets SLO


# Workload presets — fill in sensible defaults for --avg-context, --target-tok-s,
# and (in fleet mode) --target-users when the user asks for one of these labels
# instead of having to tune individual flags. Users can still override any
# individual value; the preset only fills fields the user didn't explicitly set.
#
# These are calibrated to typical production loads, not paper-spec scenarios.
# Fields:
#   avg_context  : input + output tokens per active sequence at steady state
#   target_tok_s : per-user decode rate that "feels right" for that workload
#   users        : default per-instance concurrency target (when --users not set)
#                  (only applied if --target-users is also unset; i.e., single-instance mode)
WORKLOAD_PRESETS = {
    "chat": {
        "avg_context":  1024,   # short turns, ~500 input + ~500 output
        "target_tok_s": 25,     # interactive feel — 20-30 tok/s reads as snappy
        "users":         4,
        "description": "Interactive chat: short turns, snappy decode required",
    },
    "rag": {
        "avg_context":  4096,   # retrieved context blob + modest answer
        "target_tok_s": 15,     # users tolerate slower output for grounded responses
        "users":         8,
        "description": "Retrieval-augmented generation: long context, modest output",
    },
    "code": {
        "avg_context":  2048,   # surrounding file context + completion
        "target_tok_s": 50,     # IDE users won't tolerate slow completions
        "users":         4,
        "description": "Code completion / IDE assistant: fast decode is critical",
    },
    "summarization": {
        "avg_context":  8192,   # long input documents, short summaries
        "target_tok_s": 10,     # latency tolerance is high for batch-y work
        "users":         8,
        "description": "Document summarization: long context, short output, modest latency",
    },
    "batch": {
        "avg_context":  1024,   # mixed prompts/outputs in offline pipelines
        "target_tok_s":  5,     # throughput dominates; latency doesn't matter much
        "users":        64,
        "description": "Offline batch inference: maximize throughput, latency irrelevant",
    },
}

@dataclass(frozen=True)
class ScalingRecommendation:
    option:                    Option
    max_concurrency_per_inst:  int       # VRAM-limited max concurrent sequences
    effective_capacity:        int       # after utilization factor (and SLO if applicable)
    replicas:                  int
    fleet_cost_usd_h:          float
    utilization_factor:        float
    target_users:              int
    target_tok_s:              float = 0.0
    # PR 3: estimated per-user tok/s at the effective capacity. If this is
    # below target_tok_s, we capped effective_capacity downward so the per-
    # instance load actually meets the SLO.
    estimated_tok_s_per_user:  float = 0.0
    slo_capped:                bool  = False
    slo_unmet:                 bool  = False  # True if even concurrency=1 misses SLO

    @property
    def fleet_monthly_usd(self) -> float:
        return self.fleet_cost_usd_h * 730

    @property
    def sort_key(self) -> tuple:
        # Sort by total fleet cost — what the user actually pays. A single
        # expensive instance (over per-instance ceiling) may still be the
        # cheapest fleet option because it serves more concurrent users.
        # SLO-unmet options sink to the bottom — they can't actually meet
        # the throughput requirement at any concurrency.
        return (
            self.slo_unmet,
            not self.option.in_cluster,
            self.fleet_cost_usd_h,
            self.replicas,
        )


def _max_concurrency_for_option(
    opt: Option,
    vram: VramEstimate,
    safety_margin: float,
) -> int:
    """Max concurrent sequences an instance can serve (VRAM-limited).

    Subtracts weights + activations + overhead from available VRAM, then divides
    by KV cache per sequence. This is the hard ceiling before vLLM starts queueing.

    With PP, each GPU holds 1/pp of the layers (weights AND KV cache); with TP,
    each GPU holds 1/tp of each layer's KV heads. The total KV cache per
    sequence is therefore divided by `total_sharding = tp × pp` when
    distributed across GPUs — which means a multi-GPU instance can hold
    `total_sharding ×` more concurrent sequences than a single GPU of the
    same model, beyond the obvious VRAM-budget gain.

    PR 5 — overhead consistency fix. Previously this function applied the 15%
    framework pad to (weights + activations) only, leaving KV cache un-padded.
    The fix applies the pad consistently across all three components, mirroring
    the `estimate_vram()` calculation. Algebra:
        avail = (1+pad) × (weights + activations + kv_budget)
        ⇒ kv_budget = avail/(1+pad) − weights − activations

    PR 7 (additional fix discovered during gemma-3-27b testing) — the per-seq
    KV cost was previously the full unsharded value, which heavily over-counted
    KV memory on PP/TP-sharded instances and made fleet recommendations
    massively over-provisioned (e.g. 13 replicas where 2 sufficed). KV cache
    in vLLM with vanilla TP/PP is sharded across the parallelism group, just
    like weights — so per-GPU per-seq KV = full_kv_per_seq / total_sharding.

    PR 8 (PagedAttention realism fix) — previously divided by `kv_per_seq_gb`
    (KV at FULL max_model_len), assuming every concurrent user fills the
    context cap. With vLLM's PagedAttention, KV is allocated per-token-in-flight
    rather than reserved up to max_model_len. Switching to `kv_per_active_seq_gb`
    (KV at the typical working context, default seq//4) brings concurrency math
    in line with measured behavior: in benchmarks, gemma-3-27b INT4 on a single
    L40S held 64 concurrent users at maxModelLen=4096 even though pre-fix v2
    would have predicted a hard cap of 16.
    """
    OVERHEAD_PAD = 0.15
    total_sharding = opt.total_gpus
    available_per_gpu = opt.instance.vram_gb * (1.0 - safety_margin)
    weights_per_gpu = vram.weights_gb / total_sharding
    activations_per_gpu = vram.activations_gb / total_sharding
    # Use the working-context KV (PagedAttention-realistic), not the
    # max-model-len worst case. Sharded across the TP/PP group.
    per_seq_kv = vram.kv_per_active_seq_gb if vram.kv_per_active_seq_gb > 0 else vram.kv_per_seq_gb
    kv_per_seq_per_gpu = per_seq_kv / total_sharding

    kv_budget = (
        available_per_gpu / (1.0 + OVERHEAD_PAD)
        - weights_per_gpu
        - activations_per_gpu
    )
    if kv_budget <= 0 or kv_per_seq_per_gpu <= 0:
        return 0
    return max(1, int(kv_budget / kv_per_seq_per_gpu))


def _slo_capacity(single_stream_tok_s: float, target_tok_s: float,
                  vram_capacity: int) -> tuple[int, bool, bool]:
    """Return (slo_capacity, was_capped, was_unmet) for a per-instance SLO.

    Finds the largest concurrency at which per_user_tok_s(N) >= target_tok_s.
    If even N=1 misses the SLO, returns (1, False, True) so callers know this
    instance type cannot meet the SLO at any load.
    """
    if target_tok_s <= 0 or single_stream_tok_s <= 0:
        return vram_capacity, False, False

    if single_stream_tok_s < target_tok_s:
        # Even single-stream is below SLO: this GPU is too slow. Caller
        # should escalate to a faster GPU.
        return 1, False, True

    # Walk down from vram_capacity until SLO is met. Cheaper than solving the
    # piecewise function analytically, and concurrency rarely exceeds 100.
    n = vram_capacity
    while n > 1 and per_user_tok_s(single_stream_tok_s, n) < target_tok_s:
        n -= 1
    return n, n < vram_capacity, False


def compute_scaling(
    opts:               list[Option],
    vram:               VramEstimate,
    target_users:       int,
    utilization_factor: float,
    safety_margin:      float,
    target_tok_s:       float = 0.0,
) -> list[ScalingRecommendation]:
    """For each viable option, compute how many replicas are needed to serve target_users.

    When target_tok_s > 0, the per-instance effective capacity is capped to
    the concurrency at which the per-user decode throughput meets the SLO.
    This means cheaper-but-slow GPUs require more replicas to hit the same SLO
    that an expensive-but-fast GPU clears on a single replica.
    """
    results: list[ScalingRecommendation] = []

    for opt in opts:
        max_conc = _max_concurrency_for_option(opt, vram, safety_margin)
        if max_conc < 1:
            continue

        # Step 1: VRAM-limited capacity, with utilization burst headroom.
        vram_capacity = max(1, int(max_conc * utilization_factor))

        # Step 2: SLO-limited capacity. Take the more restrictive of the two.
        slo_cap, slo_capped, slo_unmet = _slo_capacity(
            opt.single_stream_tok_s, target_tok_s, vram_capacity,
        )
        effective = min(vram_capacity, slo_cap) if target_tok_s > 0 else vram_capacity

        replicas = math.ceil(target_users / max(1, effective))
        fleet_cost = replicas * opt.price_usd_h

        # Compute the actual per-user tok/s the fleet would deliver at the
        # planned concurrency (effective capacity is the per-instance load).
        est_tok_s = (
            per_user_tok_s(opt.single_stream_tok_s, effective)
            if opt.single_stream_tok_s > 0 else 0.0
        )

        results.append(ScalingRecommendation(
            option=opt,
            max_concurrency_per_inst=max_conc,
            effective_capacity=effective,
            replicas=replicas,
            fleet_cost_usd_h=fleet_cost,
            utilization_factor=utilization_factor,
            target_users=target_users,
            target_tok_s=target_tok_s,
            estimated_tok_s_per_user=est_tok_s,
            slo_capped=slo_capped,
            slo_unmet=slo_unmet,
        ))

    results.sort(key=lambda r: r.sort_key)
    return results


# --------------------------------------------------------------------------- #
# Presentation                                                                #
# --------------------------------------------------------------------------- #

def _fmt_params(p: int) -> str:
    if p >= 1e12: return f"{p/1e12:.2f}T"
    if p >= 1e9:  return f"{p/1e9:.2f}B"
    if p >= 1e6:  return f"{p/1e6:.1f}M"
    return str(p)


def _explain_recommendation(
    best: "Option",
    all_opts: list["Option"],
    args: argparse.Namespace,
    model: ModelSpec,
) -> str:
    """Generate a single-sentence rationale for why `best` was recommended.

    The explanation walks down a hierarchy of "why":
      1. The dominant constraint (cost, throughput, or fit)
      2. Why this strategy (TP/PP/single-GPU)
      3. Any caveats (quant warning, near VRAM limit, shared mode)

    The runner-up — if there is one on a different instance type — is used
    as the comparator: "picked X over Y because ...". This makes the
    rationale concrete rather than abstract.
    """
    if best is None:
        return ""

    # Find a meaningful runner-up: next-cheapest option on a different instance.
    runner_up = None
    for o in all_opts[1:]:
        if o is best:
            continue
        if o.instance.name != best.instance.name:
            runner_up = o
            break

    parts: list[str] = []

    # --- Strategy explanation (TP vs PP vs single-GPU) -------------------- #
    if best.tp_degree > 1 and best.pp_degree > 1:
        strategy_phrase = (
            f"TP={best.tp_degree} × PP={best.pp_degree} on {best.total_gpus} GPUs "
            f"({'NVLink' if best.instance.has_nvlink else 'PCIe'})"
        )
    elif best.tp_degree > 1:
        nvlink = "NVLink" if best.instance.has_nvlink else "PCIe"
        strategy_phrase = (
            f"TP={best.tp_degree} on {nvlink} (aggregate "
            f"{best.instance.hbm_bandwidth_tb_s * best.tp_degree:.2f} TB/s HBM)"
        )
    elif best.pp_degree > 1:
        strategy_phrase = (
            f"PP={best.pp_degree} (sequential stages, no bandwidth aggregation)"
        )
    else:
        strategy_phrase = (
            f"single-GPU on {best.instance.gpu} "
            f"({best.instance.hbm_bandwidth_tb_s:.2f} TB/s HBM)"
        )

    parts.append(f"{best.instance.name} runs the model with {strategy_phrase}.")

    # --- Throughput context (what does the user actually get?) ------------ #
    util_pct = 100.0 * best.per_gpu_need_gb / best.instance.vram_gb
    if best.single_stream_tok_s > 0:
        if args.users > 1:
            parts.append(
                f"At {args.users} concurrent users, each user gets "
                f"~{best.per_user_tok_s_at_users:.0f} tok/s "
                f"(ceiling {best.single_stream_tok_s:.0f} tok/s single-stream)."
            )
        else:
            parts.append(
                f"Single-stream throughput ~{best.single_stream_tok_s:.0f} tok/s, "
                f"{util_pct:.0f}% VRAM utilized."
            )

    # --- Why this instance over the runner-up? ---------------------------- #
    if runner_up is not None and runner_up.single_stream_tok_s > 0:
        price_delta = (runner_up.price_usd_h - best.price_usd_h) / runner_up.price_usd_h
        speed_delta = (best.single_stream_tok_s - runner_up.single_stream_tok_s) / max(
            runner_up.single_stream_tok_s, 1e-6
        )
        if price_delta > 0.10 and speed_delta > -0.20:
            parts.append(
                f"Beats {runner_up.instance.name} by "
                f"{price_delta * 100:.0f}% on price."
            )
        elif speed_delta > 0.20:
            parts.append(
                f"Beats {runner_up.instance.name} by "
                f"{speed_delta * 100:.0f}% on throughput."
            )
        elif speed_delta > 0.05:
            parts.append(
                f"Slightly faster than {runner_up.instance.name} "
                f"({speed_delta * 100:.0f}% more tok/s)."
            )

    # --- Caveats ---------------------------------------------------------- #
    caveats: list[str] = []
    if best.quant_warning:
        caveats.append(f"⚠ {args.quant} dtype emulated on {best.instance.gpu} "
                       f"— real-world throughput will be ~half of estimates")
    if util_pct > 80:
        caveats.append(f"⚠ {util_pct:.0f}% VRAM used — limited headroom for "
                       f"longer contexts or higher concurrency")
    if best.shared_eligible and best.total_gpus == 1:
        caveats.append(
            f"could share GPU with up to {TIME_SLICE_REPLICAS} models "
            f"via time-slicing (set shared: true to amortize cost)"
        )
    if best.over_price_ceiling:
        caveats.append("over your --max-price ceiling — no cheaper option fits")
    if caveats:
        parts.append(" ".join(caveats[:2]))   # cap at 2 caveats

    return " ".join(parts)




def print_human(
    model:       ModelSpec,
    vram:        VramEstimate,
    opts:        list[Option],
    best:        Option | None,
    args:        argparse.Namespace,
    price_src:   str,
    scaling:     list[ScalingRecommendation] | None = None,
) -> None:
    C      = _palette(_should_use_colour(args))
    ruler  = _ruler(71, C)

    # -------- 1. Recommendation banner ----------------------------------- #
    if best is None:
        print(f"\n{C.RED}✗ No catalog instance can host this configuration.{C.RESET}")
        print(f"  Try: {C.CYAN}--quant int4{C.RESET}, a shorter {C.CYAN}--seq{C.RESET}, "
              f"fewer {C.CYAN}--users{C.RESET}, or lower {C.CYAN}--batch{C.RESET}.\n")
        return

    util_pct  = 100.0 * best.per_gpu_need_gb / best.instance.vram_gb
    marker    = f"{C.YELLOW}⚠{C.RESET}" if best.over_price_ceiling else f"{C.GREEN}✓{C.RESET}"
    verdict   = f"{C.YELLOW}FITS, BUT OVER BUDGET{C.RESET}" if best.over_price_ceiling \
                else f"{C.GREEN}RECOMMENDED{C.RESET}"
    shared_on = best.shared_eligible and best.total_gpus == 1
    if shared_on:
        mode_lbl = f"shared mode (up to {TIME_SLICE_REPLICAS} models per GPU)"
    elif best.parallelism_label:
        mode_lbl = best.parallelism_label
    else:
        mode_lbl = "dedicated GPU"

    print()
    print(ruler)
    print(f"  {marker} {C.BOLD}{verdict}: {best.instance.name}{C.RESET} — "
          f"{best.total_gpus}× NVIDIA {best.instance.gpu}, "
          f"{best.instance.vram_gb} GB VRAM per GPU · {mode_lbl}")
    monthly = _fmt_monthly(best.price_usd_h)
    line    = f"    {C.BOLD}${best.price_usd_h:.2f}/hr{C.RESET}  ·  ~{monthly}/month"
    if shared_on:
        eff   = _fmt_monthly(best.price_usd_h / TIME_SLICE_REPLICAS)
        line += (f"  ·  ~{eff}/month per model "
                 f"if {TIME_SLICE_REPLICAS} share the GPU")
    print(line)
    headroom_tone = C.GREEN if util_pct < 60 else (C.YELLOW if util_pct < 85 else C.RED)
    print(f"    Utilisation: {headroom_tone}{best.per_gpu_need_gb:.1f} / "
          f"{best.instance.vram_gb} GB ({util_pct:.0f}%){C.RESET}  "
          f"— {best.headroom_gb:.1f} GB headroom")
    if best.single_stream_tok_s > 0:
        per_user_label = (
            f"~{best.per_user_tok_s_at_users:.0f} tok/s/user @ {args.users} concurrent"
            if args.users > 1 else
            f"~{best.single_stream_tok_s:.0f} tok/s single-stream"
        )
        print(f"    Throughput:  {per_user_label} "
              f"{C.DIM}(ceiling {best.single_stream_tok_s:.0f} tok/s, "
              f"HBM {best.instance.hbm_bandwidth_tb_s:.2f} TB/s × {best.total_gpus}){C.RESET}")
    if best.quant_warning:
        print(f"    {C.YELLOW}⚠ Quant compatibility: {best.quant_warning}{C.RESET}")
    if best.over_price_ceiling:
        print(f"    {C.YELLOW}Note: exceeds --max-price ${args.max_price:.2f}/hr. "
              f"No cheaper option fits.{C.RESET}")
    print(ruler)

    # -------- 1b. Why this recommendation? ------------------------------- #
    rationale = _explain_recommendation(best, opts, args, model)
    if rationale:
        print()
        print(f"  {C.BOLD}Why:{C.RESET} {rationale}")
    if getattr(args, "_preset_applied", None):
        print(f"  {C.DIM}Workload preset '{args.workload}' applied: "
              f"{', '.join(args._preset_applied)}{C.RESET}")

    # -------- 2. MoE callout (if applicable) ----------------------------- #
    if model.is_moe and model.num_experts:
        active = model.active_experts or 0
        act_ratio = active / model.num_experts if model.num_experts else 0
        dense_eq  = int(model.params * act_ratio) if active else model.params
        print()
        print(f"  {C.YELLOW}⚠ MIXTURE-OF-EXPERTS MODEL{C.RESET}")
        print(f"    {model.num_experts} experts total, {active} active per token — "
              f"but ALL experts stay resident in VRAM.")
        print(f"    Memory cost = dense ~{_fmt_params(model.params)} model, NOT the "
              f"~{_fmt_params(dense_eq)} 'active-size' figure sometimes quoted.")
        # PR 6: surface the bandwidth side of MoE — only active experts are
        # streamed per token, so decode tok/s scales with active params, not
        # total. This explains why MoE models often have higher tok/s than
        # their parameter count would suggest.
        if active > 0 and active < model.num_experts:
            print(f"    {C.DIM}Bandwidth cost ≈ active params (~{_fmt_params(dense_eq)}); "
                  f"decode tok/s reflects this, not the {_fmt_params(model.params)} "
                  f"VRAM footprint.{C.RESET}")

    # -------- 3. Model + request summary (compact) ----------------------- #
    gqa = f"GQA {model.num_kv_heads}KV/{model.num_heads}Q" \
          if model.num_kv_heads and model.num_kv_heads != model.num_heads else \
          f"MHA {model.num_heads} heads"
    print(f"\n{C.BOLD}Model:{C.RESET}   {model.model_id}  "
          f"({C.DIM}{_fmt_params(model.params)} params, {model.num_layers} layers, "
          f"{gqa}{C.RESET})")
    print(f"{C.BOLD}Request:{C.RESET} {args.seq:,}-token context · batch {args.batch} · "
          f"{args.users} concurrent · weights {args.quant} · kv {args.kv_quant}")
    print(f"{C.BOLD}Region:{C.RESET}  {args.region}  "
          f"{C.DIM}(prices: {price_src}){C.RESET}")
    if "⚠" in price_src:
        print(f"  {C.YELLOW}⚠ Pricing fallback: catalog prices may not reflect "
              f"{args.region}. Use --refresh-prices with valid AWS credentials "
              f"for accurate pricing.{C.RESET}")
    for w in model.warnings:
        print(f"  {C.YELLOW}⚠{C.RESET} {w}")

    # -------- 4. VRAM breakdown ------------------------------------------ #
    print(f"\n{C.BOLD}VRAM usage{C.RESET} (TP=1, per-GPU before sharding)")
    total = vram.total_gb or 1e-9
    rows  = [
        ("weights",     vram.weights_gb),
        ("kv-cache",    vram.kv_cache_gb),
        ("activations", vram.activations_gb),
        ("overhead",    vram.overhead_gb),
    ]
    for label, gb in rows:
        pct = 100.0 * gb / total
        print(f"  {label:<12}{_bar(gb, total, 24)}  "
              f"{gb:>6.2f} GB  ({pct:>4.1f}%)")
    print(f"  {C.BOLD}{'total':<12}{_bar(total, total, 24)}  "
          f"{total:>6.2f} GB{C.RESET}")

    # -------- 5. Alternatives table -------------------------------------- #
    within_budget = [o for o in opts if not o.over_price_ceiling]
    over_budget   = [o for o in opts if o.over_price_ceiling]

    print(f"\n{C.BOLD}Alternatives{C.RESET} "
          f"(sorted: within budget, in-cluster, lowest $/hr)\n")
    header = (f"  {'INSTANCE':<15} {'GPU':<9} {'STRATEGY':<10} {'USAGE':<12} "
              f"{'FIT':<15}  {'TOK/S':>10}  {'$/HR':>6}  {'MONTHLY':>9}  FLAGS")
    print(f"{C.DIM}{header}{C.RESET}")
    print(f"{C.DIM}  {'-'*15} {'-'*9} {'-'*10} {'-'*12} {'-'*15}  "
          f"{'-'*10}  {'-'*6}  {'-'*9}  {'-'*13}{C.RESET}")

    for o in within_budget[: args.limit]:
        _print_table_row(o, C, over=False)
    if over_budget and len(within_budget) < args.limit:
        remaining = args.limit - len(within_budget)
        print(f"{C.DIM}  {'·'*75} above --max-price ${args.max_price:.2f}/hr{C.RESET}")
        for o in over_budget[:remaining]:
            _print_table_row(o, C, over=True)

    # -------- 6. Fleet scaling (when --target-users is given) ------------ #
    if scaling:
        _print_scaling_section(scaling, args, C)

    # -------- 7. YAML artifact + next steps ------------------------------ #
    _print_yaml_snippet(model, vram, best, args, C, scaling)

    # -------- 8. Footnotes ----------------------------------------------- #
    print(f"\n{C.DIM}Notes:")
    print(f"  • TOK/S column: per-user tok/s @ {args.users} concurrent / single-stream ceiling")
    print("    (decode is HBM-bandwidth bound; estimates ±25%, validate with `vllm bench serve`)")
    print(f"  • FLAGS: ✓ in-cluster · shared = {TIME_SLICE_REPLICAS} copies of this model")
    print("    fit on one GPU — amortised cost shown in parens · ⚠ quant = software fallback")
    print(f"  • Monthly = hourly × 730 h/month. Prices from: {price_src}")
    print("  • Use --refresh-prices to invalidate the local cache")
    if scaling:
        print(f"  • Fleet sizing uses {int(args.utilization * 100)}% utilization factor "
              f"(adjust with --utilization)")
        if args.target_tok_s > 0:
            print(f"  • SLO: ≥{args.target_tok_s:.0f} tok/s/user (set via --target-tok-s)")
    print(f"  • VRAM estimates are first-order (±10%). Real validation: deploy + benchmark.{C.RESET}")


def _print_table_row(o: Option, C: type, over: bool) -> None:
    util_pct = 100.0 * o.per_gpu_need_gb / o.instance.vram_gb
    bar      = _bar(o.per_gpu_need_gb, o.instance.vram_gb, 8)
    tone     = C.YELLOW if over else (
               C.RED if util_pct >= 85 else
               C.YELLOW if util_pct >= 60 else
               C.GREEN)
    flag_ic  = f"{C.GREEN}✓{C.RESET}" if o.in_cluster else f"{C.DIM}·{C.RESET}"
    if o.shared_eligible:
        amort = o.amortised_price_usd_h
        flag_sh = (f"shared ({C.DIM}${amort:.2f}/hr each{C.RESET})"
                   if amort is not None else "shared")
    else:
        flag_sh = "dedicated"
    flags    = f"{flag_ic} {flag_sh}"
    if over:
        flags = f"{C.YELLOW}⚠ over budget{C.RESET}"
    if o.quant_warning:
        flags = f"{flags} {C.YELLOW}⚠ quant{C.RESET}"

    strategy = o.parallelism_label or "1 GPU"
    usage  = f"{o.per_gpu_need_gb:.1f}/{o.instance.vram_gb} GB"
    fit    = f"[{tone}{bar}{C.RESET}] {util_pct:>3.0f}%"
    price  = f"${o.price_usd_h:.2f}"
    month  = _fmt_monthly(o.price_usd_h)
    fit_pad = 15 - len(f"[{bar}] {util_pct:>3.0f}%")
    # PR 2: tok/s column. Single-stream is the ceiling; per-user reflects
    # the user's --users concurrency (or 1 if not specified).
    if o.single_stream_tok_s > 0:
        ts_str = f"{o.per_user_tok_s_at_users:>4.0f}/{o.single_stream_tok_s:.0f}"
    else:
        ts_str = "  ?  "
    print(f"  {o.instance.name:<15} {o.instance.gpu:<9} {strategy:<10} {usage:<12} "
          f"{fit}{' ' * max(fit_pad, 0)}  {ts_str:>10}  {price:>6}  {month:>9}  {flags}")


def _print_scaling_section(
    scaling: list[ScalingRecommendation],
    args:    argparse.Namespace,
    C:       type,
) -> None:
    best_s = scaling[0] if scaling else None
    if not best_s:
        return

    ruler = _ruler(71, C)
    print(f"\n{ruler}")
    slo_label = (
        f"≥{args.target_tok_s:.0f} tok/s/user SLO, "
        if args.target_tok_s > 0 else ""
    )
    print(f"  {C.BOLD}FLEET SCALING{C.RESET} — {args.target_users} target users, "
          f"{slo_label}{int(args.utilization * 100)}% util headroom")
    print(ruler)

    strat = best_s.option.parallelism_label
    strat_info = f" ({strat})" if strat else ""
    if best_s.slo_unmet:
        verdict = f"{C.YELLOW}⚠ SLO NOT MET — see alternatives{C.RESET}"
    elif best_s.slo_capped:
        verdict = f"{C.GREEN}✓ RECOMMENDED FLEET (SLO-capped):{C.RESET}"
    else:
        verdict = f"{C.GREEN}✓ RECOMMENDED FLEET:{C.RESET}"
    print(f"\n  {verdict} "
          f"{C.BOLD}{best_s.replicas}× {best_s.option.instance.name}{C.RESET} "
          f"({best_s.option.instance.gpu}{strat_info})")
    print(f"    Max concurrency per instance: {best_s.max_concurrency_per_inst} "
          f"(VRAM-limited)")
    cap_note = " — capped by SLO" if best_s.slo_capped else ""
    print(f"    Effective capacity per instance: {best_s.effective_capacity} sequences"
          f"{cap_note}")
    print(f"    Fleet capacity: {best_s.effective_capacity * best_s.replicas} concurrent users")
    if best_s.estimated_tok_s_per_user > 0:
        slo_tone = (
            C.GREEN if best_s.estimated_tok_s_per_user >= args.target_tok_s else C.YELLOW
        )
        print(f"    Estimated per-user throughput: "
              f"{slo_tone}~{best_s.estimated_tok_s_per_user:.0f} tok/s/user{C.RESET}"
              f" {C.DIM}(SLO {args.target_tok_s:.0f}){C.RESET}")
    print(f"    Fleet cost: {C.BOLD}${best_s.fleet_cost_usd_h:.2f}/hr{C.RESET}  ·  "
          f"~{_fmt_monthly(best_s.fleet_cost_usd_h)}/month")
    print(f"    Total GPUs: {best_s.replicas * best_s.option.total_gpus}")

    if len(scaling) > 1:
        print(f"\n  {C.BOLD}Fleet alternatives{C.RESET} "
              f"(sorted by total fleet cost; ⚠ = SLO unmet)\n")
        header = (f"    {'INSTANCE':<15} {'GPU':<9} {'STRATEGY':<10} "
                  f"{'CONC/INST':>9}  {'REPLICAS':>8}  {'TOK/S':>6}  "
                  f"{'FLEET $/HR':>10}  {'FLEET/MO':>9}  {'NODEPOOL':<14}")
        print(f"{C.DIM}{header}{C.RESET}")
        print(f"{C.DIM}    {'-'*15} {'-'*9} {'-'*10} "
              f"{'-'*9}  {'-'*8}  {'-'*6}  {'-'*10}  {'-'*9}  {'-'*14}{C.RESET}")

        for s in scaling[: args.limit]:
            tone = C.GREEN if s is best_s else (C.YELLOW if s.slo_unmet else "")
            reset = C.RESET if tone else ""
            marker = "→ " if s is best_s else ("⚠ " if s.slo_unmet else "  ")
            s_strat = s.option.parallelism_label or "1 GPU"
            tok_s_str = f"{s.estimated_tok_s_per_user:.0f}" if s.estimated_tok_s_per_user > 0 else "?"
            print(f"  {tone}{marker}{s.option.instance.name:<15} "
                  f"{s.option.instance.gpu:<9} {s_strat:<10} "
                  f"{s.effective_capacity:>9}  {s.replicas:>8}  "
                  f"{tok_s_str:>6}  "
                  f"${s.fleet_cost_usd_h:>9.2f}  "
                  f"{_fmt_monthly(s.fleet_cost_usd_h):>9}  "
                  f"{s.option.nodepool:<14}{reset}")


def _print_yaml_snippet(model: ModelSpec, vram: VramEstimate, best: Option,
                        args: argparse.Namespace, C: type,
                        scaling: list[ScalingRecommendation] | None = None) -> None:
    name = model.model_id.split("/")[-1].lower().replace(".", "-").replace("_", "-")
    max_len = args.seq
    # Suggest shared: true only when the instance is truly shared-eligible —
    # meaning TIME_SLICE_REPLICAS copies of the model fit in GPU memory.
    # `shared_eligible` already encodes that check.
    shared = best.total_gpus == 1 and best.shared_eligible
    yaml_path  = f"workloads/models/{name}.yaml"
    commit_msg = f"feat: deploy {name}"

    # Compute a conservative VRAM hint in GiB that Karpenter's
    # `instance-gpu-memory` label (reported in MiB) will compare against with `Gt`.
    # The goal: exclude GPUs too small for the model, WITHOUT locking Karpenter
    # to a single instance family. Based on the model's actual per-GPU need plus
    # a 15% headroom — not the recommended instance's full VRAM (which would
    # e.g. pin a 48 GB L40S-class selection even when a 16 GB T4 would fit).
    # When `shared: true`, the requirement scales by TIME_SLICE_REPLICAS because
    # all co-tenants compete for the same physical VRAM.
    if shared:
        min_vram_gib = max(1, math.ceil(
            best.per_gpu_need_gb * TIME_SLICE_REPLICAS * 1.10
        ))
    else:
        min_vram_gib = max(1, math.ceil(best.per_gpu_need_gb * 1.15))

    # Size the Ray worker pod's CPU memory request based on model weights.
    # During vLLM startup the weights are briefly held in CPU RAM before being
    # transferred to the GPU — that's the peak. After startup, workers use
    # ~2-3Gi. We size for the startup peak + overhead:
    #   weights_gb (CPU copy) + 4Gi (Python, Ray, vLLM, HF tokenizer, buffers)
    # Rounded up with a floor of 8Gi so tiny models still have headroom for
    # Ray's own footprint on cold start.
    worker_mem_gib = max(8, math.ceil(vram.weights_gb + 4))

    # Build the YAML body. Intentionally flush-left so copy-paste into a shell
    # heredoc produces a clean file (no stray indentation).
    lines: list[str] = [
        "apiVersion: kro.run/v1alpha1",
        "kind: InferenceEndpoint",
        "metadata:",
        f"  name: {name}",
        "  namespace: inference",
        "spec:",
        f'  model: "{model.model_id}"',
        f"  gpuCount: {best.total_gpus}",
    ]
    if best.tp_degree > 1 or best.pp_degree > 1:
        lines.append(f"  tensorParallelSize: {best.tp_degree}")
    if best.pp_degree > 1:
        lines.append(f"  pipelineParallelSize: {best.pp_degree}")
    if shared:
        lines.append("  shared: true          "
                     "# fits with headroom — up to 4 models share the GPU")
    # Determine replica counts — use scaling recommendation if available.
    if scaling:
        best_scaling = scaling[0]
        min_replicas = best_scaling.replicas
        max_replicas = max(min_replicas, math.ceil(min_replicas * 1.5))
        # PR 6: cap vLLM batch size at the per-instance effective capacity
        # we actually planned for. Anyscale recommends 128-256 typical;
        # we use the larger of (effective_capacity × 1.5, 64) to leave headroom
        # without setting absurd defaults that risk OOM.
        max_num_seqs = max(64, math.ceil(best_scaling.effective_capacity * 1.5))
    else:
        min_replicas = 1
        max_replicas = 2
        # Single-instance mode: size for the user's --users count + headroom.
        max_num_seqs = max(64, args.users * 2)

    lines.extend([
        f"  maxModelLen: {max_len}",
        f"  minVramPerGpuGiB: {min_vram_gib}   "
        f"# min per-GPU VRAM; Karpenter picks the cheapest NVIDIA GPU that qualifies",
        f"  workerMemory: \"{worker_mem_gib}Gi\"   "
        f"# CPU memory for the Ray worker pod (default is conservative; this is sized to model)",
        # PR 6: max_num_seqs hint. The InferenceEndpoint CRD doesn't yet
        # surface this knob, so we emit it as a comment that operators can
        # plumb through (vLLM defaults to a very large value, which can OOM
        # under prefix-cache pressure). To enable: add `maxNumSeqs:` to the
        # CRD schema and pass it through to the RayService vLLM args.
        f"  # maxNumSeqs: {max_num_seqs}   "
        f"# vLLM concurrent-sequence cap (sized to fit; CRD wiring TBD)",
        f"  minReplicas: {min_replicas}",
        f"  maxReplicas: {max_replicas}",
    ])
    yaml_body = "\n".join(lines)

    # The heredoc uses a quoted 'EOF' so the YAML content is not subject to
    # shell expansion (model IDs sometimes contain '$' in other ecosystems).
    print(f"\n{C.BOLD}Deploy this model{C.RESET} — "
          f"{C.DIM}copy-paste the block below into your shell:{C.RESET}\n")
    print(f"cat > {yaml_path} <<'EOF'")
    print(yaml_body)
    print("EOF")

    print(f"\n{C.DIM}Then commit and push (ArgoCD picks it up within ~30s):{C.RESET}\n")
    print(f"git add {yaml_path}")
    print(f'git commit -m "{commit_msg}"')
    print("git push")
    print()
    print(f"{C.DIM}# Watch the deployment come up:{C.RESET}")
    print("kubectl get inferenceendpoints -n inference -w")


def print_json(
    model:     ModelSpec,
    vram:      VramEstimate,
    opts:      list[Option],
    best:      Option | None,
    args:      argparse.Namespace,
    price_src: str,
    scaling:   list[ScalingRecommendation] | None = None,
) -> None:
    payload = {
        "model": {
            "id":           model.model_id,
            "params":       model.params,
            "architecture": model.architecture,
            "num_layers":   model.num_layers,
            "hidden_size":  model.hidden_size,
            "num_heads":    model.num_heads,
            "num_kv_heads": model.num_kv_heads,
            "is_moe":       model.is_moe,
            "num_experts":  model.num_experts,
            "warnings":     model.warnings,
        },
        "request": {
            "weight_quant": args.quant,
            "kv_quant":     args.kv_quant,
            "seq_len":      args.seq,
            "batch_size":   args.batch,
            "users":        args.users,
            "tp_pin":       args.tp,
            "region":       args.region,
            "max_price":    args.max_price,
            "price_source": price_src,
        },
        "vram_gb": {
            "weights":     round(vram.weights_gb, 3),
            "kv_cache":    round(vram.kv_cache_gb, 3),
            "activations": round(vram.activations_gb, 3),
            "overhead":    round(vram.overhead_gb, 3),
            "total":       round(vram.total_gb, 3),
        },
        "options": [
            {
                "instance":             o.instance.name,
                "gpu":                  o.instance.gpu,
                "num_gpus":             o.instance.num_gpus,
                "vram_gb_per_gpu":      o.instance.vram_gb,
                "hbm_bandwidth_tb_s":   o.instance.hbm_bandwidth_tb_s,
                "compute_capability":   o.instance.compute_capability,
                "arch_family":          o.instance.arch_family,
                "tp_degree":            o.tp_degree,
                "pp_degree":            o.pp_degree,
                "per_gpu_need_gb":      round(o.per_gpu_need_gb, 3),
                "headroom_gb":          round(o.headroom_gb, 3),
                "single_stream_tok_s":  round(o.single_stream_tok_s, 1),
                "per_user_tok_s_at_users": round(o.per_user_tok_s_at_users, 1),
                "price_usd_h":          round(o.price_usd_h, 4),
                "monthly_usd":          round(o.price_usd_h * 730, 2),
                "nodepool":             o.nodepool,
                "shared_eligible":      o.shared_eligible,
                "over_price_ceiling":   o.over_price_ceiling,
                "quant_warning":        o.quant_warning,
                "notes":                o.notes,
            }
            for o in opts[: args.limit]
        ],
        "recommended": None if not best else {
            "instance":             best.instance.name,
            "tp_degree":            best.tp_degree,
            "pp_degree":            best.pp_degree,
            "total_gpus":           best.total_gpus,
            "nodepool":             best.nodepool,
            "shared_eligible":      best.shared_eligible,
            "single_stream_tok_s":  round(best.single_stream_tok_s, 1),
            "per_user_tok_s_at_users": round(best.per_user_tok_s_at_users, 1),
            "price_usd_h":          round(best.price_usd_h, 4),
            "monthly_usd":          round(best.price_usd_h * 730, 2),
            "over_price_ceiling":   best.over_price_ceiling,
            "quant_warning":        best.quant_warning,
        },
    }
    if scaling:
        best_s = scaling[0]
        payload["scaling"] = {
            "target_users":       args.target_users,
            "target_tok_s":       args.target_tok_s,
            "utilization_factor": args.utilization,
            "recommended": {
                "instance":                    best_s.option.instance.name,
                "tp_degree":                   best_s.option.tp_degree,
                "replicas":                    best_s.replicas,
                "max_concurrency_per_instance": best_s.max_concurrency_per_inst,
                "effective_capacity_per_inst":  best_s.effective_capacity,
                "fleet_capacity":              best_s.effective_capacity * best_s.replicas,
                "estimated_tok_s_per_user":    round(best_s.estimated_tok_s_per_user, 1),
                "slo_capped":                  best_s.slo_capped,
                "slo_unmet":                   best_s.slo_unmet,
                "fleet_cost_usd_h":            round(best_s.fleet_cost_usd_h, 4),
                "fleet_monthly_usd":           round(best_s.fleet_monthly_usd, 2),
                "total_gpus":                  best_s.replicas * best_s.option.total_gpus,
                "nodepool":                    best_s.option.nodepool,
            },
            "alternatives": [
                {
                    "instance":                    s.option.instance.name,
                    "tp_degree":                   s.option.tp_degree,
                    "replicas":                    s.replicas,
                    "max_concurrency_per_instance": s.max_concurrency_per_inst,
                    "effective_capacity_per_inst":  s.effective_capacity,
                    "estimated_tok_s_per_user":    round(s.estimated_tok_s_per_user, 1),
                    "slo_capped":                  s.slo_capped,
                    "slo_unmet":                   s.slo_unmet,
                    "fleet_cost_usd_h":            round(s.fleet_cost_usd_h, 4),
                    "fleet_monthly_usd":           round(s.fleet_monthly_usd, 2),
                    "nodepool":                    s.option.nodepool,
                }
                for s in scaling[: args.limit]
            ],
        }
    print(json.dumps(payload, indent=2))


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Recommend EC2 GPU instances for serving an LLM on EKS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[1] if "Examples:" in (__doc__ or "") else "",
    )
    p.add_argument("model", help="HuggingFace model ID (e.g. google/gemma-3-4b-it)")
    p.add_argument("--quant", choices=sorted(WEIGHT_BYTES),
                   default="bf16", help="Weights quantisation (default: bf16)")
    p.add_argument("--kv-quant", choices=sorted(KV_BYTES),
                   default="fp16", help="KV cache quantisation (default: fp16)")
    p.add_argument("--seq", type=int, default=8192,
                   help="Max sequence length / maxModelLen (default: 8192)")
    p.add_argument("--workload", choices=sorted(WORKLOAD_PRESETS),
                   default=None,
                   help="Workload preset that fills sensible defaults for "
                        "--avg-context, --target-tok-s, and --users. "
                        "Override any individual field by setting it explicitly. "
                        "Choices: " + ", ".join(
                            f"{k}={v['description']}"
                            for k, v in WORKLOAD_PRESETS.items()
                        ))
    p.add_argument("--avg-context", type=int, default=None,
                   help="Typical input+output tokens per active sequence (default: --seq // 4). "
                        "Used for concurrency calculations to reflect PagedAttention reality "
                        "(KV is allocated per token in flight, not reserved up to max_model_len). "
                        "Set equal to --seq for worst-case concurrency sizing.")
    p.add_argument("--batch", type=int, default=1,
                   help="Batch size per step (default: 1)")
    p.add_argument("--users", type=int, default=1,
                   help="Concurrent users / parallel requests (default: 1)")
    p.add_argument("--tp", type=int, choices=TP_DEGREES,
                   help="Pin tensor-parallel degree (1|2|4|8). Default: consider all.")
    p.add_argument("--region", default=None,
                   help="AWS region for pricing (default: $AWS_REGION, $AWS_DEFAULT_REGION, "
                        "boto3 session, or us-east-1)")
    p.add_argument("--max-price", type=float, default=20.0,
                   help="Per-hour price ceiling in USD. Options above are still shown "
                        "but flagged (default: 20.0)")
    p.add_argument("--refresh-prices", action="store_true",
                   help="Ignore cached prices and refetch from AWS Pricing API")
    p.add_argument("--in-cluster-only", action="store_true",
                   help="Only show instances allowed by this cluster's Karpenter NodePools")
    p.add_argument("--safety-margin", type=float, default=0.10,
                   help="Leave this fraction of GPU VRAM unused (default: 0.10)")
    p.add_argument("--limit", type=int, default=10,
                   help="Max options to display (default: 10)")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    p.add_argument("--verbose", action="store_true",
                   help="Print pricing API fallbacks and other diagnostics to stderr")
    p.add_argument("--target-users", type=int, default=None,
                   help="Target concurrent users across the fleet. Triggers scaling mode: "
                        "recommends instance type + replica count. Overrides --users for "
                        "single-instance sizing.")
    p.add_argument("--utilization", type=float, default=DEFAULT_UTILIZATION_FACTOR,
                   help=f"Target utilization factor for scaling mode — fraction of max "
                        f"concurrency to plan for (default: {DEFAULT_UTILIZATION_FACTOR}). "
                        f"Lower = more burst headroom.")
    p.add_argument("--target-tok-s", type=float, default=DEFAULT_TARGET_TOK_S,
                   help="Per-user decode throughput SLO in fleet mode (default: disabled). "
                        "When set, caps per-instance concurrency so each user meets this "
                        "throughput — forces escalation to faster GPUs if needed.")
    p.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"),
                   help="HuggingFace token (also $HF_TOKEN). Required for gated models.")
    args = p.parse_args(argv)

    if not 0.0 <= args.safety_margin < 0.5:
        sys.exit("error: --safety-margin must be in [0.0, 0.5)")
    if args.max_price <= 0:
        sys.exit("error: --max-price must be > 0")
    if args.target_users is not None and args.target_users < 1:
        sys.exit("error: --target-users must be >= 1")
    if not 0.1 <= args.utilization <= 1.0:
        sys.exit("error: --utilization must be in [0.1, 1.0]")
    if args.target_tok_s < 0:
        sys.exit("error: --target-tok-s must be >= 0")
    if args.avg_context is not None and args.avg_context < 1:
        sys.exit("error: --avg-context must be >= 1")

    # Apply workload preset (only fills fields the user didn't explicitly set).
    # We detect "user didn't set" by checking against the argparse default. For
    # --avg-context the default is None so detection is clean. For --target-tok-s
    # we use the DEFAULT_TARGET_TOK_S sentinel (0). For --users the default is 1.
    args._preset_applied: list[str] = []
    if args.workload:
        preset = WORKLOAD_PRESETS[args.workload]
        if args.avg_context is None:
            args.avg_context = preset["avg_context"]
            args._preset_applied.append(f"--avg-context {preset['avg_context']}")
        if args.target_tok_s == DEFAULT_TARGET_TOK_S:
            args.target_tok_s = float(preset["target_tok_s"])
            args._preset_applied.append(f"--target-tok-s {preset['target_tok_s']}")
        # Only fill --users in single-instance mode (no --target-users)
        if args.users == 1 and args.target_users is None:
            args.users = preset["users"]
            args._preset_applied.append(f"--users {preset['users']}")

    args.region = detect_region(args.region)
    prices, price_src = resolve_prices(args.region, args.refresh_prices, args.verbose)

    # In scaling mode, size a single instance for 1 user so that VRAM estimate
    # reflects weights + overhead without inflating KV cache for the full fleet.
    sizing_users = 1 if args.target_users else args.users
    model = fetch_model(args.model, args.hf_token)
    vram  = estimate_vram(model, args.quant, args.kv_quant, args.seq,
                          args.batch, sizing_users,
                          avg_context_len=args.avg_context)
    opts  = find_options(
        total_need_gb=vram.total_gb,
        num_heads=model.num_heads,
        tp_pin=args.tp,
        require_in_cluster=args.in_cluster_only,
        safety_margin=args.safety_margin,
        prices=prices,
        max_price=args.max_price,
        model=model,
        weight_q=args.quant,
        kv_q=args.kv_quant,
        users=sizing_users,
    )
    best  = opts[0] if opts else None

    scaling: list[ScalingRecommendation] | None = None
    if args.target_users and opts:
        scaling = compute_scaling(
            opts=opts,
            vram=vram,
            target_users=args.target_users,
            utilization_factor=args.utilization,
            safety_margin=args.safety_margin,
            target_tok_s=args.target_tok_s,
        )

    if args.json:
        print_json(model, vram, opts, best, args, price_src, scaling)
    else:
        print_human(model, vram, opts, best, args, price_src, scaling)

    return 0 if best else 2


if __name__ == "__main__":
    sys.exit(main())
