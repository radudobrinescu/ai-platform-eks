#!/bin/bash
# =============================================================================
# AI Platform on EKS — Demo Script
# =============================================================================
# Copy-paste commands section by section. NOT meant to run end-to-end.
#
# Prerequisites:
#   - Cluster running with gemma-4b and smollm3-3b deployed
#   - llama32-1b NOT deployed (removed before demo for live deployment)
#   - Teams onboarded (search-ranking, customer-support)
#   - Port-forwards active:
#       kubectl port-forward svc/litellm 4000:4000 -n ai-platform &
#       kubectl port-forward svc/open-webui 8080:8080 -n ai-platform &
#       kubectl port-forward svc/langfuse-web 3000:3000 -n ai-platform &
#   - Browser tabs ready:
#       EKS Console → cluster page (show capabilities)
#       ArgoCD UI   → (see terraform output for URL)
#       Open WebUI  → http://localhost:8080
#       LiteLLM UI  → http://localhost:4000/ui
#       Langfuse    → http://localhost:3000
# =============================================================================


# ─────────────────────────────────────────────────────────────────────────────
# PART 1: What's in the Cluster (5 min)
# ─────────────────────────────────────────────────────────────────────────────

# → EKS Console: show the cluster, highlight Managed Capabilities:
#   ArgoCD, KRO, ACK — all AWS-managed, run in AWS infrastructure

# → ArgoCD UI: walk through the apps
#   platform-config, models, teams, gpu-operator, kuberay,
#   litellm, langfuse, open-webui
#   "Everything is GitOps-managed — the entire platform is defined in git"

# Show ACK controllers — 55 AWS services manageable from Kubernetes
kubectl api-resources 2>/dev/null \
  | grep "services.k8s.aws" \
  | awk -F'/' '{print $1}' | awk '{print $NF}' | sort -u

# "55 AWS service controllers. Let's look at one — S3 Bucket:"
kubectl explain bucket.spec --api-version=s3.services.k8s.aws/v1alpha1

# "You can create S3 buckets, DynamoDB tables, SQS queues — all from
#  Kubernetes manifests. We use this to create CloudWatch log groups
#  for each model automatically."


# ─────────────────────────────────────────────────────────────────────────────
# PART 2: KRO and the Platform APIs (3 min)
# ─────────────────────────────────────────────────────────────────────────────

# Show the KRO ResourceGraphDefinitions — the custom APIs
kubectl get resourcegraphdefinitions

# "KRO lets the platform team define custom Kubernetes APIs.
#  InferenceEndpoint: deploy a model. AITeam: onboard a team.
#  Users write simple YAML — KRO expands it into the full resource graph."

# Show running models
kubectl get inferenceendpoints -n inference \
  -o custom-columns=NAME:.metadata.name,READY:.status.ready,STATUS:.status.modelStatus,ENDPOINT:.status.endpoint

# Show what a model definition looks like
cat workloads/models/gemma-4b.yaml

# "6 lines of YAML. KRO expands this into a RayService with vLLM,
#  GPU worker pods, a LiteLLM registration Job, and a CloudWatch
#  log group via ACK."

# Show the CloudWatch log groups created by ACK
kubectl get loggroups -n inference


# ─────────────────────────────────────────────────────────────────────────────
# PART 3: Models in LiteLLM and Open WebUI (3 min)
# ─────────────────────────────────────────────────────────────────────────────

# Show models registered in LiteLLM
LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d)

curl -s http://localhost:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_KEY" | jq -r '.data[].id'

# "All models are behind an OpenAI-compatible API. Any tool that
#  speaks OpenAI can use them — Open WebUI, LangChain, your own code."

# → Open WebUI (localhost:8080): show the model list, chat with gemma-4b
# "This is the same API — Open WebUI just calls LiteLLM under the hood."

# Show GPU nodes powering the models
kubectl get nodes -l workload-type=gpu-inference \
  -o custom-columns=NAME:.metadata.name,OS:.status.nodeInfo.osImage,GPU:.status.allocatable.nvidia\\.com/gpu


# ─────────────────────────────────────────────────────────────────────────────
# PART 4: Deploy a New Model — Live (5-7 min)
# ─────────────────────────────────────────────────────────────────────────────

# "Let's deploy Llama 3.2 1B. All I need is this YAML:"
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

cat workloads/models/llama32-1b.yaml

# Commit and push
git add workloads/models/llama32-1b.yaml
git commit -m "feat: Deploy Llama 3.2 1B Instruct"
git push origin main

# → ArgoCD UI: watch the "models" app sync

# Watch pods appear (Ctrl+C when worker shows Running)
kubectl get pods -n inference -l ray.io/cluster=llama32-1b -w

# Check model status
kubectl get inferenceendpoints -n inference \
  -o custom-columns=NAME:.metadata.name,READY:.status.ready,STATUS:.status.modelStatus

# Verify CloudWatch log group was created by ACK
kubectl get loggroups -n inference
aws logs describe-log-groups \
  --log-group-name-prefix /ai-platform/models/llama32-1b --region eu-central-1 \
  --query 'logGroups[].logGroupName'

# Once RUNNING:
# → Open WebUI: refresh models — llama32-1b appears, chat with it
# → LiteLLM UI: show the new model in the model list


# ─────────────────────────────────────────────────────────────────────────────
# PART 5: Multi-Tenant Team Isolation (5 min)
# ─────────────────────────────────────────────────────────────────────────────

# Show a team definition
cat workloads/teams/search-ranking.yaml

# "One YAML creates: namespace, RBAC, resource quotas, network policy,
#  and a scoped LiteLLM API key with budget and rate limits."

# Show what KRO created
kubectl get ns | grep team
kubectl get resourcequota -n team-search-ranking
kubectl get networkpolicy -n team-search-ranking

# Get team API keys
SEARCH_KEY=$(kubectl get secret search-ranking-api-key -n team-search-ranking \
  -o jsonpath='{.data.api-key}' | base64 -d)
SUPPORT_KEY=$(kubectl get secret customer-support-api-key -n team-customer-support \
  -o jsonpath='{.data.api-key}' | base64 -d)

# Search team calls gemma-4b — ALLOWED (in their model list)
echo ""
echo ">>> Search team → gemma-4b (ALLOWED)"
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SEARCH_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "What is EKS? One sentence."}]}' \
  | jq -r '.choices[0].message.content'

# Support team calls gemma-4b — ALLOWED
echo ""
echo ">>> Support team → gemma-4b (ALLOWED)"
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SUPPORT_KEY" \
  -d '{"model": "gemma-4b", "messages": [{"role": "user", "content": "What is Kubernetes? One sentence."}]}' \
  | jq -r '.choices[0].message.content'

# Support team tries smollm3-3b — BLOCKED (not in their model list)
echo ""
echo ">>> Support team → smollm3-3b (BLOCKED)"
curl -s http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SUPPORT_KEY" \
  -d '{"model": "smollm3-3b", "messages": [{"role": "user", "content": "Hello"}]}' \
  | jq -r '.error.message'


# ─────────────────────────────────────────────────────────────────────────────
# PART 6: Observability & Spend Tracking (3 min)
# ─────────────────────────────────────────────────────────────────────────────

# → Langfuse UI (localhost:3000)
#   - Show traces from the API calls above
#   - Each trace: model, tokens in/out, latency, cost
#   - Filter by user/metadata to see per-team traces

# Show per-team budget and spend
echo ""
echo "=== Team Spend vs Budget ==="
curl -s http://localhost:4000/team/list \
  -H "Authorization: Bearer $LITELLM_KEY" \
  | jq '[.[] | {team: .team_alias, spend: .spend, budget: .max_budget, models, rpm_limit, tpm_limit}]'

# → LiteLLM UI (localhost:4000/ui) → Usage tab
#   "When a team hits their budget, requests are automatically blocked."


# ─────────────────────────────────────────────────────────────────────────────
# PART 7: Cost Management (1 min)
# ─────────────────────────────────────────────────────────────────────────────

echo ""
echo "=== GPU Nodes ==="
kubectl get nodes -l workload-type=gpu-inference \
  -o custom-columns=NAME:.metadata.name,INSTANCE:.metadata.labels.node\\.kubernetes\\.io/instance-type,GPU:.status.allocatable.nvidia\\.com/gpu

# "GPU nodes cost ~$1/hr each. One command scales everything down,
#  Karpenter reclaims the nodes. One command brings it back."
# ./ops/scale-down.sh
# ./ops/scale-up.sh


# ─────────────────────────────────────────────────────────────────────────────
# CLOSING — Key Takeaways
# ─────────────────────────────────────────────────────────────────────────────

# - 6-line YAML to deploy any open-source LLM
# - Full GitOps — commit to git, model is live
# - Multi-tenant — per-team budgets, rate limits, model access
# - Built on EKS Managed Capabilities: ArgoCD, KRO, ACK
# - GPU cold start ~2 min with Bottlerocket + SOCI + ECR cache
# - Full observability — every request traced in Langfuse


# ─────────────────────────────────────────────────────────────────────────────
# CLEANUP (after demo)
# ─────────────────────────────────────────────────────────────────────────────

# git rm workloads/models/llama32-1b.yaml
# git commit -m "chore: Remove demo model"
# git push origin main
