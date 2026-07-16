# AI Platform on EKS — Product Report

**Purpose:** A single product view of the platform — functionality, customer benefits,
prioritization, implementation roadmap, target architecture, and the responsibility/isolation
model that separates what AWS provides, what the platform brings, and what each team deploys.

**Date:** 2026-07-08
**Status of the live reference cluster:** EKS **1.36** / Karpenter **1.13**.
**Note:** This document references only launched, publicly-available features and is safe to commit.
It consolidates `docs/enterprise-ai-platform-strategy.md` with the internal analysis plan.

---

## 1. What the platform is

**In one sentence:** a GitOps-native AI-platform *distribution* for Amazon EKS — one
OpenAI-compatible API in front of every model (Bedrock, open-source, and your fine-tuned ones),
where teams ship models the way they ship code: commit a short YAML, `git push`, and the platform
provisions the GPU, serves the model, routes to it, governs it, and traces it.

**The product is not the components** (vLLM, LiteLLM, Langfuse, Karpenter all exist).
**The product is three things:**

1. the **KRO self-service API** (a few CRDs that hide the hard parts),
2. the **opinionated, tested integration** of best-of-breed OSS + AWS-native services, and
3. the **operational surface** (GitOps, observability, cost, health).

---

## 2. Functionality & feature inventory

Grouped by capability domain. Legend: ✅ built today · 🔶 partial/gap · 🎯 roadmap.

### A. Model serving & access
- ✅ One OpenAI-compatible endpoint (`/v1/chat/completions`, `/v1/embeddings`) fronting **every** model via LiteLLM.
- ✅ **Frontier model on day one, zero GPUs** — Bedrock (Claude Opus 4.8) live the moment the cluster is up.
- ✅ Self-hosted open models on GPUs via vLLM, tensor-parallel 1/2/4/8.
- ✅ GPU time-slicing (one physical GPU → up to 4 small models) for cost.
- 🔶 Model-aware routing — today LiteLLM round-robins; 🎯 **GIE** adds KV-cache/queue/LoRA-aware pod picking.
- 🎯 Embeddings & reranker endpoints; multi-LoRA (many adapters on one base GPU); optional
  multi-node for very large models; Neuron (Trainium/Inferentia).

### B. Self-service & lifecycle (the API)
- ✅ `VLLMEndpoint` — serve any HuggingFace model or S3-hosted fine-tune from a few lines of YAML (the simple default).
- ✅ `LLMDEndpoint` / `LLMDDisaggEndpoint` — the llm-d scale tier and its prefill/decode-disaggregated performance tier.
- ✅ `AITeam` — onboard a tenant: namespace, RBAC, quota, network policy, scoped API key, budget, rate limits.
- 🎯 `EmbeddingEndpoint`, `RerankerEndpoint`, `VectorDatabase`, `RAGPipeline`, `AgentRuntime` — the
  catalog that turns it from "one demo" into "a platform."
- ⛔ **Out of scope:** fine-tuning/training. The platform *serves* models (including externally
  fine-tuned ones via `modelSource`), it does not train them (tenet: scope discipline).

### C. Cost & efficiency
- ✅ Scale-to-zero idle GPUs (Karpenter), spot + on-demand, right-sizing.
- 🎯 Reserved-fleet story (StaticCapacity / ODCR / Capacity Blocks), per-team GPU chargeback.

### D. Governance, security & multi-tenancy
- ✅ Per-team keys, budgets, rate limits, quotas via LiteLLM + `AITeam`.
- 🔶 **Tenant isolation is bypassable today** (pods can hit vLLM directly, skipping budgets/tracing) — a top fix.
- 🔶 Perimeter is plain-HTTP, the dashboard is unauthenticated — must harden before any pilot.
- 🎯 Guardrails (PII/prompt-injection/content), hard network isolation, hierarchical quotas.

### E. Observability
- ✅ **Turnkey tracing** — Langfuse live on first boot (no setup), every call traced with cost/latency.
- 🔶 GPU metrics (DCGM) are *emitted but not scraped*; zero alert rules — a Phase-0 fix.
- 🎯 Per-model/per-team GPU dashboards + cost attribution + alerting.

### F. Operations & Day-2
- ✅ Cold-start engineering (EBS image snapshot + SOCI + S3 weight cache) → ~15s vs ~60s loads.
- ✅ `platformctl` ops CLI (up/status[--check]/tunnel/edge/new-model/down), instance recommender, Platform Health
  Agent (LLM-assisted incident triage with human-approved fixes).
- ✅ GitOps everything (ArgoCD prune + self-heal + server-side apply).

---

## 3. Customer benefits (feature → value)

| Persona | What they get | Why it matters |
|---|---|---|
| **Developer / data-science team** | `git push` a YAML → a live, governed model endpoint. Same OpenAI API for Bedrock, open, and fine-tuned models. | Ship AI features in minutes without touching GPUs, Helm, or infra. No new SDK. |
| **Platform / infra team** | One opinionated, GitOps-native distribution; AWS runs the control plane, ArgoCD/KRO/ACK, and increasingly the compute. | Operate an AI platform without building one; ride the AWS roadmap instead of maintaining custom code. |
| **Finance / FinOps** | Scale-to-zero, GPU time-slicing, and per-team budgets. | Turn GPU spend into a governed, attributable, optimizable line item. |
| **Security / compliance** | Everything runs **in your AWS account and VPC**; per-team isolation, keys, and audit; CNCF/OSS components (no lock-in). | Data sovereignty and control — the core reason this segment self-hosts. |
| **Business / product** | Frontier quality on day one (Bedrock), then a path to cheaper tuned models for narrow tasks, plus RAG and agents as first-class. | Start delivering value immediately; reduce unit cost as usage scales. |

**Consolidated value proposition:** *AI infrastructure you own and control, with the self-service speed
of a SaaS, the cost discipline of scale-to-zero + tuned small models, and no vendor lock-in.*

---

## 4. Prioritization

Prioritized on **"what gates a sale"**, not raw technical interest. Three tiers:

### Tier 1 — Table stakes (no customer runs without these)
*A demo becomes a product here.*
- Security & tenant isolation that actually holds (TLS, auth, no vLLM bypass), HA + backups, real
  observability + alerts. **(Phase 0 — blocking.)**
- One governed API with per-team budgets/keys ✅, and **model-aware routing (GIE)** so scaling doesn't
  degrade latency.

### Tier 2 — Differentiators (why choose this over a SaaS or rolling your own)
*The moat.*
- The **KRO self-service catalog** (git-push-to-model) + GitOps ✅ foundation.
- **RAG blueprint** (the #1 enterprise pattern) + embeddings/rerankers + vector DB.
- **Serve any model** — day-one Bedrock ✅; self-hosted vLLM + the llm-d scale tier ✅; serve your own fine-tuned weights (`modelSource`) ✅.
- **Dual serving** (simple single-node vLLM → llm-d scale path).

### Tier 3 — Expansion (land-and-expand, enterprise depth)
*Grows account value.*
- Agents + guardrails; multi-node / very-large models; disaggregated inference; Neuron; hybrid Bedrock
  (tune on EKS → import to Bedrock); model lifecycle/canary; regulated-industry (FIPS/GovCloud/air-gapped).

**Guiding principle (from both source docs):** *ride the AWS roadmap, delete custom code.* Anything
AWS/Karpenter now ships natively (warm pools, cold-start, GPU attribution, reserved capacity) is adopted,
not rebuilt — freeing effort for Tier 2 (the catalog), which is where the differentiation lives.

---

## 5. Roadmap (benefit-driven)

Sequenced so a **credible pilot is possible after Phase 1**, and each later phase adds a saleable tier.
Effort: ~17–24 engineering-weeks solo; ~12–14 with two engineers (phases 2+ parallelize).

| Phase | Delivers (benefit) | Tier | Key work |
|---|---|---|---|
| **0 — Production floor** *(2–3 wk, blocking)* | "Safe to put a customer on it" | 1 | TLS + auth; **kill the vLLM bypass**; Postgres backup/restore + LiteLLM HA; DCGM→dashboards + ~10 alerts. **EKS 1.36 + Karpenter 1.13 done** (Phase-0 prerequisite). |
| **1 — Standards routing + storage** *(2–3 wk)* | Scales without latency cliffs → **pilot-ready** | 1→2 | Envoy AI Gateway + GIE (InferencePool/EPP) on NLB; LiteLLM stays the control plane; Mountpoint-S3 CSI for weights. |
| **2 — Serving catalog + RAG** *(4–6 wk)* | The enterprise entry drug, self-service | 2 | `EmbeddingEndpoint`, `RerankerEndpoint`, `VectorDatabase`, `RAGPipeline`; multi-LoRA; seeded model catalog + CI. |
| **3 — Scale path (llm-d)** *(3–4 wk)* | Very-large / high-concurrency serving | 3 | `servingMode` enum → disaggregated prefill/decode on GIE + EFA. *(Optional: multi-node LWS + Kueue — see decision below.)* |
| **4 — Agents + guardrails** *(3–4 wk)* | Full enterprise AI-platform story | 3 | `AgentRuntime` RGD (MCP tools, session, scoped budget, egress-locked) + guardrails. |
| **5 — Polish (continuous)** | Regulated & GTM readiness | 3 | Neuron; hybrid Bedrock; lifecycle/canary; DRA-readiness; delete custom code as AWS ships equivalents. |

**Pilot-readiness gates:** after **Phase 1** → secure, standards-routed platform (sell a pilot);
after **Phase 2** → differentiated catalog (sell the platform); after **Phase 4** → full enterprise story.

**Open decision to ratify:** whether multi-node / large-model serving (LWS + Kueue) is in scope. This is
the only place the two source strategies genuinely diverge on *what to build*. Recommendation: keep it out
of the critical path; add it as an optional enterprise tier only if customers demand >1-node models.

---

## 6. Target architecture

```
                          CONTROL PLANE (governance)  — "who, how much, which model"
  client -> NLB -> Envoy   +----------------------------------------------------------+
           [TLS,OIDC]  |   | LiteLLM: one OpenAI API - per-team keys/budgets/limits -   |
                       +-> |          Bedrock passthrough - Langfuse tracing - Guardrails|
                           +---------------+---------------------------+----------------+
                                self-hosted |                  managed  | (Bedrock / AgentCore)
                          DATA PLANE (model-aware routing)              v
                           +----------------------------------------------+
                           | Gateway API Inference Extension: InferencePool|
                           | + Endpoint Picker (KV-cache/queue/LoRA-aware) |
                           +---+---------------+---------------+-----------+
                     +---------v------+ +-------v--------+ +----v--------------+
                     | single vLLM    | | multi-node LWS | | llm-d disaggregated|
                     | (simple tier)  | | (optional tier)| | prefill/decode     |
                     +---------+------+ +-------+--------+ +----+--------------+
                               +----------------+---------------+
                                     Karpenter GPU NodePools
                        (Ampere+ - time-sliced - reserved/ODCR/Capacity Blocks - EFA - DRA-ready)

  TENANT SELF-SERVICE (KRO RGDs):  VLLMEndpoint - LLMDEndpoint - LLMDDisaggEndpoint - AITeam
                                   EmbeddingEndpoint - RerankerEndpoint - VectorDatabase - RAGPipeline - AgentRuntime  (future)
  STORAGE:  Mountpoint-S3 (weights) - S3 (model cache) - pgvector/RDS/OpenSearch/S3-Vectors - FSx Lustre (opt)
  OBSERVABILITY:  Langfuse (LLM) - DCGM+AMP/Grafana or OTel Container Insights (GPU/cost) - alerts->SNS
```

**Design tenets:** config over code; the CRDs are the API (fork & extend); GitOps everything; one
opinionated default per capability with expert-editable tfvars; lean on built-ins — differentiate only in
the composition + catalog.

---

## 7. The line of isolation — who owns what

There are **two** boundaries: (A) the **responsibility layers** (AWS <-> platform <-> tenant), and
(B) **tenant-to-tenant isolation** (how one team is walled off from another).

### 7A. Responsibility layers (shared-responsibility model)

```
+---------------------------------------------------------------------------+
| LAYER 3 - TENANT (self-service, via KRO YAML in Git)                      |  <- the customer's teams
|   VLLMEndpoint - LLMDEndpoint - LLMDDisaggEndpoint - AITeam                |
|   EmbeddingEndpoint - RerankerEndpoint - RAGPipeline - AgentRuntime (future) |
+---------------------------------------------------------------------------+
| LAYER 2 - PLATFORM (this product; installed once by the platform team)    |  <- "the distribution"
|   KRO RGD definitions (the API) - LiteLLM - GIE gateway - llm-d - Langfuse |
|   Karpenter NodePools + GPU Operator cfg - cold-start automation - CI -    |
|   observability wiring - security baseline - Terraform - platformctl      |
+---------------------------------------------------------------------------+
| LAYER 1 - AWS (out of the box; you consume, don't build)                  |  <- the substrate
|   EKS control plane - Karpenter compute - EKS Managed Capabilities        |
|   (ArgoCD/KRO/ACK) - managed addons (VPC-CNI/CoreDNS/EBS/EFS/FSx/          |
|   Mountpoint-S3 CSI) - Bedrock - AMP/CloudWatch - S3/S3-Vectors - IAM/     |
|   IRSA/Pod-Identity - ACM - NLB/ALB - GPU AMIs (Bottlerocket)             |
+---------------------------------------------------------------------------+
```

Concern-by-concern (where the line sits):

| Concern | AWS provides (L1) | Platform brings (L2) | Tenant deploys (L3) |
|---|---|---|---|
| **Compute** | EKS, Karpenter engine, GPU AMIs, capacity reservations | NodePools (gpu-inference/gpu-shared/reserved), time-slicing, cold-start | picks `gpuCount`/`shared` in a `VLLMEndpoint` |
| **Cluster ops** | ArgoCD/KRO/ACK as managed capabilities | ApplicationSets, RGD catalog, GitOps wiring | commits YAML to their path in Git |
| **Model access** | Bedrock models | LiteLLM (one API), GIE routing, model registration | names a model; gets a scoped key |
| **Serving** | — | vLLM/llm-d integration + serving modes | chooses `servingMode`, replicas |
| **Data / weights** | S3, FSx, CSIs, S3 Vectors | `modelSource` plumbing, weight cache, `VectorDatabase` RGD | points at an HF ID / S3 URI / dataset |
| **Networking edge** | NLB/ALB, ACM, VPC | TLS, gateway, HTTPRoutes, IP allowlist | nothing (consumes the shared endpoint) |
| **Identity** | IAM, IRSA, Pod Identity, Identity Center | role wiring, per-team RBAC/keys | uses their team's key/namespace |
| **Observability** | AMP, CloudWatch, X-Ray | Langfuse, DCGM scrape, dashboards, alerts | reads their traces/dashboards |
| **Cost** | billing, Karpenter right-sizing | budgets, time-slicing, chargeback | sets a team budget/limits |
| **Security controls** | KMS, SGs, network policy engine | baseline (TLS/auth/isolation), guardrails | inherits; can tighten within their ns |

**The clean mental model:**
- **AWS = the substrate** you rent (control plane, compute, managed capabilities, Bedrock, storage,
  identity). The platform never rebuilds these — and actively *deletes* custom code as AWS ships equivalents.
- **The platform = the paved road** (installed once by the platform team): the KRO RGDs *are* the API,
  plus the governed edge, routing, observability, and security baseline.
- **The tenant = the traffic on the road** (self-service): they only ever write short KRO YAML and app
  config; they never touch Terraform, Helm, IAM, or GPUs.

### 7B. Tenant-to-tenant isolation (how teams are walled off)

`AITeam` is the isolation unit. Each team gets:
- **Namespace + RBAC** (can only act in their namespace),
- **ResourceQuota + per-NodePool GPU caps** (blast-radius limit),
- **NetworkPolicy** (today too loose; Phase 0 tightens egress to *LiteLLM only*, closing the vLLM-bypass hole),
- **Scoped LiteLLM key + budget + rate limits** (spend and throughput bounded),
- **Optional per-team Langfuse project** (trace isolation).

**Honest status:** the *structure* for tenant isolation exists and is well-designed, but it is **not yet
enforced** at production strength (the direct-to-vLLM bypass defeats budgets/tracing; the perimeter is
plain-HTTP). Making this boundary real is exactly **Phase 0** — and it is the difference between a
"multi-tenant demo" and a "multi-tenant product."

---

## 8. Bottom line

- **Functionality/benefits:** the platform already delivers the headline value (one governed API, day-one
  frontier, git-push model serving, cost proof, turnkey tracing). The gaps are **production hardening** and
  the **breadth of the self-service catalog**.
- **Priority:** harden first (Tier 1 / Phase 0), then GIE routing, then the RAG + catalog differentiators
  (Tier 2), then scale/agents (Tier 3).
- **Architecture:** two-tier (LiteLLM control plane + GIE data plane) over a serving-mode-flexible KRO
  catalog on Karpenter GPUs.
- **Isolation:** three responsibility layers (AWS substrate -> platform paved-road -> tenant self-service
  via KRO), with `AITeam` as the tenant-to-tenant boundary that Phase 0 must make truly enforceable.

---

*Companion documents: `docs/enterprise-ai-platform-strategy.md` (fuller technical strategy),
`docs/platform-evolution-plan.md` (original money-demo scope), `docs/roadmap/disaggregated-inference.md`
(folded into the scale phase).*
