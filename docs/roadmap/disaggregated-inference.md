# Disaggregated Prefill/Decode Inference (llm-d)

**Status**: **Implemented** (P/D routing live; validation/tuning remaining) · **Updated**: 2026-07-13
**Priority**: Medium — for long-context / high-concurrency workloads with ITL spikes
**Date added**: 2026-06-02

> **Current state (2026-07-13).** Shipped as the **`LLMDDisaggEndpoint`** RGD
> (`platform/config/kro/llmd-disagg-endpoint.yaml`) — not the `DisaggregatedEndpoint`
> name sketched below. Both prefill and decode pools deploy with NIXL KV transfer
> (`kv_role=kv_both`); the EPP uses `disagg-profile-handler` with a decode-side
> routing-sidecar for true prefill→decode routing; and dedicated `gpu-prefill` /
> `gpu-decode` Karpenter NodePools exist. **Remaining:** benchmark/validate the
> TTFT/throughput gain under load and tune the P/D split. The plan below is
> retained as design background.

## Motivation

The current Ray Serve + vLLM setup uses queue-depth routing but each replica runs
both prefill and decode on the same GPU. Under high concurrency, prefill (compute-bound,
bursty) steals cycles from decode (memory-bound, latency-sensitive), causing
inter-token latency spikes.

Disaggregation separates them into independently-scalable GPU pools with KV cache
transfer between them. Benefits:
- 2-5x throughput improvement under load (prefill doesn't stall decode)
- Prefix KV cache sharing across requests (huge win for repeated system prompts)
- Independent scaling per phase (right-size compute vs memory GPUs)

## Architecture

```
Request → llm-d Router (prefix + P/D aware) → Prefill Pool → KV transfer → Decode Pool
```

## Implementation Plan

### 1. Install llm-d (platform/services/llm-d/)

- Add llm-d Helm chart (CNCF Sandbox, v0.5+)
- Install CRDs: `InferencePool`, router config
- ArgoCD ApplicationSet entry

### 2. Karpenter NodePool for prefill vs decode (optional)

- Prefill: compute-optimized (A100/H100) — short bursts, high FLOPS
- Decode: memory-optimized (L4/A10G) — steady state, high VRAM/$ ratio
- Or same GPU class for both (simpler, still beneficial)

### 3. KRO ResourceGraphDefinition (platform/config/kro/disaggregated-endpoint.yaml)

User-facing spec:
```yaml
apiVersion: kro.run/v1alpha1
kind: DisaggregatedEndpoint
metadata:
  name: llama-70b
  namespace: inference
spec:
  model: "meta-llama/Llama-3-70B-Instruct"
  prefill:
    gpuCount: 2
    replicas: 1
    maxReplicas: 3
  decode:
    gpuCount: 4
    replicas: 2
    maxReplicas: 8
  shared: false
```

KRO expands into:
- Prefill `InferencePool` (scale by queued prefill requests)
- Decode `InferencePool` (scale by ongoing decode sessions)
- KV cache connector config (Redis or RDMA depending on topology)
- llm-d router with P/D-aware + prefix-cache-aware routing
- Service + LiteLLM model registration (same as InferenceEndpoint)

### 4. LiteLLM integration

- Register the llm-d router endpoint as the model in LiteLLM (same pattern as today)
- No change to API consumers — same `/v1/chat/completions` interface

### 5. Observability

- Prefill latency vs decode ITL as separate metrics
- KV cache hit rate (prefix reuse efficiency)
- Per-pool GPU utilization and scaling events

## Prerequisites

- llm-d CRDs installed on cluster
- vLLM with KV connector support (v0.6+, already past this)
- Karpenter NodePool supporting the chosen GPU types

## When to Trigger

Implement when any of these are true:
- Running ≥4 replicas of a single model
- Observing ITL p99 > 100ms under production load
- Heavy prefix reuse (RAG, tool-calling, shared system prompts)
- Cost pressure on large models (70B+) at high concurrency

## References

- [llm-d docs](https://llm-d.ai) (CNCF Sandbox, v0.5)
- [vLLM Router blog](https://vllm.ai/blog/vllm-router-release)
- [NVIDIA Dynamo](https://developer.nvidia.com/dynamo) (alternative, proprietary)
- [vLLM MORI-IO connector](https://blog.vllm.ai/blog/moriio-kv-connector)
