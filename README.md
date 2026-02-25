# AI Platform on EKS — GitOps Ready

A self-service AI inference platform on Amazon EKS. Teams deploy models with a single `InferenceEndpoint` custom resource — KRO, ArgoCD, and Ray handle everything else.

## What You Get

- **Single custom resource** — `InferenceEndpoint` abstracts RayService, GPU scheduling, networking, and LiteLLM registration
- **GitOps workflow** — commit a YAML, ArgoCD deploys it, model is live
- **OpenAI-compatible API** — LiteLLM proxies all models behind a unified `/v1/chat/completions` endpoint
- **Chat UI** — Open WebUI for interactive testing
- **LLM observability** — Langfuse traces every request

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  EKS Cluster                                                    │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ ArgoCD   │  │   KRO    │  │   ACK    │  │   Karpenter   │  │
│  │(managed) │  │(managed) │  │(managed) │  │(self-managed) │  │
│  └────┬─────┘  └────┬─────┘  └──────────┘  └───────┬───────┘  │
│       │              │                              │           │
│       ▼              ▼                              ▼           │
│  ┌─────────┐  ┌──────────────┐              ┌─────────────┐   │
│  │ ArgoCD  │  │InferenceEnd- │              │  GPU Nodes   │   │
│  │  Apps   │  │point → Ray-  │              │ (g5/g6/g7)   │   │
│  │         │  │Service+Job   │              └─────────────┘   │
│  └─────────┘  └──────────────┘                                 │
│                                                                 │
│  Platform Apps:  GPU Operator │ KubeRay │ LiteLLM │ Open WebUI │
│                  Langfuse                                       │
└─────────────────────────────────────────────────────────────────┘
```

## Prerequisites

- **AWS CLI** configured with appropriate permissions
- **Terraform** >= 1.0
- **kubectl**
- **AWS Identity Center** configured (required for ArgoCD managed capability)
  - Instance ARN: `aws sso-admin list-instances`
  - User/group IDs for RBAC

## Deployment — End to End

### 1. Configure Environment

```bash
cd terraform/00.global/vars/
cp example.tfvars your-env.tfvars
```

Edit `your-env.tfvars` — the critical settings:

```hcl
# VPC
vpc_cidr = "10.4.0.0/16"

shared_config = {
  resources_prefix = "ai-platform"  # Cluster name prefix
}

cluster_config = {
  kubernetes_version  = "1.35"
  eks_auto_mode       = false
  create_mng_system   = true

  capabilities = {
    kube_proxy    = true
    networking    = true
    coredns       = true
    identity      = true
    autoscaling   = true   # Karpenter
    blockstorage  = true
    loadbalancing = true
    gitops        = true   # ArgoCD (managed)
    kro           = true   # KRO (managed)
    ack           = true   # ACK (managed)
  }

  # REQUIRED: Your Identity Center config
  capabilities_config = {
    argocd_idc_instance_arn = "arn:aws:sso:::instance/ssoins-XXXXXXXXXX"
    argocd_idc_region       = "us-east-1"
    argocd_rbac_mappings = [
      {
        role = "ADMIN"
        identities = [
          { id = "your-sso-user-id", type = "SSO_USER" }
        ]
      }
    ]
  }
}

# OPTIONAL: ECR pull-through cache for faster image pulls (~60% faster)
# Requires a free Docker Hub account. Without this, images pull from Docker Hub directly.
# docker_hub_credentials = {
#   username     = "your-dockerhub-username"
#   access_token = "dckr_pat_XXXXXXXXXX"
# }
```

### 2. Bootstrap Terraform Backend

```bash
export AWS_REGION=eu-central-1
cd terraform
make bootstrap
```

### 3. Deploy Infrastructure

```bash
make ENVIRONMENT=your-env apply-all
```

This deploys (in order):
1. **Networking** — VPC, subnets, endpoints
2. **IAM Roles** — EKS access roles (ClusterAdmin, Admin, Edit, View)
3. **EKS Cluster** — with ArgoCD, KRO, ACK capabilities
4. **EKS Addons** — LB controller, etc.

Terraform also creates automatically:
- ArgoCD cluster registration (`local-cluster` secret with cluster ARN)
- ArgoCD access policy (ClusterAdminPolicy for deploying apps)
- LiteLLM secrets (`litellm-secrets` in ai-platform, `litellm-api-key` in inference)
- Langfuse secrets (`langfuse-secrets` in ai-platform)
- Karpenter NodePools (default + gpu-inference)

### 4. Bootstrap ArgoCD Applications

After Terraform completes, apply the ArgoCD applications:

```bash
# Get cluster credentials (Terraform does this automatically, but just in case)
aws eks update-kubeconfig --region $AWS_REGION --name your-cluster-name

# Apply all ArgoCD applications
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

Wait for all apps to sync:

```bash
kubectl get applications -n argocd
# All should show Healthy within a few minutes
```

### 5. Create HuggingFace Token (for gated models)

Required for models like Gemma, Llama, etc.:

```bash
kubectl create secret generic hf-token -n inference \
  --from-literal=token=hf_YOUR_TOKEN_HERE
```

### 6. Deploy a Model

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

Push to git — ArgoCD syncs it, KRO creates the RayService, Karpenter provisions a GPU node, and the model auto-registers with LiteLLM.

```bash
git add platform/workloads/gemma-4b.yaml
git commit -m "feat: Deploy Gemma 3 4B"
git push
```

First deployment takes ~10-15 min (GPU node provisioning + ~15GB image pull + model loading).

### 7. Access Services

```bash
# LiteLLM API
kubectl port-forward svc/litellm 4000:4000 -n ai-platform
# → http://localhost:4000

# Open WebUI
kubectl port-forward svc/open-webui 8080:8080 -n ai-platform
# → http://localhost:8080

# Langfuse
kubectl port-forward svc/langfuse-web 3000:3000 -n ai-platform
# → http://localhost:3000

# Ray Dashboard (per model)
kubectl port-forward svc/<model-name>-head-svc 8265:8265 -n inference
# → http://localhost:8265
```

Get the LiteLLM API key:

```bash
kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d
```

Test the model:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_MASTER_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "Hello!"}]}'
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
  rayImage: "anyscale/ray-llm:2.53.0-py311-cu128"  # Ray image
```

KRO generates: RayService, RayCluster, GPU workers, and a LiteLLM registration Job.

## Repository Structure

```
argocd/                          # ArgoCD Application definitions (bootstrap)
  gpu-operator.yaml
  kuberay-operator.yaml
  langfuse.yaml
  litellm.yaml
  open-webui.yaml
  workloads.yaml

platform/
  namespaces/                    # Kubernetes namespaces (synced by workloads app)
  kro-definitions/               # KRO ResourceGraphDefinitions
    inference-endpoint.yaml      # InferenceEndpoint → RayService + LiteLLM registration
  apps/                          # Platform application configs
    gpu-operator/helm-values.yaml
    kuberay/helm-values.yaml
    langfuse/helm-values.yaml
    litellm/litellm.yaml
    open-webui/open-webui.yaml
  workloads/                     # KRO workload instances (synced by workloads app)
    gemma-4b.yaml

terraform/
  00.global/vars/                # Environment configurations
  10.networking/                 # VPC, subnets
  20.iam-roles-for-eks/          # IAM roles
  30.eks/30.cluster/             # EKS + capabilities + secrets
  30.eks/35.addons/              # Additional addons
  40.observability/              # Monitoring (optional)
```

## Cleanup

```bash
# Remove workloads first (releases GPU nodes)
kubectl delete inferenceendpoints --all -n inference

# Wait for GPU nodes to terminate
kubectl get nodes -l workload-type=gpu-inference

# Destroy all infrastructure
cd terraform
make ENVIRONMENT=your-env destroy-all
```
