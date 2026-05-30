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
