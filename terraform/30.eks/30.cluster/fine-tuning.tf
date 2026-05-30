################################################################################
# Fine-tuning — datasets bucket, trainer ServiceAccount + IRSA, RBAC
#
# Self-service fine-tuning: a user commits a FineTuneJob YAML, KRO expands it
# into a validation Job + a GPU training Job (Unsloth QLoRA), and on
# `autoDeploy: true` the training Job applies an InferenceEndpoint so the tuned
# model becomes a queryable LiteLLM endpoint. See the v2 plan in
# docs/fine-tuning-implementation-plan-v2.md and platform/config/kro/fine-tuning-job.yaml.
#
# A dedicated fine-tuning-worker SA (separate from inference-worker) keeps the
# IAM blast radius tight: inference pods can't write to fine-tuned/, and the
# trainer can create InferenceEndpoint CRs (for autoDeploy) but nothing else.
################################################################################

locals {
  # Fine-tuning rides on the same gitops gate as the rest of the platform.
  enable_fine_tuning = local.capabilities.gitops && var.enable_fine_tuning
}

################################################################################
# Training datasets bucket — user-uploaded data (kept; versioned for repro)
################################################################################
resource "aws_s3_bucket" "training_datasets" {
  count         = local.enable_fine_tuning ? 1 : 0
  bucket        = "${local.cluster_name}-training-datasets"
  force_destroy = false # real user data — don't auto-delete with the cluster

  tags = merge(local.tags, { Purpose = "fine-tuning-datasets" })
}

resource "aws_s3_bucket_public_access_block" "training_datasets" {
  count                   = local.enable_fine_tuning ? 1 : 0
  bucket                  = aws_s3_bucket.training_datasets[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "training_datasets" {
  count  = local.enable_fine_tuning ? 1 : 0
  bucket = aws_s3_bucket.training_datasets[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "training_datasets" {
  count  = local.enable_fine_tuning ? 1 : 0
  bucket = aws_s3_bucket.training_datasets[0].id
  versioning_configuration {
    status = "Enabled" # keep dataset history for reproducibility
  }
}

################################################################################
# IRSA role for training Pods
################################################################################
resource "aws_iam_role" "fine_tuning_worker" {
  count = local.enable_fine_tuning ? 1 : 0
  name  = "${local.cluster_name}-fine-tuning-worker"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${module.eks.oidc_provider}:sub" = "system:serviceaccount:inference:fine-tuning-worker"
          "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })

  tags = local.tags
}

resource "aws_iam_role_policy" "fine_tuning_s3" {
  count = local.enable_fine_tuning ? 1 : 0
  name  = "fine-tuning-s3-access"
  role  = aws_iam_role.fine_tuning_worker[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Write fine-tuned artifacts (output) + warm the HF cache prefix so the
        # auto-deployed InferenceEndpoint can read base weights it pulled.
        Sid    = "ReadWriteModelCache"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [
          "${aws_s3_bucket.model_cache[0].arn}/fine-tuned/*",
          "${aws_s3_bucket.model_cache[0].arn}/hf/*",
        ]
      },
      {
        Sid      = "ListModelCache"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.model_cache[0].arn
        Condition = {
          StringLike = { "s3:prefix" = ["hf/*", "fine-tuned/*", ""] }
        }
      },
      {
        # Read training datasets (separate bucket).
        Sid    = "ReadDatasets"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.training_datasets[0].arn,
          "${aws_s3_bucket.training_datasets[0].arn}/*",
        ]
      },
    ]
  })
}

resource "kubernetes_service_account" "fine_tuning_worker" {
  count = local.enable_fine_tuning ? 1 : 0

  metadata {
    name      = "fine-tuning-worker"
    namespace = "inference"
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.fine_tuning_worker[0].arn
    }
    labels = {
      "app.kubernetes.io/part-of" = "ai-platform"
    }
  }

  depends_on = [kubernetes_namespace.inference]
}

################################################################################
# RBAC — the trainer needs to:
#   - wait on the validation Job (get/watch jobs)
#   - apply an InferenceEndpoint on autoDeploy (create/patch inferenceendpoints)
################################################################################
resource "kubernetes_role" "fine_tuning_worker" {
  count = local.enable_fine_tuning ? 1 : 0

  metadata {
    name      = "fine-tuning-worker"
    namespace = "inference"
  }

  rule {
    api_groups = ["kro.run"]
    resources  = ["inferenceendpoints"]
    verbs      = ["create", "get", "list", "patch", "update"]
  }

  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["get", "list", "watch"]
  }

  depends_on = [kubernetes_namespace.inference]
}

resource "kubernetes_role_binding" "fine_tuning_worker" {
  count = local.enable_fine_tuning ? 1 : 0

  metadata {
    name      = "fine-tuning-worker"
    namespace = "inference"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.fine_tuning_worker[0].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account.fine_tuning_worker[0].metadata[0].name
    namespace = "inference"
  }

  depends_on = [kubernetes_namespace.inference]
}
