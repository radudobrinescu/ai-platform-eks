# GitHub Actions OIDC role for the E2E gate (.github/workflows/e2e.yaml).
#
# WHAT THIS IS. A PROVISIONING role: `platformctl e2e` runs `platformctl up` and
# `down`, which create and destroy a VPC, an EKS cluster, Karpenter fleets, ~15
# IAM roles/policies across four Terraform stacks, KMS keys, S3 buckets, ECR,
# Cognito. No IAM policy narrower than administrator lets that succeed, and the
# role names come from third-party modules (KarpenterController-*, eks-module and
# blueprints prefixes), so iam:* cannot be name-scoped either. Do not mistake
# this for the read-only role addon-freshness uses.
#
# WHERE LEAST PRIVILEGE IS ENFORCED INSTEAD:
#   trust     one GitHub repo AND one GitHub Environment (sub claim exact match)
#   approval  that Environment carries a required reviewer; no cloud call
#             happens before a human approves the run; fork PRs never reach it
#   account   refuses to apply in any account listed in protected_account_ids
#   session   4h max (an E2E run is ~60-90 min)
#   deny      users/access keys/login profiles, SAML/OIDC provider changes, the
#             role's own trust policy, organizations/account, role hopping, and
#             deleting the Terraform state bucket/lock table
#
# NOT part of `platformctl up`: this directory has no backend.tf, so the
# terraform/Makefile does not discover it. Apply ONCE, by an admin, with local
# state, in a sandbox account:
#   terraform -chdir=terraform/ci/github-oidc init
#   terraform -chdir=terraform/ci/github-oidc apply -var github_repository=OWNER/REPO
# then set the repo variable E2E_ROLE_ARN to the role_arn output (see e2e.yaml).

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
  # Exact-match subject: this repo, this environment, nothing else.
  subject = "repo:${var.github_repository}:environment:${var.github_environment}"
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
      values   = [local.subject]
    }
  }
}

resource "aws_iam_role" "e2e" {
  name                 = var.role_name
  description          = "GitHub Actions E2E gate for ${var.github_repository} (environment ${var.github_environment}). Provisioning role; gated by a required reviewer."
  assume_role_policy   = data.aws_iam_policy_document.trust.json
  max_session_duration = 14400
  tags                 = merge(var.tags, { Purpose = "ai-platform-e2e-gate" })

  lifecycle {
    precondition {
      condition     = !contains(var.protected_account_ids, local.account_id)
      error_message = "Refusing to create the E2E provisioning role in a protected account (${local.account_id}). Use a sandbox account."
    }
  }
}

resource "aws_iam_role_policy_attachment" "poweruser" {
  role       = aws_iam_role.e2e.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/PowerUserAccess"
}

# PowerUserAccess excludes IAM. The stacks create roles, policies, instance
# profiles and OIDC providers (IRSA), and pass roles to EKS/EC2/Lambda.
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
      "iam:CreateOpenIDConnectProvider",
      "iam:DeleteOpenIDConnectProvider",
      "iam:UpdateOpenIDConnectProviderThumbprint",
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

  statement {
    sid       = "DenyEditingOwnTrust"
    effect    = "Deny"
    actions   = ["iam:UpdateAssumeRolePolicy", "iam:DeleteRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy"]
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
