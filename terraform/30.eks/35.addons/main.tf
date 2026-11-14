data "aws_region" "current" {}

data "aws_caller_identity" "current" {}

# AWS Load Balancer Controller v3.x IAM supplement. The blueprints module's
# built-in LBC policy predates v3 and lacks ec2:DescribeRouteTables, which v3
# calls during subnet discovery (read-only; scoping mirrors the module's other
# ec2:Describe* statements). Diffed against the upstream v3.4.3
# docs/install/iam_policy.json — this is the only missing action.
data "aws_iam_policy_document" "lbc_v3_supplement" {
  statement {
    sid       = "LBCv3DescribeRouteTables"
    effect    = "Allow"
    actions   = ["ec2:DescribeRouteTables"]
    resources = ["*"]
  }
}

locals {
  # One pin for the AWS Load Balancer Controller chart and its CRDs. Since LBC
  # v3.0.0 the chart version aligns with the app version.
  lbc_chart_version = "3.6.3"
}

# The controller's CRDs, rendered from the same chart version and applied BEFORE
# the chart. Helm installs a chart's crds/ only on the first install and never
# updates them, so an upgraded cluster kept its old CRDs (3.4.3 -> 3.6.2 left
# TargetGroupBinding without a new field and globalaccelerators.aga.k8s.aws
# missing). Server-side apply takes over the Helm-installed CRDs in place;
# apply_only means Terraform never deletes one (that would delete every
# TargetGroupBinding behind the ALBs); they go with the cluster on `down`.
data "helm_template" "lbc_crds" {
  count = local.capabilities.loadbalancing ? 1 : 0

  name         = "aws-load-balancer-controller"
  namespace    = "kube-system"
  repository   = "https://aws.github.io/eks-charts"
  chart        = "aws-load-balancer-controller"
  version      = local.lbc_chart_version
  include_crds = true
  set          = [{ name = "clusterName", value = data.terraform_remote_state.eks.outputs.cluster_name }]
}

locals {
  lbc_crd_docs = local.capabilities.loadbalancing ? {
    for d in flatten([for f in data.helm_template.lbc_crds[0].crds : split("\n---", f)]) :
    yamldecode(d).metadata.name => d
    if length(regexall("(?m)^kind:\\s*CustomResourceDefinition", d)) > 0
  } : {}
}

resource "kubectl_manifest" "lbc_crds" {
  for_each = local.lbc_crd_docs

  yaml_body         = each.value
  server_side_apply = true
  force_conflicts   = true
  apply_only        = true
}

module "eks_blueprints_addons" {
  source  = "aws-ia/eks-blueprints-addons/aws"
  version = "~> 1.24.0"

  cluster_name      = data.terraform_remote_state.eks.outputs.cluster_name
  cluster_endpoint  = data.terraform_remote_state.eks.outputs.cluster_endpoint
  cluster_version   = data.terraform_remote_state.eks.outputs.kubernetes_version
  oidc_provider_arn = data.terraform_remote_state.eks.outputs.oidc_provider_arn

  create_kubernetes_resources = true

  # common addons deployed with EKS Blueprints Addons
  enable_aws_load_balancer_controller = local.capabilities.loadbalancing
  aws_load_balancer_controller = {
    # Pin the LB controller chart (local.lbc_chart_version, shared with its
    # CRDs above). The pinned blueprints module defaults to chart 1.7.1 (app
    # v2.7.1), which is far behind upstream.
    chart_version = local.lbc_chart_version
    # LBC v3 needs ec2:DescribeRouteTables (subnet discovery), which the
    # pinned module's built-in policy predates. Merged in via the module's
    # source_policy_documents passthrough.
    source_policy_documents = [data.aws_iam_policy_document.lbc_v3_supplement.json]
    set = [
      {
        name  = "vpcId"
        value = data.terraform_remote_state.vpc.outputs.vpc_id
      }
    ]
    values = [yamlencode(local.critical_addons_tolerations)]
  }


  # external-secrets is being used AMG for grafana auth
  enable_external_secrets = try(var.observability_configuration.aws_oss_tooling, false)
  external_secrets = {
    values = [
      yamlencode({
        tolerations = [local.critical_addons_tolerations.tolerations[0]]
        webhook = {
          tolerations = [local.critical_addons_tolerations.tolerations[0]]
        }
        certController = {
          tolerations = [local.critical_addons_tolerations.tolerations[0]]
        }
      })
    ]
  }

  # cert-manager as a dependency for ADOT addon
  enable_cert_manager = try(
    var.observability_configuration.aws_oss_tooling
    && var.observability_configuration.aws_oss_tooling_config.enable_adot_collector,
  false)
  cert_manager = {
    values = [
      yamlencode({
        tolerations = [local.critical_addons_tolerations.tolerations[0]]
        webhook = {
          tolerations = [local.critical_addons_tolerations.tolerations[0]]
        }
        cainjector = {
          tolerations = [local.critical_addons_tolerations.tolerations[0]]
        }
      })
    ]
  }

  # FluentBit 
  enable_aws_for_fluentbit = try(
    var.observability_configuration.aws_oss_tooling
    && !var.observability_configuration.aws_oss_tooling_config.enable_adot_collector
  , false)
  aws_for_fluentbit = {
    values = [
      yamlencode({ "tolerations" : [{ "operator" : "Exists" }] })
    ]
  }
  aws_for_fluentbit_cw_log_group = {
    name            = "/aws/eks/${data.terraform_remote_state.eks.outputs.cluster_name}/aws-fluentbit-logs"
    use_name_prefix = false
    create          = true
  }

  # CRDs first: the new controller version may already use them.
  depends_on = [kubectl_manifest.lbc_crds]
}
