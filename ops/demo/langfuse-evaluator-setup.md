# Langfuse LLM-as-Judge Evaluator Setup

Configure Claude Opus 4.8 (via LiteLLM) as a judge that scores each model's
answer to support questions on correctness, completeness, and clarity.

## 1. Connect LiteLLM as the LLM provider

Langfuse UI → **Settings → LLM Connections → + Add Connection**:
- Provider: `openai` (OpenAI-compatible)
- Base URL: `http://litellm.ai-platform.svc.cluster.local:4000/v1` (or `http://localhost:4000/v1` via port-forward)
- API Key: LiteLLM master key (`kubectl get secret litellm-secrets -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d`)
- Model: `claude-opus-4-8`

## 2. Create the evaluator

Langfuse UI → **Evaluators → + New Evaluator → Custom**:
- Name: `support-answer-quality`
- Model: the LiteLLM connection + `claude-opus-4-8`
- Prompt:

```
You are judging a customer-support AI assistant's answer.

Score 1-5 on each dimension:
- correctness: Is the information accurate and free of contradictions?
- completeness: Does it fully address the question with actionable steps?
- clarity: Is it well-structured and easy to follow?

## Question
{{input}}

## Answer
{{output}}

Respond with ONLY this JSON:
{"correctness": <1-5>, "completeness": <1-5>, "clarity": <1-5>, "overall": <1-5>, "reasoning": "<one sentence>"}
```

- Variable mapping: `{{input}}` → dataset item input, `{{output}}` → trace output
- Output: JSON, use `overall` as the primary score

## 3. Apply to the dataset runs

After running `./platformctl compare` (step 4 in the run guide):
- **Datasets → support-eval → Runs** → select both run rows (claude-opus-4-8, qwen3-4b-instruct-2507)
- **Evaluate → support-answer-quality** → run
- Langfuse calls Opus 4.8 to grade every answer

## 4. Read the result

**Datasets → support-eval → Compare Runs**: side-by-side answers per question, judge scores per answer, aggregate score per model. This is the demo's Act 3 screen.
