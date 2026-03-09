# AI Platform on EKS

Self-service AI inference platform on Amazon EKS. Teams deploy models by committing a short YAML file — the platform handles GPU provisioning, model serving, API routing, and observability.

Built on EKS Managed Capabilities (ArgoCD, KRO, ACK), Karpenter, Ray Serve, and vLLM.

## What You Get

- **Single custom resource** — `InferenceEndpoint` abstracts RayService, GPU scheduling, networking, and LiteLLM registration
- **GitOps workflow** — commit a YAML, ArgoCD deploys it, model is live
- **OpenAI-compatible API** — LiteLLM proxies all models behind `/v1/chat/completions`
- **Chat UI** — Open WebUI for interactive testing
- **LLM observability** — Langfuse traces every request
- **Fast cold starts** — Bottlerocket + SOCI parallel pull, optional ECR pull-through cache
- **Team onboarding** — `AITeam` resource creates namespace, RBAC, quotas, and scoped API key

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
│   └── gpu-inference    ──▶  GPU nodes (Bottlerocket + SOCI)
│
├── Platform Services (ArgoCD-managed)
│   ├── GPU Operator     ──▶  NVIDIA device plugin + DCGM metrics
│   ├── KubeRay          ──▶  Ray cluster lifecycle
│   ├── LiteLLM          ──▶  OpenAI-compatible API gateway
│   ├── Open WebUI       ──▶  chat interface
│   └── Langfuse         ──▶  LLM tracing and analytics
│
├── Platform Config (ArgoCD-managed)
│   ├── KRO definitions  ──▶  InferenceEndpoint, AITeam APIs
│   ├── Namespaces        ──▶  ai-platform, inference
│   ├── RBAC             ──▶  team-developer ClusterRole
│   └── Ingress          ──▶  ALB routing to all services
│
└── Workloads (ArgoCD-managed, teams self-serve)
    └── InferenceEndpoints (e.g. gemma-4b, smolLM3)
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

Edit `your-env.tfvars` — set your Identity Center ARN, VPC CIDR, and capabilities.

### 2. Deploy Infrastructure

```bash
export AWS_REGION=eu-central-1
cd terraform
make bootstrap
make ENVIRONMENT=your-env apply-all
```

This creates the VPC, IAM roles, EKS cluster with managed capabilities, Karpenter NodePools, and all platform secrets (LiteLLM, Langfuse).

### 3. (Optional) Enable ECR Pull-Through Cache

Mirrors Docker Hub images to ECR for ~60% faster GPU node image pulls:

```bash
export TF_VAR_docker_hub_username="your-dockerhub-username"
export TF_VAR_docker_hub_access_token="dckr_pat_XXXXXXXXXX"
```

Without this, images pull directly from Docker Hub (works fine, just slower).

### 3b. (Optional) Create SOCI Indices for Faster Cold Starts

GPU nodes use Bottlerocket with the SOCI snapshotter in `parallel-pull-unpack` mode. Without SOCI indices, images are pulled in parallel but fully downloaded before containers start. With SOCI indices, containers start via lazy-loading — only fetching layers on demand (~30-70% faster cold starts).

Create a SOCI index for the Ray LLM image (or any large ECR image):

```bash
./ops/create-soci-index.sh 802019299867.dkr.ecr.eu-central-1.amazonaws.com/docker-hub/anyscale/ray-llm:2.54.0-py311-cu128
```

This runs on a criticaladdons node via SSM (requires the 100GB EBS volume from the MNG config). Re-run whenever you update the Ray image tag.

> **Note:** The AWS SOCI Index Builder (Lambda-based) has a 6 GB compressed image limit. The Ray LLM image is ~13 GB, so indices must be created via this script instead.

### 4. Bootstrap ArgoCD

Update the Git repo URL in all ArgoCD application definitions:

```bash
cd argocd/
sed -i '' 's|https://github.com/radudobrinescu/ai-platform-eks.git|https://github.com/YOUR-ORG/YOUR-REPO.git|g' *.yaml
```

Apply them to the cluster:

```bash
aws eks update-kubeconfig --region $AWS_REGION --name your-cluster-name
kubectl apply -f argocd/
```

This creates 7 ArgoCD Applications:

| App | What it syncs |
|-----|---------------|
| `platform-config` | Namespaces, KRO definitions, RBAC, Ingress |
| `gpu-operator` | NVIDIA GPU Operator (Helm) |
| `kuberay-operator` | KubeRay Operator (Helm) |
| `litellm` | LiteLLM proxy + PostgreSQL |
| `open-webui` | Open WebUI chat interface |
| `langfuse` | Langfuse LLM observability (Helm) |
| `workloads` | InferenceEndpoint instances (self-service) |

Wait for all apps to sync:

```bash
kubectl get applications -n argocd
```

### 5. Create HuggingFace Token

Required for gated models (Gemma, Llama, etc.):

```bash
kubectl create secret generic hf-token -n inference \
  --from-literal=token=hf_YOUR_TOKEN_HERE
```

### 6. Deploy a Model

Add an `InferenceEndpoint` to `workloads/models/`:

```yaml
# workloads/models/gemma-4b.yaml
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
git add workloads/models/gemma-4b.yaml
git commit -m "feat: Deploy Gemma 3 4B"
git push
```

First deployment takes ~7 min with ECR cache, ~14 min without (GPU node provisioning + image pull + model loading).

### 7. Access Services

The platform uses an internal ALB with per-service listener ports. Use the SSM tunnel script to access all services from your laptop:

```bash
./ops/ssm-tunnel.sh
```

This creates three SSM port forwards through a criticaladdons node:
- `http://localhost:8080` — Open WebUI
- `http://localhost:4000` — LiteLLM API
- `http://localhost:3000` — Langfuse

Requires the [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (`brew install --cask session-manager-plugin`).

Alternatively, use `kubectl port-forward` directly:

```bash
kubectl port-forward svc/litellm 4000:4000 -n ai-platform       # API
kubectl port-forward svc/open-webui 8080:8080 -n ai-platform     # Chat UI
kubectl port-forward svc/langfuse-web 3000:3000 -n ai-platform   # Observability
```

Get the API key and test:

```bash
export LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d)

curl http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "Hello!"}]}'
```

### 8. Enable Langfuse Tracing (optional)

Create an API key pair in the Langfuse UI at `http://localhost:3000`, then:

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
  rayImage: "anyscale/ray-llm:2.54.0-py311-cu128"  # Override if needed
```

KRO expands this into a RayService (with vLLM backend), GPU worker pods, and a LiteLLM registration Job.

Status fields:

```bash
kubectl get inferenceendpoints -n inference
# NAME       READY   MODELSTATUS   ENDPOINT
# gemma-4b   True    RUNNING       gemma-4b-serve-svc.inference.svc.cluster.local:8000
```

## Repository Structure

```
argocd/                          # ArgoCD Application definitions (one-time bootstrap)
platform/                        # Platform team owns everything here
  config/                        #   Namespaces, KRO APIs, RBAC, Ingress
    kro/                         #     InferenceEndpoint + AITeam definitions
    rbac/                        #     team-developer ClusterRole
  services/                      #   Platform service configurations
    gpu-operator/                #     NVIDIA GPU Operator (Helm values)
    kuberay/                     #     KubeRay Operator (Helm values)
    langfuse/                    #     Langfuse (Helm values)
    litellm/                     #     LiteLLM proxy + DB (manifests)
    open-webui/                  #     Open WebUI (manifests)
workloads/                       # Self-service — teams add YAMLs here
  models/                        #   InferenceEndpoint instances
  teams/                         #   AITeam instances (managed separately)
ops/                             # Operational scripts
terraform/                       # Infrastructure (VPC, IAM, EKS, addons)
```

## Cost Management

Scale down during off-hours to release GPU nodes:

```bash
./ops/scale-down.sh    # Suspends ArgoCD, scales to 0, reclaims GPU nodes
./ops/scale-up.sh      # Restores replicas, re-enables ArgoCD auto-sync
```

## Cleanup

```bash
kubectl delete inferenceendpoints --all -n inference   # Release GPU nodes
kubectl get nodes -l workload-type=gpu-inference       # Wait for termination (~5 min)

cd terraform
make ENVIRONMENT=your-env destroy-all
```

## Acknowledgments

The Terraform infrastructure is based on the [Automated Provisioning of Application-Ready Amazon EKS Clusters](https://aws-solutions-library-samples.github.io/compute/automated-provisioning-of-application-ready-amazon-eks-clusters.html) guidance from the AWS Solutions Library, extended with EKS Managed Capabilities (ArgoCD, KRO, ACK), GPU-optimized Karpenter NodePools, and the AI platform layer.
