#!/usr/bin/env bash
# build-unsloth-image.sh — build + push the Unsloth fine-tuning trainer image to ECR.
#
# Driven by Terraform (terraform/30.eks/30.cluster/unsloth-image.tf) via a
# null_resource that re-runs when the Dockerfile or the image tag changes. Can
# also be run by hand / in CI. Idempotent: if the tag already exists in ECR
# (immutable repo), it's a no-op.
#
# Usage:
#   build-unsloth-image.sh -r <region> -e <ecr-repo-url> -t <tag> [-d <dockerfile-dir>]
#
# Example:
#   build-unsloth-image.sh -r us-east-1 \
#     -e 1234567890.dkr.ecr.us-east-1.amazonaws.com/wre-dev/unsloth-trainer \
#     -t 0.1.0
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ECR_REPO=""
TAG=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE_DIR="${SCRIPT_DIR}/../platform/services/unsloth-trainer"

while getopts "r:e:t:d:h" opt; do
  case "$opt" in
    r) REGION="$OPTARG" ;;
    e) ECR_REPO="$OPTARG" ;;
    t) TAG="$OPTARG" ;;
    d) DOCKERFILE_DIR="$OPTARG" ;;
    h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option" >&2; exit 1 ;;
  esac
done

if [[ -z "$ECR_REPO" || -z "$TAG" ]]; then
  echo "ERROR: -e <ecr-repo-url> and -t <tag> are required." >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  # Degrade gracefully: don't fail `terraform apply` just because the apply host
  # lacks Docker. The ECR repo still exists; build later from a Docker host or CI
  # (re-run this script / re-apply), or set enable_fine_tuning=false to opt out.
  echo "WARNING: docker not found — skipping Unsloth trainer image build." >&2
  echo "         Fine-tuning Jobs will not start until the image is pushed to:" >&2
  echo "           ${ECR_REPO}:${TAG}" >&2
  echo "         Build it later:  ops/build-unsloth-image.sh -r ${REGION} -e ${ECR_REPO} -t ${TAG}" >&2
  exit 0
fi

IMAGE_URI="${ECR_REPO}:${TAG}"
REGISTRY="${ECR_REPO%%/*}"

echo "==> Target image: ${IMAGE_URI}"

# Skip if the immutable tag already exists.
if aws ecr describe-images \
      --region "$REGION" \
      --repository-name "${ECR_REPO#*/}" \
      --image-ids "imageTag=${TAG}" >/dev/null 2>&1; then
  echo "==> ${IMAGE_URI} already present in ECR — nothing to build."
  exit 0
fi

echo "==> Logging in to ECR (${REGISTRY})"
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$REGISTRY"

echo "==> Building from ${DOCKERFILE_DIR}"
# linux/amd64 — GPU nodes are x86; build hosts may be arm64 (Apple Silicon).
docker build --platform linux/amd64 -t "$IMAGE_URI" "$DOCKERFILE_DIR"

echo "==> Pushing ${IMAGE_URI}"
docker push "$IMAGE_URI"

echo "==> Done: ${IMAGE_URI}"
