# Elastic Serving — Autoscaling + Scale-to-Zero (KEDA)

**Status**: Designed — ready to implement
**Priority**: High (platform completeness pillar #1)
**Date added**: 2026-07-10
**Applies to**: the **llm-d tier** (`LLMDEndpoint`, `LLMDDisaggEndpoint`) — now the default
serving tier. vLLM/Ray are legacy and are out of scope for autoscaling.

## Goal
Make the llm-d tier elastic: scale replicas up under load, down when idle, and
(opt-in) all the way to **zero** — so a cluster runs many models economically.
Karpenter already scales GPU **nodes** to match replicas; this adds the missing
**replica** autoscaling that drives it.

## Design principle: saturation vs arrival
Two distinct signals, used for two distinct jobs:

- **Saturation (backpressure)** — `vllm:num_requests_waiting` (queue depth) and
  KV-cache utilization. Closed-loop: only rises when the current replicas can't
  keep up. **Used for right-sizing** (how many replicas). Self-calibrates to real
  per-model/per-GPU capacity — no brittle "requests-per-replica" assumption.
- **Arrival** — a per-model request-rate signal from the always-on **LiteLLM
  gateway**. Open-loop. **Used only to wake a pool from zero** (0→1), because at
  0 replicas there is no saturation to measure. Never used as a continuous
  sizing target.

Source: **saturation** comes from scraping the vLLM replicas directly
(`vllm:num_requests_waiting` by `llm-d.ai/pool` label). **Arrival** comes from
**LiteLLM**, not the EPP.

> **VALIDATED (2026-07-11): the EPP cannot be the wake-from-zero source.** The
> GIE EPP returns an immediate **503** for a request that arrives with zero ready
> endpoints and does **not** increment its request counter
> (`llm_d_epp_request_total` held flat across repeated 503s in a live test). A
> metric that never moves can't tell KEDA to wake. **LiteLLM sits upstream of the
> EPP and counts the request before it 503s** — verified live that a request to a
> zero'd pool moves `litellm_proxy_total_requests_metric_total{requested_model=<name>}`
> from 0. LiteLLM is also the uniform front door for *every* model, so this makes
> scale-to-zero signalling consistent rather than llm-d-specific. (The EPP's
> `llm_d_epp_*` gauges — queue size, KV-cache, ready endpoints — are still useful
> for the dashboard; scraping them needs the EPP SA granted `system:auth-delegator`
> so its metrics-endpoint TokenReview works.)

## Components

### 1. KEDA install
Enable via the `aws-ia/eks-blueprints-addons` module already used in
`terraform/30.eks/35.addons/main.tf` (same pattern as the ALB controller /
external-secrets / cert-manager):
```hcl
enable_keda = local.capabilities.autoscaling   # new capability flag, default true
keda = { values = [yamlencode({ tolerations = [local.critical_addons_tolerations.tolerations[0]] })] }
```
Cluster capability, provisioned with the cluster, no ArgoCD dependency, gated by a
capability flag. (Verify `enable_keda` exists in the pinned module `~>1.21`; else
bump or use the module's `helm_release` passthrough with `kedacore/keda`.)

### 2. Autoscaling Prometheus (small, always-on)
A lightweight Prometheus dedicated to autoscaling — **not** the optional
`40.observability` stack — so the default tier's elasticity never depends on
observability being enabled.
- Scope: scrape the `inference` namespace only — the EPP `/metrics` and/or vLLM
  replica `/metrics`, plus DCGM if useful.
- No Grafana/Alertmanager, minimal retention (KEDA only needs recent values).
- KEDA's **built-in Prometheus scaler queries it directly** (no prometheus-adapter).
- Distinct Service name/port to avoid confusion with the dashboard/EPP `:9090`.
- Deploy as a platform service (ArgoCD `platform/services/`) or a Terraform addon;
  lean toward a small ArgoCD-managed Helm release scoped to `inference`.

### 3. RGD + schema changes (`LLMDEndpoint`, `LLMDDisaggEndpoint`)
Re-introduce replica bounds (removed from vLLM) on the **llm-d** schema, and render
a KEDA `ScaledObject` per endpoint:
- `minReplicas` (integer, **may be 0** to opt into scale-to-zero; default 1)
- `maxReplicas` (default e.g. 4)
- `targetQueueDepth` (per-pod waiting-requests target; default ~25, tunable)
- optional `targetLatencyP95Seconds` (SLO guardrail)
- `scaleToZero` implied by `minReplicas: 0`
The RGD renders a `ScaledObject` targeting the model-server Deployment (label
`llm-d.ai/pool: <name>`). For **disaggregation**, render **two** ScaledObjects —
prefill and decode scale **independently** (a core disagg benefit): prefill is
compute-bound/bursty, decode is bandwidth-bound/steady.

### 4. ScaledObject shape (per AWS best practice, adapted)
```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
spec:
  scaleTargetRef: { name: <name>-llmd }        # the model-server Deployment
  minReplicaCount: ${minReplicas}               # 0 => scale-to-zero
  maxReplicaCount: ${maxReplicas}
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:   { stabilizationWindowSeconds: 30,  policies: [{type: Pods, value: 2, periodSeconds: 60}] }
        scaleDown: { stabilizationWindowSeconds: 300, policies: [{type: Pods, value: 1, periodSeconds: 120}] }
  triggers:
    # SATURATION — right-sizing (per-pod queue depth)
    - type: prometheus
      metricType: AverageValue
      metadata:
        serverAddress: http://<autoscaling-prometheus>.inference.svc:9090
        query: 'sum(vllm:num_requests_waiting{pool="<name>"}) or vector(0)'
        threshold: "${targetQueueDepth}"
        activationThreshold: "1"                # don't wake GPUs on a trickle
    # (optional) SATURATION guardrail — p95 latency
    # (v2) ARRIVAL — wake from zero, sourced from the always-on LiteLLM gateway
    - type: prometheus
      metadata:
        serverAddress: http://<autoscaling-prometheus>.inference.svc:9090
        query: 'sum(rate(litellm_proxy_total_requests_metric_total{requested_model="<name>"}[1m])) or vector(0)'
        threshold: "1000"                       # high => contributes at most ~1
        activationThreshold: "0"                # any arriving request wakes 0->1
```
Asymmetric behavior: responsive scale-up, conservative scale-down (GPU pods are
slow to start).

### 5. Scale-to-zero + wake-up (v2)
- `minReplicaCount: 0` drains the pool when idle; Karpenter then reclaims the GPU
  node → real cost savings.
- The **arrival trigger from the always-on LiteLLM gateway** wakes 0→1 on the
  first request. `requested_model` = the endpoint name, so it maps 1:1 to the pool.
  The high `threshold` means this trigger only ever asks for ~1 replica; the
  queue-depth trigger owns right-sizing above 1.
- Cold start ~1–2 min (Karpenter node + model load from the S3 cache). The
  triggering request is **held via LiteLLM retries** (`num_retries` +
  `request_timeout`) while the pool wakes — the EPP 503s an empty pool, so the
  retry (not the EPP) covers the gap: cold start becomes added latency, not an
  error. Opt-in per endpoint (`minReplicas: 0`).

### 6. Karpenter interaction (already works)
Replicas→0 empties the GPU node → Karpenter deprovisions. Replicas 0→N creates
pending pods → Karpenter provisions. No Karpenter change needed; the multi-GPU
NodePool fix already lets tp>1 replicas schedule.

## Verification gate (before finalizing trigger queries)
Deploy one `LLMDEndpoint`, then inspect the **EPP `/metrics`** to confirm the exact
metric names + labels for: (a) pool-aggregated queue depth / KV utilization
(saturation), and (b) request count/rate (arrival). Finalize the PromQL against
those. If saturation isn't exposed pool-aggregated, scrape vLLM replicas directly.

## Rollout stages
1. **Install** — KEDA (35.addons) + the autoscaling Prometheus. Verify KEDA +
   Prometheus healthy, Prometheus scraping the inference ns.
2. **Verification gate** — deploy one LLMDEndpoint; capture EPP/vLLM metric names.
3. **v1 (min≥1)** — schema fields + RGD renders the saturation ScaledObject;
   GPU load-test to confirm scale 1↔N and Karpenter node follow.
4. **v2 (scale-to-zero)** — add the EPP arrival wake-up trigger + first-request
   hold/retry; confirm 0→1→0 and node reclaim.
5. **Recommender + dashboard** — `recommend-instance` emits min/max; dashboard
   shows desired-vs-current replicas + "scaled to zero" state.

## Consistency with the platform
- Install via the existing addons module (like ALB controller) — no new pattern.
- Autoscaling behavior lives in the KRO RGD (the platform's API), rendered per
  endpoint — same self-service model as everything else.
- Gated by a capability flag; degrades cleanly if disabled (fixed replicas).
