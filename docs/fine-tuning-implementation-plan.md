# Fine-Tuning KRO — Implementation Plan

**Status:** Planned
**Author:** Platform Team
**Date:** 2025-05-20

---

## 1. Objective

Extend the AI Platform with a self-service `FineTuneJob` custom resource that lets teams fine-tune LLMs by committing a short YAML to `workloads/fine-tuning/`. The platform handles GPU provisioning, training execution (via Unsloth), model export, and optional auto-deployment as an InferenceEndpoint.

### Platform Tenets Alignment

| Tenet | How Fine-Tuning Delivers |
|-------|--------------------------|
| Open | Unsloth is MIT-licensed, built on HuggingFace TRL/PEFT — no vendor lock-in |
| Easy to adopt | Same self-service pattern: commit YAML -> GPU provisions -> job runs -> model appears |
| Valuable for customers | Customized models are 10-100x more cost-effective than prompt engineering for domain-specific tasks |
| Highly automated | KRO expands one CR into: validation, training, export, and optional deployment |

---

## 2. User Experience (Target State)

### Minimal YAML (80% of use cases)

```yaml
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

5 lines of meaningful config. The platform:
1. Validates the model exists and dataset is accessible
2. Provisions an L4/A10G GPU node via Karpenter
3. Runs QLoRA training (~30 min for 1000 examples on 8B model)
4. Exports merged safetensors to S3 model-cache bucket
5. Creates an InferenceEndpoint that serves the fine-tuned model via vLLM

### Power-User YAML

```yaml
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: code-assistant-v2
  namespace: inference
spec:
  baseModel: "unsloth/Qwen2.5-Coder-32B-Instruct-bnb-4bit"
  dataset: "s3://ai-platform-datasets/code-instructions.jsonl"
  method: qlora
  loraRank: 32
  epochs: 2
  batchSize: 1
  gpuCount: 1
  minVramPerGpuGiB: 48
  outputFormat: safetensors
  autoDeploy: true
```

### Self-Service Template

```yaml
# workloads/fine-tuning/TEMPLATE.yaml.example
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: my-fine-tuned-model       # Becomes the model name in LiteLLM (if autoDeploy)
  namespace: inference
spec:
  baseModel: "unsloth/model-id"   # REQUIRED — Unsloth pre-quantized or HuggingFace model ID
  dataset: "s3://bucket/data.jsonl" # REQUIRED — S3 URI or HuggingFace dataset ID
  # autoDeploy: false             # Set true to auto-create InferenceEndpoint from output
  # method: qlora                 # qlora (default, 75% less VRAM), lora, full
  # loraRank: 16                  # LoRA rank: 8, 16, 32, 64, 128
  # epochs: 1                     # Training epochs (1-3 recommended)
  # batchSize: 2                  # Per-device batch size (reduce to 1 if OOM)
  # maxSeqLength: 2048            # Max sequence length
  # gpuCount: 1                   # GPUs (1 for QLoRA up to 70B)
  # minVramPerGpuGiB: 24          # Min VRAM per GPU (24=L4/A10G, 48=L40S, 80=A100)
  # outputFormat: safetensors     # safetensors (vLLM), gguf-q4_k_m, gguf-q8_0, lora-only
  # timeoutHours: 6               # Kill runaway training
```

---

## 3. KRO Schema Definition

```yaml
spec:
  schema:
    apiVersion: v1alpha1
    kind: FineTuneJob
    spec:
      # === REQUIRED ===
      baseModel:             "string"                         # HuggingFace or Unsloth pre-quantized model ID
      dataset:               "string"                         # s3://... URI or HuggingFace dataset (org/name)

      # === TRAINING CONFIG ===
      method:                "string  | default=qlora"        # qlora, lora, full
      loraRank:              "integer | default=16"           # LoRA rank (8, 16, 32, 64, 128)
      loraAlpha:             "integer | default=16"           # LoRA alpha (typically = rank)
      learningRate:          "string  | default=2e-4"         # Learning rate
      epochs:                "integer | default=1"            # Training epochs (1-3 recommended)
      batchSize:             "integer | default=2"            # Per-device batch size
      gradientAccumulation:  "integer | default=4"            # Gradient accumulation steps
      maxSeqLength:          "integer | default=2048"         # Max sequence length
      datasetFormat:         "string  | default=auto"         # auto, alpaca, sharegpt, chatml

      # === RESOURCE ALLOCATION ===
      gpuCount:              "integer | default=1"            # GPUs per training pod
      workerMemory:          "string  | default=32Gi"         # Memory allocation
      workerCpu:             "string  | default=8"            # CPU allocation
      minVramPerGpuGiB:      "integer | default=24"           # Min VRAM (24=L4, 48=L40S, 80=A100)
      timeoutHours:          "integer | default=6"            # Job timeout in hours
      shared:                "boolean | default=false"        # Use time-sliced GPU pool

      # === OUTPUT ===
      outputFormat:          "string  | default=safetensors"  # safetensors, gguf-q4_k_m, gguf-q8_0, lora-only
      outputBucket:          "string  | default="             # S3 bucket override (defaults to model-cache)
      pushToHub:             "string  | default="             # HuggingFace repo to push (optional)
      autoDeploy:            "boolean | default=false"        # Auto-create InferenceEndpoint

      # === OPTIONAL ===
      wandbProject:          "string  | default="             # W&B project for training metrics
      customScript:          "string  | default="             # Override training script (advanced)
      unslothImage:          "string  | default=unsloth/unsloth:latest"

    status:
      phase:      "..."   # Pending | Validating | Training | Exporting | Deploying | Completed | Failed
      progress:   "..."   # Human-readable progress (epoch, step, loss)
      outputPath: "..."   # S3 URI of exported model
      duration:   "..."   # Total wall-clock time
      endpoint:   "..."   # InferenceEndpoint name (if autoDeploy=true)
```

---

## 4. Resource Graph (KRO Expansion)

```
FineTuneJob CR
  ├── [1] validation-job          Job: validate model ID + dataset access
  ├── [2] training-script-cm      ConfigMap: generated Python training script
  ├── [3] training-job            Job: run Unsloth training on GPU node
  ├── [4] export-job              Job: merge adapter, convert format, upload to S3
  ├── [5] cloudwatch-log-group    LogGroup: /ai-platform/fine-tuning/{name}
  └── [6] inference-endpoint      InferenceEndpoint CR (only if autoDeploy=true)
```

### Resource Dependencies (execution order)

```
validation-job ──> training-script-cm ──> training-job ──> export-job ──> inference-endpoint
                                                                    │
cloudwatch-log-group (parallel, no dependencies) ──────────────────┘
```

Note: KRO doesn't natively enforce ordering between resources. We use Job
`initContainers` that wait for predecessor Job completion (same pattern as
the InferenceEndpoint registration Job waiting for LiteLLM).

---

## 5. Implementation Phases

### Phase 1: Foundation (MVP — Single File Training)

**Goal:** A committed YAML triggers training and produces a model artifact in S3.

#### 5.1.1 Create KRO ResourceGraphDefinition

**File:** `platform/config/kro/fine-tuning-job.yaml`

Resources to define:
- **validation-job** — `batch/v1 Job` with `curlimages/curl` image
  - Validates baseModel exists on HuggingFace (HTTP HEAD on model API)
  - Validates dataset is accessible (S3 HeadObject or HF API check)
  - Validates gpuCount is 1 (V1 constraint — single-node only)
  - Validates method is one of: qlora, lora, full
  - Validates loraRank is one of: 8, 16, 32, 64, 128
  - Environment: MODEL_ID, DATASET_URI, METHOD from schema spec

- **training-script-cm** — `v1 ConfigMap`
  - Contains the generated `train.py` script
  - Script uses Unsloth `FastLanguageModel` API
  - Parameterized via environment variables (not hardcoded in script)
  - Handles: model loading, LoRA config, dataset loading, SFTTrainer setup, training, save

- **training-job** — `batch/v1 Job`
  - Image: `unsloth/unsloth:latest` (configurable via schema)
  - GPU resources: `nvidia.com/gpu: {gpuCount}`
  - nodeSelector: `workload-type: gpu-inference` (or gpu-shared if shared=true)
  - tolerations: `nvidia.com/gpu: NoSchedule`
  - affinity: minVramPerGpuGiB constraint (same pattern as InferenceEndpoint)
  - activeDeadlineSeconds: `timeoutHours * 3600`
  - Volumes:
    - `training-script` (ConfigMap mount at /workspace/train.py)
    - `hf-cache` (emptyDir, 100Gi limit — model weights)
    - `dataset` (emptyDir, 50Gi limit — training data)
    - `output` (emptyDir, 100Gi limit — trained model)
  - initContainers:
    - `wait-validation`: waits for validation-job completion (kubectl wait)
    - `download-dataset`: s5cmd sync from S3 (or HF datasets download)
  - Main container:
    - command: `python /workspace/train.py`
    - env: all hyperparameters from schema spec
    - env: HF_TOKEN from secret (for gated models)
    - env: WANDB_API_KEY from secret (optional)
    - env: OUTPUT_DIR=/workspace/output
  - ServiceAccount: `fine-tuning-worker` (IRSA for S3 access)

- **export-job** — `batch/v1 Job`
  - Image: `unsloth/unsloth:latest`
  - No GPU needed (export runs on CPU for safetensors; needs GPU only for GGUF)
  - For MVP: runs on same GPU node (shares the `output` volume via a shared PVC or waits and re-downloads)
  - Alternative MVP approach: merge + upload happens at end of training-job script (simpler, one fewer Job)
  - Uploads to: `s3://{model-cache-bucket}/fine-tuned/{name}/{timestamp}/`

- **cloudwatch-log-group** — `cloudwatchlogs.services.k8s.aws/v1alpha1 LogGroup`
  - name: `/ai-platform/fine-tuning/{name}`
  - retentionDays: 30

#### 5.1.2 Create ServiceAccount + IRSA

**File:** Terraform addition in `terraform/30.eks/30.cluster/`

- ServiceAccount: `fine-tuning-worker` in `inference` namespace
- IAM Policy:
  - `s3:GetObject`, `s3:ListBucket` on dataset buckets
  - `s3:PutObject`, `s3:GetObject` on model-cache bucket (`fine-tuned/` prefix)
  - `s3:ListBucket` on model-cache bucket
- Attach via IRSA annotation on the ServiceAccount

#### 5.1.3 Create Workloads Directory

**Files:**
- `workloads/fine-tuning/TEMPLATE.yaml.example` — documented template
- `workloads/fine-tuning/.gitkeep` — ensure directory exists for ArgoCD

#### 5.1.4 Update ArgoCD ApplicationSet

**File:** `argocd/bootstrap/workloads.yaml`

Add element to the list generator:
```yaml
- name: fine-tuning
  path: workloads/fine-tuning
```

#### 5.1.5 Training Script Template

The training script (`train.py`) mounted as a ConfigMap:

```python
import os
from unsloth import FastLanguageModel
from trl import SFTTrainer
from transformers import TrainingArguments
from datasets import load_dataset

# All configuration via environment variables
MODEL_ID = os.environ["MODEL_ID"]
DATASET_PATH = os.environ["DATASET_PATH"]
METHOD = os.environ.get("METHOD", "qlora")
LORA_RANK = int(os.environ.get("LORA_RANK", "16"))
LORA_ALPHA = int(os.environ.get("LORA_ALPHA", "16"))
LEARNING_RATE = float(os.environ.get("LEARNING_RATE", "2e-4"))
EPOCHS = int(os.environ.get("EPOCHS", "1"))
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "2"))
GRAD_ACCUM = int(os.environ.get("GRAD_ACCUM", "4"))
MAX_SEQ_LEN = int(os.environ.get("MAX_SEQ_LEN", "2048"))
DATASET_FORMAT = os.environ.get("DATASET_FORMAT", "auto")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/workspace/output")

# Load model
load_in_4bit = (METHOD == "qlora")
model, tokenizer = FastLanguageModel.from_pretrained(
    model_name=MODEL_ID,
    max_seq_length=MAX_SEQ_LEN,
    dtype=None,
    load_in_4bit=load_in_4bit,
)

# Configure LoRA (skip for full fine-tuning)
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

# Load dataset
if DATASET_PATH.startswith("/"):
    dataset = load_dataset("json", data_files=DATASET_PATH, split="train")
else:
    dataset = load_dataset(DATASET_PATH, split="train")

# Auto-detect format and apply chat template
# (format detection logic based on column names)

# Train
trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
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
        save_strategy="epoch",
        bf16=True,
    ),
    max_seq_length=MAX_SEQ_LEN,
)

trainer.train()

# Save merged model
model.save_pretrained_merged(
    f"{OUTPUT_DIR}/merged",
    tokenizer,
    save_method="merged_16bit",
)
print(f"Training complete. Model saved to {OUTPUT_DIR}/merged")
```

#### 5.1.6 Dataset Format Detection

The training script auto-detects dataset format based on column structure:

| Detected Columns | Format | Chat Template Applied |
|-----------------|--------|----------------------|
| `instruction`, `input`, `output` | Alpaca | `### Instruction:\n{instruction}\n### Input:\n{input}\n### Response:\n{output}` |
| `conversations` (list of dicts) | ShareGPT | Model's native chat template via tokenizer |
| `messages` (list of role/content) | ChatML/OpenAI | Model's native chat template via tokenizer |
| `text` (single column) | Raw text | No template (continued pretraining) |
| `prompt`, `completion` | Completion | `{prompt}{completion}` |

Override with `datasetFormat` field if auto-detection is wrong.

---

### Phase 2: Export & Upload

**Goal:** Trained model is exported to the right format and uploaded to S3.

#### 5.2.1 Export Logic (end of training script)

After training completes, the script handles export based on `OUTPUT_FORMAT`:

| Format | Action |
|--------|--------|
| `safetensors` | `model.save_pretrained_merged(...)` — 16-bit merged weights |
| `gguf-q4_k_m` | `model.save_pretrained_gguf(..., quantization_method="q4_k_m")` |
| `gguf-q8_0` | `model.save_pretrained_gguf(..., quantization_method="q8_0")` |
| `lora-only` | `model.save_pretrained(...)` — adapter weights only (~100MB) |

#### 5.2.2 S3 Upload (end of training script or separate step)

```bash
# Upload to: s3://{bucket}/fine-tuned/{job-name}/{timestamp}/
s5cmd sync /workspace/output/merged/ s3://$CACHE_BUCKET/fine-tuned/$JOB_NAME/$TIMESTAMP/
```

The upload runs as a post-training step in the same Job (avoids needing a shared volume between Jobs).

#### 5.2.3 HuggingFace Hub Push (optional)

If `pushToHub` is set:
```python
model.push_to_hub_merged(push_to_hub_id, tokenizer, save_method="merged_16bit")
```

Requires HF_TOKEN with write access.

---

### Phase 3: Auto-Deploy Integration

**Goal:** When `autoDeploy: true`, automatically create an InferenceEndpoint serving the fine-tuned model.

#### 5.3.1 Approach: Post-Training Job Creates InferenceEndpoint CR

After successful export, a final step in the training Job (or a separate completion Job) creates an InferenceEndpoint YAML:

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: {fine-tune-job-name}
  namespace: inference
spec:
  model: "s3://{bucket}/fine-tuned/{name}/{timestamp}/"  # Local model path
  gpuCount: 1
  minReplicas: 1
  maxReplicas: 1
```

**Challenge:** vLLM can load from a local path or S3, but the InferenceEndpoint KRO currently expects a HuggingFace model ID.

**Solution options (choose one):**
1. **Extend InferenceEndpoint schema** — add `modelPath` field that accepts S3 URI. vLLM supports `--model s3://...` natively.
2. **Push to HuggingFace Hub** — use the HF model ID in the InferenceEndpoint (requires Hub push).
3. **Mount from S3 via initContainer** — download merged model to the same HF cache path.

**Recommended: Option 1** — Add an optional `modelPath` field to InferenceEndpoint that overrides `model` for the vLLM `--model` argument. This is the simplest and keeps everything in S3 without external dependencies.

#### 5.3.2 InferenceEndpoint Schema Extension

Add to `inference-endpoint.yaml` schema:
```yaml
modelPath: "string | default="   # S3 URI for fine-tuned model (overrides HF download)
```

In the RayService serveConfigV2, use:
```
model_id: ${schema.spec.modelPath != '' ? schema.spec.modelPath : schema.spec.model}
```

The initContainer `hf-cache-download` already handles S3 sync — we just need to point it at the fine-tuned model path.

---

### Phase 4: Observability & Status

**Goal:** Teams can track training progress and troubleshoot failures.

#### 5.4.1 Training Metrics (via stdout logging)

The training script prints structured logs:
```
{"step": 50, "epoch": 0.5, "loss": 1.23, "learning_rate": 0.0002, "grad_norm": 0.45}
```

These are captured by FluentBit/CloudWatch and visible in the CloudWatch log group.

#### 5.4.2 Status Reporting

KRO status fields derived from Job conditions:
```yaml
status:
  phase: "${validation.status.succeeded > 0 ? (training.status.succeeded > 0 ? 'Completed' : (training.status.active > 0 ? 'Training' : (training.status.failed > 0 ? 'Failed' : 'Validating'))) : 'Pending'}"
  progress: "Check logs: kubectl logs job/${name}-train -n inference -f"
  outputPath: "s3://${bucket}/fine-tuned/${name}/"
```

#### 5.4.3 Optional Weights & Biases Integration

If `wandbProject` is set:
- WANDB_API_KEY injected from a Secret (`wandb-api-key` in inference namespace)
- WANDB_PROJECT set from schema field
- Training script auto-reports to W&B (HuggingFace Trainer integration)

---

### Phase 5: Safety & Guardrails

#### 5.5.1 Resource Limits

| Guard | Implementation |
|-------|----------------|
| Job timeout | `activeDeadlineSeconds: timeoutHours * 3600` |
| Disk limit | `emptyDir.sizeLimit: 100Gi` on all volumes |
| GPU limit | Karpenter NodePool `limits.nvidia.com/gpu: 16` (shared with inference) |
| Memory limit | `resources.limits.memory` on training pod |
| Retry limit | `backoffLimit: 3` on training Job |

#### 5.5.2 Team Quota Enforcement

- Training Jobs run in the `inference` namespace (shared)
- Future: add `gpuHours` budget to AITeam spec, enforce via admission webhook
- V1: rely on Karpenter GPU limit (16 total GPUs) as a natural cap

#### 5.5.3 Dataset Validation

The validation Job checks:
- Dataset is accessible (S3 HeadObject succeeds, or HF dataset exists)
- Dataset is not empty (at least 10 rows)
- Dataset is not too large for the configured resources (warn if > 1M rows with epochs > 1)
- Dataset format is recognized (has expected columns)

---

## 6. Infrastructure Requirements

### New Terraform Resources

| Resource | Purpose | Module |
|----------|---------|--------|
| IAM Role for fine-tuning SA | S3 read/write for datasets and model output | `30.eks/30.cluster` |
| S3 bucket policy update | Allow fine-tuning SA to write to `fine-tuned/` prefix | `30.eks/30.cluster` |
| ServiceAccount `fine-tuning-worker` | IRSA-annotated SA in inference namespace | `30.eks/30.cluster` |

### Karpenter Changes

**None required.** Fine-tuning uses the same `gpu-inference` NodePool:
- QLoRA 8B: 6 GB VRAM -> L4 (24 GB) or A10G (24 GB)
- QLoRA 32B: 22 GB VRAM -> L4 (24 GB) borderline, A10G (24 GB) borderline, L40S (48 GB) comfortable
- QLoRA 70B: 41 GB VRAM -> L40S (48 GB) or A100 (80 GB)

The `minVramPerGpuGiB` field (defaulting to 24) ensures Karpenter picks an appropriate instance.

### Cold-Start Optimization

Add `unsloth/unsloth:latest` to the EBS data volume snapshot and SOCI index:
- Image is ~15 GB (similar to ray-llm)
- Add to `image-optimization.tf` triggers
- First fine-tuning job will be slow (image pull); subsequent jobs instant

---

## 7. File Changes Summary

| File | Change |
|------|--------|
| `platform/config/kro/fine-tuning-job.yaml` | **NEW** — ResourceGraphDefinition |
| `workloads/fine-tuning/TEMPLATE.yaml.example` | **NEW** — Self-service template |
| `workloads/fine-tuning/.gitkeep` | **NEW** — Directory placeholder |
| `argocd/bootstrap/workloads.yaml` | **EDIT** — Add `fine-tuning` to ApplicationSet |
| `terraform/30.eks/30.cluster/main.tf` or dedicated file | **EDIT** — Add fine-tuning SA + IRSA |
| `terraform/30.eks/30.cluster/locals.tf` | **EDIT** — Add `unsloth_image` local |
| `platform/config/kro/inference-endpoint.yaml` | **EDIT** — Add optional `modelPath` field (Phase 3) |
| `terraform/30.eks/30.cluster/image-optimization.tf` | **EDIT** — Add unsloth image to SOCI/snapshot |
| `CLAUDE.md` | **EDIT** — Document fine-tuning section |

---

## 8. Implementation Order & Effort Estimates

| Step | Phase | Description | Effort | Dependencies |
|------|-------|-------------|--------|--------------|
| 1 | P1 | Create `workloads/fine-tuning/` directory + template | 15 min | None |
| 2 | P1 | Add `fine-tuning` to ArgoCD ApplicationSet | 5 min | None |
| 3 | P1 | Create `fine-tuning-worker` ServiceAccount + IRSA | 30 min | Terraform |
| 4 | P1 | Write KRO ResourceGraphDefinition (validation + training + export) | 3 hr | Steps 1-3 |
| 5 | P1 | Write training script template (train.py) | 2 hr | Step 4 |
| 6 | P1 | End-to-end test: train SmolLM3 3B on tiny dataset | 1 hr | Steps 1-5 |
| 7 | P2 | Add GGUF export support to training script | 1 hr | Step 6 |
| 8 | P2 | Add HuggingFace Hub push support | 30 min | Step 6 |
| 9 | P3 | Extend InferenceEndpoint with `modelPath` field | 1 hr | Step 6 |
| 10 | P3 | Implement `autoDeploy` (create InferenceEndpoint from fine-tuned model) | 2 hr | Step 9 |
| 11 | P4 | Add CloudWatch log group + structured logging | 30 min | Step 4 |
| 12 | P4 | Add W&B integration (optional) | 30 min | Step 6 |
| 13 | P5 | Add unsloth image to cold-start optimization | 1 hr | Terraform |
| 14 | P5 | Update CLAUDE.md with fine-tuning documentation | 30 min | All |

**Total estimated effort: ~14 hours** (spread across 5 phases)

---

## 9. Testing Strategy

### Smoke Test (Phase 1 validation)

```bash
# Deploy a minimal fine-tuning job
cat <<EOF > workloads/fine-tuning/test-smollm.yaml
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
  minVramPerGpuGiB: 0  # Fits on any GPU (135M model)
  shared: true          # Use time-sliced GPU
EOF
git add && git commit && git push
# Watch: kubectl get finetunejobs -n inference -w
```

### Integration Test (Phase 3 validation)

```bash
# Deploy with autoDeploy to verify full pipeline
cat <<EOF > workloads/fine-tuning/test-auto-deploy.yaml
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: custom-smollm
  namespace: inference
spec:
  baseModel: "unsloth/SmolLM2-135M-Instruct-bnb-4bit"
  dataset: "HuggingFace:yahma/alpaca-cleaned"
  epochs: 1
  autoDeploy: true
  shared: true
EOF
# Verify: InferenceEndpoint created, model queryable via LiteLLM
```

---

## 10. VRAM Reference Table

For selecting `minVramPerGpuGiB`:

| Model Size | QLoRA VRAM | Recommended minVramPerGpuGiB | AWS Instance |
|-----------|------------|------------------------------|--------------|
| 135M-3B | 2-4 GB | 0 (any GPU) | g5.xlarge (A10G 24GB) |
| 7-8B | 5-6 GB | 16 | g5.xlarge (A10G 24GB) |
| 13B | 10-12 GB | 16 | g5.xlarge (A10G 24GB) |
| 27B | 20-22 GB | 24 | g5.xlarge (A10G 24GB) |
| 32-40B | 25-30 GB | 40 | g6e.xlarge (L40S 48GB) |
| 70B | 38-41 GB | 48 | g6e.xlarge (L40S 48GB) or p4d (A100 80GB) |

---

## 11. What's NOT in V1 (Explicit Scope Boundaries)

| Feature | Reason for Deferral | V2 Approach |
|---------|--------------------|----|
| Multi-node distributed training | QLoRA single-node covers 95% of use cases (up to 70B) | RayJob with DeepSpeed/FSDP |
| Hyperparameter search | Teams start with defaults, iterate manually | Optuna integration |
| RLHF/DPO/GRPO | SFT only in V1; RL methods are more complex | Add `method: dpo` with reward model config |
| Live training dashboard | `kubectl logs` + optional W&B is sufficient for V1 | Grafana dashboard with loss curves |
| Dataset versioning | Teams manage their own S3 versioning | DVC or S3 versioning integration |
| Adapter registry | Output goes to S3 flat path | Model registry (MLflow or HF Hub) |
| Multi-GPU training | Single GPU handles all QLoRA up to 70B | `accelerate` launch with FSDP |
| Cost tracking | V1 relies on Karpenter GPU limit as cap | Per-team GPU-hour budgets |

---

## 12. Decision Log

| Decision | Rationale | Alternatives Considered |
|----------|-----------|------------------------|
| Kubernetes Job (not RayJob) | Single-node training; simpler; maps to existing patterns | RayJob (overkill), KubeFlow PyTorchJob (heavy install) |
| Unsloth (not raw HF/PEFT) | 2x faster, 70% less VRAM, MIT license, official Docker image | Raw HuggingFace Trainer (slower, more VRAM) |
| Training + export in one Job | Avoids shared PVC complexity between Jobs | Separate export Job (needs PVC or re-download) |
| S3 as model store | Already have model-cache bucket + IRSA; no new infra | HuggingFace Hub (external dep), MLflow (new service) |
| Extend InferenceEndpoint with modelPath | Minimal change; vLLM supports S3 paths natively | New "LocalModel" KRO (over-engineering) |
| Default QLoRA with 4-bit | Covers 95% of use cases; 75% less VRAM; negligible accuracy loss | Default to 16-bit LoRA (wastes resources) |
| unsloth/unsloth Docker image | Official, pre-built, all deps included | Custom image (maintenance burden) |
| Reuse gpu-inference NodePool | Same GPU types needed; no new infra | Dedicated gpu-training NodePool (unnecessary) |

---

## 13. Rollout Plan

1. **Dev environment first** — deploy KRO, test with 135M model, verify full pipeline
2. **Platform team dogfood** — fine-tune a real 8B model on internal data, deploy, validate quality
3. **Documentation + template** — finalize TEMPLATE.yaml.example with real-world examples
4. **Enable for teams** — announce availability, provide dataset format guide
5. **Monitor** — watch GPU utilization, job success rates, training times

---

## 14. Success Criteria

- [ ] Team can fine-tune an 8B model by committing a 5-line YAML
- [ ] Training completes in < 1 hour for 1000-example dataset
- [ ] Fine-tuned model auto-deploys and responds to API calls
- [ ] GPU node reclaims within 5 minutes of training completion
- [ ] Failed jobs provide clear error messages in logs
- [ ] No manual intervention required for the happy path
