################################################################################
# Platform Health Agent — Kubernetes-side prerequisites.
#
# The agent itself is deployed via ArgoCD/GitOps from
# platform/services/platform-health-agent/. This file provisions only the
# bits ArgoCD CANNOT generate from the repo:
#
#   - The namespace (ArgoCD WOULD create it via CreateNamespace=true, but
#     creating it explicitly here lets us also create secrets in it before
#     ArgoCD's first sync, avoiding pod CrashLoopBackOff on first deploy).
#   - The Kiro CLI API key Secret (sourced from TF_VAR_kiro_api_key — never
#     committed to git, hence not GitOps-able).
#   - A copy of platform-db-credentials so the agent's pods can connect to
#     the shared platform-db (K8s Secrets are namespace-scoped, no cross-ns
#     references).
#
# Disable: leave var.platform_health_agent_enabled at default (false).
# Enable:  set true in tfvars AND export TF_VAR_kiro_api_key="kr-..." before
#          running terraform apply.
#
# Two-step enable (the agent is OFF by default end-to-end):
#   1. This file (tfvar=true) provisions the namespace + both secrets.
#   2. Add the `platform-health-agent` element back to the list generator in
#      argocd/bootstrap/platform.yaml so ArgoCD actually deploys the workload.
# The element is omitted from that ApplicationSet by default ON PURPOSE: adding
# it without these secrets leaves a permanently-Degraded app (the event-watcher
# pod can't resolve platform-db-credentials). With both in place the pod comes
# up Ready 1/1 within ~3 min of the next ArgoCD poll. See the agent's README.
################################################################################

locals {
  # Gated on gitops being enabled (the agent depends on ArgoCD to deploy
  # the workloads) AND the operator opting in.
  pha_enabled = local.capabilities.gitops && var.platform_health_agent_enabled
}

# Fail fast if the operator opted in but didn't supply the API key.
resource "null_resource" "platform_health_agent_validation" {
  count = local.pha_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.kiro_api_key != ""
      error_message = "platform_health_agent_enabled=true but TF_VAR_kiro_api_key is empty. Get a key from https://kiro.dev/ and export it: export TF_VAR_kiro_api_key=\"kr-...\""
    }
  }
}

resource "kubernetes_namespace" "platform_health_agent" {
  count = local.pha_enabled ? 1 : 0

  metadata {
    name = "platform-health-agent"
    labels = {
      "app.kubernetes.io/part-of" = "ai-platform"
      "app.kubernetes.io/name"    = "platform-health-agent"
    }
  }

  depends_on = [aws_eks_capability.argocd]
}

# Kiro CLI API key — the ONLY secret operators must provide for the agent.
resource "kubernetes_secret" "platform_health_agent_secrets" {
  count = local.pha_enabled ? 1 : 0

  metadata {
    name      = "platform-health-agent-secrets"
    namespace = "platform-health-agent"
  }

  data = {
    KIRO_API_KEY = var.kiro_api_key
  }

  depends_on = [kubernetes_namespace.platform_health_agent]
}

# Mirror of platform-db-credentials so the agent's pods (in the
# platform-health-agent namespace) can authenticate to platform-db
# (which lives in ai-platform). K8s Secrets are namespace-scoped —
# we can't symlink across namespaces.
resource "kubernetes_secret" "platform_health_agent_db_credentials" {
  count = local.pha_enabled ? 1 : 0

  metadata {
    name      = "platform-db-credentials"
    namespace = "platform-health-agent"
  }

  data = {
    username = "platform"
    password = random_password.platform_db_password[0].result
  }

  depends_on = [
    kubernetes_namespace.platform_health_agent,
    random_password.platform_db_password,
  ]
}
