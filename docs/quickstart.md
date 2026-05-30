# Quickstart — turnkey AI platform

Stand up the platform on your own AWS account and reach the money-demo (a 3-way
model comparison in Langfuse) without hand-editing manifests. Every step works
standalone; the value compounds.

> The thin `./platformctl` wrapper just orchestrates the existing `make` +
> `ops/` flow — you can run the underlying commands directly if you prefer.

## Prerequisites

- AWS account + credentials (`aws sts get-caller-identity` works)
- `terraform`, `kubectl`, `aws`, `make`, `python3`, `jq`
- **AWS Identity Center** enabled (ArgoCD capability requires it)
- **Bedrock model access** enabled in-account for Claude Sonnet
  (Console → Bedrock → Model access → enable the Sonnet model). The platform
  preflights this and tells you the exact fix if it's missing.
- (Optional, for faster image pulls) a Docker Hub token for the ECR pull-through
  cache — see the main [README](../README.md).

## 1. Configure

```bash
cd terraform/00.global/vars
cp example.tfvars dev.tfvars
# Edit dev.tfvars:
#   - shared_config.resources_prefix         (cluster name prefix)
#   - capabilities_config.argocd_idc_*        (your Identity Center ARN + user/group IDs)
#   - gitops_repo_url                         (this repo, or your fork)
#   - (optional) langfuse_nextauth_url        (ALB/domain URL for the Langfuse UI)
```

The turnkey defaults (no edits needed) are:

| tfvar | default | what it gives you |
|-------|---------|-------------------|
| `enable_bedrock` | `true` | Claude Opus 4.8 via LiteLLM, zero GPUs |
| `enable_fine_tuning` | `true` | `FineTuneJob` + Unsloth trainer image |
| `langfuse_nextauth_url` | `http://localhost:3000` | works with the SSM tunnel |
| `langfuse_init_user_email` | `admin@ai-platform.local` | Langfuse admin login |

## 2. Provision

```bash
export AWS_REGION=us-east-1
./platformctl up dev
# ≈ make -C terraform bootstrap ENVIRONMENT=dev && make -C terraform apply-all ENVIRONMENT=dev
```

This creates the VPC, IAM, EKS cluster + managed capabilities (ArgoCD/KRO/ACK),
Karpenter NodePools, all platform secrets, **the Langfuse project keys**, and
**the Bedrock IRSA role**. ArgoCD then syncs the platform services and the
default model catalog from git.

> `enable_fine_tuning=true` builds the Unsloth trainer image during apply, which
> needs Docker on the machine running Terraform. No Docker? Set
> `enable_fine_tuning=false` (everything else still works), or build it later
> with `ops/build-unsloth-image.sh` in CI and re-apply.

## 3. Use it immediately (zero GPUs)

```bash
./platformctl status dev      # watch ArgoCD + pods + models come up
./platformctl tunnel          # forward WebUI :8080, LiteLLM :4000, Langfuse :3000
./platformctl preflight       # confirm Bedrock + models answer; clear fix if not
```

Chat against **Claude Opus 4.8** in Open WebUI (http://localhost:8080) or:

```bash
LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d)
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-8","messages":[{"role":"user","content":"Hello!"}]}'
```

**Langfuse tracing is already live** — open http://localhost:3000 (sign in with
the `langfuse_init_user_email` and `terraform output -raw langfuse_admin_password`)
and you'll see the trace, with cost and latency, from your first call. No key
setup, no restart dance.

The default small model `qwen3-3b` (Qwen2.5-3B-Instruct) comes up on its own GPU
node once Karpenter provisions one.

## 4. Bring your data + fine-tune

```bash
# Upload your support transcripts (chat/messages JSONL) to the datasets bucket.
DATASETS_BUCKET=$(kubectl get cm platform-config -n inference -o jsonpath='{.data.trainingDatasetsBucket}')
aws s3 cp support-transcripts.jsonl s3://$DATASETS_BUCKET/

# Commit a FineTuneJob (base = Qwen2.5-3B, autoDeploy=true).
cp workloads/fine-tuning/TEMPLATE.yaml.example workloads/fine-tuning/qwen3-support-tuned.yaml
# edit: name=qwen3-support-tuned, dataset=s3://$DATASETS_BUCKET/support-transcripts.jsonl, autoDeploy: true
git add workloads/fine-tuning/qwen3-support-tuned.yaml && git commit -m "feat: support-voice fine-tune" && git push

kubectl get finetunejobs -n inference -w     # ~30 min later, qwen3-support-tuned is live
```

See [fine-tuning-getting-started.md](./fine-tuning-getting-started.md) for the full guide.

## 5. Prove it — the money demo

Run the same held-out questions through all three contenders and log a Langfuse
dataset run:

```bash
./platformctl compare \
  --dataset ops/sample-data/support-eval.jsonl \
  --models claude-opus-4-8,qwen3-3b,qwen3-support-tuned \
  --langfuse-dataset support-voice-eval \
  --self-hosted-model qwen3-support-tuned \
  --self-hosted-hf-id Qwen/Qwen2.5-3B-Instruct
```

Then in Langfuse: open the `support-voice-eval` dataset → compare the runs
side-by-side. Cost and latency are on every trace. Configure an **LLM-as-judge**
evaluator (judge = `claude-opus-4-8`) on a voice/policy/helpfulness rubric, and
use the side-by-side view for human preference. The script also prints the
**cost crossover** — the daily request volume above which the self-hosted tuned
model is cheaper per request than Sonnet.

## 6. Decide

Read the cost/quality crossover and route production traffic accordingly — the
cheap tuned model for the common case, Sonnet for the hard tail. It's all one
OpenAI-compatible API, governed by the same `AITeam` budgets and keys.

## Tear down

```bash
./platformctl down dev        # make -C terraform destroy-all ENVIRONMENT=dev (asks to confirm)
```
