################################################################################
# Cluster dashboard — Quick Links ConfigMap
#
# The dashboard shows a "Quick Links" panel to the platform's web UIs. The
# ALB-fronted services (Open WebUI / LiteLLM / Langfuse) share one ingress
# hostname and are discovered by the dashboard backend at runtime, so they need
# no Terraform input. ArgoCD is an EKS-managed capability with its own endpoint
# (server_url) that can't be derived from the ALB — so we surface it here.
#
# Consumed by platform/services/cluster-dashboard/manifests.yaml via an optional
# configMapKeyRef (ARGOCD_URL); the dashboard hides the ArgoCD link if unset.
################################################################################
resource "kubernetes_config_map" "cluster_dashboard_links" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "cluster-dashboard-links"
    namespace = "ai-platform"
  }

  data = {
    argocdUrl = try(aws_eks_capability.argocd[0].configuration[0].argo_cd[0].server_url, "")
  }

  depends_on = [
    kubernetes_namespace.ai_platform,
    aws_eks_capability.argocd,
  ]
}
