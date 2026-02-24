# Test environment - GitOps validation (no Auto Mode)
vpc_cidr = "10.4.0.0/16"

tags = {
  provisioned-by = "gitops-test"
  purpose = "ai-platform-validation"
}

shared_config = {
  resources_prefix = "ai-gitops"
}

cluster_config = {
  kubernetes_version  = "1.35"
  eks_auto_mode       = false  # Testing without Auto Mode
  private_eks_cluster = false
  create_mng_system   = true   # Create managed node group for system workloads

  # Self-managed addons (required when eks_auto_mode = false)
  capabilities = {
    kube_proxy    = true  # kube proxy
    networking    = true  # VPC CNI
    coredns       = true  # CoreDNS
    identity      = true  # Pod Identity
    autoscaling   = true  # Karpenter
    blockstorage  = true  # EBS CSI Driver
    loadbalancing = true  # LB Controller

    # EKS Managed Capabilities (AWS-managed, run in AWS-owned infrastructure)
    gitops = true # ArgoCD - Enable for GitOps workflow
    kro    = true  # Kube Resource Orchestrator
    ack    = true  # AWS Controllers for Kubernetes
  }

  # Identity Center config
  capabilities_config = {
    argocd_idc_instance_arn = "arn:aws:sso:::instance/ssoins-7907801c96b8bc54"
    argocd_idc_region       = "us-west-2"
    argocd_rbac_mappings = [
      {
        role = "ADMIN"
        identities = [
          { id = "a821e380-f011-7015-52ed-7842df600c7d", type = "SSO_USER" }
        ]
      }
    ]
  }
}

# Observability - minimal for testing
observability_configuration = {
  aws_oss_tooling    = false
  aws_native_tooling = false
  aws_oss_tooling_config = {
    enable_managed_collector = false
    enable_adot_collector    = false
    prometheus_name          = "prom"
    enable_grafana_operator  = false
  }
}
