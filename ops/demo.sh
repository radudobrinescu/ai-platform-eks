#!/bin/bash
# =============================================================================
# AI Platform on EKS — Demo Script
# =============================================================================
# Run commands manually, section by section. This is a reference script.
#
# Prerequisites:
#   - Cluster running with gemma-4b and smollm3-3b deployed
#   - Port-forwards active:
#       kubectl port-forward svc/litellm 4000:4000 -n ai-platform &
#       kubectl port-forward svc/open-webui 8080:8080 -n ai-platform &
#       kubectl port-forward svc/langfuse-web 3000:3000 -n ai-platform &
#   - Browser tabs: Open WebUI (localhost:8080), Langfuse (localhost:3000),
#     ArgoCD UI (see terraform output for URL)
# =============================================================================

# ─────────────────────────────────────────────────────────────────────────────
# ACT 1: Platform Overview (5 min)
# ─────────────────────────────────────────────────────────────────────────────

# "Everything is GitOps-managed — the entire platform is defined in git"
# → Show ArgoCD UI: platform-config, models, teams, services

# Show running models
kubectl get inferenceendpoints -n inference \
  -o custom-columns=NAME:.metadata.name,READY:.status.ready,MODEL_STATUS:.status.modelStatus,ENDPOINT:.status.endpoint

# Show GPU nodes (Bottlerocket + SOCI)
kubectl get nodes -l workload-type=gpu-inference \
  -o custom-columns=NAME:.metadata.name,OS:.status.nodeInfo.osImage,GPU:.status.allocatable.nvidia\\.com/gpu

# Show what a model definition looks like — "this is ALL a team writes"
cat workloads/models/gemma-4b.yaml

# → Open WebUI: chat with gemma-4b to show it works

# ─────────────────────────────────────────────────────────────────────────────
# ACT 2: Deploy a New Model — Live (5 min)
# ─────────────────────────────────────────────────────────────────────────────

# Create a new model file
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

# → ArgoCD UI: watch the models app sync in real-time
# → Terminal: watch pods appear
kubectl get pods -n inference -w

# "Karpenter provisions a GPU node with Bottlerocket + SOCI"
# "The 12GB Ray image pulls in ~90 seconds thanks to SOCI parallel loading"

# Once running:
# → Open WebUI: refresh models — qwen3-4b appears automatically
# → Chat with it live

# ─────────────────────────────────────────────────────────────────────────────
# ACT 3: Team Isolation (5 min)
# ─────────────────────────────────────────────────────────────────────────────

# Show what a team definition looks like
cat workloads/teams/search-ranking.yaml

# Show what KRO created for each team
echo "=== Team Namespaces ==="
kubectl get ns | grep team

echo "=== Resource Quotas (search-ranking) ==="
kubectl get resourcequota -n team-search-ranking

echo "=== Scoped API Keys ==="
kubectl get secret search-ranking-api-key -n team-search-ranking
kubectl get secret customer-support-api-key -n team-customer-support

# Get team API keys
SEARCH_KEY=$(kubectl get secret search-ranking-api-key -n team-search-ranking \
  -o jsonpath='{.data.api-key}' | base64 -d)
SUPPORT_KEY=$(kubectl get secret customer-support-api-key -n team-customer-support \
  -o jsonpath='{.data.api-key}' | base64 -d)

# --- Search team: access to gemma-4b and qwen3-4b ---
echo "=== Search team calls gemma-4b (allowed) ==="
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SEARCH_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "Hello from the search team!"}]}' \
  | jq -r '.choices[0].message.content'

# --- Support team: access to gemma-4b only ---
echo "=== Support team calls gemma-4b (allowed) ==="
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SUPPORT_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "Hello from the support team!"}]}' \
  | jq -r '.choices[0].message.content'

# --- Support team tries qwen3-4b (NOT allowed) ---
echo "=== Support team calls qwen3-4b (BLOCKED) ==="
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SUPPORT_KEY" \
  -d '{"model": "qwen3-4b", "messages": [{"role": "user", "content": "Can I use this model?"}]}' \
  | jq '.error'

# Show team info: budget and rate limits
LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d)

echo "=== Search team: budget $50, 60 rpm ==="
curl -s http://localhost:4000/team/list \
  -H "Authorization: Bearer $LITELLM_KEY" \
  | jq '.[] | select(.team_alias=="search-ranking") | {team_alias, max_budget, rpm_limit, tpm_limit, models}'

echo "=== Support team: budget $25, 30 rpm ==="
curl -s http://localhost:4000/team/list \
  -H "Authorization: Bearer $LITELLM_KEY" \
  | jq '.[] | select(.team_alias=="customer-support") | {team_alias, max_budget, rpm_limit, tpm_limit, models}'

# ─────────────────────────────────────────────────────────────────────────────
# ACT 4: Observability in Langfuse (3 min)
# ─────────────────────────────────────────────────────────────────────────────

# → Open Langfuse UI (localhost:3000)
# Show:
#   - Traces from the API calls above (search team + support team)
#   - Each trace tagged with team metadata
#   - Token counts, latency, cost per request
#   - Filter by team to show per-team spend tracking

# → Open LiteLLM UI (localhost:4000/ui) → Usage tab
# Show per-team budget and spend:
echo "=== Team spend vs budget ==="
curl -s http://localhost:4000/team/list \
  -H "Authorization: Bearer $LITELLM_KEY" \
  | jq '.[] | select(.team_alias | test("search|customer")) | {team: .team_alias, spend: .spend, budget: .max_budget, rpm_limit, tpm_limit}'

# "LiteLLM enforces budgets — when a team hits their limit, requests are blocked"
# "Langfuse gives you the full trace — every request, tokens, latency, cost"

# ─────────────────────────────────────────────────────────────────────────────
# ACT 5: Cost Management (2 min)
# ─────────────────────────────────────────────────────────────────────────────

echo "GPU nodes running (each ~\$1/hr):"
kubectl get nodes -l workload-type=gpu-inference --no-headers | wc -l

# "Scale down with one command — Karpenter reclaims GPU nodes"
# ./ops/scale-down.sh
# "Scale back up — ArgoCD restores everything automatically"
# ./ops/scale-up.sh

# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP (after demo)
# ─────────────────────────────────────────────────────────────────────────────

# Remove the model deployed during the demo
# git rm workloads/models/qwen3-4b.yaml
# git commit -m "chore: Remove demo model"
# git push origin main
