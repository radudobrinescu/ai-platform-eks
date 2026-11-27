################################################################################
# GPU image optimization — SOCI index
#
# Only with the ECR pull-through cache (docker_hub_username set): the vLLM
# serving image is then pulled through this account's ECR, and a SOCI index
# pushed next to it lets GPU nodes start the container while the layers are
# still loading. Built when vllm_image_tag changes in locals.tf.
#
# Without Docker Hub credentials nothing here is created: GPU nodes pull the
# image from docker.io when they start.
#
# The image is the vLLM serving image (vllm/vllm-openai) used by all three
# serving tiers (VLLMEndpoint / LLMDEndpoint / LLMDDisaggEndpoint).
################################################################################

locals {
  # Full ECR (pull-through cache) image URI for the vLLM serving image. Empty
  # when no Docker Hub creds are configured.
  vllm_ecr_image = var.docker_hub_username != "" ? "${local.ecr_registry}/docker-hub/${local.vllm_image}" : ""

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
