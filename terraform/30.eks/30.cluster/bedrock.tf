################################################################################
# Bedrock as a model — LiteLLM ServiceAccount + IRSA
#
# LiteLLM serves Bedrock models (e.g. Claude Opus 4.8) natively. Because
# Bedrock models are static (nothing to deploy/scale), they go straight into the
# LiteLLM config (platform/services/litellm/litellm.yaml) — no serving CR,
# no registration Job. The only infra needed is AWS credentials for the LiteLLM
# pod, granted via IRSA on a dedicated `litellm` ServiceAccount.
#
# The SA is always created (the LiteLLM Deployment references it by name); the
# IRSA role + annotation are only attached when var.enable_bedrock = true.
# Mirrors the inference_worker IRSA pattern in capabilities.tf.
################################################################################

locals {
  # IRSA only makes sense when both the platform (gitops) and Bedrock are on.
  enable_bedrock = local.capabilities.gitops && var.enable_bedrock
}

resource "aws_iam_role" "litellm_bedrock" {
  count = local.enable_bedrock ? 1 : 0
  name  = "${local.cluster_name}-litellm-bedrock"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${module.eks.oidc_provider}:sub" = "system:serviceaccount:ai-platform:litellm"
          "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "litellm_bedrock" {
  count = local.enable_bedrock ? 1 : 0
  name  = "bedrock-invoke"
  role  = aws_iam_role.litellm_bedrock[0].id

  # Resource = "*" covers foundation models and cross-region inference profiles
  # (the `us.` prefix routes across regional model ARNs). Tighten to specific
  # model/inference-profile ARNs if your security posture requires it.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
      ]
      Resource = "*"
    }]
  })
}

# The ServiceAccount the LiteLLM Deployment runs as. Always present so the
# Deployment's `serviceAccountName: litellm` resolves; the IRSA annotation is
# added only when Bedrock is enabled (empty annotations map otherwise → behaves
# like the default SA with no AWS access).
resource "kubernetes_service_account" "litellm" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm"
    namespace = "ai-platform"
    annotations = local.enable_bedrock ? {
      "eks.amazonaws.com/role-arn" = aws_iam_role.litellm_bedrock[0].arn
    } : {}
    labels = {
      "app.kubernetes.io/part-of" = "ai-platform"
    }
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

# Region for the Bedrock provider, read by LiteLLM via configMapKeyRef
# (AWS_REGION → os.environ/AWS_REGION in config.yaml). Kept out of git so the
# region isn't hardcoded in a committed manifest.
resource "kubernetes_config_map" "litellm_env" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "litellm-env"
    namespace = "ai-platform"
  }

  data = {
    AWS_REGION = local.region
  }

  depends_on = [kubernetes_namespace.ai_platform]
}
