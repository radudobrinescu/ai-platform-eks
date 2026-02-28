# AI Platform on EKS — GitOps Ready

A sample AI platform built on Amazon EKS that leverages EKS Capabilities (ArgoCD, KRO, ACK) and platform engineering practices to provide self-service AI infrastructure for teams.

Each AI use case is expressed as a KRO definition — a custom Kubernetes API that abstracts the underlying complexity into a single resource. Teams consume isolated, pre-configured environments that implement best practices without needing to understand Ray, GPU scheduling, or networking.

**Supported use cases:**
- **Model Inference** — deploy HuggingFace models with a single `InferenceEndpoint` resource
- **Chat Assistants with RAG** — model + vector DB + embeddings (planned)
- **Batch Inference for Document Processing** — Ray Jobs for offline processing (planned)
- **Agentic Workloads** — model + tools + orchestrator (planned)
- **GPU Management with DRA** — dynamic resource allocation for multi-model GPU sharing (planned)

## What You Get

- **Single custom resource** — `InferenceEndpoint` abstracts RayService, GPU scheduling, networking, and LiteLLM registration
- **GitOps workflow** — commit a YAML, ArgoCD deploys it, model is live
- **OpenAI-compatible API** — LiteLLM proxies all models behind `/v1/chat/completions`
- **Chat UI** — Open WebUI for interactive testing
- **LLM observability** — Langfuse traces every request
- **Fast cold starts** — Bottlerocket + SOCI Parallel Pull, optional ECR pull-through cache

## How It Works

```
git push → ArgoCD syncs → KRO creates RayService + registration Job
         → Karpenter provisions GPU node (Bottlerocket + SOCI)
         → vLLM loads model → LiteLLM registers endpoint
         → model available in Open WebUI and API
```

## Architecture

```
EKS Cluster
│
├── Managed Capabilities (AWS-hosted)
│   ├── ArgoCD       ──▶ syncs all apps and workloads from Git
│   ├── KRO          ──▶ turns custom resources into Kubernetes objects
│   └── ACK          ──▶ manages AWS resources from Kubernetes
│
├── Karpenter (self-managed)
│   ├── default          ──▶ platform app nodes (AL2023)
│   └── gpu-inference    ──▶ GPU nodes (Bottlerocket + SOCI)
│
├── Platform Apps (ArgoCD-managed, platform team owns)
│   ├── GPU Operator     (Helm)
│   ├── KubeRay          (Helm)
│   ├── LiteLLM          (manifests)  ──▶ OpenAI-compatible API proxy
│   ├── Open WebUI       (manifests)  ──▶ Chat UI
│   └── Langfuse         (Helm)       ──▶ LLM observability
│
├── KRO Definitions (ArgoCD-managed, platform team owns)
│   └── InferenceEndpoint CRD    ──▶ defines the API teams use
│
└── Workloads (ArgoCD-managed, teams self-serve)
    └── InferenceEndpoints (e.g. gemma-4b, qwen3-4b)
```

## Prerequisites

- **AWS CLI** configured with appropriate permissions
- **Terraform** >= 1.0
- **kubectl**
- **AWS Identity Center** configured (required for ArgoCD managed capability)

## Deployment

### 1. Configure Environment

```bash
cd terraform/00.global/vars/
cp example.tfvars your-env.tfvars
```

Edit `your-env.tfvars` with your Identity Center config, VPC CIDR, and capabilities.

### 2. (Optional) Enable ECR Pull-Through Cache

For ~60% faster image pulls. Requires a free Docker Hub account:

```bash
export TF_VAR_docker_hub_username="your-dockerhub-username"
export TF_VAR_docker_hub_access_token="dckr_pat_XXXXXXXXXX"
```

Without this, images pull directly from Docker Hub (works fine, just slower).

### 3. Bootstrap and Deploy

```bash
export AWS_REGION=eu-central-1
cd terraform
make bootstrap
make ENVIRONMENT=your-env apply-all
```

Terraform creates: VPC, IAM roles, EKS cluster with capabilities, Karpenter NodePools, ArgoCD cluster registration, all secrets (LiteLLM, Langfuse), and platform-config ConfigMap.

### 4. Update ArgoCD Source URLs

Replace the placeholder Git URL in all ArgoCD app definitions:

```bash
cd argocd/
sed -i '' 's|https://github.com/YOUR-ORG/YOUR-REPO.git|https://github.com/your-org/your-repo.git|g' *.yaml
grep repoURL *.yaml  # verify
```

### 5. Bootstrap ArgoCD Applications

```bash
aws eks update-kubeconfig --region $AWS_REGION --name your-cluster-name
kubectl apply -f argocd/
```

This creates 6 ArgoCD Applications:

| App | What it deploys |
|-----|-----------------|
| `gpu-operator` | NVIDIA GPU Operator (Helm) |
| `kuberay-operator` | KubeRay Operator (Helm) |
| `langfuse` | Langfuse LLM observability (Helm) |
| `litellm` | LiteLLM proxy + PostgreSQL |
| `open-webui` | Open WebUI chat interface |
| `workloads` | Namespaces, KRO definitions, inference workloads |

Wait for all apps to be Healthy:

```bash
kubectl get applications -n argocd
```

### 6. Create HuggingFace Token

Required for gated models (Gemma, Llama, etc.):

```bash
kubectl create secret generic hf-token -n inference \
  --from-literal=token=hf_YOUR_TOKEN_HERE
```

### 7. Deploy a Model

Commit an `InferenceEndpoint` to `platform/workloads/`:

```yaml
# platform/workloads/gemma-4b.yaml
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
git add platform/workloads/gemma-4b.yaml
git commit -m "feat: Deploy Gemma 3 4B"
git push
```

First deployment takes ~7 min with ECR cache, ~14 min without (GPU node + image pull + model loading).

### 8. Access Services

```bash
kubectl port-forward svc/litellm 4000:4000 -n ai-platform       # http://localhost:4000
kubectl port-forward svc/open-webui 8080:8080 -n ai-platform     # http://localhost:8080
kubectl port-forward svc/langfuse-web 3000:3000 -n ai-platform   # http://localhost:3000
```

Get the LiteLLM API key:

```bash
kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d
```

Test:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_MASTER_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "Hello!"}]}'
```

### 9. Enable Langfuse Tracing (optional)

After Langfuse is running, create an API key pair in the Langfuse UI (`http://localhost:3000`), then:

```bash
kubectl create secret generic langfuse-litellm-keys -n ai-platform \
  --from-literal=LANGFUSE_PUBLIC_KEY=pk-lf-... \
  --from-literal=LANGFUSE_SECRET_KEY=sk-lf-...
kubectl rollout restart deployment litellm -n ai-platform
```

## InferenceEndpoint Reference

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: my-model        # Also used as the LiteLLM model name
  namespace: inference
spec:
  model: "org/model-id"           # HuggingFace model ID
  gpuCount: 1                     # GPUs per worker (default: 1)
  minReplicas: 1                  # Min Ray Serve replicas (default: 1)
  maxReplicas: 4                  # Max Ray Serve replicas (default: 4)
  workerMemory: "24Gi"            # Memory per GPU worker (default: 24Gi)
  workerCpu: "4"                  # CPU per GPU worker (default: 4)
  maxModelLen: 8192               # Max sequence length (default: 8192)
  rayImage: "anyscale/ray-llm:2.53.0-py311-cu128"  # Override if needed
```

KRO generates: RayService, GPU workers, and a LiteLLM registration Job.

## Repository Structure

```
argocd/                          # ArgoCD Application definitions (bootstrap)
platform/
  namespaces/                    # Kubernetes namespaces
  kro-definitions/               # KRO ResourceGraphDefinitions
  apps/                          # Platform application configs
    gpu-operator/                #   NVIDIA GPU Operator helm values
    kuberay/                     #   KubeRay Operator helm values
    langfuse/                    #   Langfuse helm values
    litellm/                     #   LiteLLM deployment + DB + config
    open-webui/                  #   Open WebUI deployment
  workloads/                     # InferenceEndpoint instances (team-facing)
ops/                             # Operational scripts (scale-up, scale-down)
terraform/                       # Infrastructure (VPC, IAM, EKS, addons)
```

## Cost Management

Scale down the platform during off-hours (stops all workloads, releases GPU nodes):

```bash
./ops/scale-down.sh    # Suspends ArgoCD auto-sync, scales to 0
./ops/scale-up.sh      # Restores replicas, re-enables auto-sync
```

## Cleanup

```bash
# Remove workloads first (releases GPU nodes)
kubectl delete inferenceendpoints --all -n inference

# Wait for GPU nodes to terminate (~5 min)
kubectl get nodes -l workload-type=gpu-inference

# Destroy all infrastructure
cd terraform
make ENVIRONMENT=your-env destroy-all
```
