# Conference Demo — Self-Service AI Platform on EKS

**Audience:** Stakeholders, engineering leadership, conference floor
**Duration:** 25-30 minutes (with Q&A buffer)
**Cluster:** `ai-platform-cnd-demo` in `eu-central-1` (account `802019299867`)
**ALB:** `k8s-aiplatform-0f70754cbc-22700774.eu-central-1.elb.amazonaws.com`

---

## What you're showing

A self-service AI platform where a developer commits a 10-line YAML and gets a production-ready, OpenAI-compatible inference endpoint in ~60 seconds. Plus an autonomous Platform Health Agent that watches the cluster and proposes fixes for failures with a click-to-apply UI.

**Three acts:**
1. **GitOps model deployment** — commit a YAML, watch a model come alive
2. **End-user experience** — chat with the model, see traces, see the dashboard
3. **Autonomous incident response** — break something, watch the agent diagnose and fix

---

## T-15 min: pre-demo prep

Before stepping in front of the audience.

### 0.1 Verify cluster is clean

```bash
export AWS_REGION=eu-central-1
aws eks update-kubeconfig --region $AWS_REGION --name ai-platform-cnd-demo

# All apps should be Synced/Healthy (gpu-operator + litellm OutOfSync are
# the documented benign drifts — see docs/platform-review-2026-05-28.md):
kubectl get applications -n argocd

# No GPU nodes running yet (Karpenter scaled to zero):
kubectl get nodes -L karpenter.sh/nodepool

# Open the dashboard, confirm it loads + shows 🔔 0:
open http://k8s-aiplatform-0f70754cbc-22700774.eu-central-1.elb.amazonaws.com:9090/
```

### 0.2 Update IP allowlist if needed

```bash
MY_IP=$(curl -s https://checkip.amazonaws.com)
echo "Current public IP: $MY_IP"
# If different from 82.76.116.154 in platform/config/ingress.yaml, edit + push.
```

### 0.3 Pre-warm the demo model into the S3 weight cache (cuts cold start ~45s)

```bash
./ops/seed-model-cache.py HuggingFaceTB/SmolLM3-3B
```

### 0.4 Have these terminal tabs open

| Tab | Command |
|-----|---------|
| **A — git** | working dir of the repo |
| **B — kubectl watch** | `kubectl get pods -n inference -w` |
| **C — kubectl ie** | `kubectl get inferenceendpoints -n inference -w` |
| **D — kubectl agent** | `kubectl logs -n platform-health-agent deploy/event-watcher -f` |
| **E — browser** | `http://<ALB>:9090/` (dashboard), tabs for `:8080` (Open WebUI) and `:3000` (Langfuse) |

---

## ACT 1 — GitOps model deployment (8-10 min)

### 1.1 Show the architecture diagram (1 min)

Open the [README.md](../README.md) → "Architecture" section. Talking points:
- 3 EKS managed capabilities (ArgoCD / KRO / ACK) — no operator-managed glue code
- Karpenter for compute auto-provisioning
- Ray Serve + vLLM for inference
- LiteLLM for the unified OpenAI-compatible API

### 1.2 Show the dashboard topology (1 min)

Open `http://<ALB>:9090/`. Point out:
- Live (2s polling) view of nodes, pods, deployed models
- 4 node pools: `default`, `gpu-inference`, `gpu-shared`, `__mng__` (system addons)
- Activity feed on the left
- Topbar 🔔 0 — meaning Platform Health Agent has no pending approvals

### 1.3 Deploy a model with a 10-line YAML (1 min)

In **Tab A** (git):

```bash
cat > workloads/models/demo-smollm.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: demo-smollm
  namespace: inference
spec:
  model: "HuggingFaceTB/SmolLM3-3B"
  gpuCount: 1
  shared: true            # time-slice on a shared GPU
  maxModelLen: 4096
EOF

git add workloads/models/demo-smollm.yaml
git commit -m "feat: deploy SmolLM3-3B for the demo"
git push
```

Talking points while it pushes:
- This is the entire YAML a developer writes — no Helm chart, no Deployment, no Service
- KRO expands it server-side into ~12 Kubernetes resources

### 1.4 Watch ArgoCD sync (~30s) and KRO expand (1 min)

In **Tab C** — InferenceEndpoint state machine:
```
DEPLOYING → ACTIVE
```

In **Tab B** — pods appearing:
- `demo-smollm-xxxxx-head-yyy` (Ray head)
- `demo-smollm-xxxxx-gpu-workers-worker-yyy` (Ray worker with vLLM)
- `demo-smollm-register-yyy` (LiteLLM registration Job)

Watch a GPU node provisioning:
```bash
kubectl get nodes -L karpenter.sh/nodepool -w
# new node appears: ip-X-Y-Z.eu-central-1.compute.internal  with karpenter.sh/nodepool=gpu-shared
```

**Cold start time** (with the seed cache pre-populated): ~60-90s from `git push` to model serving.

### 1.5 Show the dashboard's reaction (30s)

Refresh `http://<ALB>:9090/` — the new node + pod + model card appear in real time. The topology shows the model bound to its GPU node.

### 1.6 Test the model (1 min)

```bash
# Quick test via the platform helper:
./ops/test-model.sh demo-smollm "Explain Kubernetes operators in one sentence."

# Or direct OpenAI-compatible call:
ALB=k8s-aiplatform-0f70754cbc-22700774.eu-central-1.elb.amazonaws.com
curl -sS http://$ALB:4000/v1/chat/completions \
  -H "Authorization: Bearer $(kubectl get secret litellm-master-key -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d)" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "demo-smollm",
    "messages": [{"role":"user","content":"Explain Kubernetes operators in one sentence."}]
  }' | python3 -m json.tool
```

Show that the response comes back in <2s. Point out it's a regular OpenAI-format response — any OpenAI client SDK works against this endpoint.

### 1.7 Show Open WebUI (1 min)

`http://<ALB>:8080/` — Open WebUI. Pick `demo-smollm` from the model dropdown, type a question, get a response. This is the "no-code" front-end teams can give to non-engineers.

### 1.8 Show Langfuse traces (1 min)

`http://<ALB>:3000/` — Langfuse. The `/v1/chat/completions` requests show up as traces with full prompt, response, and latency. This is how teams debug their agents in production.

---

## ACT 2 — Team isolation (3-5 min)

### 2.1 Show the existing teams (1 min)

```bash
kubectl get aiteam -A
# data-science (large budget, all models)
# dev-team    (small budget, only smollm3-3b)

kubectl get namespace -l app.kubernetes.io/managed-by=KRO
# team-data-science, team-dev — auto-created with quota + RBAC
```

In **Tab A** (git):
```bash
cat workloads/teams/data-science.yaml
```

Talking points:
- Same self-service pattern: commit YAML → namespace, ResourceQuota, NetworkPolicy, RBAC, scoped LiteLLM API key all created
- Budget enforcement at the LiteLLM gateway

### 2.2 (Optional) Create a new team live (2 min)

```bash
cat > workloads/teams/demo-team.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: AITeam
metadata:
  name: demo-team
  namespace: ai-platform
spec:
  teamName: demo
  models: ["demo-smollm"]
  maxBudget: "10.0"
  budgetDuration: "30d"
  rpmLimit: 30
  tpmLimit: 50000
EOF

git add workloads/teams/demo-team.yaml
git commit -m "feat: onboard demo-team"
git push

# Watch the namespace + everything else appear:
kubectl get all,quota,networkpolicy,rolebinding -n team-demo
```

---

## ACT 3 — Autonomous incident response (10-12 min)

This is the showstopper. Have **Tab D** (event-watcher logs) and **Tab E** (browser) visible side-by-side.

### 3.1 Set up the failure (30s)

In **Tab A**:

```bash
# Create a doomed deployment in a workload namespace.
# Memory limit 32 Mi but the container allocates 200 MB → OOMKilled.
kubectl create namespace team-demo-incident
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oom-victim
  namespace: team-demo-incident
spec:
  replicas: 1
  selector: { matchLabels: { app: oom-victim } }
  template:
    metadata: { labels: { app: oom-victim } }
    spec:
      nodeSelector: { kubernetes.io/arch: amd64 }
      containers:
        - name: hog
          image: python:3.12-slim
          command: ["python3","-c","import time; x=bytearray(200*1024*1024); time.sleep(3600)"]
          resources:
            requests: { cpu: 10m, memory: 16Mi }
            limits:   { cpu: 100m, memory: 32Mi }
EOF
```

### 3.2 Show the pod failing (30s)

```bash
kubectl get pods -n team-demo-incident -w
# CrashLoopBackOff, lastTerminationReason: OOMKilled
```

### 3.3 Show the agent reacting (live, ~60s)

In **Tab D** (event-watcher logs), within 30 seconds the agent logs:
```
INFO event-watcher spawned investigator <uuid> for Pod/team-demo-incident/oom-victim-... (kind=OOMKilled)
```

Talking points while it runs:
- Single watcher pod with K8s informers — no polling, event-driven
- Spawned a one-shot Job using a read-only ServiceAccount — kiro-cli can describe pods, read logs, walk owner chains, but K8s RBAC blocks any write attempt
- Investigator initContainers download `kubectl` and `kiro-cli` at pod start — no custom image
- The kiro-cli prompt asks for structured JSON output (severity, root cause, fix commands, impact, risk)

### 3.4 Open the dashboard, click the 🔔 (1 min)

`http://<ALB>:9090/` — within ~60s the topbar 🔔 badge turns amber with a `1`. Click it.

The modal shows the **rich approval card**:
- **HIGH** severity tag
- **Root cause** — "container 'hog' in Deployment 'oom-victim' allocates 200 MB but the limit is 32 Mi…"
- **⚙️ Proposed fix** — a numbered `kubectl patch` to raise the memory limit
- **📊 Impact analysis** — Affects: `Deployment team-demo-incident/oom-victim` · Operations: `✏️ Patch` · Reversibility: `✓ Fully reversible (rollout undo)` · Disruption: `Brief — pods will restart`
- **⚠️ Risk** — "All replicas of oom-victim will restart"

Talking points:
- This card is generated by an LLM (Claude Sonnet via Kiro) but the **impact analysis** is heuristic JS — parses `kubectl` verbs to predict reversibility/disruption without an LLM round-trip
- The **proposed fix** is bounded: K8s RBAC will reject anything outside `inference` and `team-*` namespaces

### 3.5 Click ✓ Approve & apply (1 min)

The modal card morphs:
- Buttons disappear
- Spinner appears: "Applying fix… (verifying with kiro-cli; usually completes in 1-2 min)"

In **Tab D** (logs):
```
spawned remediator-... for investigation <uuid>
```

In **Tab B**:
```bash
kubectl get pods -n platform-health-agent -w
# remediator-XXX-yyy   1/1   Running
```

After ~60s, the modal updates inline:
- ✓ **Fix applied & verified** (green outcome chip)
- Post-fix status: `Deployment/team-demo-incident/oom-victim → ready`
- Rollback commands (collapsed)

In **Tab B** the oom-victim pod is now `Running` (memory limit raised).

### 3.6 Show the History tab (1 min)

Click the **History** tab in the modal. The investigation just completed appears at the top with the ✓ Applied chip. Click to expand — full proposed fix, post-fix status, rollback commands all visible.

Show the **×** button on hover — operator can purge audit-trail rows when done with testing.

### 3.7 Show the safety boundary (1 min)

```bash
# Show the writer ServiceAccount RBAC — scoped via RoleBinding in the
# inference + team-* namespaces only:
kubectl get rolebindings -A -l app.kubernetes.io/managed-by=devops-agent-reconciler

# Show the reader SA only has read verbs cluster-wide:
kubectl describe clusterrole platform-health-agent-reader | head -30
```

Talking points:
- "Even if the LLM hallucinates `kubectl delete deployment kube-dns -n kube-system`, the K8s API rejects it with 403 — RBAC is the security boundary, not the prompt."
- "The agent has zero secrets or AWS credentials. It uses the in-cluster ServiceAccount token."

### 3.8 (Optional) Show stuck-resource detection (2 min)

If time allows, deploy a guaranteed-failing model and show the higher-level trigger:

```bash
cat > workloads/models/doomed.yaml <<'EOF'
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: doomed
  namespace: inference
spec:
  model: "nonexistent-org/this-model-does-not-exist"
  gpuCount: 1
  shared: true
EOF
git add workloads/models/doomed.yaml && git commit -m "demo: doomed model" && git push
```

After ~5 min the agent's `StuckResource` trigger fires on the InferenceEndpoint AND on the underlying RayService. Both show up in the dashboard's History (auto-dismissed because the fix is "edit the YAML in git" → out_of_scope).

---

## Cleanup (after demo)

```bash
# Remove demo workloads from git:
git rm workloads/models/demo-smollm.yaml workloads/teams/demo-team.yaml workloads/models/doomed.yaml 2>/dev/null
git commit -m "chore: remove demo workloads" && git push

# Remove the incident-test namespace:
kubectl delete namespace team-demo-incident --ignore-not-found

# Karpenter will scale GPU nodes back to zero within a few minutes.

# Optional: purge demo investigations from history via the dashboard's × buttons,
# or directly:
kubectl exec -n ai-platform platform-db-0 -- psql -U platform -d platform_health_agent \
  -c "DELETE FROM investigations WHERE created_at > now() - interval '30 minutes'"
```

---

## Q&A talking points

**"How do you onboard a new model that's not on HuggingFace?"**
Mount it into the S3 weight cache (`ops/seed-model-cache.py` accepts arbitrary paths). The platform's vLLM init container syncs from S3 first; falls back to HuggingFace.

**"What about fine-tuning?"**
Same self-service pattern via the (in-progress) `FineTuneJob` resource — see [`docs/fine-tuning-implementation-plan-v2.md`](./fine-tuning-implementation-plan-v2.md). Unsloth on the same GPU nodes.

**"Why kiro-cli for the agent and not Bedrock directly?"**
Bedrock would work too. Kiro CLI's headless mode gives us the right tool-use abstraction (read files, write files, run kubectl) without writing our own agentic loop. The K8s RBAC enforces safety regardless of the LLM choice.

**"What if Kiro is down?"**
The agent's `event-watcher` keeps running and queueing events into postgres. Investigator Jobs fail; the next sync re-queues them via the postgres state. The platform itself (model serving) is not affected — the agent is purely observational + remediation.

**"How much does Kiro cost per investigation?"**
~$0.01-$0.05 depending on the model (`claude-sonnet-4.6` for investigations at 1.3x credits, `claude-opus-4.6` for remediations at 2.2x). Daily caps default to 50 investigations + 20 remediations = ~$2/day worst case.

**"Could a developer push a malicious YAML and have the agent execute arbitrary commands?"**
The agent only triggers on cluster *events* (CrashLoopBackOff, OOMKilled, etc.), not on git pushes. The investigator reads the LLM's structured JSON output. The remediator's RBAC is scoped to write only in `inference` + `team-*` namespaces. A malicious YAML cannot escalate beyond what those RBAC verbs allow.

**"What happens if the LLM proposes a bad fix?"**
The user clicks Dismiss. The fix is text-only on the dashboard until approved.

**"Why not commit the fix to git instead of applying imperatively?"**
Future work — would turn the agent into a GitOps actor (PR creator). For V1 we wanted to demonstrate the end-to-end approve-and-apply flow without git plumbing. Imperative changes are reverted by ArgoCD's `selfHeal: true` if they conflict with git, which is why the agent is restricted to non-ArgoCD-managed resources.

---

## Cheat sheet — URLs and commands

```bash
# ALB hostname (constant for this cluster)
ALB=k8s-aiplatform-0f70754cbc-22700774.eu-central-1.elb.amazonaws.com

# Dashboards / endpoints
echo http://$ALB:9090/   # Cluster Dashboard (operator)
echo http://$ALB:8080/   # Open WebUI (chat with models)
echo http://$ALB:4000/   # LiteLLM API (OpenAI-compatible)
echo http://$ALB:3000/   # Langfuse (traces)

# ArgoCD UI (Identity Center SSO)
echo https://1051989dd4426cf679b2d7864b74f7b528cf0e175300d9540.eks-capabilities.eu-central-1.amazonaws.com

# Quick model test
./ops/test-model.sh <model-name> "<prompt>"

# Watch a GPU node provision
kubectl get nodes -l karpenter.sh/nodepool=gpu-shared -w

# Dashboard's approvals API (what the UI calls)
curl -sS http://$ALB:9090/data.json | jq .approvals_pending
curl -sS http://$ALB:9090/investigations | jq .
curl -sS http://$ALB:9090/investigations/all | jq '.items[0:5]'

# Postgres state (audit trail)
kubectl exec -n ai-platform platform-db-0 -- \
  psql -U platform -d platform_health_agent -c \
  "SELECT created_at, status, trigger_kind, resource_namespace, resource_name FROM investigations ORDER BY created_at DESC LIMIT 10"
```

---

## Failure recovery during the demo

| Symptom | Mitigation |
|---------|------------|
| Dashboard shows "connecting…" | Hard refresh (`Cmd+Shift+R`). If still broken: `kubectl rollout restart deployment cluster-dashboard -n ai-platform` |
| Model deployment stuck > 3 min | `kubectl describe inferenceendpoint <name> -n inference` then check the underlying Ray pods. May indicate Karpenter can't find a GPU node — check `kubectl get nodeclaims`. |
| Agent doesn't fire | `kubectl logs -n platform-health-agent deploy/event-watcher --tail=20` — should show informer events. Often: a previous test's debounce row is still active. Clear: `kubectl exec -n ai-platform platform-db-0 -- psql -U platform -d platform_health_agent -c 'DELETE FROM debounce'` |
| Approve button does nothing | Browser dev tools → Network tab → POST `/investigations/<id>/approve`. If 503 → DB connection issue, restart dashboard pod. |
| ALB returns 403 | Your IP changed. Update `platform/config/ingress.yaml` and `platform/services/cluster-dashboard/manifests.yaml`, push. |
| Karpenter not provisioning a GPU | Check NodeClass + NodePool: `kubectl get nodepool gpu-shared -o yaml`. Check Karpenter logs: `kubectl logs -n kube-system deploy/karpenter --tail=30`. |

---

*End of demo runbook.*
