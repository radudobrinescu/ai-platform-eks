################################################################################
# Unsloth trainer image — ECR repo + build/push
#
# Unsloth has no official Docker image, so we build our own from
# platform/services/unsloth-trainer/Dockerfile and push it to a private ECR
# repo. Follows the repo's established image-artifact pattern (null_resource +
# ops script, same as image-optimization.tf) rather than adding the
# kreuzwerker/docker provider — fewer moving parts.
#
# Re-runs only when the Dockerfile or the image tag changes. Requires Docker on
# the machine running `terraform apply` (the build script fails loudly if absent
# — run it on a Docker host or in CI, then re-apply to pick up the image).
################################################################################

resource "aws_ecr_repository" "unsloth_trainer" {
  count                = local.enable_fine_tuning ? 1 : 0
  name                 = "${local.cluster_name}/unsloth-trainer"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = local.tags
}

# Build + push the trainer image. Triggered by Dockerfile content or tag change.
resource "null_resource" "unsloth_image" {
  count = local.enable_fine_tuning ? 1 : 0

  triggers = {
    dockerfile_sha = filesha256("${path.module}/../../../platform/services/unsloth-trainer/Dockerfile")
    image_tag      = local.unsloth_image_tag
    repo_url       = aws_ecr_repository.unsloth_trainer[0].repository_url
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../../ops/build-unsloth-image.sh -r ${local.region} -e ${aws_ecr_repository.unsloth_trainer[0].repository_url} -t ${local.unsloth_image_tag}"
    interpreter = ["bash", "-c"]
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [aws_ecr_repository.unsloth_trainer]
}

################################################################################
# SOCI index for the trainer image — lazy-loading so the ~15 GiB trainer image
# starts fast on a cold GPU node. Without an index, the Bottlerocket SOCI
# snapshotter (parallel-pull-unpack) chokes on the un-indexed image and the
# FineTuneJob training pod gets stuck in ImagePullBackOff.
#
# Mirrors the ray-llm SOCI flow in image-optimization.tf. Runs only when fine-
# tuning is enabled and a Docker host is available (same gate as the build).
# Re-runs when the image tag changes. Uses the dedicated push-capable
# soci-builder instance profile (the EKS node role is ECR read-only).
################################################################################
resource "null_resource" "unsloth_soci_index" {
  count = local.enable_fine_tuning ? 1 : 0

  triggers = {
    image_tag = local.unsloth_image_tag
    repo_url  = aws_ecr_repository.unsloth_trainer[0].repository_url
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../../ops/create-soci-index.sh -p ${aws_iam_instance_profile.soci_builder[0].name} ${aws_ecr_repository.unsloth_trainer[0].repository_url}:${local.unsloth_image_tag}"
    interpreter = ["bash", "-c"]
    environment = {
      AWS_REGION = local.region
    }
  }

  depends_on = [
    null_resource.unsloth_image,
    aws_iam_instance_profile.soci_builder,
  ]
}

################################################################################
# SOCI builder IAM role + instance profile
#
# The create-soci-index.sh helper launches a short-lived EC2 instance that pulls
# an image, builds the SOCI index, and PUSHES it back to ECR as a referrer
# artifact. Pushing needs ECR write (ecr:InitiateLayerUpload, ...) which the EKS
# node role deliberately lacks. This dedicated profile grants exactly the ECR
# pull+push actions on this account's repos (the SOCI builder is ephemeral and
# self-terminates), plus SSM core so the script can drive it via Run Command.
################################################################################
resource "aws_iam_role" "soci_builder" {
  count = local.enable_fine_tuning ? 1 : 0
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
  count = local.enable_fine_tuning ? 1 : 0
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
        # Pull + push the SOCI index (referrer artifact) on this account's repos.
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
        Resource = "arn:aws:ecr:${local.region}:${data.aws_caller_identity.current.account_id}:repository/*"
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "soci_builder_ssm" {
  count      = local.enable_fine_tuning ? 1 : 0
  role       = aws_iam_role.soci_builder[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "soci_builder" {
  count = local.enable_fine_tuning ? 1 : 0
  name  = "${local.cluster_name}-soci-builder"
  role  = aws_iam_role.soci_builder[0].name

  tags = local.tags
}
