################################################################################
# GPU Image Optimization — EBS data volume snapshot (+ SOCI index)
#
# Automatically triggered when vllm_image_tag changes in locals.tf.
#   1. EBS snapshot with the vLLM image pre-pulled (no image pull on new GPU
#      nodes). On every cluster with Karpenter. Without a Docker Hub account the
#      image comes from docker.io anonymously; with docker_hub_username set it
#      comes through the ECR pull-through cache, as the nodes then pull it.
#   2. SOCI index in ECR (lazy-loading fallback): only with the pull-through
#      cache, since the index is pushed to ECR.
#
# The snapshot is found by image (tags Purpose + Images), not by cluster: one
# snapshot per vLLM image serves every cluster in the account and region, so
# only the first cluster to need an image builds it (~15 min, a c5.xlarge).
# The builder also points the GPU NodeClasses at it, so it is used in the same
# apply; Terraform renders the same snapshotID from the next plan on. Snapshots
# are kept when a cluster is destroyed (other clusters may use them); list
# them with: aws ec2 describe-snapshots --owner-ids self \
#   --filters Name=tag:Purpose,Values=bottlerocket-data-volume
# Set gpu_data_volume_snapshot_id to override with a specific snapshot.
#
# The image baked is the vLLM serving image (vllm/vllm-openai) used by all three
# serving tiers (VLLMEndpoint / LLMDEndpoint / LLMDDisaggEndpoint).
################################################################################

locals {
  # Full ECR (pull-through cache) image URI for the vLLM serving image. Empty
  # when no Docker Hub creds are configured.
  vllm_ecr_image = var.docker_hub_username != "" ? "${data.aws_caller_identity.current.account_id}.dkr.ecr.${local.region}.amazonaws.com/docker-hub/${local.vllm_image}" : ""

  # The reference GPU nodes pull (platform-config vllmImage, capabilities.tf),
  # with the registry spelled out so the snapshot builder takes it as given.
  snapshot_image = var.docker_hub_username != "" ? local.vllm_ecr_image : "docker.io/${local.vllm_image}"

  # The snapshot needs only Karpenter (its node role joins the builder).
  run_image_snapshot = local.capabilities.autoscaling

  # The SOCI index needs the ECR pull-through cache (it is pushed to ECR).
  run_image_optimization = local.capabilities.autoscaling && var.docker_hub_username != ""

  # The push-capable SOCI builder profile is needed whenever the SOCI index is
  # built (run_image_optimization). It pushes the index back to ECR, which the
  # read-only EKS node role can't do (would 403 and abort the apply).
  need_soci_builder = local.run_image_optimization
}

################################################################################
# SOCI Index — enables lazy-loading of container image layers
################################################################################
resource "null_resource" "soci_index" {
  count = local.run_image_optimization ? 1 : 0

  triggers = {
    vllm_image = local.vllm_ecr_image
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../../ops/image/create-soci-index.sh -p ${aws_iam_instance_profile.soci_builder[0].name} -n ${local.cluster_name} -r ${local.region} ${local.vllm_ecr_image}"
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
    null_resource.update_kubeconfig,
  ]
}

################################################################################
# EBS Data Volume Snapshot — pre-pulled image for instant cold starts
#
# Bakes the vLLM serving image onto the Bottlerocket data volume so new GPU
# nodes boot with it already on disk (0s pull). This is the primary mechanism
# for the large serving image — SOCI lazy-pull is only the fallback (and the
# Bottlerocket SOCI snapshotter is unreliable on multi-GB-layer images).
################################################################################
locals {
  snapshot_images     = local.snapshot_image
  snapshot_volume_gib = 200
  # EC2NodeClasses whose /dev/xvdb boots from the snapshot (gpu_snapshot_id_line).
  gpu_snapshot_nodeclasses = "gpu-inference,gpu-shared,gpu-prefill,gpu-decode"
}

resource "null_resource" "gpu_data_volume_snapshot" {
  count = local.run_image_snapshot && var.gpu_data_volume_snapshot_id == "" ? 1 : 0

  triggers = {
    vllm_image   = local.snapshot_image
    cluster_name = local.cluster_name
  }

  provisioner "local-exec" {
    # --reuse: a snapshot of this image already in the account is used as is.
    # --patch-nodeclasses: the GPU NodeClasses use it at once (this apply).
    command     = "${path.module}/../../../ops/image/create-data-volume-snapshot.sh --reuse --patch-nodeclasses ${local.gpu_snapshot_nodeclasses} -r ${local.region} -n ${local.cluster_name} -s ${local.snapshot_volume_gib} ${local.snapshot_images}"
    interpreter = ["bash", "-c"]
    # Best-effort cold-start optimization — never abort the platform apply.
    on_failure = continue
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [
    null_resource.soci_index,
    module.eks,
    module.karpenter,
    helm_release.karpenter,
    # The NodeClasses it patches must exist (and its builder node is not a
    # Karpenter node, so nothing here waits on it).
    kubectl_manifest.karpenter_manifests,
    # create-data-volume-snapshot.sh drives kubectl (schedules a puller pod);
    # the kubeconfig must already point at this cluster.
    null_resource.update_kubeconfig,
  ]
}

################################################################################
# Discover the newest snapshot of this image (avoids manual tfvar management)
#
# Read at plan time, so it sees snapshots that existed before this apply: any
# earlier cluster's (same account, region and image) or this cluster's from an
# earlier apply. A snapshot built during this apply is put on the NodeClasses by
# the builder itself (--patch-nodeclasses above) and rendered from the next plan.
# The list-returning `aws_ebs_snapshot_ids` tolerates zero matches (the singular
# `aws_ebs_snapshot` errors on an empty match and would abort a fresh account's
# apply); its ids are sorted by creation time, newest first.
################################################################################
data "aws_ebs_snapshot_ids" "gpu_data_volume" {
  count  = local.run_image_snapshot && var.gpu_data_volume_snapshot_id == "" ? 1 : 0
  owners = ["self"]

  filter {
    name   = "tag:Purpose"
    values = ["bottlerocket-data-volume"]
  }
  filter {
    name   = "tag:Images"
    values = [local.snapshot_image]
  }
  # GPU nodes' data volume is at least this size (gpu_node_volume_gib).
  filter {
    name   = "volume-size"
    values = [tostring(local.snapshot_volume_gib)]
  }
  filter {
    name   = "status"
    values = ["completed"]
  }
}

locals {
  # Resolve snapshot ID: explicit override > auto-discovered > empty (disabled).
  # Empty string means no snapshot — Karpenter NodeClasses omit the snapshotID field.
  resolved_snapshot_id = var.gpu_data_volume_snapshot_id != "" ? var.gpu_data_volume_snapshot_id : try(data.aws_ebs_snapshot_ids.gpu_data_volume[0].ids[0], "")
}

################################################################################
# SOCI builder IAM role + instance profile
#
# create-soci-index.sh launches a short-lived EC2 instance that pulls an image,
# builds the SOCI index, and PUSHES it back to ECR as a referrer artifact.
# Pushing needs ECR write (ecr:InitiateLayerUpload, ...) which the EKS node role
# deliberately lacks. This dedicated profile grants exactly the ECR pull+push
# actions on this account's repos (the SOCI builder is ephemeral and
# self-terminates), plus SSM core so the script can drive it via Run Command.
################################################################################
resource "aws_iam_role" "soci_builder" {
  count = local.need_soci_builder ? 1 : 0
  name  = "${local.cluster_name}-soci-builder"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "soci_builder_ecr" {
  count = local.need_soci_builder ? 1 : 0
  name  = "ecr-pull-push"
  role  = aws_iam_role.soci_builder[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Auth token is account-wide (no resource scoping allowed).
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        # Pull + push the SOCI index (referrer artifact). Scoped to the
        # docker-hub/* pull-through cache repos this builder actually touches —
        # NOT repository/*, which let a builder compromise overwrite ANY image in
        # the account (supply-chain risk). The PullThroughImport stanza below is
        # already docker-hub/*-scoped for the same reason.
        Sid    = "EcrPullPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
        ]
        Resource = "arn:${local.partition}:ecr:${local.region}:${data.aws_caller_identity.current.account_id}:repository/docker-hub/*"
      },
      {
        # The SOCI builder is the apply-time FIRST puller of the vLLM image
        # through the docker-hub/* pull-through cache, so it must be able to
        # import the upstream image (a fresh-account first pull 403s otherwise).
        Sid    = "PullThroughImport"
        Effect = "Allow"
        Action = [
          "ecr:BatchImportUpstreamImage",
          "ecr:CreateRepository",
        ]
        Resource = "arn:${local.partition}:ecr:${local.region}:${data.aws_caller_identity.current.account_id}:repository/docker-hub/*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "soci_builder_ssm" {
  count      = local.need_soci_builder ? 1 : 0
  role       = aws_iam_role.soci_builder[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "soci_builder" {
  count = local.need_soci_builder ? 1 : 0
  name  = "${local.cluster_name}-soci-builder"
  role  = aws_iam_role.soci_builder[0].name

  tags = local.tags
}
