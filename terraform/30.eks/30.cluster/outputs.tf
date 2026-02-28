output "configure_kubectl" {
  description = "Configure kubectl"
  value       = "aws eks --region ${local.region} update-kubeconfig --name ${module.eks.cluster_name}"
}

output "cluster_name" {
  description = "The EKS cluster name"
  value       = module.eks.cluster_name
}

output "argocd_url" {
  description = "ArgoCD UI URL (login with Identity Center)"
  value       = local.capabilities.gitops ? aws_eks_capability.argocd[0].configuration[0].argo_cd[0].server_url : null
}

output "next_steps" {
  description = "Post-deploy steps"
  value       = <<-EOT

    1. Configure kubectl:
       aws eks --region ${local.region} update-kubeconfig --name ${module.eks.cluster_name}

    2. Update ArgoCD app source URLs and apply:
       cd argocd/
       sed -i '' 's|https://github.com/YOUR-ORG/YOUR-REPO.git|https://github.com/YOUR-ORG/YOUR-REPO.git|g' *.yaml
       kubectl apply -f argocd/

    3. Create HuggingFace token (for gated models):
       kubectl create secret generic hf-token -n inference --from-literal=token=hf_YOUR_TOKEN

    4. Get LiteLLM API key:
       kubectl get secret litellm-secrets -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d
  EOT
}

output "cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "cluster_certificate_authority_data" {
  value = module.eks.cluster_certificate_authority_data
}

output "kubernetes_version" {
  description = "The EKS Cluster version"
  value       = module.eks.cluster_version
}

output "oidc_provider_arn" {
  description = "The OIDC Provider ARN"
  value       = module.eks.oidc_provider_arn
}

output "control_plane_subnet_ids" {
  description = "The Control Plane Subnet IDs"
  value       = local.control_plane_subnet_ids
}