# Fine-tuning — getting started

Self-service fine-tuning follows the same `git push → ArgoCD → KRO → workload`
loop as model deployment. You commit a short `FineTuneJob` YAML; the platform
runs single-node QLoRA via Unsloth, uploads the result to S3, and (optionally)
auto-deploys it as a queryable LiteLLM endpoint.

> Design and rationale: [fine-tuning-implementation-plan-v2.md](./fine-tuning-implementation-plan-v2.md).

## Prerequisites

- `enable_fine_tuning = true` in your tfvars (the default). Terraform then:
  - builds + pushes the Unsloth trainer image to ECR
    (`platform/services/unsloth-trainer/Dockerfile`, via `ops/build-unsloth-image.sh`),
  - creates the `<cluster>-training-datasets` S3 bucket,
  - provisions the `fine-tuning-worker` ServiceAccount + IRSA + RBAC.
- The `fine-tuning` ApplicationSet target is synced by ArgoCD (it watches
  `workloads/fine-tuning/`).

Verify:

```bash
kubectl get rgd fine-tuning-job                      # Status: Active
kubectl get sa fine-tuning-worker -n inference -o yaml | grep role-arn
```

## 1. Upload your dataset

Supported formats (auto-detected, override with `datasetFormat`): Alpaca
(`instruction`/`output`), ShareGPT (`conversations`), ChatML (`messages`),
prompt/completion, or raw `text`.

```bash
DATASETS_BUCKET=$(kubectl get cm platform-config -n inference -o jsonpath='{.data.trainingDatasetsBucket}')
aws s3 cp support-transcripts.jsonl s3://$DATASETS_BUCKET/
```

You can also train directly from a HuggingFace dataset with
`dataset: "HuggingFace:org/dataset"`.

## 2. Commit a FineTuneJob

```bash
cp workloads/fine-tuning/TEMPLATE.yaml.example workloads/fine-tuning/support-bot-v1.yaml
```

Minimal example (the showcase support-voice tune):

```yaml
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: qwen3-support-tuned        # becomes the LiteLLM alias when autoDeploy=true
  namespace: inference
spec:
  baseModel: "unsloth/Qwen2.5-3B-Instruct-bnb-4bit"   # default; ungated, Apache-2.0
  dataset: "s3://<cluster>-training-datasets/support-transcripts.jsonl"
  autoDeploy: true
```

```bash
git add workloads/fine-tuning/support-bot-v1.yaml
git commit -m "feat: support-voice fine-tune"
git push
```

## 3. Watch it train

```bash
kubectl get finetunejobs -n inference -w
kubectl logs -n inference job/qwen3-support-tuned-train -f
```

Flow: a cheap **validation Job** runs first (checks the base model + dataset; no
GPU provisioned if it fails). On success the **training Job** runs on a GPU node
(Karpenter provisions it), trains, exports, uploads to
`s3://<model-cache>/fine-tuned/<name>/<timestamp>/`, and — if `autoDeploy: true`
— applies an `InferenceEndpoint` that loads those exact weights.

```bash
aws s3 ls s3://$(kubectl get cm platform-config -n inference -o jsonpath='{.data.modelCacheBucket}')/fine-tuned/qwen3-support-tuned/
```

## 4. Query the tuned model

```bash
kubectl wait --for=condition=ready --timeout=20m inferenceendpoint/qwen3-support-tuned -n inference
./ops/test-model.sh qwen3-support-tuned "How do I reset my password?"
```

It's now a normal model behind the OpenAI-compatible API — usable in Open WebUI,
governed by `AITeam` budgets/keys, and traced in Langfuse.

## Key fields

| Field | Default | Notes |
|-------|---------|-------|
| `baseModel` | `unsloth/Qwen2.5-3B-Instruct-bnb-4bit` | Unsloth pre-quant or HF model |
| `dataset` | — (required) | `s3://...` or `HuggingFace:org/dataset` |
| `method` | `qlora` | `qlora` \| `lora` \| `full` |
| `loraRank` | `16` | 8/16/32/64/128 |
| `epochs` | `1` | |
| `outputFormat` | `safetensors` | `safetensors` (vLLM-ready), `gguf-q4_k_m`, `gguf-q8_0`, `lora-only` |
| `autoDeploy` | `false` | create an `InferenceEndpoint` after training |
| `minVramPerGpuGiB` | `24` | Karpenter GPU floor; run `ops/recommend-instance.py` for bigger bases |
| `shared` | `false` | `true` = time-sliced GPU (small models only) |
| `wandbProject` | — | requires a `wandb-api-key` Secret in `inference` |

## Troubleshooting

- **Validation Job fails fast** → bad `baseModel`/`dataset`/`method`. Read its logs.
- **Training Job OOM** → bump `minVramPerGpuGiB`/`workerMemory`, or lower `maxSeqLength`/`batchSize`.
- **autoDeploy endpoint stuck Pending** → `kubectl describe inferenceendpoint <name> -n inference`;
  check the `hf-cache-download` init container synced the `fine-tuned/...` prefix.
- **Gated base model** → create the `hf-token` Secret in `inference` (see main README).
