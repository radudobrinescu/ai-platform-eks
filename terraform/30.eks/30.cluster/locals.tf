locals {
  cluster_name    = "${var.shared_config.resources_prefix}-${terraform.workspace}"
  region          = data.aws_region.current.id
  tfstate_region  = try(var.tfstate_region, local.region)
  cluster_version = var.cluster_config.kubernetes_version
  eks_auto_mode   = try(var.cluster_config.eks_auto_mode, false)

  # Single source of truth for the Ray LLM image version.
  # Referenced by the platform-config ConfigMap; KRO reads it via externalRef.
  ray_image_tag = "2.54.0-py311-cu128"
  ray_image     = "anyscale/ray-llm:${local.ray_image_tag}"

  # Unsloth fine-tuning trainer image. Built from
  # platform/services/unsloth-trainer/Dockerfile and pushed to a private ECR
  # repo by unsloth-image.tf. Bump the tag to rebuild + re-push. Surfaced to KRO
  # via the platform-config ConfigMap (unslothImage) so FineTuneJob reads it.
  unsloth_image_tag = "0.1.0"
  unsloth_image     = local.enable_fine_tuning ? "${aws_ecr_repository.unsloth_trainer[0].repository_url}:${local.unsloth_image_tag}" : ""

  # HuggingFace model-weights cache.
  # S3 bucket is populated by ops/seed-model-cache.py (manual) or by the Ray
  # worker's sidecar after first successful load (automatic). The initContainer
  # in the KRO InferenceEndpoint template syncs from here on pod startup —
  # falling back to a live HF download on cache miss.
  model_cache_bucket            = "${local.cluster_name}-model-cache"
  inference_worker_sa_namespace = "inference"
  inference_worker_sa_name      = "inference-worker"

  private_subnet_ids       = data.terraform_remote_state.vpc.outputs.private_subnet_ids
  control_plane_subnet_ids = try(var.cluster_config.use_intra_subnets, true) ? data.terraform_remote_state.vpc.outputs.intra_subnet_ids : local.private_subnet_ids

  capabilities = {
    kube_proxy   = try(var.cluster_config.capabilities.kube_proxy, !local.eks_auto_mode, true)
    networking   = try(var.cluster_config.capabilities.networking, !local.eks_auto_mode, true)
    coredns      = try(var.cluster_config.capabilities.coredns, !local.eks_auto_mode, true)
    identity     = try(var.cluster_config.capabilities.identity, !local.eks_auto_mode, true)
    autoscaling  = try(var.cluster_config.capabilities.autoscaling, !local.eks_auto_mode, true)
    blockstorage = try(var.cluster_config.capabilities.blockstorage, !local.eks_auto_mode, true)
    # EKS Managed Capabilities (AWS-managed, not self-managed)
    gitops = try(var.cluster_config.capabilities.gitops, false)
    kro    = try(var.cluster_config.capabilities.kro, false)
    ack    = try(var.cluster_config.capabilities.ack, false)

    # Inference ingress/routing: Envoy AI Gateway + Gateway API Inference
    # Extension (GIE). Default OFF — turning it on installs the gateway control
    # plane; the serving path only moves onto it when an InferenceEndpoint sets
    # routing=gateway (default stays 'service'). Becomes the platform default at
    # cutover; the 'service' path is retained as break-glass.
    inference_gateway = try(var.cluster_config.capabilities.inference_gateway, false)
  }

  create_mng_system = try(var.cluster_config.create_mng_system, !local.eks_auto_mode, true)

  critical_addons_tolerations = {
    tolerations = [
      {
        key      = "CriticalAddonsOnly",
        operator = "Exists",
        effect   = "NoSchedule"
      }
    ]
  }

  tags = merge(
    var.tags,
    {
      "Environment" : terraform.workspace
      "provisioned-by" : "aws-solutions-library-samples/guidance-for-automated-provisioning-of-application-ready-amazon-eks-clusters"
    }
  )
}
