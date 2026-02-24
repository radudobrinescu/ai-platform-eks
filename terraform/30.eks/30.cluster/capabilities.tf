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
