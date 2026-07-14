################################################################################
# CloudFront edge — IRSA role for the reconcile-edge Job (opt-in)
#
# The opt-in edge (platform/services/edge) fronts the INTERNAL ALB with CloudFront
# VPC origins. Its reconcile-edge Job resolves the runtime ALB ARN from the
# ingress hostname, which needs elasticloadbalancing:DescribeLoadBalancers. This
# creates a minimal read-only IRSA role for that Job's ServiceAccount
# (ai-platform/reconcile-edge). Gated on var.enable_edge so it only exists when
# you activate the edge; the role ARN goes in platform/services/edge/reconcile-edge.yaml.
################################################################################

locals {
  enable_edge = local.capabilities.gitops && var.enable_edge
}

resource "aws_iam_role" "edge_reconcile" {
  count = local.enable_edge ? 1 : 0
  name  = "${local.cluster_name}-edge-reconcile"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${module.eks.oidc_provider}:sub" = "system:serviceaccount:ai-platform:reconcile-edge"
          "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "edge_reconcile" {
  count = local.enable_edge ? 1 : 0
  name  = "describe-load-balancers"
  role  = aws_iam_role.edge_reconcile[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # Read-only: resolve the ALB ARN from its DNS name. DescribeLoadBalancers
      # does not support resource-level scoping, so Resource is "*".
      Sid      = "DescribeLoadBalancers"
      Effect   = "Allow"
      Action   = "elasticloadbalancing:DescribeLoadBalancers"
      Resource = "*"
    }]
  })
}

output "edge_reconcile_role_arn" {
  description = "IRSA role ARN for the edge reconcile Job — set on the reconcile-edge ServiceAccount in platform/services/edge/reconcile-edge.yaml."
  value       = local.enable_edge ? aws_iam_role.edge_reconcile[0].arn : null
}
