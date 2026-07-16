# Platform Roadmap — High-Value Improvements

**Status**: Living backlog · **Updated**: 2026-07-13

This is a prioritized backlog of improvements that would significantly increase
the platform's value, ordered by **value-per-effort**. The platform today is
strong on serving (Bedrock / vLLM / llm-d tiers behind one LiteLLM API),
governance (per-team keys, budgets, rate limits), **elastic autoscaling (KEDA v1,
shipped for the llm-d tier)**, cost recommendation (`ops/recommend_instance`), and
observability basics (cluster dashboard + Platform Health Agent + Langfuse). The
gaps that most limit real-world value are now **retrieval, safety guardrails, and
pre-flight validation**.

Individual initiatives may graduate into their own doc under `docs/roadmap/`:
`elastic-serving-autoscaling.md`, `disaggregated-inference.md`,
`semantic-routing.md`.

---

## Tier 1 — Highest leverage

### 1. Autoscaling with scale-to-zero (KEDA)
**Status**: **v1 SHIPPED + validated live** (elastic 1↔N, saturation-driven) ·
scale-to-zero (v2) **PARKED** · graduated to `elastic-serving-autoscaling.md` ·
**Priority**: High · **Effort**: Medium

**Why.** Idle models burn GPU 24/7 and busy ones can't absorb spikes. This is the
single biggest cost and capability lever — it's what makes running many team
models economically viable.

**Shipped (v1).** The llm-d tier (`LLMDEndpoint`/`LLMDDisaggEndpoint`) scales
replicas 1↔N on real saturation signals (queue depth / KV-cache / pending
requests) via a KEDA `ScaledObject` per endpoint. **Scale-to-zero (v2) is parked**
— the EPP arrival-signal path didn't pan out; see the graduated doc §5 for the
LiteLLM-based unpark checklist.

**Approach.**
- Add KEDA as a Terraform EKS addon (`terraform/30.eks/35.addons`).
- Scale on **real signals** the serving tiers already expose: vLLM / GIE
  queue depth, KV-cache utilization, pending requests (KEDA Prometheus/metrics
  scaler).
- **Scale-to-zero** for idle endpoints (activator/HTTP-add-on or a queue-length
  trigger), with a warm-start path (EBS image snapshot + SOCI + S3 weight cache
  already cut cold start to ~15s).
- This is the natural home for the `minReplicas`/`maxReplicas` semantics that
  were removed from `VLLMEndpoint` (see `platform/config/kro/vllm-endpoint.yaml`):
  expose `minReplicaCount`/`maxReplicaCount` on `LLMDEndpoint` (and optionally
  `VLLMEndpoint`) and wire a KEDA `ScaledObject` per endpoint in the RGD.

### 2. RAG as a first-class resource (`KnowledgeBase` RGD + embeddings tier)
**Status**: Proposed · **Priority**: High · **Effort**: Large

**Why.** Most enterprise LLM usage is retrieval-augmented, not raw chat, and the
platform has no retrieval layer. Teams can serve a model but can't ground it on
their data without leaving the platform.

**Approach.**
- New `KnowledgeBase` KRO RGD: ingest an S3 prefix → chunk/embed → vector store
  (pgvector, OpenSearch, or Amazon Bedrock Knowledge Bases).
- Serve **embedding + reranker models** through the same self-service loop
  (vLLM supports both) — a new `taskType`/kind or a flag on the serving RGDs.
- Wire retrieval into the gateway (a LiteLLM pre-call hook or a thin retrieval
  proxy) so `KnowledgeBase` + model compose behind the unified `/v1` API.

### 3. Gateway guardrails (safety / compliance)
**Status**: Proposed · **Priority**: High · **Effort**: Medium

**Why.** Content moderation, PII redaction, prompt-injection defense, and full
prompt/response audit are frequently a *hard gate* to enterprise adoption, not a
nice-to-have.

**Approach.**
- LiteLLM guardrail hooks + Amazon Bedrock Guardrails integration (moderation,
  PII, denied topics) applied at the gateway so every tier inherits them.
- Formalize prompt/response audit (Langfuse already traces; add retention +
  per-team access controls).
- Optional per-team guardrail policy on the `AITeam` resource.

---

## Tier 2 — Strong

### 4. First-class evaluation & quality gates
**Status**: Proposed · **Priority**: High · **Effort**: Medium

**Why.** The platform serves many models (Bedrock + self-hosted vLLM/llm-d) but
has no systematic way to compare their quality or catch regressions before a
model deploys. Make model evaluation rigorous and repeatable.

**Approach.**
- Scheduled evals against golden datasets, results tracked as Langfuse dataset
  runs.
- **Regression gates in CI / code review**: a model can't deploy if it regresses
  on its golden set.
- Per-model quality + drift dashboards; surface cost/quality tradeoffs automatically.

### 5. Pre-merge validation (admission + dry-run)
**Status**: Proposed · **Priority**: High · **Effort**: Medium

**Why.** Several failures this cycle only surfaced at runtime — a `gpuCount>1`
pod that no NodePool could schedule, gated models without a token, a CRD
migration that broke rendering. Catch these before merge.

**Approach.**
- Validating admission (Kyverno/OPA) on the serving CRs: reject unschedulable
  GPU configs, missing gated-model tokens, bad field combinations.
- A `recommend-instance`-style **schedulability + cost preview posted on the PR**
  (dry-run against live NodePool constraints + the pricing catalog).

### 6. Progressive delivery for models
**Status**: Proposed · **Priority**: Medium · **Effort**: Medium

**Why.** A bad model deploy can take live traffic. Roll out safely.

**Approach.**
- Canary / weighted traffic shifting via LiteLLM model weights.
- **Health-gated registration**: only expose a model in the gateway after
  readiness *and* a smoke test pass (extends the idempotent-registration job in
  `platform/config/kro/vllm-endpoint.yaml`).

---

## Tier 3 — Round out

### 7. llm-d prefill/decode disaggregation routing
**Status**: **Implemented** — true P/D routing live via `disagg-profile-handler` +
decode routing-sidecar; prefill/decode NodePools · **validation/tuning remaining** ·
**Priority**: Medium · **Effort**: Medium.
See **`docs/roadmap/disaggregated-inference.md`**.

The `LLMDDisaggEndpoint` RGD deploys both pools with NIXL KV transfer, and the EPP
now uses `disagg-profile-handler` (true prefill→decode routing) rather than the
earlier pooled `single-profile-handler`. Remaining: benchmark/validate the
TTFT/throughput benefit under long-context / agentic load and tune the P/D split.

### 8. Batch / async inference tier
**Status**: Proposed · **Priority**: Medium · **Effort**: Medium

A queue-backed offline inference API on spot GPUs (no latency SLO) for evals,
data labeling, and bulk processing — much cheaper than the online path.

### 9. Deeper multi-tenancy
**Status**: Proposed · **Priority**: Medium · **Effort**: Medium

Back the existing per-team budgets/rate-limits with real capacity: GPU
`ResourceQuota` per team namespace + priority/preemption classes so critical
workloads win under contention.

### 10. SLO + cost-efficiency observability
**Status**: Proposed · **Priority**: Medium · **Effort**: Small–Medium

Add a cost/efficiency lens to the dashboard: p95 TTFT/TPOT per model and
**tokens-per-dollar** per model/team. The inputs already exist (DCGM, the
LiteLLM DB, the pricing catalog) — they just aren't combined into an efficiency
view.

---

### 11. Semantic routing (`auto` cost-efficiency layer)
**Status**: Proposed · **evidence-gated** · **Priority**: Medium (secondary lever) ·
**Effort**: Medium–High.
See **`docs/roadmap/semantic-routing.md`**.

An opt-in `model: auto` endpoint (vLLM Semantic Router) that routes easy queries
to cheap models and escalates only when needed, plus semantic caching. Worth
building only as a staged, evidence-gated component — **gate on measuring the
actual savings opportunity from Langfuse traffic first** (Phase 0), and confirm
per-team cost attribution. Secondary to Karpenter scale-to-zero + right-sizing.

## Suggested sequencing

**Autoscaling v1 has shipped**, so the highest-leverage *unbuilt* work is now:
**(1) RAG + embeddings** (applicability), **(2) guardrails** (adoptability), and
**(3) first-class evaluation & quality gates** (trust) — these change *who can use
the platform and for what*. Guardrails (#3) and evaluation (#4) also unblock the
semantic-routing `auto` layer (shared PII/jailbreak machinery and the Langfuse
eval tooling its Phase 0 needs). The remaining items optimize an already-working
system.
