#!/usr/bin/env bash
# Quick one-shot model test via LiteLLM API.
# Usage: ./ops/test-model.sh <model-name> [prompt]
# Example: ./ops/test-model.sh gemma-4b "What is Kubernetes?"
set -euo pipefail

MODEL="${1:?Usage: $0 <model-name> [prompt]}"
PROMPT="${2:-Hello! Tell me about yourself in one sentence.}"
PORT=4000
PF_PID=""

cleanup() { [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null; }
trap cleanup EXIT

# Find the endpoint across serving kinds (VLLMEndpoint / LLMDEndpoint /
# LLMDDisaggEndpoint) and check readiness. Bedrock models have no CR — skip the check.
READY=""; KIND=""
for k in vllmendpoint llmdendpoint llmddisaggendpoint; do
  if r=$(kubectl get "$k" "$MODEL" -n inference -o jsonpath='{.status.ready}' 2>/dev/null); then
    READY="$r"; KIND="$k"; break
  fi
done
if [ -z "$KIND" ]; then
  echo "ERROR: no VLLMEndpoint/LLMDEndpoint/LLMDDisaggEndpoint named '$MODEL' in namespace 'inference'."
  echo "Available serving endpoints:"
  kubectl get vllmendpoints,llmdendpoints,llmddisaggendpoints -n inference 2>/dev/null || echo "  (none)"
  echo "(If '$MODEL' is a Bedrock model it has no CR — it should still answer via LiteLLM.)"
  exit 1
fi
if [ "$READY" != "True" ]; then
  echo "⏳ Model '$MODEL' ($KIND) is not ready yet."
  exit 1
fi

# Get API key
LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d)

# Start port-forward in background
kubectl port-forward svc/litellm $PORT:4000 -n ai-platform &>/dev/null &
PF_PID=$!
sleep 2

# Send request
echo "→ $MODEL: $PROMPT"
echo ""
curl -s http://localhost:$PORT/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -d "$(jq -n --arg m "$MODEL" --arg p "$PROMPT" \
    '{model:$m, messages:[{role:"user",content:$p}], max_tokens:256}')" \
  | jq -r '.choices[0].message.content // .error.message // "No response"'
