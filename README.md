# AI Platform on EKS - GitOps Ready

A complete AI inference platform on Amazon EKS that delivers on the promise of **"deploying complete AI stacks with a single custom resource"** using GitOps, KRO (Kube Resource Orchestrator), and EKS managed capabilities.

## Architecture

This platform combines:
- **EKS Auto Mode** or **Self-managed Karpenter** for compute
- **ArgoCD** (EKS managed capability) for GitOps
- **KRO** (Kube Resource Orchestrator) for custom APIs
- **ACK** (AWS Controllers for Kubernetes) for AWS resource management
- **GPU Operator + KubeRay** for AI workload orchestration
- **LiteLLM + Open WebUI** for model serving and interaction

## Prerequisites

### Required
- **AWS CLI** configured with appropriate permissions
- **Terraform** >= 1.0
- **kubectl** for cluster interaction
- **AWS Identity Center** configured (required for ArgoCD capability)
  - Get your Identity Center instance ARN: `aws sso-admin list-instances`
  - Get user/group IDs for RBAC configuration

### Optional
- **HuggingFace Token** for gated models (like Gemma)

## Quick Start

### 1. Configure Environment

Copy and customize your environment configuration:

```bash
cd terraform/00.global/vars/
cp example.tfvars your-env.tfvars
```

**Critical**: Update `your-env.tfvars` with:
- Your AWS Identity Center instance ARN and region
- Your SSO user/group IDs for ArgoCD RBAC
- Desired EKS configuration (Auto Mode vs self-managed)

### 2. Bootstrap Terraform Backend

```bash
export AWS_REGION=eu-central-1
export AWS_PROFILE=default
cd terraform
make bootstrap
```

### 3. Deploy Infrastructure

```bash
make ENVIRONMENT=your-env apply-all
```

This deploys in order:
1. **Networking** - VPC, subnets, endpoints
2. **IAM Roles** - EKS access roles
3. **EKS Cluster** - With ArgoCD, KRO, ACK capabilities
4. **EKS Addons** - Load balancer controller, etc.
5. **Observability** - CloudWatch or Prometheus/Grafana

### 4. Configure GitOps

After cluster deployment, apply the ArgoCD applications:

```bash
# Get cluster credentials
aws eks update-kubeconfig --region $AWS_REGION --name your-cluster-name

# Apply ArgoCD applications (bootstrap GitOps)
kubectl apply -f argocd/platform-app.yaml
kubectl apply -f argocd/gpu-operator.yaml
kubectl apply -f argocd/kuberay-operator.yaml
kubectl apply -f argocd/langfuse.yaml
kubectl apply -f argocd/workloads-appset.yaml
```

### 5. Deploy AI Workload

Create a HuggingFace token secret (for gated models):

```bash
kubectl create secret generic hf-token -n inference \
  --from-literal=token=hf_YOUR_TOKEN_HERE
```

Deploy an inference endpoint by committing to the repository:

```yaml
# workloads/examples/my-model.yaml
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

Commit and push - ArgoCD will automatically deploy!

## GitOps Workflow

### Platform Components (Continuously Synced)

**From `/platform/` directory:**
- **Namespaces** - ai-platform, inference
- **KRO Definitions** - InferenceEndpoint custom resource
- **Core Apps** - LiteLLM, Open WebUI
- **Helm Values** - GPU Operator, KubeRay, Langfuse configurations

**From `/argocd/` directory (bootstrap only):**
- ArgoCD Applications for Helm charts
- ApplicationSet for user workloads

### User Workloads (Continuously Synced)

**From `/workloads/` directory:**
- User-created `InferenceEndpoint` resources
- ArgoCD automatically detects and deploys new workloads

### The Magic: Single Custom Resource

Users only need to create:

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: my-model
spec:
  model: "microsoft/DialoGPT-medium"
  gpuCount: 1
```

KRO automatically generates:
- RayService for model serving
- RayCluster for compute
- Services for networking
- ConfigMaps for configuration
- All necessary Ray resources

## Configuration Options

### EKS Auto Mode vs Self-Managed

**Auto Mode** (Recommended):
```hcl
cluster_config = {
  eks_auto_mode = true
  capabilities = {
    # All self-managed addons automatically disabled
    gitops = true  # ArgoCD
    kro    = true  # KRO
    ack    = true  # ACK
  }
}
```

**Self-Managed**:
```hcl
cluster_config = {
  eks_auto_mode = false
  capabilities = {
    # Enable required addons
    networking    = true  # VPC CNI
    autoscaling   = true  # Karpenter
    blockstorage  = true  # EBS CSI
    loadbalancing = true  # LB Controller
    # EKS managed capabilities
    gitops = true
    kro    = true
    ack    = true
  }
}
```

### Identity Center Configuration

```hcl
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
```

## Accessing Services

### ArgoCD UI
```bash
kubectl port-forward svc/argocd-server -n argocd 8080:443
# Access: https://localhost:8080
# Login via AWS Identity Center
```

### Open WebUI
```bash
kubectl port-forward svc/open-webui-service -n ai-platform 8081:80
# Access: http://localhost:8081
```

### LiteLLM API
```bash
kubectl port-forward svc/litellm-service -n ai-platform 8082:8000
# API: http://localhost:8082
```

## Troubleshooting

### Check ArgoCD Application Status
```bash
kubectl get applications -n argocd
kubectl describe application platform -n argocd
```

### Check KRO Resource Generation
```bash
kubectl get inferenceendpoints -n inference
kubectl describe inferenceendpoint gemma-4b -n inference
```

### Check Ray Services
```bash
kubectl get rayservices -n inference
kubectl get rayclusters -n inference
```

### GPU Node Provisioning
```bash
kubectl get nodes -l node.kubernetes.io/instance-type
kubectl describe nodepool gpu-inference  # For Auto Mode
```

## Cleanup

```bash
# Destroy all resources
make ENVIRONMENT=your-env destroy-all AUTO_APPROVE=true
```

## Architecture Decisions

### GitOps-First Design
- **Git as Source of Truth** - All platform and workload definitions in Git
- **Automated Sync** - ArgoCD continuously reconciles cluster state with Git
- **Declarative APIs** - Users interact with simple custom resources

### EKS Managed Capabilities
- **ArgoCD** - Fully managed GitOps without cluster overhead
- **KRO** - Managed custom resource orchestration
- **ACK** - Native AWS resource management via Kubernetes APIs

### Simplified User Experience
- **Single Custom Resource** - `InferenceEndpoint` abstracts complexity
- **Automatic Resource Generation** - KRO creates all necessary Kubernetes resources
- **Self-Service** - Users deploy by committing YAML files

This delivers on the promise: **Deploy complete AI stacks with a single custom resource via GitOps**.
