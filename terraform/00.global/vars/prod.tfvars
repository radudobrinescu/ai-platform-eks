# Prod environment variables
vpc_cidr = "10.0.0.0/16"

tags = {
  provisioned-by = "aws-samples/terraform-workloads-ready-eks-accelerator"
}

shared_config = {
  resources_prefix = "ai-eks"
}

cluster_config = {
  kubernetes_version  = "1.35"
  eks_auto_mode       = false
  private_eks_cluster = false

  capabilities = {
    gitops = true  # ArgoCD - requires AWS Identity Center (see capabilities_config)
    kro    = true  # Kube Resource Orchestrator
    ack    = true  # AWS Controllers for Kubernetes
  }

  # Required when gitops = true
  capabilities_config = {
    argocd_idc_instance_arn = "arn:aws:sso:::instance/ssoins-XXXXXXXXXX" // REPLACE with your IdC instance ARN
    argocd_idc_region       = "us-east-1"                                 // Region where Identity Center is configured
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

# Observability variables
observability_configuration = {
  aws_oss_tooling    = true
  aws_native_tooling = false
  aws_oss_tooling_config = {
    enable_managed_collector = true
    enable_adot_collector    = false
    prometheus_name          = "prom"
    enable_grafana_operator  = true
    sso_region               = "us-west-2"
  }
}
