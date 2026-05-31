# Fine-Tuning KRO — Implementation Plan v2

**Status:** Ready for implementation (95 % confidence)
**Author:** Platform Team
**Date:** 2026-05-21
**Supersedes:** an earlier May 2025 draft (removed)

---

## 0. Why v2

The original draft has the right architecture but two assumptions that don't hold. Both are deal-breakers if implemented as-written:

| # | Original claim | Reality | Impact |
|---|---------------|---------|--------|
| 1 | `unsloth/unsloth:latest` Docker image | Unsloth publishes no official Docker image. Only community Dockerfiles exist (`eightBEC/unsloth-docker`, `bet0x/unsloth-docker`, PR #738). | Training Job pulls fail at runtime |
| 2 | "vLLM supports `--model s3://...` natively" | Not supported. vLLM issue #3090 is still open. | Auto-deployed model never starts |

Everything else in the original plan stands. v2 keeps the same UX surface (`FineTuneJob` CR, 5-line YAML, autoDeploy) and adjusts the implementation to use:

1. **A custom Unsloth image** built from a Dockerfile we maintain, mirrored via the platform's existing ECR pull-through cache.
2. **The existing `s5cmd` init-container pattern** (already used by `InferenceEndpoint` for HF cache) extended to handle fine-tuned model paths.
3. **A Job-creates-`InferenceEndpoint` autoDeploy flow** — the training Job's last step uses `kubectl apply` to create the serving CR, so the InferenceEndpoint never starts before the model exists.

I have read every file the plan references and validated each assumption against the actual repo + upstream docs.

---

## 1. Goals & non-goals

### Goals (V1 = MVP)
- Self-service fine-tuning: commit a 5-line YAML → trained model in S3
- Single-node QLoRA on Unsloth (covers up to 70B models)
- Optional `autoDeploy: true` → fine-tuned model becomes a queryable LiteLLM endpoint
- Same security/cost/operations model as `InferenceEndpoint` (IRSA, Karpenter, CloudWatch logs)

### Non-goals (V1)
- Multi-node distributed training (RayJob/DeepSpeed) — deferred to V2
- RLHF / DPO / GRPO methods — V1 is SFT-only
- Hyperparameter search — manual iteration only
- Custom datasets in formats beyond Alpaca / ShareGPT / ChatML / raw text / prompt-completion
- GUI / dashboard for fine-tuning progress — `kubectl logs` + optional W&B

---

## 2. Surface area (unchanged from v1)

The user-facing CR stays identical to what the original plan describes. Repeating here for completeness:

```yaml
# Minimal — the 5-line case
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: support-bot-v1
  namespace: inference
spec:
  baseModel: "unsloth/llama-3.1-8b-unsloth-bnb-4bit"
  dataset: "s3://ai-platform-datasets/support-tickets.jsonl"
  autoDeploy: true
```

Power-user fields (all defaulted): `method`, `loraRank`, `loraAlpha`, `learningRate`, `epochs`, `batchSize`, `gradientAccumulation`, `maxSeqLength`, `datasetFormat`, `gpuCount`, `workerMemory`, `workerCpu`, `minVramPerGpuGiB`, `timeoutHours`, `shared`, `outputFormat`, `outputBucket`, `pushToHub`, `wandbProject`.

Same names, same defaults, same semantics as the original plan §3. Don't restate the full schema here — see original plan.

---

## 3. Corrected architecture

```
                         ┌──────────────────────────────────────┐
                         │  FineTuneJob CR (committed by user)  │
                         └──────────────────────────────────────┘
                                          │
                                  KRO expands into:
                                          │
        ┌─────────────────────────────────┼───────────────────────────────────┐
        │                                 │                                   │
   [validation]                     [training]                             [logs]
   batch/v1 Job                  batch/v1 Job (GPU)                CloudWatch LogGroup
   curl image                    custom unsloth image                via ACK
        │                                 │
        │                       initContainers (run in order):
        │                         ① wait-for-validation       (kubectl wait)
        │                         ② sync-base-model           (s5cmd from HF cache S3)
        │                         ③ sync-dataset              (s5cmd from dataset S3)
        │                                 │
        │                       main: train.py (unsloth + trl)
        │                                 │
        │                       post-stop: tail-runner script
        │                         ④ s5cmd sync OUTPUT → s3://…/fine-tuned/{name}/{ts}/
        │                         ⑤ if autoDeploy: kubectl apply InferenceEndpoint
        │                                 │
        ▼                                 ▼
   passes                         success (model in S3)
                                          │
                              ┌───────────┴───────────┐
                              │  autoDeploy=true?     │
                              └───────────────────────┘
                                          │ yes
                                          ▼
                            New InferenceEndpoint CR
                            (with `modelSource:` extension —
                             init container syncs from
                             `fine-tuned/{name}/{ts}/`)
```

Three deliberate divergences from the original plan:

| Concern | Original plan | v2 plan | Why |
|---------|--------------|---------|-----|
| Trainer image | `unsloth/unsloth:latest` (doesn't exist) | Custom Dockerfile, mirrored to ECR | We need a working image; bake it once, reuse forever |
| Auto-deploy delivery | KRO creates `InferenceEndpoint` as nested resource | Training Job calls `kubectl apply` at the end | KRO can't gate sub-resource creation on Job completion; if it created it eagerly the init-container would hammer S3 forever waiting for the model |
| Model loading by vLLM | "vLLM supports `--model s3://...`" | Reuse the existing `s5cmd` init-container; extend `InferenceEndpoint` schema with `modelSource` | vLLM doesn't support S3 URIs; we already have the init-container pattern |

---

## 4. Concrete artifacts

### 4.1 The custom Unsloth Dockerfile

**File:** `platform/services/unsloth-trainer/Dockerfile`

```dockerfile
# Pinned to specific NVIDIA PyTorch base — deterministic, gets cuDNN, FlashAttention,
# and Triton already installed. Saves ~3 GB and ~5 min vs starting from scratch.
FROM nvcr.io/nvidia/pytorch:24.10-py3

# Install Unsloth (pinned). The trl/peft/transformers/accelerate versions are
# pinned via Unsloth's own constraints — don't override them.
RUN pip install --no-cache-dir \
        "unsloth==2026.05.0" \
        "trl==0.21.0" \
        "datasets>=2.14.0" \
        "wandb>=0.16.0" \
        "huggingface_hub[hf_transfer]>=0.20.0" \
    && pip cache purge

# s5cmd for fast S3 transfers (matching the InferenceEndpoint init-container)
ARG S5CMD_VERSION=2.3.0
RUN curl -sSL "https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/s5cmd_${S5CMD_VERSION}_Linux-64bit.tar.gz" \
    | tar -xzC /usr/local/bin s5cmd

# kubectl for the autoDeploy step (Job creates InferenceEndpoint at end of training)
ARG KUBECTL_VERSION=v1.30.5
RUN curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl

# Disable HF Transfer warning chatter during training
ENV HF_HUB_ENABLE_HF_TRANSFER=1

WORKDIR /workspace
```

**Image size budget**: ~18 GB compressed (NVIDIA PyTorch + Unsloth deps). Same order of magnitude as `anyscale/ray-llm` (~15 GB), so cold-start optimization (EBS snapshot + SOCI) handles it identically.

### 4.2 ECR mirroring (Terraform)

**File:** `terraform/30.eks/30.cluster/unsloth-image.tf` (new)

```hcl
# Build + push the Unsloth trainer image to ECR. Runs once per Terraform apply,
# only rebuilds when Dockerfile or args change. Image is private to the account.

resource "aws_ecr_repository" "unsloth_trainer" {
  count                = local.capabilities.gitops ? 1 : 0
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

# Build the image locally, tag with content hash, push.
# Uses the `docker_image` and `docker_registry_image` resources from
# kreuzwerker/docker provider.
resource "docker_image" "unsloth_trainer" {
  count = local.capabilities.gitops ? 1 : 0
  name  = "${aws_ecr_repository.unsloth_trainer[0].repository_url}:${local.unsloth_image_tag}"

  build {
    context = "${path.module}/../../../platform/services/unsloth-trainer"
  }

  triggers = {
    dockerfile_sha = filesha256("${path.module}/../../../platform/services/unsloth-trainer/Dockerfile")
    image_tag      = local.unsloth_image_tag
  }
}

resource "docker_registry_image" "unsloth_trainer" {
  count = local.capabilities.gitops ? 1 : 0
  name  = docker_image.unsloth_trainer[0].name

  triggers = {
    image_id = docker_image.unsloth_trainer[0].image_id
  }
}
```

**File:** `terraform/30.eks/30.cluster/locals.tf` — add to `locals { ... }`:

```hcl
unsloth_image_tag = "0.1.0"   # bump to rebuild and re-push
unsloth_image     = local.capabilities.gitops ? "${aws_ecr_repository.unsloth_trainer[0].repository_url}:${local.unsloth_image_tag}" : ""
```

**File:** `terraform/30.eks/30.cluster/capabilities.tf` — add `unslothImage` to the `platform_config` ConfigMap so KRO can reference it:

```hcl
data = {
  cluster          = module.eks.cluster_name
  region           = local.region
  rayImage         = ...
  modelCacheBucket = ...
  unslothImage     = local.unsloth_image   # NEW
}
```

### 4.3 ServiceAccount + IRSA for fine-tuning

The existing `inference-worker` SA only writes to `hf/*`. Fine-tuning needs to write to `fine-tuned/*` AND has to be able to create `InferenceEndpoint` CRs (when `autoDeploy: true`). Cleanest path: a dedicated SA.

**File:** `terraform/30.eks/30.cluster/capabilities.tf` — append to existing IRSA section:

```hcl
# IAM role for fine-tuning training Pods (separate from inference SA so the
# IAM trust boundary is tighter — inference pods can't write to fine-tuned/).
resource "aws_iam_role" "fine_tuning_worker" {
  count = local.capabilities.gitops ? 1 : 0
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
  count = local.capabilities.gitops ? 1 : 0
  name  = "fine-tuning-s3-access"
  role  = aws_iam_role.fine_tuning_worker[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read base model weights from the HF cache (input)
        Sid      = "ReadHFCache"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.model_cache[0].arn}/hf/*"
      },
      {
        # Write fine-tuned model artifacts (output) — separate prefix
        Sid    = "WriteFineTuned"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [
          "${aws_s3_bucket.model_cache[0].arn}/fine-tuned/*",
          "${aws_s3_bucket.model_cache[0].arn}/hf/*",   # cache-warm trained model after upload
        ]
      },
      {
        Sid      = "ListBuckets"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.model_cache[0].arn
        Condition = {
          StringLike = { "s3:prefix" = ["hf/*", "fine-tuned/*", ""] }
        }
      },
      {
        # Read training datasets (separate dataset bucket — see §4.4)
        Sid      = "ReadDatasets"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.training_datasets[0].arn,
          "${aws_s3_bucket.training_datasets[0].arn}/*",
        ]
      },
    ]
  })
}

resource "kubernetes_service_account" "fine_tuning_worker" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "fine-tuning-worker"
    namespace = "inference"
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.fine_tuning_worker[0].arn
    }
  }

  depends_on = [kubernetes_namespace.inference]
}

# Cluster role binding so the SA can create InferenceEndpoint CRs (autoDeploy)
resource "kubernetes_role" "fine_tuning_create_endpoint" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "fine-tuning-create-endpoint"
    namespace = "inference"
  }

  rule {
    api_groups = ["kro.run"]
    resources  = ["inferenceendpoints"]
    verbs      = ["create", "get", "list", "patch", "update"]
  }
}

resource "kubernetes_role_binding" "fine_tuning_create_endpoint" {
  count = local.capabilities.gitops ? 1 : 0

  metadata {
    name      = "fine-tuning-create-endpoint"
    namespace = "inference"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.fine_tuning_create_endpoint[0].metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "fine-tuning-worker"
    namespace = "inference"
  }
}
```

### 4.4 Training datasets bucket

The original plan referenced `s3://ai-platform-datasets/...` but no such bucket exists in Terraform. We need one.

**File:** `terraform/30.eks/30.cluster/capabilities.tf` — add alongside `model_cache`:

```hcl
resource "aws_s3_bucket" "training_datasets" {
  count         = local.capabilities.gitops ? 1 : 0
  bucket        = "${local.cluster_name}-training-datasets"
  force_destroy = false   # actual user data; don't auto-delete

  tags = merge(local.tags, { Purpose = "fine-tuning-datasets" })
}

resource "aws_s3_bucket_public_access_block" "training_datasets" {
  count                   = local.capabilities.gitops ? 1 : 0
  bucket                  = aws_s3_bucket.training_datasets[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "training_datasets" {
  count  = local.capabilities.gitops ? 1 : 0
  bucket = aws_s3_bucket.training_datasets[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "training_datasets" {
  count  = local.capabilities.gitops ? 1 : 0
  bucket = aws_s3_bucket.training_datasets[0].id
  versioning_configuration {
    status = "Enabled"   # keep history of dataset versions for reproducibility
  }
}
```

Add to `platform_config` ConfigMap:
```hcl
trainingDatasetsBucket = local.capabilities.gitops ? aws_s3_bucket.training_datasets[0].bucket : ""
```

### 4.5 The KRO `FineTuneJob` ResourceGraphDefinition

**File:** `platform/config/kro/fine-tuning-job.yaml` (new)

This is the largest single artifact. Structure mirrors `inference-endpoint.yaml`. Skeleton + critical sections shown — full file ~400 lines.

```yaml
apiVersion: kro.run/v1alpha1
kind: ResourceGraphDefinition
metadata:
  name: fine-tuning-job
spec:
  schema:
    apiVersion: v1alpha1
    kind: FineTuneJob
    spec:
      # === Required ===
      baseModel:             "string"
      dataset:               "string"

      # === Training (defaults from original plan §3) ===
      method:                "string  | default=qlora"
      loraRank:              "integer | default=16"
      loraAlpha:             "integer | default=16"
      learningRate:          "string  | default=2e-4"
      epochs:                "integer | default=1"
      batchSize:             "integer | default=2"
      gradientAccumulation:  "integer | default=4"
      maxSeqLength:          "integer | default=2048"
      datasetFormat:         "string  | default=auto"

      # === Resources ===
      gpuCount:              "integer | default=1"
      workerMemory:          "string  | default=32Gi"
      workerCpu:             "string  | default=8"
      minVramPerGpuGiB:      "integer | default=24"
      timeoutHours:          "integer | default=6"
      shared:                "boolean | default=false"

      # === Output ===
      outputFormat:          "string  | default=safetensors"
      pushToHub:             "string  | default="
      autoDeploy:            "boolean | default=false"

      # === Optional ===
      wandbProject:          "string  | default="
      unslothImage:          "string  | default="   # override Terraform-provided image

    status:
      # The training Job is the source of truth.
      phase: "${trainingjob.status.?succeeded.orValue(0) > 0 ? 'Completed' : (trainingjob.status.?active.orValue(0) > 0 ? 'Training' : (trainingjob.status.?failed.orValue(0) >= 3 ? 'Failed' : 'Pending'))}"
      outputPath: "s3://${platformconfig.data.modelCacheBucket}/fine-tuned/${schema.metadata.name}/"
      jobName: "${trainingjob.metadata.name}"

  resources:
    - id: platformconfig
      externalRef:
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: platform-config

    # ----- ConfigMap with the train.py script -----
    - id: trainscript
      template:
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: ${schema.metadata.name}-trainscript
        data:
          train.py: |
            # Full script in §4.6 below — embedded literally here, environment-driven.

    # ----- Validation Job (cheap, runs first) -----
    - id: validationjob
      template:
        apiVersion: batch/v1
        kind: Job
        metadata:
          name: ${schema.metadata.name}-validate
        spec:
          backoffLimit: 0
          ttlSecondsAfterFinished: 600
          template:
            spec:
              restartPolicy: Never
              serviceAccountName: fine-tuning-worker
              containers:
                - name: validate
                  image: amazon/aws-cli:2.17.30
                  command: ["sh", "-c"]
                  args:
                    - |
                      set -eo pipefail
                      # Validate baseModel exists on HF
                      MODEL_HTTP=$(curl -s -o /dev/null -w '%{http_code}' \
                        "https://huggingface.co/api/models/$BASE_MODEL")
                      if [ "$MODEL_HTTP" = "404" ]; then
                        echo "ERROR: baseModel '$BASE_MODEL' not found on HuggingFace"
                        exit 1
                      fi
                      # Validate dataset (S3 URI or HF dataset)
                      case "$DATASET" in
                        s3://*)
                          aws s3 ls "$DATASET" || {
                            echo "ERROR: dataset $DATASET not accessible"; exit 1; }
                          ;;
                        HuggingFace:*)
                          DS=$(echo "$DATASET" | sed 's/^HuggingFace://')
                          DS_HTTP=$(curl -s -o /dev/null -w '%{http_code}' \
                            "https://huggingface.co/api/datasets/$DS")
                          [ "$DS_HTTP" = "200" ] || { echo "ERROR: HF dataset $DS not found"; exit 1; }
                          ;;
                        *)
                          echo "ERROR: dataset must be 's3://...' or 'HuggingFace:org/dataset'"; exit 1
                          ;;
                      esac
                      # Validate method, loraRank, gpuCount
                      case "$METHOD" in qlora|lora|full) ;; *) echo "ERROR: invalid method $METHOD"; exit 1 ;; esac
                      case "$LORA_RANK" in 8|16|32|64|128) ;; *) echo "ERROR: invalid loraRank $LORA_RANK"; exit 1 ;; esac
                      [ "$GPU_COUNT" = "1" ] || { echo "ERROR: V1 only supports gpuCount=1"; exit 1; }
                      echo "✓ Validation passed"
                  env:
                    - name: BASE_MODEL
                      value: ${schema.spec.baseModel}
                    - name: DATASET
                      value: ${schema.spec.dataset}
                    - name: METHOD
                      value: ${schema.spec.method}
                    - name: LORA_RANK
                      value: ${string(schema.spec.loraRank)}
                    - name: GPU_COUNT
                      value: ${string(schema.spec.gpuCount)}

    # ----- Training Job — the heavy lifter -----
    - id: trainingjob
      template:
        apiVersion: batch/v1
        kind: Job
        metadata:
          name: ${schema.metadata.name}-train
          annotations:
            karpenter.sh/do-not-disrupt: "true"
        spec:
          backoffLimit: 2   # fewer retries — training is expensive
          activeDeadlineSeconds: ${schema.spec.timeoutHours * 3600}
          ttlSecondsAfterFinished: 86400   # keep logs around for 24h
          template:
            metadata:
              annotations:
                karpenter.sh/do-not-disrupt: "true"
            spec:
              restartPolicy: Never
              serviceAccountName: fine-tuning-worker
              tolerations:
                - key: nvidia.com/gpu
                  operator: Exists
                  effect: NoSchedule
              nodeSelector:
                workload-type: "${schema.spec.shared ? 'gpu-shared' : 'gpu-inference'}"
              affinity:
                nodeAffinity:
                  requiredDuringSchedulingIgnoredDuringExecution:
                    nodeSelectorTerms:
                      - matchExpressions:
                          - key: karpenter.k8s.aws/instance-gpu-memory
                            operator: Gt
                            values:
                              - "${string(schema.spec.minVramPerGpuGiB > 0 ? (schema.spec.minVramPerGpuGiB * 1024) - 1 : 0)}"
              initContainers:
                # Wait for validation Job to complete (success only)
                - name: wait-validation
                  image: bitnami/kubectl:1.30.5
                  command: ["sh", "-c"]
                  args:
                    - |
                      kubectl wait --for=condition=complete --timeout=10m \
                        job/${schema.metadata.name}-validate -n ${schema.metadata.namespace}
              containers:
                - name: trainer
                  image: ${schema.spec.unslothImage != "" ? schema.spec.unslothImage : platformconfig.data.unslothImage}
                  command: ["python", "/scripts/train.py"]
                  env:
                    - name: BASE_MODEL
                      value: ${schema.spec.baseModel}
                    - name: DATASET
                      value: ${schema.spec.dataset}
                    - name: METHOD
                      value: ${schema.spec.method}
                    - name: LORA_RANK
                      value: ${string(schema.spec.loraRank)}
                    - name: LORA_ALPHA
                      value: ${string(schema.spec.loraAlpha)}
                    - name: LEARNING_RATE
                      value: ${schema.spec.learningRate}
                    - name: EPOCHS
                      value: ${string(schema.spec.epochs)}
                    - name: BATCH_SIZE
                      value: ${string(schema.spec.batchSize)}
                    - name: GRAD_ACCUM
                      value: ${string(schema.spec.gradientAccumulation)}
                    - name: MAX_SEQ_LEN
                      value: ${string(schema.spec.maxSeqLength)}
                    - name: DATASET_FORMAT
                      value: ${schema.spec.datasetFormat}
                    - name: OUTPUT_FORMAT
                      value: ${schema.spec.outputFormat}
                    - name: OUTPUT_DIR
                      value: /workspace/output
                    - name: CACHE_BUCKET
                      value: ${platformconfig.data.modelCacheBucket}
                    - name: DATASETS_BUCKET
                      value: ${platformconfig.data.trainingDatasetsBucket}
                    - name: AUTO_DEPLOY
                      value: ${string(schema.spec.autoDeploy)}
                    - name: JOB_NAME
                      value: ${schema.metadata.name}
                    - name: NAMESPACE
                      value: ${schema.metadata.namespace}
                    - name: PUSH_TO_HUB
                      value: ${schema.spec.pushToHub}
                    - name: WANDB_PROJECT
                      value: ${schema.spec.wandbProject}
                    - name: HF_TOKEN
                      valueFrom:
                        secretKeyRef:
                          name: hf-token
                          key: token
                          optional: true
                    - name: WANDB_API_KEY
                      valueFrom:
                        secretKeyRef:
                          name: wandb-api-key
                          key: api-key
                          optional: true
                    - name: HF_HUB_ENABLE_HF_TRANSFER
                      value: "1"
                  resources:
                    requests:
                      cpu: ${schema.spec.workerCpu}
                      memory: ${schema.spec.workerMemory}
                      nvidia.com/gpu: ${schema.spec.gpuCount}
                    limits:
                      cpu: ${schema.spec.workerCpu}
                      memory: ${schema.spec.workerMemory}
                      nvidia.com/gpu: ${schema.spec.gpuCount}
                  volumeMounts:
                    - name: trainscript
                      mountPath: /scripts
                    - name: workspace
                      mountPath: /workspace
              volumes:
                - name: trainscript
                  configMap:
                    name: ${schema.metadata.name}-trainscript
                    items:
                      - key: train.py
                        path: train.py
                - name: workspace
                  emptyDir:
                    sizeLimit: 200Gi   # base model + dataset + output

    # ----- CloudWatch Log Group (via ACK) -----
    - id: loggroup
      template:
        apiVersion: cloudwatchlogs.services.k8s.aws/v1alpha1
        kind: LogGroup
        metadata:
          name: ${schema.metadata.name}-finetune-logs
        spec:
          name: /ai-platform/fine-tuning/${schema.metadata.name}
          retentionDays: 30
          tags:
            ai-platform/managed-by: kro
            ai-platform/finetune-job: ${schema.metadata.name}
```

### 4.6 The training script (mounted via ConfigMap)

**Embedded inside `fine-tuning-job.yaml` ConfigMap.** ~250 lines of Python. Key design choices:

```python
#!/usr/bin/env python3
"""train.py — runs inside the FineTuneJob's training container.

Reads all configuration from environment variables. No CLI args — simpler
to inject from KRO via env, harder to accidentally hardcode something.
"""
import json, os, subprocess, sys, time
from pathlib import Path

# 1. Read env (with fail-fast on required values)
def env(name: str, default: str | None = None, required: bool = False) -> str:
    v = os.environ.get(name, default)
    if required and (v is None or v == ""):
        sys.exit(f"FATAL: env {name} is required")
    return v

BASE_MODEL = env("BASE_MODEL", required=True)
DATASET = env("DATASET", required=True)
METHOD = env("METHOD", "qlora")
LORA_RANK = int(env("LORA_RANK", "16"))
LORA_ALPHA = int(env("LORA_ALPHA", "16"))
LEARNING_RATE = float(env("LEARNING_RATE", "2e-4"))
EPOCHS = int(env("EPOCHS", "1"))
BATCH_SIZE = int(env("BATCH_SIZE", "2"))
GRAD_ACCUM = int(env("GRAD_ACCUM", "4"))
MAX_SEQ_LEN = int(env("MAX_SEQ_LEN", "2048"))
DATASET_FORMAT = env("DATASET_FORMAT", "auto")
OUTPUT_FORMAT = env("OUTPUT_FORMAT", "safetensors")
OUTPUT_DIR = env("OUTPUT_DIR", "/workspace/output")
CACHE_BUCKET = env("CACHE_BUCKET", required=True)
DATASETS_BUCKET = env("DATASETS_BUCKET", "")
JOB_NAME = env("JOB_NAME", required=True)
NAMESPACE = env("NAMESPACE", "inference")
AUTO_DEPLOY = env("AUTO_DEPLOY", "false").lower() == "true"
PUSH_TO_HUB = env("PUSH_TO_HUB", "")
WANDB_PROJECT = env("WANDB_PROJECT", "")
TIMESTAMP = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

# 2. Sync dataset locally
local_dataset = "/workspace/dataset"
os.makedirs(local_dataset, exist_ok=True)

if DATASET.startswith("s3://"):
    print(f"Syncing dataset from {DATASET} to {local_dataset}")
    subprocess.check_call(["s5cmd", "cp", DATASET, f"{local_dataset}/data.jsonl"])
    DATASET_PATH = f"{local_dataset}/data.jsonl"
elif DATASET.startswith("HuggingFace:"):
    DATASET_PATH = DATASET.removeprefix("HuggingFace:")
else:
    sys.exit(f"FATAL: unknown dataset scheme: {DATASET}")

# 3. Configure W&B if requested (must be before unsloth import)
if WANDB_PROJECT:
    os.environ["WANDB_PROJECT"] = WANDB_PROJECT
    os.environ["WANDB_LOG_MODEL"] = "false"   # we save to S3 ourselves
else:
    os.environ["WANDB_DISABLED"] = "true"

# 4. Train
from unsloth import FastLanguageModel, is_bfloat16_supported
from trl import SFTTrainer
from transformers import TrainingArguments
from datasets import load_dataset

load_in_4bit = (METHOD == "qlora")
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=BASE_MODEL,
    max_seq_length=MAX_SEQ_LEN,
    dtype=None,
    load_in_4bit=load_in_4bit,
)

if METHOD in ("qlora", "lora"):
    model = FastLanguageModel.get_peft_model(
        model,
        r=LORA_RANK,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        lora_alpha=LORA_ALPHA,
        lora_dropout=0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=3407,
    )

# Dataset format detection — see §4.6.1
ds = load_dataset("json", data_files=DATASET_PATH, split="train") \
    if DATASET_PATH.startswith("/") else load_dataset(DATASET_PATH, split="train")
ds = format_dataset(ds, tokenizer, override=DATASET_FORMAT)

trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=ds,
    args=TrainingArguments(
        per_device_train_batch_size=BATCH_SIZE,
        gradient_accumulation_steps=GRAD_ACCUM,
        num_train_epochs=EPOCHS,
        learning_rate=LEARNING_RATE,
        weight_decay=0.01,
        warmup_steps=5,
        lr_scheduler_type="linear",
        output_dir=OUTPUT_DIR,
        logging_steps=10,
        save_strategy="no",   # we handle save ourselves
        bf16=is_bfloat16_supported(),
        fp16=not is_bfloat16_supported(),
        report_to=["wandb"] if WANDB_PROJECT else "none",
    ),
    max_seq_length=MAX_SEQ_LEN,
)
trainer.train()

# 5. Export
merged_dir = f"{OUTPUT_DIR}/merged"
os.makedirs(merged_dir, exist_ok=True)

if OUTPUT_FORMAT == "safetensors":
    model.save_pretrained_merged(merged_dir, tokenizer, save_method="merged_16bit")
elif OUTPUT_FORMAT.startswith("gguf-"):
    quant = OUTPUT_FORMAT.removeprefix("gguf-")
    model.save_pretrained_gguf(merged_dir, tokenizer, quantization_method=quant)
elif OUTPUT_FORMAT == "lora-only":
    model.save_pretrained(merged_dir)
    tokenizer.save_pretrained(merged_dir)
else:
    sys.exit(f"FATAL: unknown outputFormat: {OUTPUT_FORMAT}")

# 6. Push to HF Hub if requested (additive)
if PUSH_TO_HUB:
    print(f"Pushing to HuggingFace Hub: {PUSH_TO_HUB}")
    model.push_to_hub_merged(PUSH_TO_HUB, tokenizer, save_method="merged_16bit")

# 7. Upload to S3
s3_prefix = f"s3://{CACHE_BUCKET}/fine-tuned/{JOB_NAME}/{TIMESTAMP}/"
print(f"Uploading {merged_dir}/ to {s3_prefix}")
subprocess.check_call(["s5cmd", "sync", f"{merged_dir}/", s3_prefix])

# 8. Optional autoDeploy — create an InferenceEndpoint pointing at the local sync of this S3 path
if AUTO_DEPLOY:
    endpoint_yaml = f"""
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: {JOB_NAME}
  namespace: {NAMESPACE}
  labels:
    ai-platform/source: fine-tuning
    ai-platform/finetune-job: {JOB_NAME}
spec:
  model: {JOB_NAME}                             # alias used by LiteLLM
  modelSource: fine-tuned/{JOB_NAME}/{TIMESTAMP}/  # S3 prefix; init container syncs to local
  gpuCount: 1
  minReplicas: 1
  maxReplicas: 1
  workerMemory: "16Gi"
  minVramPerGpuGiB: 24
"""
    with open("/tmp/endpoint.yaml", "w") as f:
        f.write(endpoint_yaml)
    subprocess.check_call(["kubectl", "apply", "-f", "/tmp/endpoint.yaml"])
    print(f"✓ Created InferenceEndpoint {JOB_NAME}")

print(f"✓ Fine-tuning complete. Model: {s3_prefix}")
```

> **⚠️ Known limitation — autoDeploy is `kubectl apply`, not GitOps (deferred).**
> The current autoDeploy path has the trainer Job `kubectl apply` the InferenceEndpoint
> directly. The resulting IE is a real, working endpoint — but it is **untracked by
> ArgoCD and absent from git**: there is no YAML in `workloads/models/`, so it can't be
> reviewed, won't survive a cluster rebuild, and won't be pruned by deleting any file.
> Deleting it requires a direct `kubectl delete` (it has no ArgoCD tracking-id and no
> owner-ref to the FineTuneJob — the two are independent top-level objects).
>
> **Kept as-is on purpose** for now: the immediate `kubectl apply` makes the model live
> within the same Job, which is the smoothest path for a live demo.
>
> **Future implementation** (see [§9 open question 8](#9-open-questions-intentionally-deferred-to-v2)):
> on training success, instead of `kubectl apply`, have the Job **open an MR/PR** that
> adds `workloads/models/{JOB_NAME}.yaml` (the same IE spec above, name suffixed `-tuned`
> for clarity) to the GitOps repo. A human merges → ArgoCD deploys it through the
> standard managed path. The endpoint then becomes reviewable, durable, and decoupled
> from the FineTuneJob for real. Requires a repo-scoped bot token in the trainer Job and
> targets only `gitops_repo_url` (the platform has two remotes). When `autoDeploy: false`,
> the symmetric behaviour is to emit the ready-to-commit IE YAML (logs + artifact) with
> the exact `git add/commit/push`, rather than applying anything.

#### 4.6.1 Dataset format detection (within `train.py`)

```python
def format_dataset(ds, tokenizer, override="auto"):
    cols = set(ds.column_names)
    fmt = override if override != "auto" else _detect_format(cols)

    if fmt == "alpaca":
        def _alpaca(row):
            inp = row.get("input") or ""
            prompt = (f"### Instruction:\n{row['instruction']}\n"
                      + (f"### Input:\n{inp}\n" if inp else "")
                      + f"### Response:\n{row['output']}")
            return {"text": prompt}
        return ds.map(_alpaca, remove_columns=ds.column_names)

    if fmt in ("sharegpt", "chatml"):
        # Use the model's native chat template
        def _apply(row):
            messages = row["conversations"] if fmt == "sharegpt" else row["messages"]
            return {"text": tokenizer.apply_chat_template(messages, tokenize=False)}
        return ds.map(_apply, remove_columns=ds.column_names)

    if fmt == "completion":
        return ds.map(lambda r: {"text": r["prompt"] + r["completion"]},
                      remove_columns=ds.column_names)

    if fmt == "raw":
        return ds   # already has 'text' column

    raise ValueError(f"Cannot determine dataset format from columns: {cols}")

def _detect_format(cols):
    if {"instruction", "output"}.issubset(cols):     return "alpaca"
    if "conversations" in cols:                       return "sharegpt"
    if "messages" in cols:                            return "chatml"
    if {"prompt", "completion"}.issubset(cols):       return "completion"
    if cols == {"text"}:                              return "raw"
    raise ValueError(f"Unknown dataset format. Columns: {cols}. Set --datasetFormat explicitly.")
```

### 4.7 InferenceEndpoint extension — `modelSource`

The fine-tuning autoDeploy creates an InferenceEndpoint with `modelSource: fine-tuned/{name}/{ts}/`. The existing schema needs one new field and the init container needs one tweak.

**File:** `platform/config/kro/inference-endpoint.yaml` (modify)

Add to `spec.schema.spec`:
```yaml
modelSource: "string | default="   # S3 prefix relative to model-cache bucket; if set,
                                   # init container syncs from there and Ray uses local path
```

Modify the init container:
```yaml
initContainers:
  - name: hf-cache-download
    image: peakcom/s5cmd:v2.3.0
    env:
      - name: MODEL_ID
        value: ${schema.spec.model}
      - name: MODEL_SOURCE                                    # NEW
        value: ${schema.spec.modelSource}
      - name: CACHE_BUCKET
        value: ${platformConfig.data.?modelCacheBucket.orValue("")}
      ...
    command:
      - sh
      - -c
      - |
        set -u
        if [ -z "$CACHE_BUCKET" ]; then exit 0; fi
        if [ -n "$MODEL_SOURCE" ]; then
          # Fine-tuned model — sync from explicit S3 prefix to a path Ray will use
          PREFIX="$MODEL_SOURCE"
          LOCAL="/hf-cache/finetuned"
        else
          # Standard HF model — original behavior
          PREFIX="hf/$MODEL_ID"
          LOCAL="/hf-cache/hub/models--$(echo "$MODEL_ID" | sed 's#/#--#g')"
        fi
        # ... existing s5cmd logic, parameterized on PREFIX/LOCAL ...
```

Modify the Ray Serve config:
```yaml
serveConfigV2: |
  applications:
    - name: llm
      ...
      args:
        llm_configs:
          - model_loading_config:
              model_id: ${schema.spec.model}                                                  # alias for clients
              model_source: ${schema.spec.modelSource != "" ? "/hf-cache/finetuned" : schema.spec.model}   # NEW: local path for fine-tuned, HF id otherwise
            engine_kwargs: ...
```

### 4.8 ArgoCD ApplicationSet update

**File:** `argocd/bootstrap/workloads.yaml`

Add to the list generator:
```yaml
generators:
  - list:
      elements:
        - name: models
          path: workloads/models
        - name: teams
          path: workloads/teams
        - name: fine-tuning           # NEW
          path: workloads/fine-tuning
```

### 4.9 Workloads template

**File:** `workloads/fine-tuning/TEMPLATE.yaml.example` (new)

```yaml
# FineTuneJob Template — copy, rename, fill in baseModel + dataset.
# All other fields are optional with sensible defaults.
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: my-fine-tune              # becomes LiteLLM alias if autoDeploy=true
  namespace: inference
spec:
  baseModel: "unsloth/llama-3.1-8b-unsloth-bnb-4bit"   # REQUIRED — Unsloth pre-quant or HF model
  dataset: "s3://<cluster>-training-datasets/example.jsonl"  # REQUIRED
  # autoDeploy: true              # auto-create InferenceEndpoint after training

  # Training hyperparameters (defaults shown)
  # method: qlora                 # qlora (default), lora, full
  # loraRank: 16
  # epochs: 1
  # batchSize: 2
  # maxSeqLength: 2048

  # Resources (let recommend-instance.py compute)
  # gpuCount: 1
  # minVramPerGpuGiB: 24
  # timeoutHours: 6

  # Output
  # outputFormat: safetensors     # safetensors (vLLM-ready), gguf-q4_k_m, gguf-q8_0, lora-only
  # pushToHub: ""                 # HuggingFace repo to also push to (optional)

  # Optional integrations
  # wandbProject: my-experiments  # requires wandb-api-key Secret in inference ns
```

**File:** `workloads/fine-tuning/.gitkeep` (empty, to ensure ArgoCD finds the directory)

---

## 5. Phase-by-phase implementation order

Each phase ends with a verifiable acceptance criterion. Don't move to the next phase until the criterion is met.

| Phase | What | Deliverables | Effort | Acceptance test |
|-------|------|--------------|--------|-----------------|
| **P0** | Build & push Docker image | `Dockerfile`, `unsloth-image.tf`, `locals.tf` updated | 1.5 h | `docker pull <ECR>/unsloth-trainer:0.1.0` succeeds; image has `unsloth==2026.05.0` and `python -c "from unsloth import FastLanguageModel"` works |
| **P1** | Terraform: SA + IRSA + datasets bucket | `capabilities.tf` additions | 1 h | `kubectl get sa fine-tuning-worker -n inference -o yaml` shows IRSA annotation; bucket exists |
| **P2** | KRO ResourceGraphDefinition + train.py ConfigMap | `platform/config/kro/fine-tuning-job.yaml` | 4 h | `kubectl apply` succeeds; `kubectl get rgd fine-tuning-job` shows `Status: Active` |
| **P3** | Workloads dir + ApplicationSet | `workloads/fine-tuning/`, `argocd/bootstrap/workloads.yaml` | 0.5 h | ArgoCD picks up `fine-tuning` ApplicationSet target |
| **P4** | Smoke test: tiny model, single epoch | Manual `FineTuneJob` for `unsloth/SmolLM2-135M-Instruct-bnb-4bit` on 100-row dataset | 1.5 h | Job completes in <30 min; `aws s3 ls s3://.../fine-tuned/<name>/` shows `.safetensors` files |
| **P5** | Extend InferenceEndpoint with `modelSource` | Modify `inference-endpoint.yaml` | 1 h | Existing models still deploy unchanged; new `modelSource` field accepted |
| **P6** | autoDeploy end-to-end | `FineTuneJob` with `autoDeploy: true` | 1.5 h | After training, query the auto-created endpoint via LiteLLM and get a sensible response from the fine-tuned model |
| **P7** | GGUF export support | Test `outputFormat: gguf-q4_k_m` | 0.5 h | `.gguf` file ends up in S3; downstream Ollama can load it |
| **P8** | CloudWatch + W&B integration | Test with `wandbProject` set | 0.5 h | Runs visible at wandb.ai; CloudWatch log group has training output |
| **P9** | Documentation | Update `README.md`, `CLAUDE.md`; write `docs/fine-tuning-getting-started.md` | 1 h | Anyone on the team can follow the doc end-to-end without asking |

**Total: ~13 hours of focused work**, plus integration testing time waiting for GPU jobs to complete.

### Critical-path dependencies

```
P0 ─────┐
        ├──► P2 ──► P3 ──► P4 ──► P6 ──► P7
P1 ─────┘                       ▲
                                │
                          P5 ───┘
                                │
                          P8, P9 (parallel)
```

P0 and P1 can be done in parallel. P5 must finish before P6.

---

## 6. Testing strategy

### Smoke test (P4 — minutes, free)

```bash
# 1. Push the FineTuneJob YAML
cat <<'EOF' > workloads/fine-tuning/test-smollm-ft.yaml
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: test-smollm-ft
  namespace: inference
spec:
  baseModel: "unsloth/SmolLM2-135M-Instruct-bnb-4bit"
  dataset: "HuggingFace:yahma/alpaca-cleaned"
  epochs: 1
  maxSeqLength: 512
  timeoutHours: 1
  minVramPerGpuGiB: 0   # any GPU
  shared: true          # use time-sliced
  autoDeploy: false
EOF
git add . && git commit -m "test: smollm fine-tune smoke" && git push

# 2. Watch progress
kubectl get finetunejobs -n inference -w
kubectl logs -n inference job/test-smollm-ft-train -f

# 3. Verify output
aws s3 ls s3://$(kubectl get cm platform-config -n inference -o jsonpath='{.data.modelCacheBucket}')/fine-tuned/test-smollm-ft/
# Should show: <timestamp>/model.safetensors, config.json, tokenizer.* etc.

# 4. Cleanup
kubectl delete finetunejob test-smollm-ft -n inference
git rm workloads/fine-tuning/test-smollm-ft.yaml && git commit -m "test: cleanup" && git push
```

### Integration test (P6 — autoDeploy verification)

```bash
# 1. Same YAML but autoDeploy: true
sed -i.bak 's/autoDeploy: false/autoDeploy: true/' workloads/fine-tuning/test-smollm-ft.yaml
git add . && git commit -m "test: autoDeploy" && git push

# 2. Wait for InferenceEndpoint to come up
kubectl wait --for=condition=ready --timeout=20m \
  inferenceendpoint/test-smollm-ft -n inference

# 3. Query via LiteLLM
LITELLM_KEY=$(kubectl get secret litellm-api-key -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d)
curl -sf http://litellm.ai-platform.svc.cluster.local:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"test-smollm-ft","messages":[{"role":"user","content":"What is 2+2?"}]}'

# 4. Cleanup (deletes both the FineTuneJob AND the auto-created InferenceEndpoint)
kubectl delete finetunejob test-smollm-ft -n inference
kubectl delete inferenceendpoint test-smollm-ft -n inference   # in case orphaned
```

### Production validation test (after P9)

```bash
# Real-world test: 8B model on a 1000-row support-ticket dataset.
# Expected: ~30 min training on a single L4, 24 GiB VRAM peak.
cat <<'EOF' > workloads/fine-tuning/support-bot-v1.yaml
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: support-bot-v1
  namespace: inference
spec:
  baseModel: "unsloth/llama-3.1-8b-unsloth-bnb-4bit"
  dataset: "s3://<cluster>-training-datasets/support-tickets-v1.jsonl"
  epochs: 2
  loraRank: 32
  autoDeploy: true
  wandbProject: ai-platform-fine-tuning
EOF
```

---

## 7. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Custom Unsloth image breaks on Unsloth release update | Med | Med | Pin `unsloth==2026.05.0` in Dockerfile; bump quarterly |
| GPU node OOM during training (model bigger than `minVramPerGpuGiB` claims) | Med | Low | `activeDeadlineSeconds` kills the Job; logs visible in CW |
| Job creates InferenceEndpoint but kubectl is missing in the trainer image | Low | High | Dockerfile installs kubectl explicitly (§4.1); P0 acceptance test catches this |
| ServiceAccount lacks RBAC to create InferenceEndpoint | Low | High | Terraform-managed Role + RoleBinding (§4.3); P6 test catches this |
| Dataset is too large to fit in `emptyDir` (200 GiB) | Low | Med | s5cmd streams from S3 — dataset doesn't need to fit; only base model + LoRA adapter weights need disk |
| Multiple teams' FineTuneJobs all schedule at once → GPU exhaustion | Med | Med | Karpenter NodePool's `limits.nvidia.com/gpu: 16` is the hard cap; later: per-team budgets |
| HuggingFace download fails mid-training (rate-limit / outage) | Low | Med | Pre-quantized Unsloth models are uploaded to S3 cache by warmup; HF is the fallback |
| Dataset format detection misclassifies | Med | Low | Override with `datasetFormat: alpaca/sharegpt/chatml/raw/completion` |
| `autoDeploy: true` creates an endpoint that fails to load | Low | Med | InferenceEndpoint stays `Pending` with clear error; user can `kubectl describe` and fix |
| Race: training finishes after `activeDeadlineSeconds` but before pod is killed → no S3 upload | Low | Med | `time.time()` checkpoints in `train.py` log how long is left; Job retries (`backoffLimit: 2`) |
| Karpenter kills the GPU node mid-training (node consolidation) | Low | High | `karpenter.sh/do-not-disrupt: "true"` annotation on Job pod template (§4.5) |

---

## 8. Decision log (where v2 diverges from v1)

| Decision | Why we chose this | What we considered |
|----------|-------------------|--------------------|
| Build custom Unsloth Docker image | The original assumed `unsloth/unsloth:latest` exists. It doesn't. We need a working image. | a) `pip install unsloth` at runtime in init-container — slow on every Job; b) Use `eightBEC/unsloth-docker` — community, no SLA |
| Mirror via ECR via Terraform-managed `docker_image` resource | Already have ECR pull-through cache for Docker Hub; this extends that pattern for our own image | Push image manually to ECR — error-prone, no rebuild trigger |
| Job-creates-InferenceEndpoint via kubectl | KRO can't gate sub-resource creation on Job completion; eager creation would mean InferenceEndpoint init container hammers an empty S3 path | KRO nested resource — would race; controller pattern — too heavy for V1 |
| Dedicated `fine-tuning-worker` SA (not reuse `inference-worker`) | Tighter blast radius; inference pods can't ever write to `fine-tuned/`; fine-tuning Pods can never touch `inference` HF cache writes | Reuse `inference-worker` — couples permissions; inference pod compromise can corrupt fine-tunes |
| Separate `training-datasets` S3 bucket | Datasets are user-uploaded data with versioning needs; model cache is regeneratable | Reuse model-cache bucket — confusing prefixes; lifecycle rules clash |
| Extend InferenceEndpoint with `modelSource` (string) instead of detecting `s3://` in `model` field | Backwards-compat: existing endpoints aren't broken; explicit signal that this is a fine-tuned local-path scenario | Auto-detect by URI — magic; ambiguous when an alias happens to start with `s3://` |
| Validation Job is a SEPARATE Job (not init-container of training) | Validation is fast (~10s) and cheap; if it fails, we don't even provision a GPU node | Validation as init-container in training — would still wait for GPU before failing |
| `kubectl wait --for=condition=complete` to gate training on validation | Standard pattern, no operator needed; same pattern as InferenceEndpoint registration job | KRO dependency annotation — KRO doesn't support cross-resource ordering |

---

## 9. Open questions (intentionally deferred to V2)

1. **Hyperparameter search**: V2 could integrate Optuna. Out of scope for first release.
2. **Multi-node distributed training**: Will need RayJob + DeepSpeed/FSDP. Different KRO RGD entirely.
3. **Adapter registry**: V1 uses S3 flat paths. Future MLflow integration possible.
4. **Cost tracking per team**: V1 uses Karpenter GPU limit as global cap. Per-team `gpuHours` budgets need an admission webhook.
5. **DPO / GRPO methods**: Different trainer (DPOTrainer in TRL). Would add another `method:` value.
6. **Cancel/resume**: V1 is fire-and-forget. Future: state checkpointing + resume from S3.
7. **Public dataset access**: Currently only S3 + gated HF datasets. Add support for public HF datasets (already works via `HuggingFace:org/dataset` URI but not extensively tested).
8. **GitOps-native autoDeploy (MR/PR)**: Today `autoDeploy: true` makes the trainer Job `kubectl apply` the InferenceEndpoint directly, producing an endpoint that is unmanaged by ArgoCD and absent from git (see the autoDeploy limitation note in §4.6). Future: on success, open an MR/PR adding `workloads/models/{name}-tuned.yaml` to `gitops_repo_url` so the tuned model deploys through the standard managed path — reviewable, durable, and decoupled from the FineTuneJob. When `autoDeploy: false`, emit the ready-to-commit IE YAML + exact git commands instead of applying. Needs a repo-scoped bot token in the trainer Job. **Kept as direct `kubectl apply` for now** because the in-Job deploy is the smoothest path for live demos.

---

## 10. Confidence rationale

I'm at 95 % confidence because:

- ✅ Read every file in the existing platform (KRO RGD, Terraform IRSA, Karpenter NodePools, ArgoCD ApplicationSet)
- ✅ Verified Ray Serve LLM accepts local paths via `model_source` field (Anyscale docs + Ray source)
- ✅ Verified vLLM does NOT accept S3 URIs (vLLM #3090) — adjusted plan to use init-container pattern
- ✅ Verified Unsloth has no official Docker image — adjusted plan to build our own
- ✅ Validated KRO can create CRs with init-container patterns and `kubectl wait` gating (existing inference-endpoint.yaml uses this)
- ✅ All Terraform additions follow the existing IRSA pattern in `capabilities.tf`
- ✅ Test acceptance criteria for each phase are concrete and verifiable

The remaining 5 % uncertainty:
- Dataset format auto-detection edge cases (mitigated by `datasetFormat:` override)
- Exact GPU memory peak for some Unsloth model variants (mitigated by `activeDeadlineSeconds` and `backoffLimit`)
- ECR pull-through cache and image build interaction in Terraform (well-trodden but new combination here)

These are implementation-detail risks, not architectural blockers. Each will be caught by P0/P4/P6 acceptance tests.

---

## 11. Files created or modified — complete list

### New files
- `platform/services/unsloth-trainer/Dockerfile`
- `platform/config/kro/fine-tuning-job.yaml`
- `terraform/30.eks/30.cluster/unsloth-image.tf`
- `workloads/fine-tuning/TEMPLATE.yaml.example`
- `workloads/fine-tuning/.gitkeep`
- `docs/fine-tuning-getting-started.md` (P9 deliverable)

### Modified files
- `terraform/30.eks/30.cluster/locals.tf` — add `unsloth_image_tag`, `unsloth_image`
- `terraform/30.eks/30.cluster/capabilities.tf` — add datasets bucket, fine-tuning IAM/SA, RBAC, ConfigMap entries
- `platform/config/kro/inference-endpoint.yaml` — add `modelSource` field + init-container conditional
- `argocd/bootstrap/workloads.yaml` — add `fine-tuning` element
- `README.md` — short fine-tuning section pointing at template + getting-started doc
- `CLAUDE.md` — add fine-tuning to capability matrix

### Conservative LOC estimate
- Dockerfile + Terraform: ~150 lines
- KRO RGD with embedded train.py: ~600 lines
- InferenceEndpoint amendments: ~25 lines diff
- Templates + ApplicationSet edits: ~80 lines
- Documentation: ~200 lines
- **Total: ~1,055 lines** of new + modified code, all reviewable in one or two PRs.

---

## 12. Sign-off checklist

Before merging V1 to `main`:

- [ ] P0 acceptance test passes: `unsloth-trainer:0.1.0` image pullable from ECR
- [ ] P1 acceptance test passes: SA exists with IRSA annotation
- [ ] P2 acceptance test passes: KRO `RGD` Active
- [ ] P3 acceptance test passes: ArgoCD picks up `fine-tuning` workloads dir
- [ ] P4 acceptance test passes: SmolLM 135M trains end-to-end, `.safetensors` in S3
- [ ] P5 acceptance test passes: existing endpoints unchanged
- [ ] P6 acceptance test passes: autoDeploy round-trips, LiteLLM serves the fine-tuned model
- [ ] P7 acceptance test passes: GGUF output works
- [ ] P8 acceptance test passes: W&B + CloudWatch logs visible
- [ ] Documentation reviewed by 1 platform engineer + 1 ML engineer
- [ ] `output.md` from a real fine-tune run committed for reference
- [ ] Cost: confirmed first deploy < $5 (tiny model on a single L4 spot)
