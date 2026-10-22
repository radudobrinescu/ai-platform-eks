variable "github_repositories" {
  description = "GitHub repositories allowed to assume the role, each as OWNER/REPO. Exact match; other forks are not trusted. Phase 2 adds the fork."
  type        = list(string)
  validation {
    condition     = length(var.github_repositories) > 0 && alltrue([for r in var.github_repositories : can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", r))])
    error_message = "github_repositories must be a non-empty list of OWNER/REPO."
  }
}

variable "github_environment" {
  description = "GitHub Environment the workflow job must run in. Configure a required reviewer on it; that approval is the gate for every cloud-touching run."
  type        = string
  default     = "e2e"
}

variable "role_name" {
  description = "IAM role name."
  type        = string
  default     = "ai-platform-e2e-github"
}

variable "protected_account_ids" {
  description = "Account ids in which this provisioning role must never be created. Apply fails if the current account is listed."
  type        = list(string)
  default     = []
}

variable "create_oidc_provider" {
  description = "Create the token.actions.githubusercontent.com OIDC provider. Set false if the account already has one (it is account-wide, one per account)."
  type        = bool
  default     = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
