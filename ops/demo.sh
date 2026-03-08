#!/bin/bash
# =============================================================================
# AI Platform on EKS — Demo Script
# =============================================================================
# Prerequisites:
#   - Cluster running with at least gemma-4b deployed
#   - Port-forwards active:
#       kubectl port-forward svc/litellm 4000:4000 -n ai-platform &
#       kubectl port-forward svc/open-webui 8080:8080 -n ai-platform &
#       kubectl port-forward svc/langfuse-web 3000:3000 -n ai-platform &
#   - ArgoCD UI open: see terraform output for URL
#   - Browser tabs ready: Open WebUI (localhost:8080), Langfuse (localhost:3000)
#
# Usage: Run commands manually, section by section. This is a reference, not
# meant to be executed as a script.
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# ACT 1: Platform Overview
# ─────────────────────────────────────────────────────────────────────────────

# Show running models
kubectl get inferenceendpoints -n inference

# Show GPU nodes (Bottlerocket + SOCI)
kubectl get nodes -l workload-type=gpu-inference \
  -o custom-columns=NAME:.metadata.name,OS:.status.nodeInfo.osImage,GPU:.status.allocatable.nvidia\\.com/gpu

# Show ArgoCD apps
kubectl get applications -n argocd

# Show what a model definition looks like
cat workloads/models/gemma-4b.yaml

# → Switch to Open WebUI (localhost:8080) and chat with gemma-4b

# ─────────────────────────────────────────────────────────────────────────────
# ACT 2: Deploy a New Model (Live)
# ─────────────────────────────────────────────────────────────────────────────

# Create a new model — this is ALL a team needs to write
cat > workloads/models/qwen3-4b.yaml << 'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: qwen3-4b
  namespace: inference
spec:
  model: "Qwen/Qwen3-4B"
  gpuCount: 1
  minReplicas: 1
  maxReplicas: 2
EOF

# Commit and push — ArgoCD takes it from here
git add workloads/models/qwen3-4b.yaml
git commit -m "feat: Deploy Qwen3 4B"
git push origin main

# → Switch to ArgoCD UI — watch the models app sync

# Watch pods appear
kubectl get pods -n inference -w

# Track image pull time (expect ~90s with SOCI)
kubectl get events -n inference --sort-by='.lastTimestamp' | grep -E "Pull|Scheduled"

# Once running, verify in LiteLLM
LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d)

curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_KEY" | jq '.data[].id'

# Test the new model via API
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -d '{"model": "qwen3-4b", "messages": [{"role": "user", "content": "What is EKS?"}]}' \
  | jq '.choices[0].message.content'

# → Switch to Open WebUI — new model appears automatically, chat with it

# ─────────────────────────────────────────────────────────────────────────────
# ACT 3: Team Onboarding
# ─────────────────────────────────────────────────────────────────────────────

# Show what a team definition looks like
cat workloads/teams/search-ranking.yaml

# Show what KRO created for each team
echo "=== Team Namespaces ==="
kubectl get ns | grep team

echo "=== Resource Quotas ==="
kubectl get resourcequota -n team-search-ranking

echo "=== Network Policies ==="
kubectl get networkpolicy -n team-search-ranking

echo "=== RBAC ==="
kubectl get rolebinding -n team-search-ranking

echo "=== Scoped API Key ==="
kubectl get secret search-ranking-api-key -n team-search-ranking

# Get the team's scoped API key
TEAM_KEY=$(kubectl get secret search-ranking-api-key -n team-search-ranking \
  -o jsonpath='{.data.api-key}' | base64 -d)

# Test with team credentials — works for allowed models
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TEAM_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "Hello from the search team!"}]}' \
  | jq '.choices[0].message.content'

# Show team info in LiteLLM (budget, rate limits)
curl -s http://localhost:4000/team/list \
  -H "Authorization: Bearer $LITELLM_KEY" \
  | jq '.[] | select(.team_alias=="search-ranking") | {team_alias, max_budget, tpm_limit, rpm_limit, models}'

# ─────────────────────────────────────────────────────────────────────────────
# ACT 4: Observability
# ─────────────────────────────────────────────────────────────────────────────

# → Switch to Langfuse UI (localhost:3000)
# Show: traces from API calls, latency, token counts, cost tracking
# Filter by team metadata

# ─────────────────────────────────────────────────────────────────────────────
# ACT 5: Cost Management
# ─────────────────────────────────────────────────────────────────────────────

# Show current GPU cost
echo "GPU nodes running (each ~\$1/hr):"
kubectl get nodes -l workload-type=gpu-inference --no-headers | wc -l

# Scale down (don't run during demo unless you want to show it)
# ./ops/scale-down.sh

# Scale back up
# ./ops/scale-up.sh

# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP (after demo)
# ─────────────────────────────────────────────────────────────────────────────

# Remove the model we deployed during the demo
# git rm workloads/models/qwen3-4b.yaml
# git commit -m "chore: Remove demo model"
# git push origin main
