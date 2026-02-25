vpc_cidr = "10.5.0.0/16"

# custom tags to apply to all resources
# NOTE: Do not add an "environment" or "Environment" tag here - it is auto-added by the modules
tags = {
}

shared_config = {
  resources_prefix = "ai-platform"
}

cluster_config = {
  kubernetes_version  = "1.35"
  eks_auto_mode       = true // When enabled, all self-managed addons are false by default
  private_eks_cluster = false
  create_mng_system   = false // CriticalAddons MNG NodeGroup

  // Self-managed addons (when eks_auto_mode = true, these are false by default)
  capabilities = {
    kube_proxy    = false // kube proxy
    networking    = false // VPC CNI
    coredns       = false // CoreDNS
    identity      = false // Pod Identity
    autoscaling   = false // Karpenter
    blockstorage  = false // EBS CSI Driver
    loadbalancing = false // LB Controller

    // EKS Managed Capabilities (AWS-managed, run in AWS-owned infrastructure)
    gitops = true  // ArgoCD - REQUIRES AWS Identity Center (see capabilities_config below)
    kro    = true  // Kube Resource Orchestrator
    ack    = true  // AWS Controllers for Kubernetes
  }

  // Required when gitops = true. ArgoCD capability requires AWS Identity Center for authentication.
  // See: https://docs.aws.amazon.com/eks/latest/userguide/argocd.html
  capabilities_config = {
    argocd_idc_instance_arn = "arn:aws:sso:::instance/ssoins-XXXXXXXXXX" // REPLACE with your IdC instance ARN
    argocd_idc_region       = "us-east-1"                                 // Region where Identity Center is configured
    // RBAC mappings - grant IdC users/groups access to ArgoCD UI
    // Get user IDs: aws identitystore list-users --identity-store-id <store-id> --region <idc-region>
    // Get group IDs: aws identitystore list-groups --identity-store-id <store-id> --region <idc-region>
    // Roles: ADMIN, EDITOR, VIEWER
    argocd_rbac_mappings = [
      {
        role = "ADMIN"
        identities = [
          { id = "REPLACE-WITH-SSO-USER-ID", type = "SSO_USER" }
          // { id = "REPLACE-WITH-SSO-GROUP-ID", type = "SSO_GROUP" }
        ]
      }
    ]
  }
}

# Observability variables
observability_configuration = {
  aws_oss_tooling    = false
  aws_native_tooling = true
  aws_oss_tooling_config = {
    sso_region               = "us-east-1"
    enable_managed_collector = true
    enable_adot_collector    = false
    prometheus_name          = "prom"
    enable_grafana_operator  = true
  }
}

# ECR Pull-Through Cache (optional)
# When set, mirrors Docker Hub images to ECR for faster pulls via SOCI.
# Without this, images pull directly from Docker Hub (works fine, just slower).
# Get a free Docker Hub access token at: https://hub.docker.com/settings/security
# Set via environment variables — do NOT put credentials in tfvars files:
#
#   export TF_VAR_docker_hub_username="your-dockerhub-username"
#   export TF_VAR_docker_hub_access_token="dckr_pat_XXXXXXXXXX"
