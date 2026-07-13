# Semantic Routing — `auto` Cost-Efficiency Layer (vLLM Semantic Router)

**Status**: Proposed · **evidence-gated** (do not build the full path on spec) ·
**Priority**: Medium (secondary cost lever) · **Effort**: Medium–High
**Date added**: 2026-07-13

## TL;DR / verdict

An **optional, opt-in** `model: auto` endpoint — backed by
[vLLM Semantic Router (vSR)](https://github.com/vllm-project/semantic-router) —
that classifies each request and routes easy queries to a cheap model,
escalating to a frontier model only when the prompt warrants it, plus semantic
caching. Teams point *specific workloads* at it; all other traffic is untouched.

**It is worth building only as a staged, evidence-gated component.** The value is
real but strictly conditional on workload shape, and the complexity is
concentrated in the governance integration (per-team attribution), which is the
least-proven part. **Gate the build on a cheap measurement of the actual savings
opportunity from existing Langfuse traffic**, and confirm per-team attribution
before investing in the full governed path. Our primary cost levers (Karpenter
scale-to-zero + right-sizing) are bigger; this is secondary.

## What it is

vSR is an **Envoy External Processor (ExtProc)** — a CPU service (no GPU) that,
per request, extracts signals (domain, embedding, and optionally PII / jailbreak),
selects the best-fit model from a configured pool, and can inject system prompts
and serve a semantic cache. It is itself OpenAI-compatible and has its own
`providers` backend config. v0.1 "Iris" (Jan 2026) moved from a fixed 14-category
classifier to a signal-decision plugin chain. It is young and fast-moving.

## How it fits the platform (governance-preserving topology)

Three complementary layers — no component is duplicated:

- **vSR = decision** ("which model/tier?") + semantic cache (+ safety, later).
- **LiteLLM = execution + governance** (keys, budgets, rate limits, Langfuse
  tracing, provider routing to Bedrock or self-hosted). **Unchanged.**
- **llm-d EPP = replica scheduling** (KV-cache / load-aware) within the chosen
  model's InferencePool. **Unchanged.**

To preserve our core invariant (*every model answers through the one governed
LiteLLM `/v1` front door*), vSR is configured with **exactly one backend:
LiteLLM**. Request flow for an opt-in workload:

```
client → LiteLLM(model: auto, team-key) → vSR (classify → pick tier model)
       → LiteLLM(chosen-model, team-key) → { Bedrock | llm-d EPP → vLLM }
```

vSR **cannot escalate privilege**: the real model call goes back through LiteLLM
under the team key, so access/budget are enforced there regardless of what vSR
picks. Worst case of a misconfig is a clean 403 (failed request), never a
governance breach.

**Explicitly excluded:** Bifrost (LiteLLM already is our provider-agnostic
execution layer; adding Bifrost would duplicate it). The "LiteLLM pre-invoke hook
calling vSR" pattern is **not** vSR — that describes the lighter, embedding-only
`aurelio-labs/semantic-router` library, which lacks vSR's classifiers / guards /
cache. If we ever want a zero-new-gateway option, that library is the separate
tool to consider — but it is not this feature.

## Locked design decisions

1. **Opt-in parallel endpoint** — only workloads that ask for it traverse vSR;
   existing direct-model calls are unaffected (zero latency/impact on them).
2. **`auto` is a separate LiteLLM model** whose backend is vSR.
3. **Cost-only for v1** — model selection + semantic cache. PII/jailbreak guards
   deferred (they belong with the Gateway Guardrails initiative).
4. **In-memory semantic cache** for v1 (no new Redis/Milvus dependency).
5. **Admin-owned routing policy**, expressed as **compliance-aligned profiles**
   (see below), not a single universal tier set.

## Enterprise usage model (how real teams use it)

`auto` is **not** a platform-wide default. Teams split traffic:

- **Pin explicit models** for production-critical, compliance/eval-gated, or
  determinism-sensitive paths (reproducibility, audit, SLAs).
- **Use `auto`** for high-volume, cost-sensitive, variance-tolerant workloads:
  internal assistants, drafting, triage/classification, batch enrichment,
  dev/test, and the **cheap sub-steps of agentic pipelines** (the sweet spot).

**Access ≈ compliance class.** In an enterprise, a team's model access encodes
data-residency, vendor approval, and budget. Routing must stay inside that
boundary, so `auto` is delivered as a **small number of admin-defined routing
profiles aligned to access classes**, e.g.:

- `auto` (cloud-cleared): fast = small self-hosted → deep = Bedrock Claude.
- `auto-selfhosted` (residency-constrained): fast = small self-hosted → deep =
  large self-hosted.

A team is granted the profile matching its class; because a profile contains only
models the team is already cleared for, "routed to a model I can't access" cannot
occur by construction. Per-team **custom** routing pools are a later (v2) item.

**Adoption is evidence-driven:** shadow/observe (show projected savings + quality
in Langfuse) → opt in on non-critical paths → expand. **Per-team cost showback
must survive `auto`** — FinOps needs the per-team number regardless of routing.

## Platform-wide vs team-specific

| Platform-wide (admin builds & owns) | Team-specific (per consuming team) |
|---|---|
| vSR service + Envoy ext_proc + classifier models | Opt-in: point chosen workloads at `model: auto` |
| `auto` registered in LiteLLM → vSR | Granted the profile matching their compliance class |
| Compliance-aligned routing profiles, category→tier policy, cost/quality posture | Monitor own savings/quality; pick posture; back out anytime |
| Team-key propagation / cost attribution | (v2) custom per-team routing pool |
| Semantic cache, metrics, dashboard, Langfuse wiring | |
| Ongoing profile/tier/eval curation | |

~90% is platform-wide; teams do little (opt in + watch). Good for central
control, heavy for the platform team.

## Complexity

Moderate-to-high, concentrated in governance — not in the Helm install:

- **New always-on data plane**: vSR (CPU) + its Envoy ext_proc + MoM classifier
  models to source/cache.
- **3-hop path** (`LiteLLM → vSR → LiteLLM`) — extra latency; must be benchmarked
  against TTFT SLOs.
- **Per-team attribution is the risky piece**: vSR uses a fixed provider key, so
  **team-key passthrough is unconfirmed**. If it fails, per-team showback/budgets
  break for `auto` traffic — a non-starter for enterprises. **Verify early.**
- **Ongoing curation** of profiles / tiers / evals.
- **Maturity risk**: vSR v0.1, integration marked "preview."

Note: **"optional" limits blast radius, not build cost.** For teams that use it,
the whole governed path must exist.

## Benefits (conditional)

- Cost reduction by not sending easy queries to expensive models; semantic cache
  adds more.
- Removes model-selection burden from app teams.
- Reusable foundation for guardrails (later).
- Strengthens the platform's cost-lever story.

**Caveat:** savings only materialize where a workload is (a) mixed-difficulty and
(b) currently over-served by expensive models. All-cheap or all-hard workloads
save ~nothing, and mis-routing a hard query degrades quality — so trust needs
eval investment.

## When to use / when not

- **Use**: high-volume, cost-sensitive, variance-tolerant, mixed-difficulty
  workloads; cheap agentic sub-steps.
- **Don't**: production-critical / compliance / eval-gated / deterministic paths;
  low-volume workloads; uniform-difficulty workloads.

## Risks / open verification items

1. **Team-key passthrough** (attribution/budgets) — the make-or-break integration.
2. **Latency** of classification + extra hops vs TTFT SLO.
3. **vSR maturity** (v0.1, churn risk).
4. **Quality regressions** from mis-routing — needs eval gating.

## Phased, evidence-gated plan

**Phase 0 — size the opportunity before building anything (days).**
Run a classifier offline over a real Langfuse traffic sample to estimate: *what
fraction of current spend goes to expensive models on queries a cheap model could
handle?* No vSR/Envoy/key-passthrough needed yet.
- **Kill criteria**: modeled saving small (e.g. <15%) or traffic uniform-difficulty
  → **stop**; the complexity isn't justified.

**Phase 0.5 — cheaper 80/20.** Evaluate whether **semantic caching alone** plus
guiding teams to pin a cheaper model for cheap workloads captures most of the
value without per-request routing.

**Phase 1 — minimal pilot (only if Phase 0 justifies it).** One compliance
profile, one pilot team, cost-only, in-memory, **shadow mode first** (project
savings/quality in Langfuse) then live. **Resolve team-key attribution here.**

**Phase 2 — productize.** More profiles, more teams, cost/quality posture knob,
then fold in guardrails (with the Gateway Guardrails initiative).

## Relationship to other roadmap items

- **Gateway guardrails**: vSR's PII/jailbreak plugins could later satisfy part of
  that initiative — same component, deferred here to keep v1 cost-only.
- **Evaluation & quality gates**: Phase 0 sizing and Phase 1 shadow-mode reuse
  the Langfuse dataset-run / `ops/compare-models.py` machinery.
- **Deploy**: its Helm chart as an ArgoCD platform service (like LiteLLM/Langfuse);
  no operator/CRD unless its lifecycle is needed.
