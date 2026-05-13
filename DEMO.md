# Demo Runbook — Platform Self-Service Story

**Length**: ~8-10 min live. **Audience action**: 3 git commits.

## Story

Acme Co.'s data-science team asks the platform team for access. They
get a scoped namespace, API key, budget, and rate limits via one YAML.
They pick a model with our recommender, commit another YAML, and call
it through the internal API gateway — all within minutes.

## Prerequisites (set once before the demo)

```bash
# 1. AWS region + kubectl context pointed at the demo cluster
export AWS_REGION=eu-central-1
aws eks update-kubeconfig --region $AWS_REGION --name ai-platform-cnd-demo

# 2. Verify cluster is clean and healthy
kubectl get inferenceendpoints,aiteams --all-namespaces
kubectl get applicationsets -n argocd

# 3. Make sure your IP is in the ALB allowlist
#    File: platform/config/ingress.yaml — alb.ingress.kubernetes.io/inbound-cidrs
#    (If your public IP changed, update, commit, push, wait ~5s for reconcile.)
MY_IP=$(curl -s https://checkip.amazonaws.com)
echo "My IP: $MY_IP — is it in platform/config/ingress.yaml allowlist?"
grep -i inbound-cidrs platform/config/ingress.yaml

# 4. hf-token Secret exists in inference namespace (for gated models; not needed for smollm3)
kubectl get secret hf-token -n inference

# 5. ALB hostname handy
export ALB=$(kubectl get ingress ai-platform-litellm -n ai-platform \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
echo "ALB: $ALB"
# LiteLLM :4000, Open WebUI :8080, Langfuse :3000
```

---

## Act 1 — Platform team onboards the data-science team (~2 min)

### Step 1 · Create the AITeam YAML

```bash
cat > workloads/teams/data-science.yaml << 'EOF'
# Team: Data Science
# One CR expands into: namespace, ResourceQuota, NetworkPolicy,
# ServiceAccount, RoleBinding, LiteLLM team + scoped API key, Langfuse project.
apiVersion: kro.run/v1alpha1
kind: AITeam
metadata:
  name: team-data-science
  namespace: ai-platform
  annotations:
    argocd.argoproj.io/sync-wave: "1"
spec:
  teamName: data-science
  models:
    - "*"                      # Allow any model (demo). Prod would scope explicitly.
  maxBudget: "100.0"           # USD / period
  budgetDuration: "30d"
  rpmLimit: 120
  tpmLimit: 100000
EOF
```

### Step 2 · Commit, push, force ArgoCD sync

```bash
git add workloads/teams/data-science.yaml
git commit -m "feat: onboard data-science team"
git push

kubectl patch application teams -n argocd --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

### Step 3 · Watch KRO expand the CR (~30 seconds)

```bash
# Show the team become active — Ctrl-C when STATE=ACTIVE
kubectl get aiteam team-data-science -n ai-platform -w
```

### Step 4 · Show what the platform created

```bash
kubectl get resourcequota,networkpolicy,sa,rolebinding,secret,configmap \
  -n team-data-science
```

You should see: ResourceQuota (CPU/mem/GPU caps), NetworkPolicy (ingress+egress),
ServiceAccount, RoleBinding → team-developer ClusterRole, API key Secret,
welcome ConfigMap.

### Step 5 · Hand the team their welcome doc + API key

```bash
# The welcome doc — generated per-team by KRO
kubectl get configmap welcome -n team-data-science \
  -o jsonpath='{.data.README\.md}'

# The team's scoped LiteLLM API key
export TEAM_KEY=$(kubectl get secret data-science-api-key -n team-data-science \
  -o jsonpath='{.data.api-key}' | base64 -d)
echo "$TEAM_KEY"
```

**Optional visual**: open `http://$ALB:4000/ui` → Teams tab → `data-science`
is listed with budget + limits.

---

## Act 2 — Data-science team deploys SmolLM3-3B (~6-7 min)

### Step 6 · Run the recommender

```bash
./ops/recommend-instance.py HuggingFaceTB/SmolLM3-3B
```

Output: a recommended GPU instance + a ready-to-commit `InferenceEndpoint`
YAML, including a right-sized `workerMemory`. Explain the VRAM
decomposition (weights, KV cache, overhead) and the alternatives table.

### Step 7 · Save the YAML snippet (copy-paste from the recommender's output)

```bash
cat > workloads/models/smollm3-3b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: smollm3-3b
  namespace: inference
spec:
  model: "HuggingFaceTB/SmolLM3-3B"
  gpuCount: 1
  maxModelLen: 8192
  minVramPerGpuGiB: 9       # recommender's conservative hint
  workerMemory: "10Gi"      # sized to the model — fits g6.xlarge
  minReplicas: 1
  maxReplicas: 2
EOF
```

### Step 8 · Commit + push + sync

```bash
git add workloads/models/smollm3-3b.yaml
git commit -m "feat: deploy smollm3-3b"
git push

kubectl patch application models -n argocd --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

### Step 9 · Narrate the lifecycle via the MESSAGE field

```bash
kubectl get inferenceendpoints -n inference -w \
  -o custom-columns='NAME:.metadata.name,READY:.status.ready,STATUS:.status.modelStatus,MESSAGE:.status.message'
```

You'll see three phases (~6 min end-to-end on a warm cluster):
1. `Pending` — "Waiting for GPU node provisioning and image pull"
2. `Deploying` — "Model is loading onto GPU workers"
3. `Running` — **"Model is live and serving requests"** ← demo moment

### Step 10 · Invoke via ALB using the team's scoped key

```bash
curl -s http://$ALB:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TEAM_KEY" \
  -d '{
    "model": "smollm3-3b",
    "messages": [{"role": "user", "content": "In one sentence, what is EKS?"}],
    "max_tokens": 100
  }' | python3 -m json.tool
```

This proves:
- Model is live (vLLM serving on the new GPU node)
- LiteLLM routed it using the team key (not master key)
- Budget + rate limits apply per-team (not visible in one curl, but enforced)

### Step 11 · (Optional) Show the trace in Langfuse

Open `http://$ALB:3000` → Traces → filter by team. Shows prompt, completion,
tokens, latency — tagged to data-science.

---

## Cleanup after the demo

```bash
# Remove the demo artifacts
git rm workloads/models/smollm3-3b.yaml
git rm workloads/teams/data-science.yaml
git commit -m "chore: demo cleanup"
git push

# Force ArgoCD to prune immediately instead of waiting for poll
kubectl patch application models -n argocd --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
kubectl patch application teams -n argocd --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
```

ArgoCD prunes the CRs → KRO tears down the namespace + RayService →
Karpenter reclaims the GPU node after 300s empty.

---

## Summary slide / closer

| Step | Human input | Platform output |
|---|---|---|
| Onboard team | 1 commit (10-line YAML) | Namespace, quotas, network policies, scoped API key, Langfuse project |
| Pick instance | 1 command | Model analyzed, GPU sized, YAML emitted |
| Deploy model | 1 commit (12-line YAML) | GPU node provisioned, weights pulled, model registered with gateway |
| Use model | 1 curl | Routed, budget-enforced, trace recorded |

**Human time: ~3 min. Platform time: ~7 min. Zero manual kubectl.**

## Known gaps (for Q&A)

- **NetworkPolicy egress is declared on team namespaces but not enforced** by the VPC CNI today (CNI runs with `--enable-network-policy=false`). Enabling it requires a rolling DaemonSet restart that hits an image-pull issue on older Bottlerocket nodes — safe to fix in a calmer window by replacing those nodes. Until then, team isolation works at these layers: namespace boundary + RBAC + ResourceQuota + LiteLLM team-scoped API key with budget/rate limits + Langfuse project tag. Network-level egress blocking is the missing layer.
- Langfuse + LiteLLM integration requires a one-time manual API key exchange (see README step 9). Not part of the live onboarding flow.
