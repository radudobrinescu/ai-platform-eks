#!/bin/bash
# =============================================================================
# AI Platform on EKS — Demo Script
# =============================================================================
# Copy-paste commands section by section. NOT meant to run end-to-end.
#
# Prerequisites:
#   - Cluster running with gemma-4b, smollm3-3b, llama32-1b deployed
#   - Teams onboarded (search-ranking, customer-support)
#   - Port-forwards active:
#       kubectl port-forward svc/litellm 4000:4000 -n ai-platform &
#       kubectl port-forward svc/open-webui 8080:8080 -n ai-platform &
#       kubectl port-forward svc/langfuse-web 3000:3000 -n ai-platform &
#   - Browser tabs ready:
#       Open WebUI  → http://localhost:8080
#       Langfuse    → http://localhost:3000
#       ArgoCD      → (see terraform output for URL)
#       LiteLLM UI  → http://localhost:4000/ui
#
# Tip: To avoid the ~5 min GPU provisioning wait during the live demo,
# pre-deploy qwen3-4b before the demo and delete it right before starting.
# The GPU node stays warm, so re-deploy takes ~2 min instead of ~7 min.
# =============================================================================


# ─────────────────────────────────────────────────────────────────────────────
# ACT 1: Platform Overview (5 min)
# "Every team wants to run LLMs. The platform makes it self-service."
# ─────────────────────────────────────────────────────────────────────────────

# → Show ArgoCD UI: walk through the apps
#   platform-config, models, teams, gpu-operator, kuberay, litellm, langfuse, open-webui

# Show the KRO ResourceGraphDefinitions — the platform APIs
kubectl get resourcegraphdefinitions
# NAME                 APIVERSION   KIND                STATE    AGE
# inference-endpoint   v1alpha1     InferenceEndpoint   Active   ...
# team-onboarding      v1alpha1     AITeam              Active   ...

# Show running models and their status
kubectl get inferenceendpoints -n inference \
  -o custom-columns=NAME:.metadata.name,READY:.status.ready,STATUS:.status.modelStatus,ENDPOINT:.status.endpoint

# Show GPU nodes — Bottlerocket with SOCI for fast image pulls
kubectl get nodes -l workload-type=gpu-inference \
  -o custom-columns=NAME:.metadata.name,OS:.status.nodeInfo.osImage,GPU:.status.allocatable.nvidia\\.com/gpu

# Show what a model definition looks like — "this is ALL a team writes"
cat workloads/models/gemma-4b.yaml

# "6 lines of YAML. KRO expands this into a RayService, GPU workers,
#  LiteLLM registration, and a CloudWatch log group."

# → Open WebUI (localhost:8080): chat with gemma-4b to show it works


# ─────────────────────────────────────────────────────────────────────────────
# ACT 2: Deploy a New Model — Live (5-7 min)
# "Commit a YAML, model is live. That's the workflow."
# ─────────────────────────────────────────────────────────────────────────────

# Create a new model file — do this live
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

cat > workloads/models/llama32-1b.yaml << 'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: llama32-1b
  namespace: inference
spec:
  model: "meta-llama/Llama-3.2-1B-Instruct"
  gpuCount: 1
  minReplicas: 1
  maxReplicas: 2
EOF

# Commit and push
git add workloads/models/qwen3-4b.yaml
git commit -m "Deploy Qwen3 4B"
git push origin main

git add workloads/models/llama32-1b.yaml
git commit -m "Deploy Llama32 1B"
git push origin main

# → ArgoCD UI: watch the "models" app sync

# Watch the deployment progress (Ctrl+C when all pods are Running)
kubectl get pods -n inference -l ray.io/cluster=qwen3-4b -w

# Check model status
kubectl get inferenceendpoints -n inference \
  -o custom-columns=NAME:.metadata.name,READY:.status.ready,STATUS:.status.modelStatus

# Verify the CloudWatch log group was created by ACK
# "KRO told ACK to create this AWS resource — no CloudFormation, no Terraform."
kubectl get loggroups -n inference
aws logs describe-log-groups \
  --log-group-name-prefix /ai-platform/models/qwen3-4b --region eu-central-1 \
  --query 'logGroups[].logGroupName'

# Show ACK capabilities — AWS services manageable from Kubernetes
echo ""
echo "=== ACK controllers available ==="
kubectl api-resources 2>/dev/null | grep "services.k8s.aws" \
  | awk -F'/' '{print $1}' | awk '{print $NF}' | sort -u
echo ""
echo "$(kubectl api-resources 2>/dev/null | grep 'services.k8s.aws' | awk -F'/' '{print $1}' | awk '{print $NF}' | sort -u | wc -l | tr -d ' ') AWS service controllers ready to use"

# "Any of these AWS services can be composed into a KRO ResourceGraphDefinition.
#  S3 buckets, DynamoDB tables, SQS queues — all managed from Kubernetes."

# → Open WebUI: refresh the model list — qwen3-4b appears automatically
# → Chat with it live


# ─────────────────────────────────────────────────────────────────────────────
# ACT 3: Multi-Tenant Team Isolation (5 min)
# "Teams get scoped access. Budget, rate limits, model access — all enforced."
# ─────────────────────────────────────────────────────────────────────────────

# Show what a team definition looks like
cat workloads/teams/search-ranking.yaml

# "One YAML creates: namespace, RBAC, resource quotas, network policy,
#  a scoped LiteLLM API key, and optionally a Langfuse project."

# Show what KRO created
kubectl get ns | grep team
kubectl get resourcequota -n team-search-ranking
kubectl get networkpolicy -n team-search-ranking

# Get the team API keys
SEARCH_KEY=$(kubectl get secret search-ranking-api-key -n team-search-ranking \
  -o jsonpath='{.data.api-key}' | base64 -d)
SUPPORT_KEY=$(kubectl get secret customer-support-api-key -n team-customer-support \
  -o jsonpath='{.data.api-key}' | base64 -d)

# --- Search team: has access to gemma-4b and qwen3-4b ---
echo ""
echo ">>> Search team calls gemma-4b (ALLOWED)"
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SEARCH_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "Summarize what EKS is in one sentence."}]}' \
  | jq -r '.choices[0].message.content'

# --- Support team: has access to gemma-4b only ---
echo ""
echo ">>> Support team calls gemma-4b (ALLOWED)"
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SUPPORT_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "What is Kubernetes?"}]}' \
  | jq -r '.choices[0].message.content'

# --- Support team tries a model they don't have access to ---
echo ""
echo ">>> Support team calls qwen3-4b (BLOCKED)"
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SUPPORT_KEY" \
  -d '{"model": "qwen3-4b", "messages": [{"role": "user", "content": "Hello"}]}' \
  | jq -r '.error.message'


# ─────────────────────────────────────────────────────────────────────────────
# ACT 4: Observability & Spend Tracking (3 min)
# "Every request is traced. Every dollar is tracked."
# ─────────────────────────────────────────────────────────────────────────────

# → Open Langfuse UI (localhost:3000)
#   - Show traces from the API calls above
#   - Each trace shows: model, tokens (input/output), latency, cost
#   - Filter by user/metadata to show per-team traces

# Show per-team budget and spend via LiteLLM API
LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d)

echo ""
echo "=== Team Spend vs Budget ==="
curl -s http://localhost:4000/team/list \
  -H "Authorization: Bearer $LITELLM_KEY" \
  | jq '[.[] | {team: .team_alias, spend: .spend, budget: .max_budget, models, rpm_limit, tpm_limit}]'

# → Open LiteLLM UI (localhost:4000/ui) → Usage tab
#   - Show per-team spend breakdown
#   - "When a team hits their budget, requests are automatically blocked"


# ─────────────────────────────────────────────────────────────────────────────
# ACT 5: Cost Management (2 min)
# "GPU nodes cost ~$1/hr each. We scale to zero when not in use."
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "GPU nodes running:"
kubectl get nodes -l workload-type=gpu-inference \
  -o custom-columns=NAME:.metadata.name,INSTANCE:.metadata.labels.node\\.kubernetes\\.io/instance-type,GPU:.status.allocatable.nvidia\\.com/gpu

# "One command scales everything down. Karpenter reclaims GPU nodes in ~5 min."
# ./ops/scale-down.sh

# "One command brings it back. ArgoCD restores all workloads automatically."
# ./ops/scale-up.sh


# ─────────────────────────────────────────────────────────────────────────────
# CLOSING
# ─────────────────────────────────────────────────────────────────────────────

# Key takeaways:
#   - 6-line YAML to deploy any open-source LLM
#   - Full GitOps — commit to git, model is live
#   - Multi-tenant — per-team budgets, rate limits, model access
#   - Built on EKS managed capabilities: ArgoCD, KRO, ACK
#   - GPU cold start ~2 min with Bottlerocket + SOCI + ECR cache
#   - Full observability via Langfuse — every request traced


# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP (after demo)
# ─────────────────────────────────────────────────────────────────────────────

# git rm workloads/models/qwen3-4b.yaml
# git commit -m "chore: Remove demo model"
# git push origin main
