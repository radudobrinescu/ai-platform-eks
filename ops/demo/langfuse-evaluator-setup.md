# Langfuse LLM-as-Judge Evaluator Setup

Configure an LLM-as-judge evaluator in the Langfuse UI that uses Claude Opus 4.8
(via LiteLLM) to score each model's responses on voice, policy, and helpfulness.

## Setup Steps (in Langfuse UI)

### 1. Connect Langfuse to LiteLLM as the LLM Provider

Go to **Settings → LLM Connections → + Add Connection**:
- **Provider**: OpenAI-compatible
- **Base URL**: `http://litellm.ai-platform.svc.cluster.local:4000/v1`
  (or `http://localhost:4000/v1` if using port-forward)
- **API Key**: your LiteLLM master key
- **Model**: `claude-opus-4-8`

### 2. Create the Evaluator

Go to **Evaluators → + New Evaluator**:
- **Name**: `support-voice-quality`
- **Model**: select the LiteLLM connection + `claude-opus-4-8`
- **Evaluation criteria** (use this prompt template):

```
You are evaluating a customer support AI assistant for NovaBay, a helpdesk SaaS product.

Score the response on three dimensions (each 1-5):

**Voice Consistency (1-5):**
- Does it use the "Nova" persona name naturally?
- Is the tone warm but concise (not overly formal or robotic)?
- Does it end with "Anything else I can help with? 🌊"?
- Does it reference NovaBay-specific concepts (plans, features, settings paths)?

**Policy Accuracy (1-5):**
- Does it provide specific, plausible plan limits and features?
- Does it give concrete navigation paths (Settings → X → Y)?
- Are its recommendations actionable and internally consistent?
- Does it avoid contradictions or impossible features?

**Helpfulness (1-5):**
- Does it directly answer the question asked?
- Does it provide next steps or proactive suggestions?
- Is it appropriately detailed without being overwhelming?
- Does it anticipate follow-up questions?

## Input
**User question:** {{input}}

**Assistant response:** {{output}}

## Scoring
Respond with ONLY a JSON object:
{"voice": <1-5>, "policy": <1-5>, "helpfulness": <1-5>, "overall": <1-5>, "reasoning": "<brief explanation>"}
```

- **Output parsing**: JSON mode
- **Score mapping**: use the `overall` field as the primary score

### 3. Apply Evaluator to the Dataset Run

After running `./platformctl compare`:
1. Go to **Datasets → support-voice-eval** (or whatever `--langfuse-dataset` you used)
2. Select the dataset runs (one per model)
3. Click **Evaluate** → select `support-voice-quality`
4. Run it — Langfuse calls Claude Opus 4.8 via LiteLLM to judge each response

### 4. View Results

Go to **Datasets → support-voice-eval → Compare Runs**:
- Side-by-side responses from all models for each eval item
- LLM-judge scores per response
- Filter/sort by score
- Aggregate scores per model (avg voice, policy, helpfulness)

## Running the Full Demo Flow

```bash
# 1. Upload training data
BUCKET=$(kubectl get cm platform-config -n inference -o jsonpath='{.data.trainingDatasetsBucket}')
aws s3 cp ops/demo/training-data/support-transcripts.jsonl s3://$BUCKET/

# 2. Deploy the fine-tune job (already committed)
git add workloads/fine-tuning/novabay-support-tuned.yaml
git commit -m "feat: NovaBay support voice fine-tune"
git push

# 3. Wait for training + auto-deploy
kubectl get finetunejobs -n inference -w

# 4. Run the comparison (uses eval dataset)
./platformctl compare \
    --dataset ops/demo/training-data/support-eval.jsonl \
    --models claude-opus-4-8,qwen3-4b-instruct-2507,novabay-support-tuned \
    --langfuse-dataset novabay-support-eval \
    --self-hosted-model novabay-support-tuned \
    --self-hosted-hf-id Qwen/Qwen2.5-3B-Instruct

# 5. In Langfuse UI: apply the evaluator to the dataset runs
# 6. Show the side-by-side comparison with scores
```

## What the Demo Proves

| Model | Expected Voice Score | Expected Cost/Request |
|-------|---------------------|----------------------|
| Claude Opus 4.8 | 3-4 (good answers, wrong voice) | ~$0.003 |
| Qwen3 4B base | 1-2 (generic, no NovaBay knowledge) | ~$0.00006 |
| NovaBay fine-tuned | 4-5 (correct voice + policy) | ~$0.00006 |

The fine-tuned model matches or beats Claude on the **narrow task** (NovaBay support)
while costing 50x less per request. Claude gives helpful generic answers but doesn't
know NovaBay's specific products, plans, navigation paths, or tone. The base model
knows neither. The fine-tuned model knows both.
