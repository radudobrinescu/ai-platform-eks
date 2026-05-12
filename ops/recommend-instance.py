#!/usr/bin/env python3
"""
recommend-instance.py — Right-size EC2 GPU instances for an LLM on this EKS platform.

Mirrors the inputs of https://apxml.com/tools/vram-calculator, but instead of
reporting VRAM utilisation on a fixed GPU, it outputs a ranked list of AWS GPU
instances that can host the model, and highlights which ones match this cluster's
Karpenter NodePool constraints (g5/g5e/g6/g6e/g7/g7e × xlarge/2xlarge/4xlarge).

Uses only the Python stdlib. Reads model architecture from the HuggingFace Hub.

Examples:
  ./ops/recommend-instance.py google/gemma-3-4b-it
  ./ops/recommend-instance.py meta-llama/Llama-3.1-8B-Instruct --seq 16384 --users 8
  ./ops/recommend-instance.py Qwen/Qwen2.5-32B-Instruct --quant int4 --json
  HF_TOKEN=hf_... ./ops/recommend-instance.py google/gemma-3-27b-it --quant bf16
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
    name:              str
    gpu:               str
    num_gpus:          int
    vram_gb:           int     # per GPU
    vcpu:              int
    mem_gb:            int     # host RAM
    price_usd_h:       float   # us-east-1 on-demand, approximate fallback
    gpu_manufacturer:  str = "nvidia"

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


INSTANCES: list[Instance] = [
    # g4dn — T4 (legacy, kept for reference; not in this cluster's NodePool)
    Instance("g4dn.xlarge",   "T4",         1, 16,   4,  16,  0.526),
    Instance("g4dn.2xlarge",  "T4",         1, 16,   8,  32,  0.752),
    Instance("g4dn.12xlarge", "T4",         4, 16,  48, 192,  3.912),

    # g5 — A10G (24GB)
    Instance("g5.xlarge",     "A10G",       1, 24,   4,  16,  1.006),
    Instance("g5.2xlarge",    "A10G",       1, 24,   8,  32,  1.212),
    Instance("g5.4xlarge",    "A10G",       1, 24,  16,  64,  1.624),
    Instance("g5.12xlarge",   "A10G",       4, 24,  48, 192,  5.672),
    Instance("g5.48xlarge",   "A10G",       8, 24, 192, 768, 16.288),

    # g6 — L4 (24GB)
    Instance("g6.xlarge",     "L4",         1, 24,   4,  16,  0.805),
    Instance("g6.2xlarge",    "L4",         1, 24,   8,  32,  0.978),
    Instance("g6.4xlarge",    "L4",         1, 24,  16,  64,  1.323),
    Instance("g6.12xlarge",   "L4",         4, 24,  48, 192,  4.602),
    Instance("g6.48xlarge",   "L4",         8, 24, 192, 768, 13.350),

    # g6e — L40S (48GB)
    Instance("g6e.xlarge",    "L40S",       1, 48,   4,  32,  1.861),
    Instance("g6e.2xlarge",   "L40S",       1, 48,   8,  64,  2.242),
    Instance("g6e.4xlarge",   "L40S",       1, 48,  16, 128,  3.004),
    Instance("g6e.12xlarge",  "L40S",       4, 48,  48, 384, 10.493),
    Instance("g6e.48xlarge",  "L40S",       8, 48, 192, 1536, 30.131),

    # p4d / p4de — A100
    Instance("p4d.24xlarge",  "A100 40GB",  8, 40,  96, 1152, 32.773),
    Instance("p4de.24xlarge", "A100 80GB",  8, 80,  96, 1152, 40.966),

    # p5 / p5e / p5en — H100 / H200
    Instance("p5.48xlarge",   "H100 80GB",  8, 80, 192, 2048, 98.320),
    Instance("p5e.48xlarge",  "H200 141GB", 8, 141, 192, 2048, 118.020),
    Instance("p5en.48xlarge", "H200 141GB", 8, 141, 192, 2048, 124.000),
]


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
        return static, f"static catalog ({CATALOG_PRICING_REGION}, not {region})"
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

    @property
    def total_gb(self) -> float:
        return self.weights_gb + self.kv_cache_gb + self.activations_gb + self.overhead_gb


def estimate_vram(
    m:          ModelSpec,
    weight_q:   str,
    kv_q:       str,
    seq_len:    int,
    batch_size: int,
    users:      int,
) -> VramEstimate:
    """
    Per-GPU VRAM required to serve `m`, ignoring tensor parallelism (TP=1).
    Dividing this total by the TP degree gives a first-order per-GPU estimate.
    """
    w_bytes = WEIGHT_BYTES[weight_q]
    k_bytes = KV_BYTES[kv_q]

    weights = m.params * w_bytes

    # KV cache per token = 2 (K+V) × layers × num_kv_heads × head_dim × bytes.
    # Effective concurrency in vLLM ≈ max(batch_size, users).
    concurrency = max(batch_size, users)
    kv_per_tok  = 2 * m.num_layers * m.num_kv_heads * m.head_dim * k_bytes
    kv_cache    = kv_per_tok * seq_len * concurrency

    # Activations: rough upper bound for prefill — O(batch * seq * hidden).
    # vLLM / Flash-Attention reduces this materially; factor 4 is a safe pad.
    activations = 4 * batch_size * seq_len * m.hidden_size * 2  # fp16 activations

    # 15% pad for framework, CUDA graphs, allocator fragmentation.
    subtotal = weights + kv_cache + activations
    overhead = 0.15 * subtotal

    gb = 1024 ** 3
    return VramEstimate(
        weights_gb=weights / gb,
        kv_cache_gb=kv_cache / gb,
        activations_gb=activations / gb,
        overhead_gb=overhead / gb,
    )


# --------------------------------------------------------------------------- #
# Recommendation                                                              #
# --------------------------------------------------------------------------- #

@dataclass
class Option:
    instance:           Instance
    tp_degree:          int
    per_gpu_need_gb:    float
    headroom_gb:        float
    price_usd_h:        float    # effective price (from Pricing API or fallback)
    nodepool:           str      # "gpu-inference" | "gpu-shared" | "out-of-cluster"
    shared_eligible:    bool     # can legitimately use gpu-shared time-slicing
    over_price_ceiling: bool
    notes:              list[str] = field(default_factory=list)

    @property
    def in_cluster(self) -> bool:
        return self.nodepool != "out-of-cluster"

    @property
    def effective_price_usd_h(self) -> float:
        """Per-model cost, accounting for 4-way time-slicing when applicable.

        If the model uses <30% of a shared-eligible GPU, the YAML snippet sets
        `shared: true` — meaning the GPU is amortised across up to 4 models.
        Rank using that effective cost so the recommender doesn't undercut its
        own shared-mode suggestion with a cheaper-but-dedicated alternative.
        """
        if self.shared_eligible and self.headroom_gb >= (self.instance.vram_gb * 0.70):
            return self.price_usd_h / 4.0
        return self.price_usd_h

    @property
    def sort_key(self) -> tuple:
        # Prefer: below ceiling, in-cluster, lowest effective price, tightest TP.
        return (
            self.over_price_ceiling,
            not self.in_cluster,
            self.effective_price_usd_h,
            self.tp_degree,
        )


def _tp_overhead(tp: int) -> float:
    """Multiplicative pad for tensor-parallel activation duplication and comm buffers."""
    return 1.0 if tp == 1 else 1.0 + 0.05 * math.log2(tp)


def _classify_nodepool(inst: Instance, tp: int) -> tuple[str, bool]:
    """Which NodePool can schedule this instance, and is it shared-eligible?

    Returns (nodepool, shared_eligible). Mirrors the requirements in
    terraform/30.eks/30.cluster/karpenter/gpu-{inference,shared}.yaml.
    """
    if inst.gpu_manufacturer != CLUSTER_GPU_MANUFACTURER:
        return "out-of-cluster", False

    # gpu-shared: single-GPU G-family NVIDIA, large enough to time-slice sensibly.
    shared_eligible = (
        inst.category in CLUSTER_SHARED_CATEGORIES
        and inst.num_gpus == 1
        and inst.vram_gb >= 24
    )

    # gpu-inference: any NVIDIA G or P instance.
    if inst.category in CLUSTER_INFERENCE_CATEGORIES:
        # When TP == num_gpus and > 1, we need the whole dedicated node;
        # gpu-inference serves both single- and multi-GPU cases.
        return "gpu-inference", shared_eligible

    return "out-of-cluster", False


def find_options(
    total_need_gb:      float,
    tp_pin:             int | None,
    require_in_cluster: bool,
    safety_margin:      float,
    prices:             dict[str, float],
    max_price:          float,
) -> list[Option]:
    options: list[Option] = []
    tp_candidates = (tp_pin,) if tp_pin else TP_DEGREES

    for inst in INSTANCES:
        for tp in tp_candidates:
            if tp > inst.num_gpus:
                continue
            # vLLM uses all GPUs on the node for TP; skip partial allocations on
            # multi-GPU nodes where the pool wouldn't naturally bin-pack.
            if tp != inst.num_gpus and inst.num_gpus > 1:
                continue

            per_gpu = (total_need_gb / tp) * _tp_overhead(tp)
            available = inst.vram_gb * (1.0 - safety_margin)
            if per_gpu > available:
                continue

            nodepool, shared_eligible = _classify_nodepool(inst, tp)
            if require_in_cluster and nodepool == "out-of-cluster":
                continue

            price = prices.get(inst.name, inst.price_usd_h)
            over_ceiling = price > max_price

            notes: list[str] = []
            if tp > 1:
                notes.append(f"tensor-parallel across {tp} GPUs")
            if nodepool == "out-of-cluster":
                notes.append("not covered by Karpenter NodePools (non-NVIDIA or non-G/P)")
            if over_ceiling:
                notes.append(f"${price:.2f}/hr exceeds --max-price ${max_price:.2f}")

            options.append(Option(
                instance=inst,
                tp_degree=tp,
                per_gpu_need_gb=per_gpu,
                headroom_gb=inst.vram_gb - per_gpu,
                price_usd_h=price,
                nodepool=nodepool,
                shared_eligible=shared_eligible,
                over_price_ceiling=over_ceiling,
                notes=notes,
            ))

    options.sort(key=lambda o: o.sort_key)
    return options


# --------------------------------------------------------------------------- #
# Presentation                                                                #
# --------------------------------------------------------------------------- #

def _fmt_params(p: int) -> str:
    if p >= 1e12: return f"{p/1e12:.2f}T"
    if p >= 1e9:  return f"{p/1e9:.2f}B"
    if p >= 1e6:  return f"{p/1e6:.1f}M"
    return str(p)


def print_human(
    model:       ModelSpec,
    vram:        VramEstimate,
    opts:        list[Option],
    best:        Option | None,
    args:        argparse.Namespace,
    price_src:   str,
) -> None:
    print(f"\nModel: {model.model_id}")
    print(f"  Architecture:  {model.architecture or '?'}")
    print(f"  Parameters:    {_fmt_params(model.params)} ({model.params:,})")
    print(f"  Layers:        {model.num_layers}")
    print(f"  Hidden / heads: {model.hidden_size} / {model.num_heads} (KV heads: {model.num_kv_heads})")
    print(f"  Max position:  {model.max_position}")
    if model.is_moe:
        print(f"  MoE:           {model.num_experts} experts ({model.active_experts} active/token) — all experts held in VRAM")
    for w in model.warnings:
        print(f"  ! {w}")

    print(f"\nRequest:")
    print(f"  Weights quant:  {args.quant}")
    print(f"  KV cache quant: {args.kv_quant}")
    print(f"  Seq length:     {args.seq:,}")
    print(f"  Batch size:     {args.batch}")
    print(f"  Concurrent:     {args.users}")
    print(f"  Region:         {args.region}  (prices: {price_src})")
    print(f"  Price ceiling:  ${args.max_price:.2f}/hr  (options above are flagged)")

    print(f"\nVRAM estimate (TP=1, before sharding):")
    print(f"  Weights:      {vram.weights_gb:7.2f} GB")
    print(f"  KV cache:     {vram.kv_cache_gb:7.2f} GB")
    print(f"  Activations:  {vram.activations_gb:7.2f} GB")
    print(f"  Overhead:     {vram.overhead_gb:7.2f} GB   (+15%)")
    print(f"  Total:        {vram.total_gb:7.2f} GB")

    if not opts:
        print("\nNo instance in the catalog can host this configuration.")
        print("Try: --quant int4, a shorter --seq, fewer --users, or lower --batch.")
        return

    within_budget = [o for o in opts if not o.over_price_ceiling]
    over_budget   = [o for o in opts if o.over_price_ceiling]

    print(f"\nViable instances (sorted: within budget, in-cluster, then cheapest):\n")
    print(f"  {'INSTANCE':<18} {'GPU':<13} {'TP':>3} {'VRAM/GPU':>9} "
          f"{'PER-GPU NEED':>12} {'HEADROOM':>9} {'$/HR':>7} {'NODEPOOL':<14}")
    print(f"  {'-'*18} {'-'*13} {'-'*3} {'-'*9} {'-'*12} {'-'*9} {'-'*7} {'-'*14}")

    for o in within_budget[: args.limit]:
        _print_row(o)

    if over_budget and len(within_budget) < args.limit:
        print(f"  {'-'*18} above --max-price ceiling {'-'*39}")
        for o in over_budget[: args.limit - len(within_budget)]:
            _print_row(o, over_budget=True)

    if best:
        tag = " ⚠ OVER BUDGET" if best.over_price_ceiling else ""
        print(f"\nRecommended: {best.instance.name} ({best.instance.gpu}, "
              f"TP={best.tp_degree}, ${best.price_usd_h:.2f}/hr {args.region}){tag}")
        for n in best.notes:
            print(f"  • {n}")

        _print_yaml_snippet(model, best, args)

    print("\nNotes:")
    print("  • NODEPOOL = gpu-inference | gpu-shared | out-of-cluster (per Karpenter)")
    print("  • gpu-shared requires a single-GPU NVIDIA G instance with time-slicing.")
    print("  • Prices from: " + price_src +
          ". Use --refresh-prices to invalidate the cache.")
    print("  • Estimates are first-order. Validate with the actual deployment.")


def _print_row(o: Option, over_budget: bool = False) -> None:
    marker = "⚠" if over_budget else ("✓" if o.in_cluster else " ")
    pool = o.nodepool if o.nodepool != "out-of-cluster" else "—"
    if o.shared_eligible and o.nodepool == "gpu-inference":
        pool = "gpu-inference*"  # asterisk = also eligible for gpu-shared
    print(f"  {o.instance.name:<18} {o.instance.gpu:<13} {o.tp_degree:>3} "
          f"{o.instance.vram_gb:>7} GB {o.per_gpu_need_gb:>10.1f} GB "
          f"{o.headroom_gb:>7.1f} GB {o.price_usd_h:>7.2f}  {marker} {pool:<12}")


def _print_yaml_snippet(model: ModelSpec, best: Option, args: argparse.Namespace) -> None:
    name = model.model_id.split("/")[-1].lower().replace(".", "-").replace("_", "-")
    max_len = args.seq
    # Suggest shared: true only when the instance is actually shared-eligible AND
    # the model fits with huge headroom (using <30% of the GPU).
    shared = (
        best.tp_degree == 1
        and best.shared_eligible
        and best.headroom_gb >= (best.instance.vram_gb * 0.70)
    )
    print(f"\nDrop-in InferenceEndpoint for workloads/models/{name}.yaml:")
    print("---")
    print("apiVersion: kro.run/v1alpha1")
    print("kind: InferenceEndpoint")
    print("metadata:")
    print(f"  name: {name}")
    print("  namespace: inference")
    print("spec:")
    print(f"  model: \"{model.model_id}\"")
    print(f"  gpuCount: {best.tp_degree}")
    if shared:
        print("  shared: true          # fits with headroom — share GPU with up to 3 other models")
    print(f"  maxModelLen: {max_len}")
    print("  minReplicas: 1")
    print("  maxReplicas: 2")


def print_json(
    model:     ModelSpec,
    vram:      VramEstimate,
    opts:      list[Option],
    best:      Option | None,
    args:      argparse.Namespace,
    price_src: str,
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
                "instance":           o.instance.name,
                "gpu":                o.instance.gpu,
                "num_gpus":           o.instance.num_gpus,
                "vram_gb_per_gpu":    o.instance.vram_gb,
                "tp_degree":          o.tp_degree,
                "per_gpu_need_gb":    round(o.per_gpu_need_gb, 3),
                "headroom_gb":        round(o.headroom_gb, 3),
                "price_usd_h":        round(o.price_usd_h, 4),
                "nodepool":           o.nodepool,
                "shared_eligible":    o.shared_eligible,
                "over_price_ceiling": o.over_price_ceiling,
                "notes":              o.notes,
            }
            for o in opts[: args.limit]
        ],
        "recommended": None if not best else {
            "instance":           best.instance.name,
            "tp_degree":          best.tp_degree,
            "nodepool":           best.nodepool,
            "shared_eligible":    best.shared_eligible,
            "price_usd_h":        round(best.price_usd_h, 4),
            "over_price_ceiling": best.over_price_ceiling,
        },
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
    p.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"),
                   help="HuggingFace token (also $HF_TOKEN). Required for gated models.")
    args = p.parse_args(argv)

    if not 0.0 <= args.safety_margin < 0.5:
        sys.exit("error: --safety-margin must be in [0.0, 0.5)")
    if args.max_price <= 0:
        sys.exit("error: --max-price must be > 0")

    args.region = detect_region(args.region)
    prices, price_src = resolve_prices(args.region, args.refresh_prices, args.verbose)

    model = fetch_model(args.model, args.hf_token)
    vram  = estimate_vram(model, args.quant, args.kv_quant, args.seq, args.batch, args.users)
    opts  = find_options(
        total_need_gb=vram.total_gb,
        tp_pin=args.tp,
        require_in_cluster=args.in_cluster_only,
        safety_margin=args.safety_margin,
        prices=prices,
        max_price=args.max_price,
    )
    best  = opts[0] if opts else None

    if args.json:
        print_json(model, vram, opts, best, args, price_src)
    else:
        print_human(model, vram, opts, best, args, price_src)

    return 0 if best else 2


if __name__ == "__main__":
    sys.exit(main())
