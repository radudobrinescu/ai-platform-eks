# Test environment - AI Platform for Inference validation
vpc_cidr = "10.3.0.0/16"

tags = {
  provisioned-by = "architecture-review"
}

shared_config = {
  resources_prefix = "ai-platform"
}

cluster_config = {
  kubernetes_version  = "1.35"
  eks_auto_mode       = false
  private_eks_cluster = false
  capabilities = {
    gitops = true  # ArgoCD (requires AWS Identity Center - see capabilities_config)
    kro    = true  # Kube Resource Orchestrator
    ack    = true  # AWS Controllers for Kubernetes
  }
  capabilities_config = {
    argocd_idc_instance_arn = "arn:aws:sso:::instance/ssoins-XXXXXXXXXX" // REPLACE with your IdC instance ARN
    argocd_idc_region       = "us-east-1"                                 // Region where Identity Center is configured
    argocd_rbac_mappings = [
      {
        role = "ADMIN"
        identities = [
          { id = "REPLACE-WITH-SSO-USER-ID", type = "SSO_USER" }
          # { id = "REPLACE-WITH-SSO-GROUP-ID", type = "SSO_GROUP" } # Admin group
        ]
      }
    ]
  }
}
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
