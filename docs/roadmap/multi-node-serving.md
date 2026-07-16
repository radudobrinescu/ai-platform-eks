# Multi-node Serving (models larger than one node)

**Status**: **Deferred** — not planned; single-node + quantization is the current stance · **Updated**: 2026-07-16
**Priority**: Low — narrow, high-cost niche
**Date added**: 2026-07-16

Every serving tier today (`VLLMEndpoint`, `LLMDEndpoint`, `LLMDDisaggEndpoint`)
deploys a single-node vLLM `Deployment` — one pod requesting up to a node's GPU
count. The largest node is **8× H200 = 1,128 GB**. A model that doesn't fit one
node can't be served, and `platformctl new-model` correctly says so (and points
at the int4 fit when there is one).

This doc records the decision **not** to add multi-node model sharding now, why,
and — if that changes — how to do it (LeaderWorkerSet, not Ray) and what the
recommender would need.

## Decision: keep single-node + quantization

The need is genuinely narrow. With quantization almost everything fits on one
8× H200 node:

| Model size | bf16 | int8 | int4 |
|---|---|---|---|
| ~405B | ~810 GB → fits | fits | fits |
| ~671B | ~1.3 TB → **needs >1 node** | ~670 GB → fits | ~340 GB → fits |
| ~950B (e.g. Inkling) | ~2.0 TB → **needs >1 node** | ~950 GB → fits | ~510 GB → fits |

So the *only* case that forces multi-node is **serving ~550B+ models at
full precision (bf16/fp16)** where quantization is unacceptable for quality — an
expensive, specialist niche (2× p5e ≈ $236/hr ≈ $170K/mo). For a self-service
platform that's a rounding error of use cases, and the recommender staying honest
("quantize or pick a smaller checkpoint") is the right default.

Multi-node is also costly and complex *regardless* of the mechanism: cross-node
tensor/pipeline parallelism needs EFA/InfiniBand or throughput collapses; you take
on gang scheduling, leader/worker coordination, all-or-nothing failure (one node
down → the whole model down), cross-node KV transfer, and the networking to match.
That's a large jump in operational surface for a platform that is otherwise clean
and single-node.

## If we ever do it: LeaderWorkerSet, not Ray

- **Ray is exactly the weight we shed.** The platform deliberately removed the
  Ray/kuberay serving stack (heavy runtime: Ray head/workers, dashboard,
  autoscaler, more failure modes). Reintroducing it for one narrow capability is a
  step backward. See the Ray retirement in the git history.
- **LeaderWorkerSet (LWS)** is the Kubernetes-native multi-node primitive that
  vLLM and llm-d are built around — a leader+workers group, gang-scheduled, no
  extra runtime. The right shape is a **multi-node variant on the existing llm-d
  tier** (which is already the "scale" path), kept opt-in and clearly flagged as
  high-cost / high-complexity — not a resurrected parallel serving engine.

## What the recommender would need

`ops/lib/recommend_instance` is single-node today: `_valid_parallelism_configs`
factorizes only `inst.num_gpus` (max 8), so the VRAM ceiling is one node. To
support multi-node it would need to:

1. Extend the search space from `tp × pp ≤ num_gpus` (one node) to
   `tp × pp = num_gpus × N` across **N nodes** (N as a bounded knob, e.g. `--nodes`).
2. Model the **cross-node penalty** — the throughput model already discounts PCIe
   vs NVLink; add an inter-node factor gated on EFA presence, so it never
   recommends multi-node without the interconnect to make it viable.
3. Emit an LWS-backed manifest (a new/extended `LLMDEndpoint` field, e.g.
   `nodes: N`), and require the EFA-enabled NodePools (`p5e`/`p5en`) it depends on.
4. Keep the honest guardrail: prefer quantization onto one node first; only escalate
   to multi-node when full precision on 550B+ is explicitly required.

## Revisit when

"Serve frontier open models at full precision, larger than one node" becomes a real
product requirement **and** EFA-enabled capacity + the ops maturity are in place.
Until then, single-node + quantization is the answer.
