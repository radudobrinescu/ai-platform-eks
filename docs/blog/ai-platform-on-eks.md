# Ship AI models like you ship code: a self-service AI platform on Amazon EKS

Most teams that want to build with large language models (LLMs) hit the same wall.
The frontier model APIs are easy to call but can get expensive at scale and tie you
to one vendor. Self-hosting open-source models gives you control and better unit
economics — but suddenly every team is reinventing GPU provisioning, model serving,
request routing, API keys, budgets, and observability. The result is a pile of
bespoke infrastructure and a very long path from "I have a model" to "it's in
production."

This post describes a **self-service AI platform built on Amazon EKS** that
collapses that path. It puts **one OpenAI-compatible API in front of every model** —
Amazon Bedrock, open-source models, and your own fine-tuned ones — and lets teams
**ship a model the way they ship code**: commit a short YAML file, `git push`, and
the platform provisions the GPU, serves the model, routes to it, governs it, and
traces every request.

> The platform is **open source and deploys onto your own AWS account** — you can
> read the code, run it, and extend it. Repository: `<PUBLIC_REPO_URL>` *(placeholder)*.

## The core idea: a model is a git commit

To put a self-hosted model into production, a developer adds a few lines of YAML to
the platform's Git repository and opens a pull request:

```yaml
apiVersion: kro.run/v1alpha1
kind: VLLMEndpoint
metadata:
  name: qwen3-8b
  namespace: inference
spec:
  model: "Qwen/Qwen2.5-7B-Instruct"   # a Hugging Face model ID
  gpuCount: 1
```

That commit is the whole interface. Once it's merged, the platform automatically:

- provisions a right-sized GPU node,
- loads and serves the model,
- registers it behind a single, unified API,
- applies the owning team's budget and rate limits, and
- traces every request for cost and quality analysis.

You can point it at essentially any open model on Hugging Face — small or large — and
for gated models you supply an access token once. And because a frontier model
through Amazon Bedrock has nothing to deploy or scale, it works on **day one with
zero GPUs**: teams start building immediately against the governed API and introduce
self-hosted models only when the economics call for it.

Because deployment is a pull request, **governance is built in from the first
commit**: model rollouts go through normal code review, and who can deploy what — and
who can spend on GPUs — is controlled by the same Git permissions your organization
already trusts.

## One front door for every model

The heart of the platform is a single gateway. **LiteLLM** — an open-source,
OpenAI-compatible proxy — presents one `/v1/chat/completions` endpoint that fronts
three kinds of models:

- **Amazon Bedrock** models (AWS's managed access to frontier foundation models such
  as Anthropic's Claude), which need no infrastructure at all;
- **open-source models** you self-host on GPUs; and
- **your fine-tuned models**, trained on your own data.

Every one of them is called the same way, with the same team API keys, the same
budgets, and the same tracing. That uniformity is the quiet superpower: there is no
lock-in to a single provider, governance is applied in one place, and comparing or
switching between models — even across the "managed vs. self-hosted" line — is a
configuration change, not a migration. Developers can also explore models through a
built-in chat interface (**Open WebUI**, an open-source ChatGPT-style UI), while every
request's cost, latency, and quality is captured by **Langfuse**, an open-source LLM
observability tool.

## Match the serving tier to the workload

Self-hosting isn't one-size-fits-all, and the platform turns the *sophistication* of
the serving into a choice you dial in per model — by picking a resource kind, not by
re-architecting:

- **`VLLMEndpoint`** serves a single open model on GPUs with vLLM. It's the simple,
  efficient default — ideal for most models and moderate traffic.
- **`LLMDEndpoint`** is the scale tier. When a model takes real, sustained traffic,
  llm-d runs a fleet of replicas and routes each request to the best one using live
  signals — which replica already has the relevant data cached, which is least loaded —
  while autoscaling the fleet on actual serving saturation. Better throughput and GPU
  efficiency for the same model; just a different resource kind.
- **`LLMDDisaggEndpoint`** goes further for the most demanding, high-concurrency or
  long-context workloads: it separates the two phases of inference — prompt processing
  (prefill) and token generation (decode) — onto GPU pools that scale independently and
  can even run on hardware tuned to each phase, so a burst of heavy prompt processing
  never stalls the steady stream of tokens users are waiting on.

The point isn't the machinery; it's that you **start simple and escalate the serving
tier as demand grows** — changing a few lines of YAML rather than replatforming — and
only pay for the added sophistication when the workload actually calls for it.

## Governance, isolation, and data residency

For platform owners, three properties matter as much as the developer experience:

- **Per-team isolation and budgets.** Onboarding a team is itself a declarative
  resource that provisions its own namespace, scoped API keys, spending budget, and
  rate limits — so adoption never turns into a runaway bill or a governance gap.
- **Change control through Git.** Every model and team change is a reviewed,
  auditable pull request. There is no out-of-band console clicking.
- **Your data stays in your environment.** Self-hosted models run on GPUs **in your
  own AWS account and VPC** — the model weights and every prompt and response stay
  inside your boundary. For teams with data-residency or sensitivity requirements,
  that is often the deciding factor over a third-party API.

## Why it matters

**For the business**, the value is control and economics. Scoped keys, budgets, and
rate limits keep spend predictable. And because open-source and fine-tuned models sit
behind the same API as the frontier ones, you can route expensive workloads to a
cheaper self-hosted model — a small model fine-tuned on a narrow task can rival a much
larger frontier model at a fraction of the cost per request.

**For product and application teams**, the value is speed and autonomy. Shipping a
model is a pull request, not a project — no ticket to an infrastructure team, no
hand-rolled GPU deployment, no bespoke serving stack to maintain.

**For platform and architecture teams**, the value is standardization and leverage.
The hard parts of inference — GPU sizing, tensor parallelism, autoscaling, routing,
governance — are solved once, in reusable templates, and every model and team
inherits them.

## How it's architected

The platform is built on modern, declarative platform-engineering primitives,
delivered as **Amazon EKS Capabilities** — Argo CD, KRO, and ACK, which run as
AWS-managed features of the EKS cluster rather than components you install and
babysit:

- **Argo CD (GitOps)** continuously reconciles the live cluster to what's declared in
  Git. Git is the single source of truth; a `git push` is the deployment.
- **KRO (Kube Resource Orchestrator)** lets you define a simple, high-level custom
  resource (like the `VLLMEndpoint` above) and have it automatically expand into the
  dozens of lower-level Kubernetes and AWS resources it really requires.
- **ACK (AWS Controllers for Kubernetes)** lets the cluster create and manage AWS
  services (such as storage and IAM) directly from Kubernetes, so a single
  declarative flow spans both Kubernetes and AWS.

Put together, the end-to-end loop looks like this:

```
git push  →  Argo CD syncs  →  KRO expands your YAML into Kubernetes + AWS resources
          →  Karpenter provisions a GPU node  →  vLLM loads and serves the model
          →  LiteLLM registers it  →  available via the API, a chat UI, and traces
```

A few supporting building blocks are worth naming, because the platform combines the
AWS stack with the best of the open-source ecosystem:

- **Karpenter** — a just-in-time Kubernetes node autoscaler that launches exactly the
  right GPU instance type for a workload and removes it when it's no longer needed.
- **vLLM** — a high-throughput inference engine for serving open-source LLMs
  efficiently on GPUs.
- **llm-d** — a distributed inference layer for high-scale workloads that routes each
  request to the best replica using live signals (which one already holds the relevant
  data in its cache, which is least loaded).
- **KEDA** — a metric-driven autoscaler that adds and removes model replicas based on
  real serving-saturation signals, so capacity follows actual demand.

Crucially, **the custom resources *are* the platform's API.** A small, curated set of
them — serve a model, onboard a team, run a fine-tune job — captures the complex
machinery behind a few readable lines of YAML. Developers never see the expansion;
they see the intent. And operators get a live dashboard that shows the cluster's
nodes, GPU slots, and deployed models at a glance — and explains *why* a model isn't
serving yet when a deployment is still coming up.

## Performance and cost, handled for you

Because inference is GPU-bound, the platform treats GPUs as a first-class economic
concern. Karpenter continuously right-sizes and consolidates GPU nodes to match
demand; KEDA scales model replicas up under load and back down as it subsides — and,
in the disaggregated tier, scales the prefill and decode pools independently so each
follows its own demand. The platform also includes several layers designed to reduce the
multi-minute cold starts that usually accompany large container images and model
weights. The net effect: you pay for the capacity your traffic actually needs, and
self-hosting becomes a genuine cost lever rather than an operational burden.

## Prove the savings, don't assume them

"A small model can be cheaper" is only useful if you can trust it for *your* task, so
the platform makes the comparison measurable. You can run the same evaluation set
through a frontier Bedrock model, a base open-source model, and a fine-tuned version —
and see the quality and the per-request cost side by side in Langfuse, including the
break-even volume above which self-hosting wins. Fine-tuning is a first-class,
self-service workflow too: a fine-tune job trains a smaller model on your own dataset
and can automatically deploy the result as a new endpoint — closing the loop from
"expensive frontier model" to "cheaper specialized model that's good enough," with
evidence.

## Extending the platform: new capabilities as new resources

The most important architectural property is how the platform grows. Its API is a set
of KRO **ResourceGraphDefinitions (RGDs)** — the templates that define what each
custom resource expands into. Adding a new capability means authoring a new RGD, not
teaching every team a new runbook.

Want to offer a new serving pattern, a new fine-tuning workflow, a
retrieval-augmented-generation (RAG) endpoint, or a managed vector store? You express
it once as an RGD, with all the GPU sizing, networking, governance, and wiring baked
in. From that moment, teams consume the new capability the same way they consume every
other one: a short YAML file and a pull request. The platform's surface area for its
users stays small and stable even as its capabilities expand — which is exactly what
you want from a platform meant to last.

## Where it fits

This isn't a replacement for managed AI services — it complements them. Amazon Bedrock
is a first-class citizen behind the same door as everything else. What the platform
adds is a **single, governed front door across managed *and* self-hosted models**,
Kubernetes-native self-service that feels like shipping code, and full control of the
models and data inside your own account. You reach for it when you want that
combination — cost leverage, governance, and no lock-in — under one roof.

## Takeaway

Adopting LLMs at an organization is less about any single model and more about the
**platform** around them: one governed door for every model, self-service delivery
that feels like shipping code, GPU economics that actually work, and a clean way to
grow. Built on Amazon EKS with EKS Capabilities (Argo CD, KRO, and ACK) — and
combining Amazon Bedrock with open-source engines like vLLM, llm-d, LiteLLM, and
Langfuse — this platform shows how the AWS and open-source ecosystems fit together
into something that is both powerful for engineers and legible for the business.

*The platform is open source and deployable on your own AWS account. Explore the code,
try it, and extend it: `<PUBLIC_REPO_URL>` (placeholder).*
