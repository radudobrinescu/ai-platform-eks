#!/usr/bin/env python3
"""
compare-models.py — run the same eval set through several models and prove the
money-demo thesis in Langfuse: a small fine-tuned model can match or beat a
large commercial one on a narrow task, at a fraction of the cost.

This is the one genuinely new component of the turnkey platform, and it stays
intentionally thin: it leans on tools that already exist.

  - LiteLLM unifies every model (the Bedrock frontier model, self-hosted Qwen
    base, the fine-tuned Qwen) behind ONE OpenAI-compatible endpoint and ONE key.
  - LiteLLM's Langfuse callback already traces every call with cost / latency /
    tokens — so the objective metrics are logged for free.
  - Langfuse owns the dataset storage, the side-by-side run comparison UI, the
    LLM-as-judge evaluators, and human annotation. We don't rebuild any of it.

What this script does:
  1. Upload the held-out eval prompts as a Langfuse Dataset (idempotent).
  2. Run each prompt through each model via LiteLLM, tagging each model's pass
     as a Langfuse Dataset Run (run name = model alias) and linking the trace to
     the dataset item — so Langfuse renders the native 3-way comparison table.
  3. Print a summary (avg cost/req, p50 latency, tokens) per model and the
     Langfuse dataset-run URL. The GPU-hour cost crossover ("above ~N req/day
     the self-hosted tuned model is cheaper than Bedrock per request") reuses
     recommend-instance.py's pricing/throughput model — no new cost system.

Quality scoring (voice match, policy correctness, helpfulness) is done IN
Langfuse: configure an LLM-as-judge evaluator once in the UI (judge = the
frontier model, e.g. claude-opus-4-8, via this same LiteLLM endpoint), and use
the side-by-side view for human preference. This script deliberately does NOT
implement a judge — that would duplicate a built-in.

Dataset format (JSONL, one object per line):
    {"id": "q1", "input": "How do I reset my password?", "expected_output": "..."}
  `id` and `expected_output` are optional; `input` may be a string or an
  OpenAI-style messages list.

Examples:
  # Preflight only — check connectivity + Bedrock access, change nothing.
  ./ops/compare-models.py --preflight

  # Full 3-way comparison.
  ./ops/compare-models.py \
      --dataset support-eval.jsonl \
      --models claude-opus-4-8,qwen3-3b,qwen3-support-tuned \
      --langfuse-dataset support-voice-eval

  # Add a cost crossover for the self-hosted model (needs HF model id of the base).
  ./ops/compare-models.py --dataset support-eval.jsonl \
      --models claude-opus-4-8,qwen3-3b \
      --self-hosted-model qwen3-3b --self-hosted-hf-id Qwen/Qwen2.5-3B-Instruct

Connection: by default talks to LiteLLM + Langfuse on localhost (the
./ops/ssm-tunnel.sh ports). Override with --litellm-url / --langfuse-url or the
matching env vars. Secrets come from env / kubectl, never hardcoded.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Defaults & connection config                                                #
# --------------------------------------------------------------------------- #

DEFAULT_LITELLM_URL = os.environ.get("LITELLM_URL", "http://localhost:4000")
DEFAULT_LANGFUSE_URL = os.environ.get("LANGFUSE_URL", "http://localhost:3000")
DEFAULT_MODELS = "claude-opus-4-8,qwen3-3b"
REQUEST_TIMEOUT_SEC = 120
# Approximate hours/month an always-on GPU node runs (matches recommend-instance.py).
HOURS_PER_MONTH = 730.0


def _ssl_context() -> ssl.SSLContext:
    """SSL context that works across common macOS Python installs (certifi fallback)."""
    ctx = ssl.create_default_context()
    if ctx.get_ca_certs():
        return ctx
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ctx


_SSL = _ssl_context()


# --------------------------------------------------------------------------- #
# Small HTTP helpers (stdlib only — same style as recommend-instance.py)      #
# --------------------------------------------------------------------------- #

class HttpError(RuntimeError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:500]}")
        self.status = status
        self.body = body
        self.url = url


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = REQUEST_TIMEOUT_SEC,
) -> tuple[int, dict[str, Any]]:
    """Issue an HTTP request, returning (status, parsed-json-or-empty-dict)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            raw = resp.read().decode()
            parsed = json.loads(raw) if raw.strip() else {}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        raise HttpError(e.code, raw, url) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Cannot reach {url}: {e.reason}. "
                           f"Is the service up / SSM tunnel open (./ops/ssm-tunnel.sh)?") from None


# --------------------------------------------------------------------------- #
# Secret resolution — env first, then kubectl (never hardcoded)               #
# --------------------------------------------------------------------------- #

def _kubectl_secret(namespace: str, name: str, key: str) -> str | None:
    """Read a single key from a Kubernetes Secret via kubectl, base64-decoded."""
    try:
        out = subprocess.check_output(
            ["kubectl", "get", "secret", name, "-n", namespace,
             "-o", f"jsonpath={{.data.{key}}}"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    if not out:
        return None
    try:
        return base64.b64decode(out).decode()
    except Exception:
        return None


def resolve_litellm_key(explicit: str | None) -> str:
    """LiteLLM master key: --litellm-key > LITELLM_MASTER_KEY env > kubectl secret."""
    if explicit:
        return explicit
    env = os.environ.get("LITELLM_MASTER_KEY")
    if env:
        return env
    k = _kubectl_secret("ai-platform", "litellm-secrets", "master-key")
    if k:
        return k
    sys.exit("ERROR: no LiteLLM key. Set --litellm-key, LITELLM_MASTER_KEY, or "
             "ensure kubectl can read secret/litellm-secrets in ai-platform.")


def resolve_langfuse_keys(
    pub: str | None, sec: str | None
) -> tuple[str | None, str | None]:
    """Langfuse keys: flags > env > kubectl. Returns (public, secret); may be None."""
    pub = pub or os.environ.get("LANGFUSE_PUBLIC_KEY") \
        or _kubectl_secret("ai-platform", "langfuse-litellm-keys", "LANGFUSE_PUBLIC_KEY")
    sec = sec or os.environ.get("LANGFUSE_SECRET_KEY") \
        or _kubectl_secret("ai-platform", "langfuse-litellm-keys", "LANGFUSE_SECRET_KEY")
    return pub, sec


# --------------------------------------------------------------------------- #
# Dataset loading                                                             #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EvalItem:
    id: str
    input: Any                      # str or list[messages]
    expected_output: str | None = None

    def to_messages(self) -> list[dict[str, str]]:
        if isinstance(self.input, list):
            return self.input
        return [{"role": "user", "content": str(self.input)}]


def load_dataset(path: Path) -> list[EvalItem]:
    """Parse a JSONL eval file into EvalItems, validating each line."""
    if not path.exists():
        sys.exit(f"ERROR: dataset file not found: {path}")
    items: list[EvalItem] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            sys.exit(f"ERROR: {path}:{lineno} is not valid JSON: {e}")
        if "input" not in obj:
            sys.exit(f"ERROR: {path}:{lineno} missing required field 'input'")
        items.append(EvalItem(
            id=str(obj.get("id") or f"item-{lineno}"),
            input=obj["input"],
            expected_output=obj.get("expected_output"),
        ))
    if not items:
        sys.exit(f"ERROR: dataset {path} has no usable rows")
    return items


# --------------------------------------------------------------------------- #
# Langfuse client (REST — no SDK dependency)                                  #
# --------------------------------------------------------------------------- #

class Langfuse:
    """Minimal Langfuse REST client for the dataset + run + trace-link flow.

    Uses the public API (Basic auth with the project public/secret keys). Only
    the handful of endpoints this script needs are implemented; everything else
    (judging, the comparison UI) lives in Langfuse itself.
    """

    def __init__(self, base_url: str, public_key: str, secret_key: str):
        self.base = base_url.rstrip("/")
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        return _request(method, f"{self.base}/api/public{path}",
                        headers=self._auth, body=body)[1]

    def health(self) -> bool:
        try:
            _request("GET", f"{self.base}/api/public/health", headers=self._auth)
            return True
        except Exception:
            return False

    def ensure_dataset(self, name: str) -> None:
        """Create the dataset (idempotent — Langfuse upserts by name)."""
        self._api("POST", "/v2/datasets", {"name": name})

    def upsert_item(self, dataset_name: str, item: EvalItem) -> None:
        """Create/update a dataset item. Langfuse upserts when id is supplied."""
        payload: dict[str, Any] = {
            "datasetName": dataset_name,
            "id": item.id,
            "input": item.input,
        }
        if item.expected_output is not None:
            payload["expectedOutput"] = item.expected_output
        self._api("POST", "/dataset-items", payload)

    def get_dataset_items(self, dataset_name: str) -> list[dict]:
        """Return all items for a dataset (handles pagination)."""
        items: list[dict] = []
        page = 1
        while True:
            from urllib.parse import quote
            resp = self._api(
                "GET", f"/dataset-items?datasetName={quote(dataset_name)}&page={page}&limit=50")
            batch = resp.get("data", [])
            items.extend(batch)
            meta = resp.get("meta", {})
            if not batch or page >= meta.get("totalPages", page):
                break
            page += 1
        return items

    def link_run_item(
        self, *, run_name: str, item_id: str, trace_id: str,
        observation_id: str | None = None,
    ) -> None:
        """Attach a trace to a dataset item under a named run (the comparison row)."""
        body: dict[str, Any] = {
            "runName": run_name,
            "datasetItemId": item_id,
            "traceId": trace_id,
        }
        if observation_id:
            body["observationId"] = observation_id
        self._api("POST", "/dataset-run-items", body)

    def run_url(self, dataset_name: str) -> str:
        """Best-effort UI URL for the dataset (exact run path varies by version)."""
        return f"{self.base}/project/datasets"


# --------------------------------------------------------------------------- #
# LiteLLM calls (OpenAI-compatible; Langfuse callback traces them server-side) #
# --------------------------------------------------------------------------- #

@dataclass
class CallResult:
    ok: bool
    item_id: str
    model: str
    output: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float | None = None
    trace_id: str | None = None
    error: str | None = None


def call_model(
    *, litellm_url: str, api_key: str, model: str, item: EvalItem,
    dataset_name: str, run_name: str,
) -> CallResult:
    """Run one prompt through one model via LiteLLM.

    Sets metadata so LiteLLM's Langfuse callback links the resulting trace to the
    dataset run, and returns a deterministic trace id we also link via the
    Langfuse API (belt-and-suspenders, since callback metadata support varies by
    LiteLLM version).
    """
    # Deterministic trace id per (run, item) so we can link it explicitly later.
    trace_id = f"{run_name}-{item.id}".replace("/", "-")[:64]
    body = {
        "model": model,
        "messages": item.to_messages(),
        "temperature": 0.2,
        "max_tokens": 1024,
        # LiteLLM forwards `metadata` to its Langfuse callback. trace_name/tags
        # make the run easy to find in the UI; the explicit API link is the
        # source of truth for the dataset comparison.
        "metadata": {
            "trace_id": trace_id,
            "trace_name": f"compare:{run_name}",
            "generation_name": f"{model}:{item.id}",
            "tags": ["compare-models", dataset_name, run_name],
            "langfuse_dataset_name": dataset_name,
            "langfuse_dataset_item_id": item.id,
            "langfuse_dataset_run_name": run_name,
        },
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    started = time.monotonic()
    try:
        status, resp = _request(
            "POST", f"{litellm_url.rstrip('/')}/v1/chat/completions",
            headers=headers, body=body)
    except (HttpError, RuntimeError) as e:
        return CallResult(ok=False, item_id=item.id, model=model,
                          latency_s=time.monotonic() - started, error=str(e))

    latency = time.monotonic() - started
    try:
        output = resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return CallResult(ok=False, item_id=item.id, model=model, latency_s=latency,
                          error=f"unexpected response shape: {json.dumps(resp)[:300]}")
    usage = resp.get("usage", {}) or {}
    # LiteLLM surfaces per-call cost in a response header / hidden params on some
    # versions; usage tokens are always present. Cost is read from Langfuse
    # traces (authoritative) — we keep tokens here for the local summary.
    return CallResult(
        ok=True, item_id=item.id, model=model, output=output,
        prompt_tokens=int(usage.get("prompt_tokens", 0)),
        completion_tokens=int(usage.get("completion_tokens", 0)),
        latency_s=latency, trace_id=trace_id,
    )


# --------------------------------------------------------------------------- #
# Cost crossover — reuse recommend-instance.py's pricing/throughput model      #
# --------------------------------------------------------------------------- #

@dataclass
class Crossover:
    gpu_instance: str
    gpu_price_usd_h: float
    gpu_monthly_usd: float
    tuned_tokens_per_req: float
    tuned_cost_per_req: float          # at the break-even request volume
    baseline_cost_per_req: float       # frontier (e.g. Opus 4.8) per-request cost
    breakeven_reqs_per_day: float | None
    note: str = ""


def gpu_price_for_model(hf_model_id: str, region: str | None) -> dict | None:
    """Ask recommend-instance.py (subprocess, --json) for the cheapest GPU + price.

    Reuses the existing pricing/throughput model instead of duplicating it.
    Returns the `recommended` block or None on failure.
    """
    script = Path(__file__).with_name("recommend-instance.py")
    if not script.exists():
        return None
    cmd = [sys.executable, str(script), hf_model_id, "--json"]
    if region:
        cmd += ["--region", region]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=90).decode()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    try:
        payload = json.loads(out)
    except json.JSONDecodeError:
        return None
    return payload.get("recommended")


def compute_crossover(
    *, hf_model_id: str, region: str | None,
    avg_completion_tokens: float, baseline_cost_per_req: float,
) -> Crossover | None:
    """Compute the request-volume break-even between a self-hosted GPU node and
    per-request Bedrock pricing.

    A self-hosted model has a (roughly) fixed GPU $/hr regardless of volume; its
    per-request cost falls as volume rises. Bedrock is ~flat per request. The
    crossover is the daily volume above which self-hosting is cheaper per request.
    """
    rec = gpu_price_for_model(hf_model_id, region)
    if not rec or not rec.get("price_usd_h"):
        return None
    price_h = float(rec["price_usd_h"])
    tok_s = float(rec.get("single_stream_tok_s") or 0) or None

    # Effective requests/hour the node can serve, derived from throughput:
    #   reqs/hour = (tok/s * 3600) / tokens_per_req
    # Fall back to a conservative fixed throughput if the model didn't report it.
    tokens_per_req = max(avg_completion_tokens, 1.0)
    if tok_s:
        reqs_per_hour_capacity = (tok_s * 3600.0) / tokens_per_req
    else:
        reqs_per_hour_capacity = 0.0

    if baseline_cost_per_req <= 0 or reqs_per_hour_capacity <= 0:
        breakeven = None
        note = "insufficient data (no baseline cost or no throughput estimate)"
    else:
        # self-hosted $/req at volume V/day = price_h * 24 / V (node runs 24h).
        # break-even where that equals the baseline's per-request cost:
        #   price_h * 24 / V = baseline_cost_per_req  →  V = price_h * 24 / baseline_cost_per_req
        breakeven = (price_h * 24.0) / baseline_cost_per_req
        # Don't claim a break-even the node physically can't serve.
        max_per_day = reqs_per_hour_capacity * 24.0
        if breakeven > max_per_day:
            note = (f"break-even ({breakeven:,.0f}/day) exceeds one node's capacity "
                    f"(~{max_per_day:,.0f}/day) — add replicas or it never crosses on one node")
        else:
            note = (f"above ~{breakeven:,.0f} req/day the self-hosted model is "
                    f"cheaper per request than the frontier baseline")

    tuned_cost_per_req = (price_h * 24.0 / breakeven) if breakeven else 0.0
    return Crossover(
        gpu_instance=rec.get("instance", "?"),
        gpu_price_usd_h=price_h,
        gpu_monthly_usd=price_h * HOURS_PER_MONTH,
        tuned_tokens_per_req=tokens_per_req,
        tuned_cost_per_req=tuned_cost_per_req,
        baseline_cost_per_req=baseline_cost_per_req,
        breakeven_reqs_per_day=breakeven,
        note=note,
    )


# --------------------------------------------------------------------------- #
# Summary table                                                               #
# --------------------------------------------------------------------------- #

@dataclass
class ModelSummary:
    model: str
    n: int
    n_ok: int
    avg_prompt_tokens: float
    avg_completion_tokens: float
    p50_latency_s: float
    avg_latency_s: float


def summarize(model: str, results: list[CallResult]) -> ModelSummary:
    ok = [r for r in results if r.ok]
    lat = sorted(r.latency_s for r in ok)
    p50 = statistics.median(lat) if lat else 0.0
    return ModelSummary(
        model=model,
        n=len(results),
        n_ok=len(ok),
        avg_prompt_tokens=statistics.fmean(r.prompt_tokens for r in ok) if ok else 0.0,
        avg_completion_tokens=statistics.fmean(r.completion_tokens for r in ok) if ok else 0.0,
        p50_latency_s=p50,
        avg_latency_s=statistics.fmean(lat) if lat else 0.0,
    )


def print_summary(summaries: list[ModelSummary]) -> None:
    print("\n" + "=" * 78)
    print("SUMMARY (objective metrics — quality is judged in Langfuse)")
    print("=" * 78)
    hdr = f"{'model':<28}{'ok/n':>8}{'prompt tok':>12}{'compl tok':>12}{'p50 s':>9}"
    print(hdr)
    print("-" * 78)
    for s in summaries:
        print(f"{s.model:<28}{f'{s.n_ok}/{s.n}':>8}"
              f"{s.avg_prompt_tokens:>12.0f}{s.avg_completion_tokens:>12.0f}"
              f"{s.p50_latency_s:>9.2f}")
    print("-" * 78)
    print("Cost/latency/tokens per call are traced in Langfuse automatically.")
    print("Configure an LLM-as-judge evaluator + use the side-by-side view there")
    print("for voice/policy/helpfulness scoring and human preference.")


def print_crossover(c: Crossover) -> None:
    print("\n" + "=" * 78)
    print("COST CROSSOVER (self-hosted GPU $/hr vs Bedrock per-request)")
    print("=" * 78)
    print(f"  Cheapest GPU node : {c.gpu_instance}  "
          f"(${c.gpu_price_usd_h:.3f}/hr, ~${c.gpu_monthly_usd:,.0f}/mo)")
    print(f"  Avg completion    : {c.tuned_tokens_per_req:.0f} tokens/req")
    print(f"  Baseline cost/req : ${c.baseline_cost_per_req:.5f}")
    if c.breakeven_reqs_per_day:
        print(f"  Break-even        : ~{c.breakeven_reqs_per_day:,.0f} req/day")
    print(f"  → {c.note}")
    print("\n  (Pricing/throughput from ops/recommend-instance.py — no separate cost model.)")


# --------------------------------------------------------------------------- #
# Preflight                                                                   #
# --------------------------------------------------------------------------- #

def preflight(
    *, litellm_url: str, api_key: str, models: list[str],
    langfuse: Langfuse | None,
) -> bool:
    """Check connectivity + that each model answers. Print actionable fixes."""
    ok = True
    print("Preflight checks")
    print("-" * 40)

    # LiteLLM reachable + model list
    try:
        _, resp = _request("GET", f"{litellm_url.rstrip('/')}/v1/models",
                           headers={"Authorization": f"Bearer {api_key}"})
        available = {m.get("id") for m in resp.get("data", [])}
        print(f"[ok] LiteLLM reachable — {len(available)} models registered")
    except Exception as e:
        print(f"[FAIL] LiteLLM unreachable at {litellm_url}: {e}")
        print("       Fix: run ./ops/ssm-tunnel.sh, or pass --litellm-url.")
        return False

    for m in models:
        if m not in available:
            print(f"[warn] model '{m}' not in LiteLLM registry yet "
                  f"(deploy it, or it will 4xx).")

    # One tiny call per model — surfaces Bedrock-not-enabled (403) clearly.
    for m in models:
        item = EvalItem(id="preflight", input="Reply with the single word: ok")
        r = call_model(litellm_url=litellm_url, api_key=api_key, model=m,
                       item=item, dataset_name="preflight", run_name="preflight")
        if r.ok:
            print(f"[ok] {m} responded ({r.latency_s:.2f}s)")
        else:
            ok = False
            print(f"[FAIL] {m}: {r.error}")
            if "AccessDenied" in (r.error or "") or "403" in (r.error or "") \
                    or "not authorized" in (r.error or "").lower():
                print("       Fix (Bedrock): enable model access in the Bedrock console")
                print("       (Console → Bedrock → Model access) and confirm enable_bedrock=true.")

    # Langfuse
    if langfuse is not None:
        if langfuse.health():
            print("[ok] Langfuse reachable")
        else:
            print("[warn] Langfuse unreachable — results still run, but won't be traced/linked.")
    else:
        print("[warn] No Langfuse keys — comparison runs but isn't linked to a dataset run.")

    return ok


# --------------------------------------------------------------------------- #
# Main                                                                        #
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run an eval set through several models and log a Langfuse comparison.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dataset", type=Path,
                   help="JSONL eval file (required unless --preflight).")
    p.add_argument("--models", default=DEFAULT_MODELS,
                   help=f"Comma-separated LiteLLM model aliases (default: {DEFAULT_MODELS}).")
    p.add_argument("--langfuse-dataset", default="model-comparison",
                   help="Langfuse dataset name (default: model-comparison).")
    p.add_argument("--run-suffix", default="",
                   help="Appended to each run name to distinguish repeated runs.")
    p.add_argument("--litellm-url", default=DEFAULT_LITELLM_URL)
    p.add_argument("--litellm-key", default=None,
                   help="LiteLLM key (else LITELLM_MASTER_KEY env or kubectl secret).")
    p.add_argument("--langfuse-url", default=DEFAULT_LANGFUSE_URL)
    p.add_argument("--langfuse-public-key", default=None)
    p.add_argument("--langfuse-secret-key", default=None)
    p.add_argument("--no-langfuse", action="store_true",
                   help="Skip all Langfuse dataset/linking (still calls models).")
    p.add_argument("--preflight", action="store_true",
                   help="Only check connectivity + model access, then exit.")
    p.add_argument("--self-hosted-model", default=None,
                   help="Which --models alias is the self-hosted contender (for cost crossover).")
    p.add_argument("--self-hosted-hf-id", default=None,
                   help="HuggingFace id of the self-hosted base model (drives the GPU price lookup).")
    p.add_argument("--baseline-model", "--sonnet-model", dest="baseline_model",
                   default="claude-opus-4-8",
                   help="Which alias is the Bedrock frontier baseline "
                        "(default: claude-opus-4-8). --sonnet-model is a "
                        "deprecated alias.")
    p.add_argument("--baseline-cost-per-req", "--sonnet-cost-per-req",
                   dest="baseline_cost_per_req", type=float, default=None,
                   help="Override the baseline $/req for the crossover (else "
                        "estimated from tokens). --sonnet-cost-per-req is a "
                        "deprecated alias.")
    p.add_argument("--region", default=None,
                   help="AWS region for GPU pricing (default: recommend-instance.py autodetect).")
    return p.parse_args(argv)


# Rough Bedrock Claude Opus 4.8 pricing ($/1K tokens) used ONLY to estimate a
# per-request cost for the crossover when Langfuse cost isn't queried. These are
# the frontier-baseline list prices (~$15 / $75 per 1M tokens). Verify against
# current Bedrock pricing; override with --baseline-cost-per-req.
BASELINE_USD_PER_1K_INPUT = 0.015
BASELINE_USD_PER_1K_OUTPUT = 0.075


def estimate_baseline_cost_per_req(summary: ModelSummary | None) -> float:
    if not summary or summary.n_ok == 0:
        return 0.0
    return (summary.avg_prompt_tokens / 1000.0) * BASELINE_USD_PER_1K_INPUT \
        + (summary.avg_completion_tokens / 1000.0) * BASELINE_USD_PER_1K_OUTPUT


def run(args: argparse.Namespace) -> int:
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        sys.exit("ERROR: --models is empty")

    api_key = resolve_litellm_key(args.litellm_key)

    # Langfuse wiring (optional)
    langfuse: Langfuse | None = None
    if not args.no_langfuse:
        pub, sec = resolve_langfuse_keys(args.langfuse_public_key, args.langfuse_secret_key)
        if pub and sec:
            langfuse = Langfuse(args.langfuse_url, pub, sec)
        else:
            print("[warn] No Langfuse keys found — proceeding without dataset linking.\n"
                  "       (Set --langfuse-public-key/--langfuse-secret-key or env vars.)")

    if args.preflight:
        ok = preflight(litellm_url=args.litellm_url, api_key=api_key,
                       models=models, langfuse=langfuse)
        return 0 if ok else 1

    if not args.dataset:
        sys.exit("ERROR: --dataset is required (or use --preflight).")
    items = load_dataset(args.dataset)
    print(f"Loaded {len(items)} eval items from {args.dataset}")

    # 1. Upload dataset to Langfuse (idempotent)
    if langfuse:
        try:
            langfuse.ensure_dataset(args.langfuse_dataset)
            for it in items:
                langfuse.upsert_item(args.langfuse_dataset, it)
            print(f"Langfuse dataset '{args.langfuse_dataset}' synced "
                  f"({len(items)} items).")
        except (HttpError, RuntimeError) as e:
            print(f"[warn] Langfuse dataset upload failed: {e}\n"
                  "       Continuing without dataset linking.")
            langfuse = None

    # 2. Run each prompt through each model, link to a dataset run
    summaries: list[ModelSummary] = []
    per_model_results: dict[str, list[CallResult]] = {}
    suffix = f"-{args.run_suffix}" if args.run_suffix else ""
    for model in models:
        run_name = f"{model}{suffix}"
        print(f"\n>>> Running {len(items)} prompts through '{model}' "
              f"(run '{run_name}')")
        results: list[CallResult] = []
        for i, it in enumerate(items, start=1):
            r = call_model(litellm_url=args.litellm_url, api_key=api_key,
                           model=model, item=it,
                           dataset_name=args.langfuse_dataset, run_name=run_name)
            results.append(r)
            status = "ok" if r.ok else f"FAIL ({r.error})"
            print(f"  [{i}/{len(items)}] {it.id}: {status}")
            if r.ok and langfuse and r.trace_id:
                try:
                    langfuse.link_run_item(run_name=run_name, item_id=it.id,
                                           trace_id=r.trace_id)
                except (HttpError, RuntimeError) as e:
                    print(f"      [warn] link failed for {it.id}: {e}")
        per_model_results[model] = results
        summaries.append(summarize(model, results))

    # 3. Output
    print_summary(summaries)

    # Cost crossover (optional)
    if args.self_hosted_model and args.self_hosted_hf_id:
        baseline_summary = next(
            (s for s in summaries if s.model == args.baseline_model), None)
        baseline_cost = args.baseline_cost_per_req \
            if args.baseline_cost_per_req is not None \
            else estimate_baseline_cost_per_req(baseline_summary)
        self_summary = next(
            (s for s in summaries if s.model == args.self_hosted_model), None)
        avg_compl = self_summary.avg_completion_tokens if self_summary else 256.0
        c = compute_crossover(
            hf_model_id=args.self_hosted_hf_id, region=args.region,
            avg_completion_tokens=avg_compl, baseline_cost_per_req=baseline_cost)
        if c:
            print_crossover(c)
        else:
            print("\n[warn] Cost crossover unavailable "
                  "(recommend-instance.py lookup failed).")

    if langfuse:
        print(f"\nLangfuse comparison: {langfuse.run_url(args.langfuse_dataset)}")
        print("  Open the dataset → compare runs side by side; cost/latency are on each trace.")

    any_fail = any(not r.ok for rs in per_model_results.values() for r in rs)
    return 1 if any_fail else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
