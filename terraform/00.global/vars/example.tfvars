vpc_cidr = "10.5.0.0/16"

tags = {}

shared_config = {
  resources_prefix = "ai-platform"
}

cluster_config = {
  kubernetes_version  = "1.35"
  eks_auto_mode       = false
  private_eks_cluster = false
  create_mng_system   = true   # Required — runs Karpenter, CoreDNS, VPC CNI

  capabilities = {
    kube_proxy    = true   # kube proxy
    networking    = true   # VPC CNI
    coredns       = true   # CoreDNS
    identity      = true   # Pod Identity
    autoscaling   = true   # Karpenter
    blockstorage  = true   # EBS CSI Driver
    loadbalancing = true   # LB Controller

    # EKS Managed Capabilities (AWS-managed, run in AWS-owned infrastructure)
    gitops = true   # ArgoCD — requires Identity Center (see capabilities_config)
    kro    = true   # Kube Resource Orchestrator
    ack    = true   # AWS Controllers for Kubernetes
  }

  # Required when gitops = true
  # See: https://docs.aws.amazon.com/eks/latest/userguide/argocd.html
  capabilities_config = {
    argocd_idc_instance_arn = "arn:aws:sso:::instance/ssoins-XXXXXXXXXX"  # REPLACE
    argocd_idc_region       = "us-east-1"                                  # REPLACE
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
