# Demo Script — AI Platform on EKS

## Pre-Demo Setup (10 minutes before)

### 1. Deploy smollm3-3b on dedicated GPU

```bash
cat > workloads/models/smollm3-3b.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: smollm3-3b
  namespace: inference
spec:
  model: "HuggingFaceTB/SmolLM3-3B"
  shared: false
  gpuCount: 1
  maxModelLen: 4096
  minVramPerGpuGiB: 9
  workerMemory: "10Gi"
  minReplicas: 1
  maxReplicas: 2
EOF
git add workloads/models/smollm3-3b.yaml
git commit -m "feat: deploy smollm3-3b (dedicated)"
git push origin main
```

Nudge ArgoCD from UI (Sync "models" app). Wait ~5 min for RUNNING.

Verify:
```bash
kubectl get inferenceendpoints -n inference
```

### 2. Open tabs

- **Dashboard**: `http://<ALB>:9090/cluster-topology.html`
- **Open WebUI**: `http://<ALB>:8080`
- **Terminal**: ready for curl commands
- **ArgoCD UI**: for sync nudges

### 3. Get ALB hostname and admin key

```bash
export ALB=$(kubectl get ingress ai-platform-litellm -n ai-platform \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
export ADMIN_KEY=$(kubectl get secret litellm-secrets -n ai-platform \
  -o jsonpath='{.data.master-key}' | base64 -d)
```

### 4. Verify smollm3-3b responds

```bash
curl -s http://$ALB:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -d '{"model":"smollm3-3b","messages":[{"role":"user","content":"Hello!"}],"max_tokens":30}' | python3 -m json.tool
```

---

## Live Demo

### Step 1: Show the Platform (1 min)

**Show Dashboard** — point out:
- Karpenter NodePools (MNG, default, gpu-shared, gpu-inference)
- smollm3-3b running on a dedicated GPU node
- Warm pool node ready with pre-cached image
- InferenceEndpoints section showing smollm3-3b RUNNING

**Talking point:**
> "This is our AI inference platform. Models deploy via GitOps — commit a YAML, platform handles GPU provisioning, model serving, and API routing. Right now we have one model running on a dedicated GPU."

---

### Step 2: Onboard Two Teams (2 min)

**Commit both teams:**

```bash
cat > workloads/teams/dev-team.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: AITeam
metadata:
  name: dev-team
  namespace: ai-platform
spec:
  teamName: dev
  models: ["smollm3-3b"]
  maxBudget: "100.0"
  budgetDuration: "30d"
  rpmLimit: 60
  tpmLimit: 100000
EOF

cat > workloads/teams/data-science.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: AITeam
metadata:
  name: data-science
  namespace: ai-platform
spec:
  teamName: data-science
  models: ["smollm3-3b", "gemma-3-1b-it"]
  maxBudget: "500.0"
  budgetDuration: "30d"
  rpmLimit: 120
  tpmLimit: 200000
EOF

git add workloads/teams/
git commit -m "feat: onboard dev and data-science teams"
git push origin main
```

Nudge ArgoCD (Sync "teams" app). Wait ~30s.

**Talking point:**
> "We're onboarding two teams. Dev team gets access to smollm3-3b only, with a $100/month budget. Data science gets access to both smollm3 and gemma, with $500 and higher rate limits. Each gets their own namespace, RBAC, and a scoped API key."

**Retrieve team API keys:**

```bash
export DEV_KEY=$(kubectl get configmap welcome -n team-dev \
  -o jsonpath='{.data.api-key}' 2>/dev/null || \
  kubectl get secret litellm-team-key -n team-dev \
  -o jsonpath='{.data.key}' | base64 -d)
echo "Dev team key: $DEV_KEY"

export DS_KEY=$(kubectl get configmap welcome -n team-data-science \
  -o jsonpath='{.data.api-key}' 2>/dev/null || \
  kubectl get secret litellm-team-key -n team-data-science \
  -o jsonpath='{.data.key}' | base64 -d)
echo "Data science key: $DS_KEY"
```

---

### Step 3: Show Access Control (1 min)

**Dev team calls smollm3-3b — WORKS:**

```bash
curl -s http://$ALB:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DEV_KEY" \
  -d '{"model":"smollm3-3b","messages":[{"role":"user","content":"What is Kubernetes in one sentence?"}],"max_tokens":50}'
```

**Data science calls smollm3-3b — WORKS:**

```bash
curl -s http://$ALB:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DS_KEY" \
  -d '{"model":"smollm3-3b","messages":[{"role":"user","content":"What is Kubernetes in one sentence?"}],"max_tokens":50}'
```

**Talking point:**
> "Both teams can access smollm3 — they're authorized for it. But watch what happens when the dev team tries to access a model they're not approved for..."

---

### Step 4: Deploy gemma-3-1b-it on Shared GPU (3 min)

**Switch to Dashboard view.**

**Commit the model:**

```bash
cat > workloads/models/gemma-3-1b-it.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gemma-3-1b-it
  namespace: inference
spec:
  model: "google/gemma-3-1b-it"
  shared: true
  gpuCount: 1
  maxModelLen: 4096
  minVramPerGpuGiB: 9
  workerMemory: "10Gi"
  minReplicas: 1
  maxReplicas: 2
EOF
git add workloads/models/gemma-3-1b-it.yaml
git commit -m "feat: deploy gemma-3-1b-it (shared GPU)"
git push origin main
```

Nudge ArgoCD (Sync "models" app). **Watch the Dashboard** — narrate as events appear.

**Talking points while waiting (~3 min):**
> "Watch the dashboard — the model goes through Pending, then DEPLOYING. It's landing on the warm-pool node where we pre-cached the container image. No image pull needed — that 13 gigabyte download takes zero seconds because of our EBS snapshot optimization."

> "The timer shows real elapsed time. vLLM is loading the model weights from our S3 cache and initializing CUDA kernels on the GPU."

> "There it is — RUNNING. From git push to serving requests in about 3 minutes. Without our optimizations, this would take 7+ minutes."

---

### Step 5: Show Access Control Difference (1 min)

**Data science calls gemma3 — WORKS:**

```bash
curl -s http://$ALB:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DS_KEY" \
  -d '{"model":"gemma-3-1b-it","messages":[{"role":"user","content":"Explain machine learning to a 5 year old."}],"max_tokens":80}'
```

**Dev team calls gemma3 — BLOCKED:**

```bash
curl -s http://$ALB:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $DEV_KEY" \
  -d '{"model":"gemma-3-1b-it","messages":[{"role":"user","content":"Hello"}],"max_tokens":30}'
```

Should return 403 or model access error.

**Talking point:**
> "Data science has access to gemma — it's in their approved model list. The dev team doesn't. Same platform, same API, different access levels. All governed by a single YAML file."

---

### Step 6: Show Open WebUI (30 sec)

Switch to Open WebUI tab. Select gemma-3-1b-it or smollm3-3b. Type a question. Show streaming response.

**Talking point:**
> "This is what end users see — a familiar chat interface. Behind it: GPU scheduling, model serving, API routing, observability. All managed by the platform."

---

## Timing Summary

| Step | Duration | Cumulative |
|------|----------|-----------|
| Show platform | 1 min | 1 min |
| Onboard teams | 2 min | 3 min |
| Access control (smollm3) | 1 min | 4 min |
| Deploy gemma3 (live) | 3 min | 7 min |
| Access control (gemma3) | 1 min | 8 min |
| Open WebUI | 30 sec | 8.5 min |

**Total: ~8-9 minutes live demo**

---

## Cleanup (after demo)

```bash
rm workloads/models/gemma-3-1b-it.yaml workloads/models/smollm3-3b.yaml
rm workloads/teams/dev-team.yaml workloads/teams/data-science.yaml
git add -A workloads/
git commit -m "chore: cleanup after demo"
git push origin main
```

Force prune in ArgoCD UI for both "models" and "teams" apps.

---

## Fallback Commands

If ArgoCD is slow to sync:
```bash
kubectl annotate application models -n argocd argocd.argoproj.io/refresh=hard --overwrite
kubectl annotate application teams -n argocd argocd.argoproj.io/refresh=hard --overwrite
```

If you need to check model status:
```bash
kubectl get inferenceendpoints -n inference
kubectl get events -n inference --sort-by='.lastTimestamp' | tail -10
```

If team keys aren't in ConfigMap yet:
```bash
kubectl get secrets -n team-dev
kubectl get secrets -n team-data-science
```
