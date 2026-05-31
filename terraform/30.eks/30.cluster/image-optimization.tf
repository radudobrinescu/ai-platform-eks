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
    # Always use the push-capable soci-builder profile (unsloth-image.tf) — it
    # exists whenever run_image_optimization is true (local.need_soci_builder).
    # The builder pushes the SOCI index back to ECR, which the read-only EKS node
    # role can't do (would 403 and abort the apply).
    command     = "${path.module}/../../../ops/create-soci-index.sh -p ${aws_iam_instance_profile.soci_builder[0].name} -n ${local.cluster_name} -r ${local.region} ${local.ray_ecr_image}"
    interpreter = ["bash", "-c"]
    # Best-effort: SOCI is a cold-start optimization, not load-bearing. A
    # transient builder failure must degrade to lazy-pull, not abort the apply.
    on_failure = continue
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [
    aws_ecr_pull_through_cache_rule.docker_hub,
    module.eks,
    # Ensure the host kubeconfig points at THIS cluster before the script runs.
    # The script resolves networking hermetically via -n/-r, but this keeps any
    # kubectl fallback (ad-hoc invocations) pointed at the right cluster too.
    null_resource.update_kubeconfig,
  ]
}

################################################################################
# EBS Data Volume Snapshot — pre-pulled images for instant cold starts
#
# Bakes the large GPU images onto the Bottlerocket data volume so new GPU nodes
# boot with them already on disk (0s pull). This is the primary mechanism for
# large images — SOCI lazy-pull is only the fallback (and the Bottlerocket SOCI
# snapshotter is unreliable on multi-GB-layer images). We bake BOTH:
#   - the Ray LLM serving image (always), and
#   - the Unsloth trainer image (when fine-tuning is enabled), so FineTuneJob
#     training pods never lazy-pull the ~14.5 GiB trainer.
# The volume is sized to hold both unpacked (the trainer expands well beyond its
# compressed size); 300 GiB leaves headroom.
################################################################################
locals {
  # Images to bake. The Unsloth trainer is listed when fine-tuning is enabled,
  # but create-data-volume-snapshot.sh skips any image NOT present in ECR (the
  # no-Docker path never pushes it), so listing it is safe — it's silently
  # dropped if absent rather than hanging the puller. Avoids a plan-time
  # data.aws_ecr_image read error on a not-yet-built image.
  snapshot_images = local.enable_fine_tuning ? "${local.ray_ecr_image} ${local.unsloth_image}" : local.ray_ecr_image
  # Size for both unpacked when fine-tuning bakes the extra ~14.5 GiB trainer.
  snapshot_volume_gib = local.enable_fine_tuning ? 300 : 200
}

resource "null_resource" "gpu_data_volume_snapshot" {
  count = local.run_image_optimization ? 1 : 0

  triggers = {
    ray_image     = local.ray_ecr_image
    unsloth_image = local.enable_fine_tuning ? local.unsloth_image : ""
    cluster_name  = local.cluster_name
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../../ops/create-data-volume-snapshot.sh -r ${local.region} -n ${local.cluster_name} -s ${local.snapshot_volume_gib} ${local.snapshot_images}"
    interpreter = ["bash", "-c"]
    # Best-effort cold-start optimization — never abort the platform apply.
    on_failure = continue
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [
    null_resource.soci_index,
    null_resource.unsloth_soci_index,
    null_resource.unsloth_image,
    module.eks,
    module.karpenter,
    helm_release.karpenter,
    # create-data-volume-snapshot.sh drives kubectl (schedules a puller pod);
    # the kubeconfig must already point at this cluster.
    null_resource.update_kubeconfig,
  ]
}

################################################################################
# Discover the latest snapshot by tags (avoids manual tfvar management)
#
# On the FIRST apply for a new cluster the snapshot doesn't exist yet (the
# null_resource creates it during apply, but data sources resolve at plan time).
# So this MUST tolerate zero matches without erroring — the singular
# `aws_ebs_snapshot` data source raises "Your query returned no results" on an
# empty match and would abort `terraform apply` on a brand-new account. We use
# the list-returning `aws_ebs_snapshot_ids` (empty list, no error) to gate, then
# read the singular source only when ≥1 snapshot exists (so we still get
# most_recent ordering). First deploy → SOCI-only; second deploy picks up the
# snapshot. (Discovered: the maintainer never hit this because a gitignored
# snapshot.auto.tfvars pinned the id, forcing count=0 here.)
################################################################################
data "aws_ebs_snapshot_ids" "gpu_data_volume" {
  count  = local.run_image_optimization && var.gpu_data_volume_snapshot_id == "" ? 1 : 0
  owners = ["self"]

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
  # True only once at least one matching snapshot exists (zero on first apply).
  gpu_snapshot_exists = try(length(data.aws_ebs_snapshot_ids.gpu_data_volume[0].ids) > 0, false)
}

# Singular lookup for most_recent ordering — read ONLY when a snapshot exists,
# so a fresh account (zero matches) never triggers the no-results error.
data "aws_ebs_snapshot" "gpu_data_volume" {
  count = local.run_image_optimization && var.gpu_data_volume_snapshot_id == "" && local.gpu_snapshot_exists ? 1 : 0

  most_recent  = true
  owners       = ["self"]
  snapshot_ids = data.aws_ebs_snapshot_ids.gpu_data_volume[0].ids
}

locals {
  # Resolve snapshot ID: explicit override > auto-discovered > empty (disabled).
  # Empty string means no snapshot — Karpenter NodeClasses omit the snapshotID field.
  resolved_snapshot_id = var.gpu_data_volume_snapshot_id != "" ? var.gpu_data_volume_snapshot_id : try(data.aws_ebs_snapshot.gpu_data_volume[0].id, "")
}
