################################################################################
# GPU Image Optimization — SOCI index + EBS data volume snapshot
#
# Automatically triggered when ray_image_tag changes in locals.tf.
# Creates both optimization artifacts in the correct order:
#   1. SOCI index in ECR (enables lazy-loading for fallback/new layers)
#   2. EBS snapshot with pre-pulled image (0s image pull on new nodes)
#
# The snapshot ID is discovered by tags, so no manual tfvar needed.
# Set gpu_data_volume_snapshot_id to override with a specific snapshot.
################################################################################

locals {
  # Full ECR image URI for the Ray LLM image
  ray_ecr_image = var.docker_hub_username != "" ? "${data.aws_caller_identity.current.account_id}.dkr.ecr.${local.region}.amazonaws.com/docker-hub/${local.ray_image}" : ""

  # Whether to run image optimization (requires ECR pull-through cache)
  run_image_optimization = local.capabilities.autoscaling && var.docker_hub_username != ""
}

################################################################################
# SOCI Index — enables lazy-loading of container image layers
################################################################################
resource "null_resource" "soci_index" {
  count = local.run_image_optimization ? 1 : 0

  triggers = {
    ray_image = local.ray_ecr_image
  }

  provisioner "local-exec" {
    # Use the push-capable soci-builder profile (unsloth-image.tf) when fine-tuning
    # is enabled; otherwise fall back to the script's node-profile default. The
    # SOCI builder pushes the index back to ECR, which the EKS node role can't do.
    command     = "${path.module}/../../../ops/create-soci-index.sh ${local.enable_fine_tuning ? "-p ${aws_iam_instance_profile.soci_builder[0].name} " : ""}${local.ray_ecr_image}"
    interpreter = ["bash", "-c"]
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [
    aws_ecr_pull_through_cache_rule.docker_hub,
    module.eks
  ]
}

################################################################################
# EBS Data Volume Snapshot — pre-pulled image for instant cold starts
################################################################################
resource "null_resource" "gpu_data_volume_snapshot" {
  count = local.run_image_optimization ? 1 : 0

  triggers = {
    ray_image    = local.ray_ecr_image
    cluster_name = local.cluster_name
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../../ops/create-data-volume-snapshot.sh -r ${local.region} -n ${local.cluster_name} ${local.ray_ecr_image}"
    interpreter = ["bash", "-c"]
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [
    null_resource.soci_index,
    module.eks,
    module.karpenter,
    helm_release.karpenter
  ]
}

################################################################################
# Discover the latest snapshot by tags (avoids manual tfvar management)
#
# On first apply for a new cluster, the snapshot won't exist yet (null_resource
# runs during apply, but data sources resolve at plan time). The snapshot is
# picked up on the next apply or plan. This is expected — first deploy gets
# SOCI-only optimization, second deploy activates the EBS snapshot.
################################################################################
data "aws_ebs_snapshot" "gpu_data_volume" {
  count = local.run_image_optimization && var.gpu_data_volume_snapshot_id == "" ? 1 : 0

  most_recent = true
  owners      = ["self"]

  filter {
    name   = "tag:Cluster"
    values = [local.cluster_name]
  }

  filter {
    name   = "tag:Purpose"
    values = ["bottlerocket-data-volume"]
  }

  filter {
    name   = "status"
    values = ["completed"]
  }
}

locals {
  # Resolve snapshot ID: explicit override > auto-discovered > empty (disabled).
  # Empty string means no snapshot — Karpenter NodeClasses omit the snapshotID field.
  resolved_snapshot_id = var.gpu_data_volume_snapshot_id != "" ? var.gpu_data_volume_snapshot_id : try(data.aws_ebs_snapshot.gpu_data_volume[0].id, "")
}
