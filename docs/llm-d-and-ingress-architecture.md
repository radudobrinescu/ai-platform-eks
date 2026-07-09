# llm-d scale tier + ingress architecture

Status: recommendation. Revises the earlier "NLB → Envoy AI Gateway → split"
ingress decision, based on a review of current llm-d architecture.

## 1. What llm-d actually is

llm-d is **not a gateway and not a model server** — it is a **scheduling/routing
layer** that sits between a Gateway API gateway and vLLM pods:

- It is an **EPP (Endpoint Picker)** built on the **Gateway API Inference
  Extension (GIE)** and **Envoy External Processing (ext_proc)**. It performs
  **KV-cache-aware, load-aware, prefix-aware, session-affinity** routing across
  the pods of an **`InferencePool`**.
- It adds **Prefill/Decode (P/D) disaggregation**, **flow control** (fair
  queuing for multi-tenant spikes), and **SLO-aware autoscaling**.
- It ships its **own advanced EPP** (the `llm-d-router`), which supersedes the
  reference GIE EndpointPicker.
- Install: Helm (`llm-d-incubation/llm-d-infra`) + Kustomize "well-lit path"
  recipes; the entry recipe is **optimized-baseline** (prefix-cache + load-aware
  routing over a single InferencePool).
- Constraint (upstream): **one InferencePool and one EPP per Envoy gateway**, and
  **one base model per InferencePool** — multiple models means multiple
  InferencePools.

## 2. Duplicate-component analysis

What we already deployed (gated by `capabilities.inference_gateway`) vs. what
llm-d needs:

| Component we deployed | Verdict for llm-d | Action |
|---|---|---|
| **Base Envoy Gateway** (+ InferencePool support values) | **Reused** — this is the data plane llm-d wires its EPP into via ext_proc | **Keep** |
| **GIE CRDs** (`InferencePool`, etc.) | **Required + reused** — llm-d builds directly on these | **Keep** |
| **A standalone/reference EPP** | We deployed **CRDs only, no EPP** → no conflict; **llm-d supplies the EPP** | Do **not** run a separate EPP; do **not** hand-render EPP in KRO |
| **Envoy AI Gateway** (AIGatewayRoute, provider routing, token rate-limiting) | **Not required by llm-d** (it needs base Envoy Gateway + GIE only); its AI-layer features **overlap LiteLLM** | **Ingress-only candidate → see §3; recommend drop** |

**Conclusion:** llm-d **subsumes the EPP**. Our GIE + base Envoy Gateway are the
correct, reused foundation. The **Envoy AI Gateway** layer has **no routing role**
in our stack — its only possible job is public ingress, which is exactly the
question below.

## 3. Ingress: ALB vs Envoy AI Gateway

The decision hinges on one thing: **is LiteLLM the single front door?**

In this platform LiteLLM already provides OpenAI-compatible API, provider routing
(Bedrock + self-hosted), per-team keys/budgets/rate-limits, and Langfuse tracing.
Every AI-layer feature Envoy AI Gateway offers, **LiteLLM already covers**. So the
public ingress only has to expose HTTP(S) to LiteLLM (and the WebUI / Langfuse /
dashboard) — a job the **ALB does today**.

llm-d's smart routing does **not** have to be the front door. LiteLLM can forward
scale-model traffic to an **in-cluster Gateway HTTPRoute whose backend is the
`InferencePool`**; the EPP then picks the optimal pod. This keeps **governance on
all traffic** (including scale models) while still getting llm-d's KV-aware
routing.

| Dimension | ALB (current) | Envoy AI Gateway as public ingress |
|---|---|---|
| Role needed at ingress | Expose LiteLLM + UIs over HTTP(S) | Same |
| AI-layer features (OpenAI schema, provider routing, token limits) | Provided by **LiteLLM** behind it | **Duplicates LiteLLM** |
| AWS integration | Native: ACM TLS, WAF, SG allowlist, path routing, LBC-managed | L4 NLB + self-managed Envoy dataplane; TLS via cert-manager; no native WAF/ACM path routing |
| Inference-aware / InferencePool routing | Not at ingress (lives **behind** LiteLLM as an internal tier) | Its differentiator — but unused if LiteLLM fronts it |
| Operational surface | Managed, already in use | Extra control plane + dataplane to scale/patch |
| Cost | One ALB | NLB + Envoy pods |

### Recommendation

1. **Keep ALB as the public ingress → LiteLLM** (single front door; governance on
   all traffic).
2. **Run llm-d as an internal smart-routing tier**: base Envoy Gateway + GIE +
   llm-d EPP + `InferencePool` + vLLM pods. LiteLLM registers each scale model
   with `api_base` = the in-cluster Gateway route (InferencePool backend).
3. **Drop the Envoy AI Gateway (AI) layer** — keep only the base Envoy Gateway
   that llm-d needs. This de-bloats the stack and avoids fragmenting governance.

Only revisit (adopt Envoy AI Gateway at the edge) if we later need
gateway-native controls that LiteLLM genuinely cannot provide.

## 4. Resulting serving tiers

| Tier | Path | When |
|---|---|---|
| **VLLMEndpoint** (simple) | LiteLLM → Service → vLLM (K8s round-robin, no EPP) | Small/simple models; low ops |
| **llm-d** (scale) | LiteLLM → Envoy Gateway → EPP (KV/load/prefix-aware) → InferencePool → vLLM | High throughput, P/D disaggregation, multi-turn/agentic, autoscaling |

Bedrock stays on LiteLLM directly (no GPUs). LiteLLM is the constant front door
across all three.
