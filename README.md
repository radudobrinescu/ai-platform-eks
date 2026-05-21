# AI Platform on EKS

A self-service AI platform that lets teams deploy and serve LLMs on Amazon EKS through GitOps. Commit a short YAML, get a production-ready inference endpoint — the platform handles GPU provisioning, model serving, API routing, team isolation, and observability automatically.

**Core stack:** EKS Managed Capabilities (ArgoCD, KRO, ACK) · Karpenter · Ray Serve · vLLM · LiteLLM · Langfuse

## What Teams Get

| Capability | How it works |
|-----------|-------------|
| **Deploy any HuggingFace model** | Commit an `InferenceEndpoint` YAML → model is live in ~60s |
| **OpenAI-compatible API** | LiteLLM proxies all models behind `/v1/chat/completions` |
| **Chat UI** | Open WebUI for interactive testing |
| **Team isolation** | `AITeam` resource creates namespace, RBAC, budget, rate limits, scoped API key |
| **Auto GPU sizing** | Karpenter provisions the right GPU, scales to zero when idle |
| **Fast cold starts** | EBS snapshots (0s image pull) + S3 weight cache (~15s) + warm pool |
| **Observability** | Langfuse tracing on every request |
| **Fine-tuning** *(coming soon)* | `FineTuneJob` resource — same self-service pattern for model customization via Unsloth |

## How It Works

```
git push → ArgoCD syncs → KRO expands custom resource
         → Karpenter provisions GPU node (Bottlerocket + SOCI)
         → vLLM loads model → LiteLLM registers endpoint
         → model available via API and Open WebUI
```

## Architecture

```
EKS Cluster
├── Managed Capabilities (AWS-hosted)
│   ├── ArgoCD       → syncs platform + workloads from Git
│   ├── KRO          → expands custom resources into K8s objects
│   └── ACK          → manages AWS resources from Kubernetes
│
├── Karpenter NodePools
│   ├── default          → platform nodes (AL2023, Graviton)
│   ├── gpu-inference    → dedicated GPU nodes (Bottlerocket + SOCI)
│   └── gpu-shared       → time-sliced GPU nodes (4 models per GPU)
│
├── Platform Services
│   ├── GPU Operator     → NVIDIA device plugin + DCGM metrics
│   ├── KubeRay          → Ray cluster lifecycle
│   ├── LiteLLM          → OpenAI-compatible API gateway
│   ├── Open WebUI       → chat interface
│   ├── Platform DB      → shared PostgreSQL
│   └── Langfuse         → LLM tracing and analytics
│
└── Workloads (teams self-serve)
    ├── InferenceEndpoints
    └── AITeams
```

---

## Quick Start

### 1. Deploy Infrastructure

```bash
cd terraform/00.global/vars/
cp example.tfvars your-env.tfvars
# Edit: set Identity Center ARN, VPC CIDR, gitops_repo_url, capabilities

cd terraform
export AWS_REGION=eu-central-1
make bootstrap ENVIRONMENT=your-env
make ENVIRONMENT=your-env apply-all
```

This creates VPC, IAM roles, EKS cluster with managed capabilities, Karpenter NodePools, and all platform secrets. Cluster name: `{resources_prefix}-{ENVIRONMENT}`.

### 2. (Optional) GPU Image Optimization

Enable ECR pull-through cache for faster image pulls:

```bash
export TF_VAR_docker_hub_username="your-dockerhub-username"
export TF_VAR_docker_hub_access_token="dckr_pat_XXXXXXXXXX"
```

With this set, Terraform **automatically** creates a SOCI index + EBS data volume snapshot on `apply`. Two-apply flow for new clusters — second apply wires the snapshot into Karpenter NodeClasses:

```bash
make ENVIRONMENT=your-env MODULE=./30.eks/30.cluster apply
```

### 3. Verify ArgoCD

ArgoCD is deployed as a managed capability — no manual bootstrap needed. Terraform renders a bootstrap Application pointing at `argocd/bootstrap/` with your `gitops_repo_url`.

```bash
aws eks update-kubeconfig --region $AWS_REGION --name ai-platform-your-env
kubectl get applications -n argocd
```

### 4. Create HuggingFace Token (for gated models)

```bash
kubectl create secret generic hf-token -n inference \
  --from-literal=token=hf_YOUR_TOKEN_HERE
```

### 5. Deploy a Model

```bash
cp workloads/models/TEMPLATE.yaml.example workloads/models/my-model.yaml
```

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gemma-4b
  namespace: inference
spec:
  model: "google/gemma-3-4b-it"
  gpuCount: 1
```

```bash
git add workloads/models/my-model.yaml
git commit -m "feat: deploy gemma-4b"
git push
kubectl get inferenceendpoints -n inference -w
```

### 6. Access Services

The platform exposes services via an internet-facing ALB restricted by IP allowlist:

- `http://<alb>:8080` — Open WebUI
- `http://<alb>:4000` — LiteLLM API
- `http://<alb>:3000` — Langfuse

```bash
# Get ALB hostname
kubectl get ingress ai-platform-litellm -n ai-platform \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

# Or use SSM tunnel (works from anywhere, no public IP needed)
./ops/ssm-tunnel.sh    # localhost:8080 / :4000 / :3000
```

Update the allowlist in `platform/config/ingress.yaml` when your IP changes.

### 7. Test

```bash
./ops/test-model.sh gemma-4b "What is Kubernetes?"
```

---

## GPU Instance Recommender

The built-in recommender reads model architecture from HuggingFace, estimates VRAM, models decode throughput, picks the cheapest GPU with the right parallelism strategy, and emits a ready-to-commit YAML:

```bash
# What GPU fits a 4B model?
./ops/recommend-instance.py google/gemma-3-4b-it

# 8B model, 16K context, 8 concurrent users
./ops/recommend-instance.py meta-llama/Llama-3.1-8B-Instruct --seq 16384 --users 8

# Quantised 32B — int4 halves VRAM
./ops/recommend-instance.py Qwen/Qwen2.5-32B-Instruct --quant int4

# 100 users with 25 tok/s SLO — auto-scales to fleet if needed
./ops/recommend-instance.py Qwen/Qwen2.5-7B-Instruct --users 100 --target-tok-s 25

# Gated model
HF_TOKEN=hf_... ./ops/recommend-instance.py google/gemma-3-27b-it
```

Key flags: `--quant`, `--kv-quant`, `--seq`, `--users`, `--tp`, `--target-tok-s`, `--max-price`, `--in-cluster-only`, `--json`, `--workload`. Run with `--help` for full documentation.

---

## Custom Resource Reference

### InferenceEndpoint

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: my-model
  namespace: inference
spec:
  model: "org/model-id"           # REQUIRED — HuggingFace model ID
  gpuCount: 1                     # GPUs per worker (1, 2, 4, or 8)
  tensorParallelSize: 1           # TP — shards each layer's weights across GPUs (prefers NVLink)
  pipelineParallelSize: 1         # PP — assigns layer groups to pipeline stages (works on any interconnect)
  shared: false                   # Time-slice GPU with up to 4 models
  minReplicas: 1
  maxReplicas: 4
  maxModelLen: 8192               # Max sequence length
  minVramPerGpuGiB: 0             # Karpenter GPU sizing hint (0 = unconstrained)
  workerMemory: "12Gi"            # CPU memory per Ray worker
  workerCpu: "4"
```

KRO expands this into: RayService (vLLM backend), GPU worker pods, LiteLLM registration Job, CloudWatch Log Group.

**Parallelism** (or let `recommend-instance.py` choose):
- `gpuCount: 1` — single GPU (most ≤7B models)
- `gpuCount: 4, tensorParallelSize: 4` — NVLink node (A100/H100)
- `gpuCount: 4, pipelineParallelSize: 4` — PCIe node (L4/L40S/A10G)
- `gpuCount: 8, tensorParallelSize: 4, pipelineParallelSize: 2` — TP×PP

### AITeam

```yaml
apiVersion: kro.run/v1alpha1
kind: AITeam
metadata:
  name: team-search
  namespace: ai-platform
spec:
  teamName: search-ranking
  models: ["gemma-4b", "qwen3-4b"]
  maxBudget: "50.0"
  budgetDuration: "30d"
  rpmLimit: 60
  tpmLimit: 50000
```

KRO creates: namespace, ResourceQuota, NetworkPolicy, RBAC, LiteLLM team + scoped API key, welcome ConfigMap.

### FineTuneJob *(coming soon)*

```yaml
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: gemma-support-v1
  namespace: inference
spec:
  baseModel: "google/gemma-3-4b-it"
  dataset: "s3://my-bucket/training-data.jsonl"
  gpuCount: 1
  autoDeploy: true              # Deploy as InferenceEndpoint when done
```

Self-service fine-tuning via Unsloth — same GitOps pattern. Handles validation, QLoRA training, model export to S3, and optional auto-deployment.

---

## GPU Time-Slicing

For models that don't need a full GPU, `shared: true` enables NVIDIA time-slicing — up to 4 models share one physical GPU, reducing costs by ~75%.

| Model size | Shared? |
|-----------|---------|
| ≤ 3B (FP16) | Yes |
| 3-4B (FP16) | Tight — reduce `maxModelLen` |
| ≥ 7B | No — use dedicated GPU |

```yaml
spec:
  model: "HuggingFaceTB/SmolLM3-3B"
  shared: true
  maxModelLen: 4096
```

---

## Cold-Start Optimization

Four layers reduce first-inference time from ~7 min to ~60s:

| Layer | Effect | How |
|-------|--------|-----|
| EBS data volume snapshot | 0s image pull | Terraform auto-creates on `ray_image_tag` change |
| SOCI lazy-loading | ~50% faster pull (fallback) | Terraform auto-creates alongside snapshot |
| S3 model weight cache | ~15s vs ~60s model load | Sidecar auto-warms after first deploy |
| GPU warm pool | Skip 90s node provisioning | Placeholder pod in `platform/config/warm-pool/` |

**Pre-seed the cache** before a demo:

```bash
./ops/seed-model-cache.py HuggingFaceTB/SmolLM3-3B
./ops/seed-model-cache.py list
```

---

## Operational Scripts

```bash
./ops/recommend-instance.py <model>       # GPU sizing + fleet scaling
./ops/test-model.sh <name> "prompt"          # Quick model test
./ops/ssm-tunnel.sh                          # Port-forward via SSM
./ops/seed-model-cache.py <model>            # Pre-populate S3 weight cache
./ops/scale-down.sh                          # Suspend platform, reclaim GPUs
./ops/scale-up.sh                            # Restore via ArgoCD sync
./ops/demo.sh                                # End-to-end demo flow
```

---

## Repository Structure

```
argocd/bootstrap/            # ApplicationSets (platform + workloads)
platform/
  config/kro/                # InferenceEndpoint + AITeam definitions
  config/rbac/               # ClusterRoles
  config/ingress.yaml        # ALB routing
  config/warm-pool/          # GPU warm-pool placeholder
  services/                  # gpu-operator, kuberay, litellm, open-webui, langfuse
workloads/
  models/                    # InferenceEndpoint YAMLs (self-service)
  teams/                     # AITeam YAMLs (self-service)
ops/                         # Operational scripts
terraform/                   # Infrastructure modules (VPC → IAM → EKS → Observability)
```

---

## Langfuse Tracing

Langfuse is deployed automatically. For ALB access, set `langfuse.nextauth.url` in `platform/services/langfuse/helm-values.yaml` to your ALB hostname. Then create API keys in the Langfuse UI and wire them to LiteLLM:

```bash
kubectl create secret generic langfuse-litellm-keys -n ai-platform \
  --from-literal=LANGFUSE_PUBLIC_KEY=pk-lf-... \
  --from-literal=LANGFUSE_SECRET_KEY=sk-lf-...
kubectl rollout restart deployment litellm -n ai-platform
```

---

## Cleanup

```bash
kubectl delete inferenceendpoints --all -n inference
cd terraform && make ENVIRONMENT=your-env destroy-all
```

---

## Acknowledgments

Infrastructure based on [Automated Provisioning of Application-Ready Amazon EKS Clusters](https://aws-solutions-library-samples.github.io/compute/automated-provisioning-of-application-ready-amazon-eks-clusters.html) from the AWS Solutions Library, extended with EKS Managed Capabilities, GPU-optimized Karpenter NodePools, and the AI platform layer.
