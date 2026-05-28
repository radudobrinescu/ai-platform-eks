variable "tfstate_region" {
  description = "region where the terraform state is stored"
  type        = string
  default     = null
}

variable "kms_key_admin_roles" {
  description = "list of role ARNs to add to the KMS policy"
  type        = list(string)
  default     = []

}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

variable "cluster_config" {
  description = "cluster configurations such as version, public/private API endpoint, and more"
  type        = any
  default     = {}
}

variable "shared_config" {
  description = "Shared configuration across all modules/folders"
  type        = map(any)
  default     = {}
}

variable "docker_hub_username" {
  description = "Docker Hub username for ECR pull-through cache. Set via TF_VAR_docker_hub_username env var."
  type        = string
  default     = ""
  sensitive   = true
}

variable "docker_hub_access_token" {
  description = "Docker Hub access token for ECR pull-through cache. Set via TF_VAR_docker_hub_access_token env var."
  type        = string
  default     = ""
  sensitive   = true
}

variable "gitops_repo_url" {
  description = "Git repository URL for ArgoCD to sync the AI platform from. Required when cluster_config.capabilities.gitops is true. Example: https://github.com/my-org/ai-platform.git"
  type        = string
  default     = ""
}

variable "gitops_revision" {
  description = "Git revision (branch, tag, or commit) for ArgoCD to track."
  type        = string
  default     = "main"
}

variable "gpu_data_volume_snapshot_id" {
  description = "EBS snapshot ID containing pre-pulled GPU container images (Ray LLM). Created by ops/create-data-volume-snapshot.sh. When set, Karpenter GPU nodes boot with images already on disk, eliminating the multi-minute image pull."
  type        = string
  default     = ""
}


variable "platform_health_agent_enabled" {
  description = "Provision Kubernetes-side prerequisites for the Platform Health Agent (namespace + secrets). The agent itself is deployed via ArgoCD/GitOps from platform/services/platform-health-agent/. When true, var.kiro_api_key (or TF_VAR_kiro_api_key env var) must also be set."
  type        = bool
  default     = false
}

variable "kiro_api_key" {
  description = "Kiro CLI API key for headless mode. Required when var.platform_health_agent_enabled = true. Get one from https://kiro.dev/ → API keys. Set via TF_VAR_kiro_api_key env var (don't commit to .tfvars)."
  type        = string
  default     = ""
  sensitive   = true
}
