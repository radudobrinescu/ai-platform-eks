# Turnkey AI Platform — Plan

**Status:** Draft for review
**Author:** Platform Team
**Date:** 2026-05-30
**Design constraint (non-negotiable):** **Simplicity.** Fewer moving parts, less custom code, maximum reuse of what already works. Every addition must justify itself against "could we do this with config instead of code?"

---

## 0. Summary

Turn this reference architecture into a **self-contained, turnkey AI platform** a business can stand up on its own AWS account and get value from on day one:

1. **Access to AI models out of the box** — a frontier commercial model (**Amazon Bedrock — Claude Opus 4.8**) and a small open-source model (**Qwen2.5-3B-Instruct**, ungated), both behind one OpenAI-compatible API.
2. **Observability out of the box** — **Langfuse** tracing live on the first request, no manual setup.
3. **Fine-tune and deploy** — run a `FineTuneJob` on your own data, deploy the result as a normal model.
4. **The proof** — a **comparison** that runs the *same* task through Opus 4.8, the base small model, and the **fine-tuned** small model, and shows in Langfuse that **a small fine-tuned model can match or beat a large commercial one on a narrow task, at a fraction of the cost.**

**Showcase use case:** *customer-support replies in a company's voice* — fine-tune the small model on a company's support transcripts, then compare reply quality (Langfuse LLM-as-judge + human side-by-side) and cost/latency (objective, from Langfuse) against Opus 4.8.

**The headline of the whole design:** almost none of this is new code. Bedrock is a few lines in an existing ConfigMap plus one IAM role. Langfuse-on-first-boot is a Terraform secret + env vars. Fine-tuning is the already-validated [v2 plan](./fine-tuning-implementation-plan-v2.md). The only genuinely new piece is a **single comparison script** that leans on Langfuse's built-in Datasets/Evaluations. That is the entire build.

---

## 1. The "money demo"

The platform exists to make one argument, concretely and reproducibly:

> A business has support transcripts. In ~30 minutes it fine-tunes a 3B model on them, deploys it next to Claude Opus 4.8, runs both (plus the un-tuned 3B) over a held-out set of real questions, and watches Langfuse show: **the fine-tuned 3B answers in the company's voice as well as Opus (judged), while costing ~20× less per request and responding faster.**

Three contenders, one API, one trace view:

| Contender | What it shows |
|---|---|
| **Opus 4.8** (Bedrock) | The expensive generalist baseline — great quality, high per-request cost |
| **Qwen2.5-3B base** (self-hosted) | The cheap generalist — fast/cheap but off-voice, often wrong on policy |
| **Qwen2.5-3B fine-tuned** (self-hosted) | The punchline — cheap *and* on-voice/on-policy after fine-tuning |

The base model matters to the narrative: base small = mediocre → fine-tuned small = matches Opus → at a fraction of the cost. That arc is the sale.

---

## 2. Design principles (anti-overengineering)

These govern every decision below.

1. **Config over code.** If a capability can be a few lines in an existing ConfigMap/Helm values/tfvars, it does **not** get a CRD, a controller, or a Job.
2. **Reuse the proven loop.** `git push → ArgoCD → KRO/Helm → workload`. No new control plane, no new datastore, no new UI.
3. **One opinionated default**, not a matrix of profiles. A business gets a working setup; experts can still edit tfvars.
4. **Lean on the tools' built-in features.** LiteLLM already unifies models, budgets, keys, and Langfuse callback. Langfuse already does Datasets, LLM-as-judge, human annotation, and cost/latency. We *use* those — we don't rebuild them.
5. **The one new script earns its place** because there's no built-in "run my dataset through model A vs B and log it" trigger — and even it is a thin wrapper over the Langfuse SDK.

---

## 3. What we reuse vs what's new

The point of this table is how little is new.

| Piece | Status | Effort |
|---|---|---|
| Self-service model deploy (`InferenceEndpoint`) | 🟢 exists | 0 |
| LiteLLM unified OpenAI API + budgets/keys | 🟢 exists | 0 |
| Langfuse (tracing, **Datasets, Evaluations, human annotation**) | 🟢 deployed | 0 — *use built-ins* |
| GPU autoscaling, cold-start opt, time-slicing | 🟢 exists | 0 |
| Fine-tuning (`FineTuneJob`, QLoRA, autoDeploy) | 🟠 designed ([v2](./fine-tuning-implementation-plan-v2.md)) | ship as-is |
| **Bedrock (Opus 4.8) as a model** | 🔴 missing | **~1 IAM role + ~6 lines of config** |
| **Langfuse tracing on first boot** (no manual keys) | 🟠 manual today | **Terraform secret + env vars** |
| **Preconfigured small model** (Qwen2.5-3B) | 🔴 missing | a catalog YAML |
| **Model comparison** | 🔴 missing | **one script** (`ops/compare-models.py`) |
| Turnkey install polish | 🟠 partial | docs + a thin wrapper |

Five small things. No new CRDs beyond the already-planned `FineTuneJob`. No new services.

---

## 4. The building blocks

### A. Bedrock (Opus 4.8) as a model — config, not code

LiteLLM supports `bedrock/` models natively. Because Bedrock models are static (nothing to deploy/scale), they don't need the `InferenceEndpoint` machinery or a registration Job — **they go straight into the LiteLLM config**, version-controlled in git.

**A1. Declare the model** — `platform/services/litellm/litellm.yaml`, in the `litellm-config` ConfigMap (today `model_list: []`):

```yaml
data:
  config.yaml: |
    model_list:
      - model_name: claude-opus-4-8            # the alias clients call
        litellm_params:
          model: bedrock/global.anthropic.claude-opus-4-8   # global. = region-agnostic cross-region inference profile; verify exact id in the Bedrock console
          aws_region_name: os.environ/AWS_REGION
    general_settings:
      master_key: os.environ/LITELLM_MASTER_KEY
    # InferenceEndpoint models still self-register into the DB at deploy time;
    # LiteLLM merges config models + DB models.
```

**A2. Give LiteLLM permission** — the one piece of real infra. LiteLLM's Deployment currently uses the `default` ServiceAccount with no AWS access. Add a `litellm` ServiceAccount with an IRSA role granting Bedrock invoke, using the **exact IRSA pattern already in `capabilities.tf`** (`inference_worker` is the template):

```hcl
# terraform/30.eks/30.cluster/capabilities.tf  (mirror inference_worker)
resource "aws_iam_role" "litellm_bedrock" {
  count = local.capabilities.gitops ? 1 : 0
  name  = "${local.cluster_name}-litellm-bedrock"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow"
    Principal = { Federated = module.eks.oidc_provider_arn }
    Action    = "sts:AssumeRoleWithWebIdentity"
    Condition = { StringEquals = {
      "${module.eks.oidc_provider}:sub" = "system:serviceaccount:ai-platform:litellm"
      "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
    }}
  }]})
  tags = local.tags
}

resource "aws_iam_role_policy" "litellm_bedrock" {
  count = local.capabilities.gitops ? 1 : 0
  name  = "bedrock-invoke"
  role  = aws_iam_role.litellm_bedrock[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow"
    Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
    Resource = "*"   # tighten to specific model ARNs if desired
  }]})
}
```

Then add `serviceAccountName: litellm` to the Deployment and annotate the SA with the role ARN (and ensure `AWS_REGION` is in the pod env).

**A3. Enablement** — one tfvar `enable_bedrock = true` (gates the IAM role) and a documented prerequisite: the account must have **Bedrock model access enabled** for the Opus model (a one-time console toggle, or `platformctl` checks it and tells the user). Networking: SMB uses the public Bedrock endpoint (default); add a `bedrock-runtime` VPC endpoint only for private clusters (one entry in `10.networking`).

> **Outcome:** `claude-opus-4-8` shows up in the same `/v1/chat/completions` API, Open WebUI dropdown, and Langfuse traces as every other model — governed by the same `AITeam` budgets and keys. A business can use the platform with **zero GPUs** on day one.

### B. Langfuse observability on first boot — secret + env vars, no clicks

Today: deploy → log into Langfuse UI → create project + API keys → create `langfuse-litellm-keys` secret → restart LiteLLM. That's the manual friction. LiteLLM's Langfuse callback is already wired (`LITELLM_CALLBACKS: langfuse`), with the keys marked `optional: true`.

**Simplest robust fix:** use Langfuse's **headless initialization** (`LANGFUSE_INIT_*`). Terraform already generates every other platform secret (LiteLLM master key, DB password, Langfuse encryption/salt/nextauth) — add one more: a deterministic Langfuse project public/secret key pair.

- Inject the pair into **Langfuse** via the chart's `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `LANGFUSE_INIT_PROJECT_SECRET_KEY` (+ org/project init vars) so the project and keys exist on first boot.
- Inject the **same pair** into **LiteLLM** as `langfuse-litellm-keys` (already referenced).

Result: the first model call is traced. No UI, no Job, no restart dance. ~15 lines of Terraform + Helm values, all following the existing secret pattern. (Also fold the `nextauth.url` ALB-hostname edit into a tfvar so it's not a manual post-install step.)

### C. Fine-tuning — ship the v2 plan as-is

The [v2 fine-tuning plan](./fine-tuning-implementation-plan-v2.md) is validated and deliberately minimal: single-node QLoRA via a custom Unsloth image, a `fine-tuning-worker` IRSA, a datasets bucket, the `FineTuneJob` KRO RGD, a `modelSource` extension to `InferenceEndpoint`, and `autoDeploy`. **Adopt it unchanged** — it already matches the simplicity bar (no multi-node, no RLHF, no HP search in V1).

For this platform's purpose the only configuration choices are:
- **Base model:** `unsloth/Qwen2.5-3B-Instruct-bnb-4bit` (ungated, Apache-2.0, strong instruction-following → good at tone/style; Unsloth ships the matching pre-quantized base). `SmolLM3-3B` is the already-cluster-validated fallback.
- **Dataset:** the company's support transcripts in chat format (`messages`), uploaded to the datasets bucket. The v2 plan's dataset-format auto-detection already handles ShareGPT/ChatML.
- **`autoDeploy: true`** → the fine-tuned model becomes a queryable LiteLLM endpoint automatically, ready for the comparison.

No additions to the v2 plan. (Eval-gate, model registry, and gpuHours quotas from the earlier draft are **deferred** — they're polish, not core to the demo.)

### D. The comparison — one script over Langfuse Datasets

This is the only new component, and it's intentionally tiny. There's no built-in "run my held-out set through model A, B, C and log it," so we write that — as a **single CI-friendly script**, not a service or CRD.

**`ops/compare-models.py`** (mirrors the style/quality of the existing `ops/recommend-instance.py`):

```
compare-models.py \
  --dataset support-eval.jsonl \
  --models claude-opus-4-8,qwen3-3b,qwen3-support-tuned \
  --langfuse-dataset support-voice-eval
```

What it does — and what it deliberately leans on Langfuse for:

1. **Upload** the held-out eval prompts (the split of the same transcripts used for fine-tuning) as a **Langfuse Dataset** (one API call; idempotent).
2. **Run** each prompt through each model **via LiteLLM** (one OpenAI-compatible endpoint, one key). Because LiteLLM's Langfuse callback is on, every call is automatically traced with **cost, latency, and tokens** — we log nothing extra for the objective metrics.
3. **Tag** each model's pass as a Langfuse **Dataset Run** (e.g., run name = model alias), so Langfuse renders the **side-by-side comparison table** natively.
4. **Quality scoring** — *use Langfuse's built-in evaluators*, don't build one:
   - **LLM-as-judge** evaluators configured in the Langfuse UI (judge = Opus 4.8 via the same LiteLLM endpoint) score each reply on a rubric (voice match, policy correctness, helpfulness). Configured once in the UI; runs automatically on the dataset runs.
   - **Human annotation** — Langfuse's side-by-side view lets a reviewer score/prefer outputs. This is the honest answer for a *style* task where automated metrics are soft.
5. **Output** — the script prints a summary table (avg judge score, avg cost/req, p50 latency per model) and the Langfuse dataset-run URL. The cost crossover ("above ~N req/day the tuned 3B is cheaper") is computed by reusing **`recommend-instance.py`**'s pricing/throughput data — no new cost system.

> **Why a script and not a `ComparisonJob` CRD?** Because it runs occasionally (when you have a new model to evaluate), it's inherently imperative, and Langfuse already owns the storage/UI/scoring. A CRD + controller would be pure overengineering. If we later want it GitOps-triggered, the script wraps trivially into a `Job` — but not before there's a reason.

---

## 5. The showcase use case — support replies in company voice

**Task:** given a customer question (and optionally a snippet of context/policy), produce a reply in the company's tone that is correct on policy and helpful.

**Why this task:** it's the most *relatable* for a business (everyone has support), and fine-tuning's strength — absorbing voice, format, and domain policy from examples — is exactly what a generalist model lacks without a long, expensive prompt.

**Data:** a few hundred to a few thousand real (anonymized) support Q→A pairs in `messages` chat format. Split: ~90% train / ~10% held-out eval. The eval split is the comparison dataset — **free, no separate labeling**.

**Evaluation method (honest about subjectivity):**
- **Objective axis (hard numbers, automatic):** cost per request and latency — straight from Langfuse traces. This axis alone often closes the deal (~20× cost gap).
- **Quality axis (judged):** Langfuse **LLM-as-judge** on a rubric (voice/brand match, policy correctness, helpfulness) + **human side-by-side** preference on a sample. We state plainly that quality is *judged*, not measured — and the rubric + human check is the standard, credible way to do that.

**Expected result (the thesis):** the fine-tuned 3B matches Opus 4.8 on voice/policy (judged), beats the base 3B clearly, and wins decisively on cost/latency. If on some sub-categories Opus still wins, that's a *feature* of the demo — it shows where to keep using the frontier model (and the platform serves both from one API, so a business can route accordingly).

---

## 6. End-to-end user journey

```
1. Provision        platformctl up        → cluster + platform; Langfuse tracing live;
                                             Opus 4.8 (Bedrock) + Qwen2.5-3B available
2. Use immediately  chat in Open WebUI / curl /v1/chat/completions
                                             → works against Opus 4.8 with zero GPUs
3. Bring your data  upload support transcripts to the datasets bucket
4. Fine-tune        commit a FineTuneJob YAML (base=Qwen2.5-3B, autoDeploy=true)
                                             → ~30 min later, qwen3-support-tuned is live
5. Compare          ops/compare-models.py --models claude-opus-4-8,qwen3-3b,qwen3-support-tuned
                                             → Langfuse dataset run with the 3-way table
6. Decide           read the cost/quality crossover; route prod traffic accordingly
                                             (cheap tuned model for the common case,
                                              Opus 4.8 for the hard tail) — all one API
```

Every step works standalone; the value compounds.

---

## 7. Phased roadmap

Small, independently shippable phases. Acceptance criteria are concrete.

| Phase | Deliverable | Acceptance test |
|---|---|---|
| **P1. Bedrock as a model** | LiteLLM `litellm` SA + Bedrock IRSA; Opus 4.8 in `litellm-config`; `enable_bedrock` tfvar | `curl /v1/chat/completions -d '{"model":"claude-opus-4-8",...}'` returns a Bedrock reply; a trace appears in Langfuse |
| **P2. Langfuse on first boot** | Terraform-generated Langfuse project keys; `LANGFUSE_INIT_*` in Langfuse + `langfuse-litellm-keys` in LiteLLM; `nextauth.url` tfvar | Fresh install → first model call is traced with cost/latency, **no manual key creation** |
| **P3. Preconfigured small model** | `workloads/models/catalog/qwen3-3b.yaml` (ungated); deployed by default or one-command | `qwen3-3b` serves via LiteLLM after install |
| **P4. Fine-tuning GA** | Execute v2 plan P0–P9 (base = Qwen2.5-3B) | v2 sign-off checklist passes; a tuned model autoDeploys and answers via LiteLLM |
| **P5. Comparison runner** | `ops/compare-models.py` + a Langfuse LLM-as-judge rubric + a sample support dataset | Running it produces a 3-way Langfuse dataset run with judge scores + cost/latency; summary table prints the cost crossover |
| **P6. Turnkey polish** | Thin `platformctl up/status` wrapper over `make`; automate IP allowlist + secrets via tfvars; quickstart doc | A teammate stands up the platform and reaches the comparison with no hand-editing of manifests |

**Order:** P1 → P2 → P3 deliver "models + observability out of the box" (usable, GPU-free) fast. P4 → P5 deliver the fine-tune-and-prove loop. P6 is adoption polish. P1+P2+P3 alone are a shippable, valuable milestone.

---

## 8. What we are deliberately NOT building

Stating the anti-scope is part of keeping it simple. **Out of scope** for this product:

- ❌ Agentic workflows, `Agent`/`AgenticWorkflow` CRs, Bedrock AgentCore — a *different* product (was Workstream C; removed).
- ❌ A model-registry CRD, an eval-gate admission controller, per-team gpuHours quotas — deferred polish, not core to the demo.
- ❌ A profiles matrix (smb/enterprise/airgapped) — one opinionated default; experts edit tfvars.
- ❌ A `ComparisonJob` CRD or a custom eval framework/UI — Langfuse Datasets + Evaluations already do this.
- ❌ A heavy provisioning console/SaaS — a thin CLI wrapper over the existing `make` flow, nothing more.
- ❌ Multi-node distributed training, RLHF/DPO, hyperparameter search — excluded by the v2 plan and we keep it that way.

The standalone self-healing agent product remains tracked separately in [`self-healing-agent-product-plan.md`](./self-healing-agent-product-plan.md) and is independent of this plan.

---

## 9. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Bedrock model access not enabled in the account → Opus calls 403 | Med | Med | One-time console enablement documented; `platformctl`/`compare-models.py` preflight-checks and prints the exact fix |
| "Small model wins" doesn't hold for the chosen task | Med | High | Showcase task is narrow (support voice) — fine-tuning's sweet spot; the **cost/latency** win is guaranteed regardless; show per-category results so the honest "Opus wins the hard tail" case is a routing feature, not a failure |
| Quality is subjective (style task) → demo feels hand-wavy | Med | Med | Lean on Langfuse LLM-as-judge **rubric** + human side-by-side (standard, credible); keep cost/latency as the hard objective axis |
| Langfuse headless init varies by chart version | Low | Med | Pin the Langfuse chart version; verify `LANGFUSE_INIT_*` support; fallback is a tiny one-shot bootstrap Job (still no manual clicks) |
| Cost comparison is apples-to-oranges (per-request vs GPU-hours) | Med | Med | Frame honestly as a **crossover** ("cheaper above ~N req/day"); compute with `recommend-instance.py`'s existing pricing/throughput model |
| Fine-tune quality depends on data quality | Med | Med | Ship a known-good sample dataset for the demo; document data-prep expectations; eval split surfaces problems immediately |
| Gemma desired later but gated | Low | Low | Default is ungated Qwen2.5-3B; document Gemma 3 as opt-in once an HF token is provided |

---

## 10. Decision log

| # | Decision | Rationale | Alternative |
|---|---|---|---|
| 1 | Bedrock model = **config in LiteLLM**, not a CRD/Job | Bedrock models are static; LiteLLM merges config + DB models; minimal code | A `BedrockModel` registration Job — unnecessary moving part |
| 2 | One IRSA role for LiteLLM→Bedrock, mirroring `inference_worker` | Reuses the proven IRSA pattern; least privilege | Static AWS keys — insecure, non-idiomatic |
| 3 | Langfuse tracing via **headless `LANGFUSE_INIT_*` + Terraform-generated keys** | Zero manual steps on first boot; reuses the existing secret-generation pattern | Bootstrap Job hitting the Langfuse API — more code; manual UI steps — friction |
| 4 | Default small model = **Qwen2.5-3B (ungated)** | Works on first boot (no HF token/license gate); strong at tone/style | Gemma 3 (gated — breaks turnkey); offered as opt-in |
| 5 | Showcase task = **support replies in company voice** | Most relatable; fine-tuning's sweet spot (voice/policy absorption) | Classification (more objective but less relatable) — kept as a possible second demo |
| 6 | Comparison = **one `ops/` script over Langfuse Datasets/Evaluations** | No built-in trigger exists, but storage/UI/scoring do — don't rebuild them | A `ComparisonJob` CRD + controller, or a custom eval UI — overengineering |
| 7 | Adopt fine-tuning **v2 as-is**; defer eval-gate/registry/quotas | v2 already meets the simplicity bar; extras aren't needed for the demo | Build the full lifecycle now — premature |
| 8 | **One opinionated install**, thin CLI over `make` | Lowers entry bar without a provisioning platform to maintain | Profiles matrix / SaaS console — complexity the goal doesn't need |

---

*Scope: turnkey model access + observability + fine-tune-and-prove. Agentic workflows and the standalone self-healing agent are out of scope here (the latter tracked in [`self-healing-agent-product-plan.md`](./self-healing-agent-product-plan.md)).*
