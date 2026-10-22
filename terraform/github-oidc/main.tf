# GitHub Actions OIDC role for the E2E harness (.github/workflows/e2e.yaml).
#
# WHAT THIS IS. A PROVISIONING role: an E2E run executes the product's
# `./platformctl up`, `upgrade` and `down`, which create and destroy a VPC, an
# EKS cluster, Karpenter fleets, IAM roles/policies and the cluster's IRSA OIDC
# provider across four Terraform stacks, KMS keys, S3 buckets, ECR, Cognito. No
# IAM policy narrower than administrator lets that succeed, and the role names
# come from third-party modules, so iam:* cannot be name-scoped either.
#
# WHERE LEAST PRIVILEGE IS ENFORCED INSTEAD:
#   trust     listed GitHub repos AND one GitHub Environment (exact `sub` match)
#   approval  that Environment carries a required reviewer; no cloud call
#             happens before a human approves the run; forks never reach it
#   account   refuses to apply in any account listed in protected_account_ids
#   session   6 h max: upgrade mode provisions, upgrades and tears down
#   deny      IAM users/keys/login profiles, SAML providers, the GitHub OIDC
#             provider, the role's own trust and policies, organizations/account,
#             role hopping, deleting the Terraform state bucket/lock table
#
# Ported from v1 terraform/ci/github-oidc. Changes: trusts a LIST of repos (the
# fork is added later), 6 h sessions, and the OIDC-provider deny is scoped to
# the GitHub provider (v1 denied every Create/DeleteOpenIDConnectProvider, which
# would also block the EKS module's IRSA provider).
#
# Local state only (this directory has no backend). Apply ONCE, by an admin:
#   terraform -chdir=terraform/github-oidc init
#   terraform -chdir=terraform/github-oidc apply \
#     -var 'github_repositories=["OWNER/REPO"]'
# then set the repository variable E2E_ROLE_ARN to the role_arn output.

terraform {
  required_version = ">= 1.1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.69.0"
    }
  }
}

provider "aws" {}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  oidc_host  = "token.actions.githubusercontent.com"
  # Exact-match subjects: these repos, this environment, nothing else.
  subjects = [for r in var.github_repositories : "repo:${r}:environment:${var.github_environment}"]
}

# One OIDC provider per account. Reuse an existing one when present.
data "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 0 : 1
  url   = "https://${local.oidc_host}"
}

resource "aws_iam_openid_connect_provider" "github" {
  count          = var.create_oidc_provider ? 1 : 0
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  # GitHub's OIDC thumbprints are no longer verified by AWS for this issuer, but
  # the API requires a value; keep the well-known one.
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
  tags            = var.tags
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : data.aws_iam_openid_connect_provider.github[0].arn
}

data "aws_iam_policy_document" "trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = local.subjects
    }
  }
}

resource "aws_iam_role" "e2e" {
  name                 = var.role_name
  description          = "GitHub Actions E2E harness (environment ${var.github_environment}). Provisioning role; gated by a required reviewer."
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 21600
  tags                 = merge(var.tags, { Purpose = "ai-platform-e2e" })

  lifecycle {
    precondition {
      condition     = !contains(var.protected_account_ids, local.account_id)
      error_message = "Refusing to create the E2E provisioning role in a protected account. Use another account or remove it from protected_account_ids."
    }
  }
}

resource "aws_iam_role_policy_attachment" "poweruser" {
  role       = aws_iam_role.e2e.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/PowerUserAccess"
}

# PowerUserAccess excludes IAM. The stacks create roles, policies, instance
# profiles and the cluster's IRSA OIDC provider, and pass roles to EKS/EC2.
data "aws_iam_policy_document" "iam_for_stacks" {
  statement {
    sid    = "IamForTerraformStacks"
    effect = "Allow"
    actions = [
      "iam:*Role*",
      "iam:*Policy*",
      "iam:*InstanceProfile*",
      "iam:*OpenIDConnectProvider*",
      "iam:*ServiceLinkedRole*",
      "iam:Get*",
      "iam:List*",
      "iam:Tag*",
      "iam:Untag*",
      "iam:PassRole",
    ]
    resources = ["*"]
  }

  # Hard stops. These are the surfaces whose abuse outlives the run.
  statement {
    sid    = "DenyIdentityAndEscalation"
    effect = "Deny"
    actions = [
      "iam:*User*",
      "iam:*AccessKey*",
      "iam:*LoginProfile*",
      "iam:*MFADevice*",
      "iam:*SAMLProvider*",
      "iam:*Group*",
      "iam:*AccountAlias*",
      "iam:*AccountPasswordPolicy*",
      "organizations:*",
      "account:*",
      "sts:AssumeRole",
      "sts:AssumeRoleWithSAML",
    ]
    resources = ["*"]
  }

  # The role may create and delete EKS IRSA providers, never touch the one it
  # authenticates through.
  statement {
    sid    = "DenyTouchingGitHubOidcProvider"
    effect = "Deny"
    actions = [
      "iam:DeleteOpenIDConnectProvider",
      "iam:UpdateOpenIDConnectProviderThumbprint",
      "iam:AddClientIDToOpenIDConnectProvider",
      "iam:RemoveClientIDFromOpenIDConnectProvider",
      "iam:TagOpenIDConnectProvider",
      "iam:UntagOpenIDConnectProvider",
    ]
    resources = ["arn:${local.partition}:iam::${local.account_id}:oidc-provider/${local.oidc_host}"]
  }

  statement {
    sid       = "DenyEditingOwnTrust"
    effect    = "Deny"
    actions   = ["iam:UpdateAssumeRolePolicy", "iam:DeleteRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:UpdateRole"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/${var.role_name}"]
  }

  statement {
    sid     = "DenyDestroyingTerraformState"
    effect  = "Deny"
    actions = ["s3:DeleteBucket", "s3:DeleteBucketPolicy", "s3:PutBucketVersioning", "dynamodb:DeleteTable"]
    resources = [
      "arn:${local.partition}:s3:::tfstate-${local.account_id}",
      "arn:${local.partition}:dynamodb:*:${local.account_id}:table/tfstate-lock",
    ]
  }
}

resource "aws_iam_role_policy" "iam_for_stacks" {
  name   = "iam-for-terraform-stacks"
  role   = aws_iam_role.e2e.id
  policy = data.aws_iam_policy_document.iam_for_stacks.json
}
