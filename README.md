# AI Platform on EKS

**Run LLMs on your own AWS account like a managed service.** Teams commit a short
YAML, push, and get a production-ready, OpenAI-compatible endpoint — the platform
handles GPU provisioning, model serving, API routing, team budgets, fine-tuning,
and observability. A frontier commercial model works on day one with **zero GPUs**.

**Stack:** EKS Managed Capabilities (ArgoCD · KRO · ACK) · Karpenter · Ray Serve · vLLM · LiteLLM · Langfuse

![Cluster dashboard — live topology of nodes, GPU slots, and deployed models](docs/img/cluster-dashboard.png)

---

## Why it matters

- **Value on day one, no GPUs.** Amazon Bedrock **Claude Opus 4.8** is live behind
  the same API the moment the cluster is up — start building before you provision a
  single GPU.
- **Self-service, not tickets.** A developer deploys a model or a data scientist
  fine-tunes one the same way they ship code: commit a YAML, `git push`. ArgoCD does
  the rest.
- **Pay for what you use.** Karpenter right-sizes GPUs and **scales them to zero**
  when idle; small models share one GPU via time-slicing.
- **One API, every model.** Bedrock, any HuggingFace model, and your fine-tuned
  models all sit behind one `/v1/chat/completions` endpoint with per-team keys,
  budgets, and rate limits.
- **Prove the savings.** The built-in model comparison shows a small fine-tuned
  model matching a frontier model on a narrow task at a fraction of the cost — with
  the numbers traced in Langfuse.

## What you get

| Capability | How it works |
|---|---|
| **Frontier model, zero GPUs** | Bedrock **Claude Opus 4.8** behind the same API (`enable_bedrock`) |
| **Deploy any HuggingFace model** | Commit an `InferenceEndpoint` YAML → GPU model served via vLLM |
| **OpenAI-compatible API** | LiteLLM proxies every model behind `/v1/chat/completions` |
| **Team isolation** | `AITeam` resource → namespace, RBAC, budget, rate limits, scoped API key |
| **Self-service fine-tuning** | `FineTuneJob` → QLoRA via Unsloth, `autoDeploy` to a live endpoint |
| **Auto GPU sizing & scale-to-zero** | Karpenter picks the right GPU, reclaims it when idle |
| **GPU time-slicing** | `shared: true` runs up to 4 small models on one physical GPU |
| **Fast cold starts** | EBS image snapshots (0s pull) + S3 weight cache (~15s load) |
| **Observability on first boot** | Langfuse tracing live on the first request — no manual setup |
| **Model comparison** | `ops/compare-models.py` → side-by-side Langfuse run + cost crossover |
| **Live cluster dashboard** | Topology of nodes, GPU slots, and models (pictured above) |

## How it works

```
git push → ArgoCD syncs → KRO expands your YAML into K8s + AWS resources
         → Karpenter provisions a GPU node → vLLM loads the model
         → LiteLLM registers it → available via API, Open WebUI, and Langfuse
```

Three custom resources are the entire self-service interface:

- **`InferenceEndpoint`** — serve a model (HuggingFace ID or a fine-tuned model from S3)
- **`AITeam`** — onboard a team with its own namespace, budget, and scoped API key
- **`FineTuneJob`** — fine-tune a model and optionally deploy the result

Static models (Bedrock) need no resource — they're a few lines of LiteLLM config.

---

## Quick start

The [**quickstart guide**](docs/quickstart.md) walks the full turnkey path
(provision → use Opus 4.8 with zero GPUs → fine-tune → prove it with a comparison)
via the thin `./platformctl` wrapper. The essentials:

```bash
# 1. Point the platform at the git repo ArgoCD will sync from.
#    If you FORKED this repo, set your fork URL in THREE places (they must match):
#      - gitops_repo_url in your tfvars
#      - argocd/bootstrap/platform.yaml   (the `$repo :=` line)
#      - argocd/bootstrap/workloads.yaml  (the `repoURL:` line)
#    If you're deploying this repo as-is, these are already set — skip to step 2.
git commit -am "chore: point platform at my fork" && git push   # only if you changed URLs

# 2. Provision (VPC, IAM, EKS + managed capabilities, Karpenter, secrets).
cd terraform/00.global/vars && cp example.tfvars your-env.tfvars   # edit: IdC ARN, fork URL
cd .. && export AWS_REGION=eu-central-1
make bootstrap ENVIRONMENT=your-env
make ENVIRONMENT=your-env apply-all

# 3. Talk to the frontier model immediately — no GPU required.
#    Cluster name = <resources_prefix>-<env> (prefix is in your tfvars; example.tfvars → "ai-platform").
aws eks update-kubeconfig --region $AWS_REGION --name ai-platform-your-env
# Bedrock models are static (no InferenceEndpoint) — query them via the LiteLLM API:
curl -s http://localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"claude-opus-4-8","messages":[{"role":"user","content":"What is Kubernetes?"}]}'
# (./ops/test-model.sh works for self-hosted models — those have an InferenceEndpoint CR.)
```

Deploy a self-hosted model — commit a YAML and push:

```yaml
# workloads/models/qwen3-3b.yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: qwen3-3b
  namespace: inference
spec:
  model: "Qwen/Qwen2.5-3B-Instruct"   # ungated — no token needed
  gpuCount: 1
```

```bash
git add workloads/models/qwen3-3b.yaml && git commit -m "feat: deploy qwen3-3b" && git push
kubectl get inferenceendpoints -n inference -w   # watch it come up
```

> **Gated models** (e.g. `google/gemma-3-4b-it`, Llama) need a HuggingFace token.
> Create it once in the `inference` namespace before deploying such a model:
> `kubectl create secret generic hf-token -n inference --from-literal=token=hf_...`
> (and accept the model's license on HuggingFace). Ungated models like Qwen need nothing.

> Not sure which GPU a model needs? `./ops/recommend-instance.py <hf-model-id>`
> reads the architecture, estimates VRAM, and emits a ready-to-commit YAML.

**Access the UIs** (internet-facing ALB restricted by IP allowlist, or `./ops/ssm-tunnel.sh` from anywhere):
Open WebUI `:8080` · LiteLLM API `:4000` · Langfuse `:3000` · Cluster Dashboard `:9090`.

> **Set the allowlist to YOUR IP before relying on the public ALB.** The shipped
> `inbound-cidrs` is a placeholder and won't match you — leave it and the UIs are
> silently unreachable (use `./ops/ssm-tunnel.sh` meanwhile). Replace the CIDR in
> all four spots (they must stay identical — one shared ALB group):
> `platform/config/ingress.yaml` (×3) and
> `platform/services/cluster-dashboard/manifests.yaml` (×1), then commit + push.
> `curl -s https://checkip.amazonaws.com` gives your current IP → use `<ip>/32`.

---

## The money demo — small + fine-tuned beats big

Run the same eval set through the frontier model, the base small model, and a
**fine-tuned** small model. Langfuse shows the fine-tuned 3B matching Opus 4.8 on a
narrow task (e.g. support replies in your voice) — at a fraction of the cost.

```bash
./ops/compare-models.py \
  --dataset ops/sample-data/support-eval.jsonl \
  --models claude-opus-4-8,qwen3-3b,qwen3-support-tuned \
  --langfuse-dataset support-voice-eval \
  --self-hosted-model qwen3-support-tuned --self-hosted-hf-id Qwen/Qwen2.5-3B-Instruct
```

Every call is traced (cost / latency / tokens) and logged as a Langfuse **dataset
run** for side-by-side comparison. The script prints the **cost crossover** — the
daily request volume above which the self-hosted tuned model is cheaper than
Bedrock. Design notes: [docs/platform-evolution-plan.md](docs/platform-evolution-plan.md).

**Fine-tune your own** with the same `git push` loop — upload a dataset, commit a
`FineTuneJob`, and (with `autoDeploy: true`) the tuned model becomes a live
endpoint. Full guide: [docs/fine-tuning-getting-started.md](docs/fine-tuning-getting-started.md).

> **Showing this on stage?** [docs/demo-walkthrough.md](docs/demo-walkthrough.md)
> is a timed presenter's script (10/20/30-min cuts) with talk track and fallbacks.

---

## Self-service resources

```yaml
# InferenceEndpoint — serve a model
spec:
  model: "org/model-id"     # REQUIRED — HuggingFace ID (or set modelSource for a fine-tuned model)
  gpuCount: 1               # GPUs per worker (1, 2, 4, or 8)
  shared: false             # true → time-slice one GPU across up to 4 small models
  minReplicas: 1
  maxReplicas: 4
# Full field reference (parallelism, VRAM hints, sizing): docs/quickstart.md
```

```yaml
# AITeam — onboard a team with its own budget + scoped key
spec:
  teamName: search-ranking
  models: ["qwen3-3b", "claude-opus-4-8"]
  maxBudget: "50.0"
  budgetDuration: "30d"
  rpmLimit: 60
```

```yaml
# FineTuneJob — QLoRA fine-tune, optionally deploy the result
spec:
  baseModel: "unsloth/Qwen2.5-3B-Instruct-bnb-4bit"
  dataset: "s3://<cluster>-training-datasets/training-data.jsonl"
  autoDeploy: true
```

---

## Operations

The **cluster dashboard** (`:9090`, pictured above) is the primary operator view: a
live topology of nodes, GPU slots, deployed models, and recent activity. It also
ships the **Platform Health Agent** — watches for failures and proposes fixes for
one-click approval in the same dashboard (idle until you provide a Kiro key) — see
[its README](platform/services/cluster-dashboard/PLATFORM-HEALTH-AGENT.md) to enable it.

```bash
./ops/recommend-instance.py <model>    # GPU sizing + fleet scaling
./ops/compare-models.py ...            # 3-way Langfuse comparison + cost crossover
./ops/test-model.sh <name> "prompt"    # Quick model test
./ops/ssm-tunnel.sh                    # Port-forward the UIs from anywhere
./ops/seed-model-cache.py <model>      # Pre-populate the S3 weight cache
./ops/scale-down.sh | scale-up.sh      # Reclaim / restore GPU capacity
./platformctl up|status|tunnel|preflight|compare|down   # Turnkey wrapper over make + ops
```

**Cleanup:**

```bash
kubectl delete inferenceendpoints --all -n inference
cd terraform && make ENVIRONMENT=your-env destroy-all
```

---

## Repository layout

```
argocd/bootstrap/   ApplicationSets (platform services + self-service workloads)
platform/
  config/kro/       InferenceEndpoint · AITeam · FineTuneJob definitions
  services/         litellm, open-webui, langfuse, gpu-operator, kuberay,
                    cluster-dashboard (+ Platform Health Agent component)
workloads/          Self-service YAMLs: models/ · teams/ · fine-tuning/
ops/                Operational scripts (ops/demo/ holds demo-only scripts)
terraform/          Infrastructure modules (VPC → IAM → EKS → observability)
docs/               quickstart · demo-walkthrough · fine-tuning · platform-evolution-plan
```

## Acknowledgments

Infrastructure based on [Automated Provisioning of Application-Ready Amazon EKS Clusters](https://aws-solutions-library-samples.github.io/compute/automated-provisioning-of-application-ready-amazon-eks-clusters.html)
from the AWS Solutions Library, extended with EKS Managed Capabilities,
GPU-optimized Karpenter NodePools, and the self-service AI platform layer.
