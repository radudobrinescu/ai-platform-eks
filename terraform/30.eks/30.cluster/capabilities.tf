################################################################################
# EKS Managed Capabilities - IAM Roles
################################################################################
locals {
  enabled_capabilities = {
    for k, v in {
      argocd = { type = "ARGOCD", enabled = local.capabilities.gitops }
      kro    = { type = "KRO", enabled = local.capabilities.kro }
      ack    = { type = "ACK", enabled = local.capabilities.ack }
    } : k => v if v.enabled
  }
}

data "aws_iam_policy_document" "capability_trust" {
  count = length(local.enabled_capabilities) > 0 ? 1 : 0

  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["capabilities.eks.amazonaws.com"]
    }
    actions = ["sts:AssumeRole", "sts:TagSession"]
  }
}

resource "aws_iam_role" "capability" {
  for_each = local.enabled_capabilities

  name               = "${local.cluster_name}-capability-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.capability_trust[0].json
  tags               = local.tags
}

################################################################################
# EKS Managed Capabilities - ArgoCD (requires Identity Center config)
################################################################################
resource "aws_eks_capability" "argocd" {
  count = local.capabilities.gitops ? 1 : 0

  cluster_name              = module.eks.cluster_name
  capability_name           = "argocd"
  type                      = "ARGOCD"
  role_arn                  = aws_iam_role.capability["argocd"].arn
  delete_propagation_policy = "RETAIN"
  tags                      = local.tags

  configuration {
    argo_cd {
      namespace = "argocd"
      aws_idc {
        idc_instance_arn = var.cluster_config.capabilities_config.argocd_idc_instance_arn
        idc_region       = try(var.cluster_config.capabilities_config.argocd_idc_region, null)
      }
      dynamic "rbac_role_mapping" {
        for_each = try(var.cluster_config.capabilities_config.argocd_rbac_mappings, [])
        content {
          role = rbac_role_mapping.value.role
          dynamic "identity" {
            for_each = rbac_role_mapping.value.identities
            content {
              id   = identity.value.id
              type = identity.value.type
            }
          }
        }
      }
    }
  }

  depends_on = [module.eks]
}

################################################################################
# EKS Managed Capabilities - KRO and ACK (no extra config needed)
################################################################################
resource "aws_eks_capability" "simple" {
  for_each = {
    for k, v in local.enabled_capabilities : k => v
    if k != "argocd"
  }

  cluster_name              = module.eks.cluster_name
  capability_name           = each.key
  type                      = each.value.type
  role_arn                  = aws_iam_role.capability[each.key].arn
  delete_propagation_policy = "RETAIN"
  tags                      = local.tags

  depends_on = [module.eks]
}

# KRO needs cluster-admin level access to create/manage arbitrary resources
# defined in ResourceGraphDefinitions (Deployments, Services, RayServices, etc.)
resource "aws_eks_access_policy_association" "kro_edit" {
  count = local.capabilities.kro ? 1 : 0

  cluster_name  = module.eks.cluster_name
  principal_arn = aws_iam_role.capability["kro"].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_capability.simple]
}

################################################################################
# ECR Pull-Through Cache (optional) — mirror Docker Hub images to private ECR
# Only created when docker_hub_credentials is provided in tfvars.
# Without it, KRO falls back to pulling directly from Docker Hub.
################################################################################
resource "aws_secretsmanager_secret" "docker_hub" {
  count                   = var.docker_hub_username != "" ? 1 : 0
  name                    = "ecr-pullthroughcache/docker-hub"
  recovery_window_in_days = 0
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "docker_hub" {
  count     = var.docker_hub_username != "" ? 1 : 0
  secret_id = aws_secretsmanager_secret.docker_hub[0].id
  secret_string = jsonencode({
    username    = var.docker_hub_username
    accessToken = var.docker_hub_access_token
  })
}

resource "aws_ecr_pull_through_cache_rule" "docker_hub" {
  count                 = var.docker_hub_username != "" ? 1 : 0
  ecr_repository_prefix = "docker-hub"
  upstream_registry_url = "registry-1.docker.io"
  credential_arn        = aws_secretsmanager_secret.docker_hub[0].arn
}

################################################################################
# Platform Config — KRO externalRef reads this ConfigMap.
# Always created. rayImage key only set when ECR cache is enabled.
# Without rayImage, KRO orValue() falls back to Docker Hub.
################################################################################
resource "kubernetes_config_map" "platform_config" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "platform-config"
    namespace = "inference"
  }

  data = merge(
    { cluster = module.eks.cluster_name },
    var.docker_hub_username != "" ? {
      rayImage = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${local.region}.amazonaws.com/docker-hub/anyscale/ray-llm:2.53.0-py311-cu128"
    } : {}
  )

  depends_on = [kubernetes_namespace.inference]
}

################################################################################
# ArgoCD — cluster-admin access for deploying applications locally
# Ref: https://docs.aws.amazon.com/eks/latest/userguide/argocd-register-clusters.html
################################################################################
resource "aws_eks_access_policy_association" "argocd_admin" {
  count = local.capabilities.gitops ? 1 : 0

  cluster_name  = module.eks.cluster_name
  principal_arn = aws_iam_role.capability["argocd"].arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }

  depends_on = [aws_eks_capability.argocd]
}

################################################################################
# ArgoCD — register the local cluster as a deployment target
# The managed ArgoCD capability does NOT auto-register the local cluster.
# Uses the EKS cluster ARN as server (kubernetes.default.svc is not supported).
################################################################################
resource "kubernetes_secret" "argocd_cluster" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "local-cluster"
    namespace = "argocd"
    labels = {
      "argocd.argoproj.io/secret-type" = "cluster"
    }
  }

  data = {
    name    = "local-cluster"
    server = module.eks.cluster_arn
  }

  depends_on = [aws_eks_capability.argocd]
}

################################################################################
# Namespaces — create before secrets so Terraform doesn't fail on fresh clusters
################################################################################
resource "kubernetes_namespace" "ai_platform" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name = "ai-platform"
    labels = { "app.kubernetes.io/part-of" = "ai-platform" }
  }

  depends_on = [aws_eks_capability.argocd]
}

resource "kubernetes_namespace" "inference" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name = "inference"
    labels = {
      "app.kubernetes.io/part-of" = "ai-platform"
      "purpose"                   = "inference-endpoints"
    }
  }

  depends_on = [aws_eks_capability.argocd]
}

################################################################################
# LiteLLM — pre-create master key secret
################################################################################
resource "random_password" "litellm_master_key" {
  count   = local.capabilities.gitops ? 1 : 0
  length  = 32
  special = false
}

resource "kubernetes_secret" "litellm_secrets" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm-secrets"
    namespace = "ai-platform"
  }

  data = {
    master-key = random_password.litellm_master_key[0].result
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

################################################################################
# LiteLLM — PostgreSQL credentials (referenced by litellm + litellm-db pods)
################################################################################
resource "random_password" "litellm_db_password" {
  count   = local.capabilities.gitops ? 1 : 0
  length  = 24
  special = false
}

resource "kubernetes_secret" "litellm_db_credentials" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm-db-credentials"
    namespace = "ai-platform"
  }

  data = {
    username     = "litellm"
    password     = random_password.litellm_db_password[0].result
    database-url = "postgres://litellm:${random_password.litellm_db_password[0].result}@litellm-db:5432/litellm"
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

# LiteLLM API key in inference namespace — used by KRO registration Jobs
resource "kubernetes_secret" "litellm_api_key" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm-api-key"
    namespace = "inference"
  }

  data = {
    master-key = random_password.litellm_master_key[0].result
  }

  depends_on = [kubernetes_namespace.inference]
}

################################################################################
# Langfuse — pre-create secrets so the Helm chart can reference them
################################################################################
resource "random_password" "langfuse" {
  for_each = local.capabilities.gitops ? toset([
    "salt", "nextauth-secret", "encryption-key",
    "password", "clickhouse-password", "redis-password"
  ]) : toset([])

  length  = 32
  special = false
}

resource "kubernetes_secret" "langfuse_secrets" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "langfuse-secrets"
    namespace = "ai-platform"
  }

  data = { for k, v in random_password.langfuse : k => v.result }

  depends_on = [kubernetes_namespace.ai_platform]
}
