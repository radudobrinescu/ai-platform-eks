data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

################################################################################
# CW EKS Addon
################################################################################
module "aws_cloudwatch_observability_irsa" {

  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts"
  version = "~> 6.0"
  count   = var.observability_configuration.aws_native_tooling ? 1 : 0

  name            = "${data.terraform_remote_state.eks.outputs.cluster_name}-cw-ci"
  use_name_prefix = false

  policies = {
    policy = "arn:${data.aws_partition.current.partition}:iam::aws:policy/CloudWatchAgentServerPolicy"
  }

  oidc_providers = {
    cluster = {
      provider_arn               = data.terraform_remote_state.eks.outputs.oidc_provider_arn
      namespace_service_accounts = ["amazon-cloudwatch:cloudwatch-agent"]
    }
  }
}

module "aws_cloudwatch_observability" {
  source  = "aws-ia/eks-blueprints-addons/aws"
  version = "~> 1.25.0"
  count   = var.observability_configuration.aws_native_tooling ? 1 : 0

  cluster_name      = data.terraform_remote_state.eks.outputs.cluster_name
  cluster_endpoint  = data.terraform_remote_state.eks.outputs.cluster_endpoint
  cluster_version   = data.terraform_remote_state.eks.outputs.kubernetes_version
  oidc_provider_arn = data.terraform_remote_state.eks.outputs.oidc_provider_arn

  create_kubernetes_resources = true
  eks_addons = {
    amazon-cloudwatch-observability = {
      most_recent              = true
      service_account_role_arn = module.aws_cloudwatch_observability_irsa[0].arn
    }
  }
}

# terraform-aws-modules/iam 6 renamed the policy attachment inside the module.
# Without this an existing environment would detach and re-attach the policy.
moved {
  from = module.aws_cloudwatch_observability_irsa[0].aws_iam_role_policy_attachment.this["policy"]
  to   = module.aws_cloudwatch_observability_irsa[0].aws_iam_role_policy_attachment.additional["policy"]
}
