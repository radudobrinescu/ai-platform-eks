# AI Platform on EKS

Self-service AI inference platform on Amazon EKS. Teams deploy models by committing a short YAML file — the platform handles GPU provisioning, model serving, API routing, and observability.

Built on EKS Managed Capabilities (ArgoCD, KRO, ACK), Karpenter, Ray Serve, and vLLM.

## What You Get

- **Single custom resource** — `InferenceEndpoint` abstracts RayService, GPU scheduling, networking, and LiteLLM registration
- **GitOps workflow** — commit a YAML, ArgoCD deploys it, model is live
- **OpenAI-compatible API** — LiteLLM proxies all models behind `/v1/chat/completions`
- **Chat UI** — Open WebUI for interactive testing
- **Fast cold starts** — Bottlerocket + SOCI parallel pull, optional ECR pull-through cache
- **Team onboarding** — `AITeam` resource creates namespace, RBAC, quotas, and scoped API key
- **LLM observability** — Langfuse tracing

## How It Works

```
git push → ArgoCD syncs → KRO creates RayService + registration Job
         → Karpenter provisions GPU node (Bottlerocket + SOCI)
         → vLLM loads model → LiteLLM registers endpoint
         → model available via API and Open WebUI
```

## Architecture

```
EKS Cluster
│
├── Managed Capabilities (AWS-hosted)
│   ├── ArgoCD       ──▶  syncs platform and workloads from Git
│   ├── KRO          ──▶  expands custom resources into K8s objects
│   └── ACK          ──▶  manages AWS resources from Kubernetes
│
├── Karpenter
│   ├── default          ──▶  platform nodes (AL2023, Graviton)
│   ├── gpu-inference    ──▶  GPU nodes (Bottlerocket + SOCI)
│   └── gpu-shared       ──▶  GPU time-sliced nodes (4 models per GPU)
│
├── Platform Services (ArgoCD-managed)
│   ├── GPU Operator     ──▶  NVIDIA device plugin + DCGM metrics
│   ├── KubeRay          ──▶  Ray cluster lifecycle
│   ├── LiteLLM          ──▶  OpenAI-compatible API gateway
│   ├── Open WebUI       ──▶  chat interface
│   ├── Platform DB      ──▶  shared PostgreSQL (LiteLLM + Langfuse)
│   └── Langfuse         ──▶  LLM tracing and analytics
│
├── Platform Config (ArgoCD-managed)
│   ├── KRO definitions  ──▶  InferenceEndpoint, AITeam APIs
│   ├── RBAC             ──▶  team-developer ClusterRole
│   └── Ingress          ──▶  ALB routing to services
│
└── Workloads (ArgoCD-managed, teams self-serve)
    ├── InferenceEndpoints (e.g. gemma-4b, smolLM3)
    └── AITeams (e.g. search-ranking, customer-support)
```

## Prerequisites

- AWS CLI configured with appropriate permissions
- Terraform >= 1.0
- kubectl
- AWS Identity Center configured (required for ArgoCD managed capability)

## Deployment

### 1. Configure

```bash
cd terraform/00.global/vars/
cp example.tfvars your-env.tfvars
```

Edit `your-env.tfvars` — set your Identity Center ARN, VPC CIDR, GitOps repo URL (`gitops_repo_url`), and capabilities.

### 2. Deploy Infrastructure

```bash
export AWS_REGION=eu-central-1
cd terraform
make bootstrap ENVIRONMENT=your-env
make ENVIRONMENT=your-env apply-all
```

This creates the VPC, IAM roles, EKS cluster with managed capabilities, Karpenter NodePools, shared PostgreSQL credentials, and all platform secrets (LiteLLM, Langfuse).

The cluster name follows the pattern `{resources_prefix}-{ENVIRONMENT}` (e.g., `ai-platform-your-env`).

### 3. (Optional) Enable ECR Pull-Through Cache

Mirrors Docker Hub images to ECR for ~60% faster GPU node image pulls:

```bash
export TF_VAR_docker_hub_username="your-dockerhub-username"
export TF_VAR_docker_hub_access_token="dckr_pat_XXXXXXXXXX"
```

Without this, images pull directly from Docker Hub (works fine, just slower).

### 3b. (Optional) Create SOCI Indices for Faster Cold Starts

> **Requires Step 3** — SOCI indices are created for ECR images, so the pull-through cache must be configured first.

GPU nodes use Bottlerocket with the SOCI snapshotter in `parallel-pull-unpack` mode. Without SOCI indices, images are pulled in parallel but fully downloaded before containers start. With SOCI indices, containers start via lazy-loading — only fetching layers on demand (~30-70% faster cold starts).

Create a SOCI index for the Ray LLM image (or any large ECR image):

```bash
./ops/create-soci-index.sh <ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com/docker-hub/anyscale/ray-llm:2.54.0-py311-cu128
```

This runs on a criticaladdons node via SSM (requires the 100GB EBS volume from the MNG config). Re-run whenever you update the Ray image tag.

> **Note:** The AWS SOCI Index Builder (Lambda-based) has a 6 GB compressed image limit. The Ray LLM image is ~13 GB, so indices must be created via this script instead.

### 4. Bootstrap ArgoCD

ArgoCD is already running (deployed as a managed capability by Terraform). Terraform also created a single bootstrap `Application` that points at `argocd/bootstrap/` in your Git repo — there's no `sed` step. Set `gitops_repo_url` in your tfvars file to your fork's URL; Terraform renders the bootstrap Application with that value.

The bootstrap Application syncs two ApplicationSets:

| ApplicationSet | Creates |
|-----|---------|
| `platform` | `platform-config`, `gpu-operator`, `kuberay-operator`, `litellm`, `open-webui`, `langfuse` |
| `workloads` | `models` (InferenceEndpoint instances), `teams` (AITeam instances) |

Verify everything syncs:

```bash
aws eks update-kubeconfig --region $AWS_REGION --name ai-platform-your-env
kubectl get applicationsets -n argocd
kubectl get applications -n argocd
```

If you forked this repo, you only need to change:
- `gitops_repo_url` in your tfvars file
- The two literal URLs inside `argocd/bootstrap/platform.yaml` and the one in `argocd/bootstrap/workloads.yaml`

| Application | What it syncs |
|-----------|---------------|
| `platform-config` | KRO definitions, RBAC, Ingress |
| `gpu-operator` | NVIDIA GPU Operator (Helm) |
| `kuberay-operator` | KubeRay Operator (Helm) |
| `litellm` | LiteLLM proxy + shared PostgreSQL |
| `open-webui` | Open WebUI chat interface |
| `langfuse` | Langfuse LLM observability |

### 5. Create HuggingFace Token

Required for gated models (Gemma, Llama, etc.). Create this **before** deploying gated models — without it, GPU workers can't download model weights:

```bash
kubectl create secret generic hf-token -n inference \
  --from-literal=token=hf_YOUR_TOKEN_HERE
```

> **Note:** The sample models in `workloads/models/` include Gemma and Llama which are gated. ArgoCD will deploy them automatically on bootstrap, but they won't become ready until this secret exists. Non-gated models (like SmolLM3) work without it.

### 6. Deploy a Model

Copy the template and fill in the model ID:

```bash
cp workloads/models/TEMPLATE.yaml.example workloads/models/my-model.yaml
```

Edit `my-model.yaml` — only `spec.model` is required:

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gemma-4b
  namespace: inference
spec:
  model: "google/gemma-3-4b-it"
  gpuCount: 1
  minReplicas: 1
  maxReplicas: 2
```

```bash
git add workloads/models/my-model.yaml
git commit -m "feat: Deploy my model"
git push
```

Track deployment progress:

```bash
kubectl get inferenceendpoints -n inference -w
```

The `MESSAGE` column shows the current phase:
- `Waiting for GPU node provisioning and image pull` — Karpenter is provisioning a GPU node
- `Model is loading onto GPU workers` — vLLM is loading the model
- `Model is live and serving requests` — ready to use

First deployment takes ~7 min with ECR cache, ~14 min without.

### 7. Access Services

The platform runs an **internet-facing ALB restricted by an IP allowlist** at the Security Group level (`alb.ingress.kubernetes.io/inbound-cidrs`). All three services share one ALB via `group.name: ai-platform`; each gets its own HTTP listener port.

```bash
kubectl get ingress -n ai-platform ai-platform-litellm \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}{"\n"}'
# → k8s-aiplatform-<hash>.<region>.elb.amazonaws.com
```

- `http://<alb-host>:8080` — Open WebUI
- `http://<alb-host>:4000` — LiteLLM API
- `http://<alb-host>:3000` — Langfuse

Update the allowlist in `platform/config/ingress.yaml` when your public IP changes:

```yaml
alb.ingress.kubernetes.io/inbound-cidrs: 203.0.113.42/32  # operator laptop
```

Commit + push → ArgoCD reconciles the Security Group rule in ~5 s; the ALB itself is not re-provisioned.

**Fallback for unstable public IPs (airport, VPN, etc.):** `./ops/ssm-tunnel.sh` still works — it forwards via SSM through a criticaladdons node. Requires the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (`brew install --cask session-manager-plugin`). Local ports stay the same: `localhost:8080 / :4000 / :3000`.

Alternatively, short-lived `kubectl port-forward` for quick API calls:

```bash
kubectl port-forward svc/litellm 4000:4000 -n ai-platform       # API
kubectl port-forward svc/open-webui 8080:8080 -n ai-platform     # Chat UI
```

### 8. Test a Model

```bash
export LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d)
export ALB_HOST=$(kubectl get ingress ai-platform-litellm -n ai-platform \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')

curl http://$ALB_HOST:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "Hello!"}]}'
```

Or the convenience wrapper (uses the SSM tunnel path by default):

```bash
./ops/test-model.sh gemma-4b "What is Kubernetes?"
```

### 9. Enable Langfuse Tracing

Langfuse is deployed automatically as part of the platform. Create an API key pair in the Langfuse UI at `http://<alb-host>:3000`:

```bash
kubectl create secret generic langfuse-litellm-keys -n ai-platform \
  --from-literal=LANGFUSE_PUBLIC_KEY=pk-lf-... \
  --from-literal=LANGFUSE_SECRET_KEY=sk-lf-...
kubectl rollout restart deployment litellm -n ai-platform
```

LiteLLM auto-detects the Langfuse keys and starts sending traces.

## InferenceEndpoint Reference

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: my-model        # Also used as the LiteLLM model name
  namespace: inference
spec:
  model: "org/model-id"           # REQUIRED — HuggingFace model ID
  gpuCount: 1                     # GPUs per worker — must be 1, 2, 4, or 8
  shared: false                   # GPU time-slicing — share GPU with other models (default: false)
  minReplicas: 1                  # Min Ray Serve replicas (default: 1)
  maxReplicas: 4                  # Max Ray Serve replicas (default: 4)
  workerMemory: "12Gi"            # Memory per GPU worker (default: 12Gi — fits 3B; the recommender emits a right-sized value per model)
  workerCpu: "4"                  # CPU per GPU worker (default: 4)
  maxModelLen: 8192               # Max sequence length (default: 8192)
  minVramPerGpuGiB: 0             # Min per-GPU VRAM in GiB (Karpenter hint, default: 0 = unconstrained)
  rayImage: "anyscale/ray-llm:2.54.0-py311-cu128"  # Override if needed
```

`minVramPerGpuGiB` adds a `nodeAffinity` rule (`karpenter.k8s.aws/instance-gpu-memory > n`) to the Ray worker pod template, so Karpenter is forced to pick a GPU that actually fits the model. Without it, Karpenter optimises purely for `$/hr` and can land on a T4 (16 GB) when the model needs an L4 (24 GB) or larger — which silently OOMs at model-load time. Leave it at `0` for small models that can run on any GPU; `recommend-instance.py` emits a correct conservative value automatically.

KRO expands this into a RayService (with vLLM backend), GPU worker pods, a LiteLLM registration Job (with validation), and a CloudWatch Log Group (via ACK).

The registration Job validates before deploying:
- `gpuCount` must be 1, 2, 4, or 8 (vLLM tensor parallelism requirement)
- Model ID is checked against HuggingFace (404 = immediate failure)

Status fields:

```bash
kubectl get inferenceendpoints -n inference
# NAME       READY   MODELSTATUS   MESSAGE                                          ENDPOINT
# gemma-4b   True    RUNNING       Model is live and serving requests               gemma-4b-serve-svc.inference.svc.cluster.local:8000
```

## GPU Time-Slicing

By default, each model gets a dedicated GPU node. For smaller models that don't need a full GPU, enable time-slicing to share a single GPU across up to 4 models — reducing GPU node costs by up to 75%.

### How it works

The platform uses two Karpenter NodePools, both open to any NVIDIA-backed AWS GPU instance (G and P families):

| Pool | Eligible instances | Use case |
|------|--------------------|----------|
| `gpu-inference` | Any NVIDIA G or P (g4dn, g5, g6, g6e, g7, p4d, p4de, p5, p5e, p5en, …) | Dedicated GPU for a single model |
| `gpu-shared` | Single-GPU NVIDIA G only (g4dn/g5/g6/g6e/g7 · xlarge–16xlarge) | Time-sliced (4 slices per GPU) for small models |

Both pools use semantic Karpenter requirements (`instance-category In [g, p]` + `instance-gpu-manufacturer In [nvidia]`) rather than enumerating families, so new G/P generations become eligible automatically. Cost blast-radius is bounded by each NodePool's `limits.nvidia.com/gpu` (16 for dedicated, 32 for shared) — adjust these in `terraform/30.eks/30.cluster/karpenter/gpu-*.yaml` if you want to allow more simultaneous P5 nodes or keep a tighter cap.

When `shared: true`, the GPU worker schedules onto a time-sliced node where NVIDIA's device plugin advertises 1 physical GPU as 4 `nvidia.com/gpu` resources. A [NodeOverlay](https://karpenter.sh/docs/concepts/nodeoverlays/) tells Karpenter about this capacity so it provisions 1 node for 4 models instead of 4 separate nodes.

> **Neuron/Trainium:** the platform runs vLLM via `anyscale/ray-llm` (CUDA build), and HuggingFace weights aren't pre-compiled with `neuronx-cc`, so inf2/trn1 instances are intentionally excluded. If you want Neuron support that requires a separate Ray image and ahead-of-time model compilation — out of scope today.

### Will my model fit?

**Option 1 — use the built-in recommender.** Outputs a ranked list of EC2 instances for your region, flags anything above your price ceiling, and emits a drop-in `InferenceEndpoint` snippet:

```bash
./ops/recommend-instance.py google/gemma-3-4b-it --seq 8192 --users 4
./ops/recommend-instance.py Qwen/Qwen2.5-32B-Instruct --quant int4 --seq 16384 --users 8
HF_TOKEN=hf_... ./ops/recommend-instance.py meta-llama/Llama-3.1-70B-Instruct --quant bf16
```

It reads the model's HuggingFace `config.json` (including GQA KV-head counts, MoE expert counts, etc.), estimates VRAM for weights + KV cache + activations, and matches against the full NVIDIA GPU instance catalog (G5/G6/G6e/P4/P5/P5e). Pricing is fetched live from the AWS Pricing API for your region (default from `$AWS_REGION`) and cached locally for 30 days.

Useful flags:
- `--region eu-central-1` — override the detected region
- `--max-price 15` — flag anything above $15/hr as over-budget (default: $20/hr)
- `--refresh-prices` — bust the pricing cache
- `--in-cluster-only` — suppress instances your Karpenter NodePools can't schedule
- `--json` — machine-readable output for scripting

Boto3 is a soft dependency: if it's not installed or credentials are missing, the tool falls back to the bundled us-east-1 catalog and says so in its output.

**Option 2 — web calculator.** The [APXML VRAM Calculator](https://apxml.com/tools/vram-calculator) is handy for interactive experimentation. As a rule of thumb for a g6.2xlarge (24GB VRAM, 4 slices = ~6GB each):

| Model size | Quantization | Shared? |
|-----------|-------------|---------|
| ≤ 1B params | FP16 | ✅ Yes |
| 1-3B params | FP16 | ✅ Yes |
| 3-4B params | FP16 | ⚠️ Tight — reduce `maxModelLen` |
| ≥ 7B params | FP16 | ❌ No — use dedicated GPU |

### Deploy a shared model

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: smollm3-3b
  namespace: inference
spec:
  model: "HuggingFaceTB/SmolLM3-3B"
  shared: true          # ← shares GPU with other models
  maxModelLen: 4096     # ← reduce context length to fit in ~6GB VRAM
```

## AITeam Reference

```yaml
apiVersion: kro.run/v1alpha1
kind: AITeam
metadata:
  name: team-search
  namespace: ai-platform
spec:
  teamName: search-ranking        # Creates namespace team-search-ranking
  models: ["gemma-4b", "qwen3-4b"]  # Allowed models (* = all)
  maxBudget: "50.0"               # USD budget
  budgetDuration: "30d"           # Reset period
  rpmLimit: 60                    # Requests per minute
  tpmLimit: 50000                 # Tokens per minute
```

KRO creates: namespace, ResourceQuota, NetworkPolicy, ServiceAccount, RoleBinding, LiteLLM team + scoped API key, and a welcome ConfigMap with usage instructions.

View team welcome info:

```bash
kubectl get configmap welcome -n team-search-ranking -o jsonpath='{.data.README\.md}'
```

## Repository Structure

```
argocd/                          # ArgoCD bootstrap — the only ArgoCD content in git
  bootstrap/                     # Synced by the root Application (rendered by Terraform)
    platform.yaml                #   ApplicationSet: platform-config, gpu-operator, kuberay, litellm, open-webui, langfuse
    workloads.yaml               #   ApplicationSet: models + teams
platform/                        # Platform team owns everything here
  config/                        #   KRO APIs, RBAC, Ingress
    kro/                         #     InferenceEndpoint + AITeam definitions
    rbac/                        #     team-developer ClusterRole + team-onboarding SA/ClusterRole
  services/                      #   Platform service configurations
    gpu-operator/                #     NVIDIA GPU Operator (Helm values)
    kuberay/                     #     KubeRay Operator (Helm values)
    langfuse/                    #     Langfuse (Helm values)
    litellm/                     #     LiteLLM + shared PostgreSQL (manifests)
    open-webui/                  #     Open WebUI (manifests)
workloads/                       # Self-service — teams add YAMLs here
  models/                        #   InferenceEndpoint instances
    TEMPLATE.yaml.example        #   Copy this to create a new model
  teams/                         #   AITeam instances
ops/                             # Operational scripts
  recommend-instance.py          #   Recommend EC2 GPU instances for a given model
  seed-model-cache.py            #   Pre-populate S3 HF weights cache for fast deploys
  ssm-tunnel.sh                  #   SSM port forwarding to platform services
  test-model.sh                  #   One-shot model testing
  scale-down.sh                  #   Cost savings: suspend platform
  scale-up.sh                    #   Restore platform via ArgoCD sync
  create-soci-index.sh           #   Create SOCI indices for large images
terraform/                       # Infrastructure (VPC, IAM, EKS, addons)
```

## Fast model deployment (S3 cache + warm pool)

First deploys of a model pull ~13 GB of container image and download weights from HuggingFace — typically 5-7 minutes end-to-end. The platform ships two optimizations that knock that down to ~90 seconds for demo-friendly models:

### HuggingFace weights cache in S3

Terraform creates an S3 bucket `{cluster-name}-model-cache` and an IRSA role scoped to the `inference:inference-worker` ServiceAccount. Every Ray worker pod gets:

- **initContainer** (`hf-cache-download`) — runs `s5cmd` to sync `s3://{bucket}/hf/{model-id}/` to a local `emptyDir` at `/hf-cache` before vLLM starts. Cache miss is silent: vLLM falls back to a live HF download as before.
- **HF_HOME** env var pointing at the same `emptyDir`, so vLLM reads from wherever the weights ended up.
- **auto-warm sidecar** (`hf-cache-uploader`) — after waiting 5 minutes for vLLM to finish loading, uploads the populated cache to S3 *if the bucket doesn't already have this model*. Next deploy of the same model hits the cache automatically.

Result: *any model tried before is fast; any new model still works, with a one-time slow first deploy.*

**Manual seed** (pre-populate before a demo):

```bash
./ops/seed-model-cache.py HuggingFaceTB/SmolLM3-3B
HF_TOKEN=hf_... ./ops/seed-model-cache.py google/gemma-3-4b-it
./ops/seed-model-cache.py list         # what's cached
./ops/seed-model-cache.py purge org/model
```

The tool reads the bucket name from the `platform-config` ConfigMap, downloads the model locally via `huggingface-cli` (uses `hf_transfer` for speed), and uploads to `s3://{bucket}/hf/{model}/` via `aws s3 sync`.

### Warm GPU node pool

`platform/config/warm-pool/gpu-shared-warmup.yaml` defines a low-priority (`value: -100`) placeholder Deployment that holds one GPU slice on the `gpu-shared` NodePool. Karpenter keeps the node alive; the Ray LLM image is pre-pulled there. When a `shared: true` InferenceEndpoint arrives, its pod preempts the placeholder and schedules onto the warm node — skipping ~90 s of Karpenter provisioning and ~100 s of image pull. Scale replicas to 0 to disable.

### Expected demo timeline (SmolLM3-3B)

| Stage | Cold | With S3 cache + warm pool |
|---|---|---|
| ArgoCD sync (with nudge) | 30 s | 10 s |
| Karpenter node provisioning | 90 s | **0 s** (warm) |
| Ray LLM image pull | 100 s | **0 s** (pre-pulled) |
| Weights → pod | 60 s (HF) | **~15 s** (S3 sync) |
| vLLM init + weight load | 45 s | 45 s |
| LiteLLM register + Open WebUI refresh | 35 s | 35 s |
| **Total from `git push` to working model** | ~5-7 min | **~90 s** |



Scale down during off-hours to release GPU nodes:

```bash
./ops/scale-down.sh    # Suspends ArgoCD, scales to 0, reclaims GPU nodes
./ops/scale-up.sh      # Re-enables ArgoCD auto-sync, reconciles everything
```

## Cleanup

```bash
kubectl delete inferenceendpoints --all -n inference   # Release GPU nodes
kubectl get nodes -l workload-type=gpu-inference       # Wait for termination (~5 min)
kubectl get nodes -l workload-type=gpu-shared          # Wait for shared nodes too

cd terraform
make ENVIRONMENT=your-env destroy-all
```

## Acknowledgments

The Terraform infrastructure is based on the [Automated Provisioning of Application-Ready Amazon EKS Clusters](https://aws-solutions-library-samples.github.io/compute/automated-provisioning-of-application-ready-amazon-eks-clusters.html) guidance from the AWS Solutions Library, extended with EKS Managed Capabilities (ArgoCD, KRO, ACK), GPU-optimized Karpenter NodePools, and the AI platform layer.
