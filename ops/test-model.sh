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

# Check model status
STATUS=$(kubectl get inferenceendpoint "$MODEL" -n inference -o jsonpath='{.status.message}' 2>/dev/null) || {
  echo "ERROR: InferenceEndpoint '$MODEL' not found in namespace 'inference'."
  echo "Available models:"
  kubectl get inferenceendpoints -n inference -o custom-columns='NAME:.metadata.name,STATUS:.status.modelStatus,MESSAGE:.status.message' 2>/dev/null || echo "  (none)"
  exit 1
}

READY=$(kubectl get inferenceendpoint "$MODEL" -n inference -o jsonpath='{.status.ready}' 2>/dev/null)
if [ "$READY" != "True" ]; then
  echo "⏳ Model '$MODEL' is not ready yet: $STATUS"
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
