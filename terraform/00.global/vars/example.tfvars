# VPC CIDR for the platform network. IMPORTANT: keep this in sync with the
# `alb.ingress.kubernetes.io/inbound-cidrs` value in the ingress manifests
# (platform/config/ingress.yaml x3 + platform/services/cluster-dashboard/manifests.yaml).
# For the default internal ALB, that allowlist must cover this CIDR so in-VPC
# sources — the CloudFront edge's VPC origin and the SSM tunnel — can reach the
# ALB. Both ship as 10.10.0.0/16; if you change one, change the other.
vpc_cidr = "10.10.0.0/16"

tags = {}

shared_config = {
  resources_prefix = "ai-platform"
}

cluster_config = {
  kubernetes_version  = "1.36"
  eks_auto_mode       = false
  private_eks_cluster = false
  create_mng_system   = true # Required — runs Karpenter, CoreDNS, VPC CNI

  capabilities = {
    kube_proxy    = true # kube proxy
    networking    = true # VPC CNI
    coredns       = true # CoreDNS
    identity      = true # Pod Identity
    autoscaling   = true # Karpenter
    blockstorage  = true # EBS CSI Driver
    loadbalancing = true # LB Controller

    # EKS Managed Capabilities (AWS-managed, run in AWS-owned infrastructure)
    gitops = true # ArgoCD — requires Identity Center (see capabilities_config)
    kro    = true # Kube Resource Orchestrator
    ack    = true # AWS Controllers for Kubernetes
  }

  # Required when gitops = true
  # See: https://docs.aws.amazon.com/eks/latest/userguide/argocd.html
  capabilities_config = {
    argocd_idc_instance_arn = "arn:aws:sso:::instance/ssoins-XXXXXXXXXX" # REPLACE
    argocd_idc_region       = "us-east-1"                                # REPLACE
    argocd_rbac_mappings = [
      {
        role = "ADMIN"
        identities = [
          { id = "REPLACE-WITH-SSO-USER-ID", type = "SSO_USER" }
        ]
      }
    ]
  }
}

observability_configuration = {
  aws_oss_tooling    = false
  aws_native_tooling = false
}

# ECR Pull-Through Cache (optional) — ~60% faster image pulls
# Set via environment variables, not in this file:
#   export TF_VAR_docker_hub_username="your-dockerhub-username"
#   export TF_VAR_docker_hub_access_token="dckr_pat_XXXXXXXXXX"

# GitOps repository — ArgoCD syncs the platform from this repo.
# Required when cluster_config.capabilities.gitops = true.
# FORKING = set this ONE value. Terraform renders the root `bootstrap`
# Application (a Helm app-of-apps) and passes this URL down as a Helm value, so
# every ApplicationSet inherits it — no other git file to edit.
gitops_repo_url = "https://github.com/YOUR-ORG/YOUR-REPO.git"
gitops_revision = "main"

# Self-service workloads repo (optional). Leave empty (default) to keep models/,
# teams/, scale-models/ in the SAME repo above (single-repo, simplest). Set to a
# separate, tenant-owned repo for multi-team self-service — teams get write to
# that repo only, never the platform repo. See workloads/README.md.
# gitops_workloads_repo_url = "https://github.com/YOUR-ORG/ai-platform-workloads.git"

# GPU cold-start optimization (optional) — pre-pulled container images on EBS snapshot.
# Created by: ./ops/create-data-volume-snapshot.sh <ecr-image-uri>
# When set, GPU nodes boot with the vLLM serving image already on disk (~0s image pull).
# When empty (default), nodes fall back to SOCI lazy-loading or full image pull.
# gpu_data_volume_snapshot_id = "snap-0123456789abcdef0"

# Platform Health Agent (optional) — autonomous incident investigation/remediation.
# NOT a Terraform concern: it ships as a component of the cluster-dashboard
# ArgoCD app (always deployed, ai-platform namespace) and idles until you create
# its Kiro API key Secret with kubectl — same pattern as the hf-token Secret:
#   kubectl create secret generic platform-health-agent-secrets \
#     -n ai-platform --from-literal=KIRO_API_KEY="kr-..."   # get from https://kiro.dev/
#   kubectl rollout restart deployment event-watcher -n ai-platform
# See platform/services/cluster-dashboard/PLATFORM-HEALTH-AGENT.md.

# Amazon Bedrock (default: enabled) — exposes frontier models (e.g. Claude
# Sonnet 4.6) through LiteLLM with zero GPUs. Requires Bedrock model access
# enabled in-account for the target model (one-time AWS console toggle).
# enable_bedrock = true

# SSO + per-user cost attribution (default: enabled). Ships a Cognito user pool
# with a hosted login page, role groups (admins/developers/users), and three
# seed users whose generated passwords are surfaced as the `sso_seed_user_passwords`
# Terraform output. SSO works out of the box via `./platformctl tunnel` (Cognito
# permits localhost callbacks). Open WebUI / LiteLLM UI / Langfuse federate to it;
# per-user cost shows up in LiteLLM spend reports. Identity Center is still only
# needed for ArgoCD SSO.
#   enable_sso = true
#
# Public front door (CloudFront). Opt-in, billable per distribution. When true,
# Terraform stands up CloudFront VPC origins to the private ALB with a free
# *.cloudfront.net cert and wires the Cognito callbacks automatically — no
# sso_public_urls needed. Enable it AFTER `up` (the ALB must exist), most easily
# via `./platformctl edge cloudfront`:
#   enable_cloudfront_edge = false
#
# For your OWN domain on an internet-facing ALB instead, leave the edge off and
# set the per-UI public base URLs here so the browser OIDC redirects resolve
# publicly (tunnel keeps working too):
#   sso_public_urls = {
#     open-webui = "https://webui.your-domain.com"
#     litellm    = "https://litellm.your-domain.com"
#     langfuse   = "https://langfuse.your-domain.com"
#   }
# To federate your own enterprise IdP, add it to the Cognito pool (SAML/OIDC).

# Langfuse first-boot init (tracing live on the first call — no manual setup).
# Default URL works with the SSM tunnel (./platformctl tunnel). For ALB access use
# e.g. http://k8s-aiplatform-<hash>.<region>.elb.amazonaws.com:3000, or
# https://langfuse.<your-domain> behind a domain + cert.
# langfuse_nextauth_url    = "http://localhost:3000"
# langfuse_init_user_email = "admin@ai-platform.local"
