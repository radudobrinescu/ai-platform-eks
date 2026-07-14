# Conference Demo Walkthrough

A presenter's script for showcasing the platform on stage. **~30 minutes** for the
full arc; each act is self-contained, so you can cut to fit a 10/20/30-minute slot.

The narrative: *one OpenAI API, a frontier model live with zero GPUs, self-hosted
models deployed by `git push`, per-team budgets, and a fine-tuned 3B model that
matches the frontier model on a narrow task for a fraction of the cost — every
call traced live.*

> This walkthrough orchestrates the copy-paste blocks in
> [`ops/demo/demo.sh`](../ops/demo/demo.sh). Keep that file open in a second pane —
> it has the exact commands. This doc tells you **what to run, what to say, and what
> to do when something stalls.**

---

## Choose your length

| Slot | Acts to run | The point you land |
|------|-------------|--------------------|
| **10 min (lightning)** | 0 → 1 → 3 → 5 | "Frontier model, zero GPUs; deploy by git push; small+tuned beats big." |
| **20 min (breakout)** | 0 → 1 → 2 → 3 → 5 → 6 | Add live deploy + multi-tenancy. |
| **30 min (full)** | 0 → 6, plus Act 7 finale | Add observability deep-dive + optional self-healing. |

---

## Act 0 — Before you walk on stage (do this 10 min early)

Cold starts kill demos. Warm everything up first.

```bash
# 1. Bring the platform up (or confirm it's already up).
./platformctl status dev        # ArgoCD apps Synced/Healthy, pods Running

# 2. Confirm the frontier model + small model answer. This is your safety net —
#    if preflight is green, Acts 1 and 3 cannot fail.
./platformctl preflight         # checks Bedrock + every registered model

# 3. Pre-warm a GPU node so the LIVE deploy in Act 4 lands in seconds, not ~2 min.
./ops/demo/prepare-demo.sh      # provisions one gpu-shared node, pre-pulls the image

# 4. Open the tunnel and your browser tabs.
./platformctl tunnel            # WebUI :8080 · LiteLLM :4000 · Langfuse :3000
```

**Pre-flight checklist** (the demo assumes this state — see the header of
[`ops/demo/demo.sh`](../ops/demo/demo.sh)):

- [ ] `claude-opus-4-8` answers (Bedrock, zero GPUs — always on)
- [ ] `qwen3-3b` deployed and `READY` (the cheap contender; no models ship by
      default, so commit `Qwen/Qwen2.5-3B-Instruct` to `workloads/models/inference/`
      ahead of time)
- [ ] `qwen3-support-tuned` deployed (the tuned contender for Act 5's money demo —
      your fine-tuned weights served from S3 via `modelSource`; upload them ahead
      of time — the platform serves tuned models but does not train them)
- [ ] `llama32-1b` **NOT** deployed (you add it live in Act 4)
- [ ] Teams onboarded: `data-science` (all models) and `dev-team` (locked down)
- [ ] Browser tabs: **EKS Console** (cluster page), **ArgoCD UI**, **Open WebUI**
      (`:8080`), **LiteLLM UI** (`:4000/ui`), **Langfuse** (`:3000`),
      **Cluster Dashboard** (`:9090`)
- [ ] Terminal font ≥ 18pt, light-on-dark, `clear` between acts

> **Money-demo prep.** Have your fine-tuned weights in the model-cache bucket and
> serve them via a `VLLMEndpoint` with `modelSource` (no in-platform training), so
> `qwen3-support-tuned` is a live endpoint when you reach Act 5.

---

## Act 1 — Frontier model, zero GPUs (3 min) — *the hook*

**Say:** *"This is a self-service AI platform on EKS. Before I provision a single
GPU, I already have a frontier model live behind a standard OpenAI API."*

```bash
# Talk to Claude Opus 4.8 — served via Bedrock, no GPU in the cluster.
./ops/test-model.sh claude-opus-4-8 "Explain Kubernetes to a CFO in one sentence."
```

**Then** switch to **Open WebUI** (`:8080`) and chat with `claude-opus-4-8` live.

**Land it:** *"Zero GPUs running. This is a few lines of LiteLLM config pointed at
Bedrock — teams start building day one, and we provision GPUs only when they
actually need to self-host."*

Immediately switch to **Langfuse** (`:3000`) → open the trace from that call.

**Say:** *"And it's already observable. No setup, no restart — every call is traced
with cost, latency, and tokens from the very first request."*

---

## Act 2 — What's actually running (4 min) — *the foundation*

**Say:** *"None of this is bespoke glue. It's built on EKS Managed Capabilities —
ArgoCD, KRO, and ACK — that AWS runs for you."*

→ **EKS Console:** show the cluster's **Managed Capabilities** tab.
→ **ArgoCD UI:** *"The entire platform is defined in git. ArgoCD keeps the cluster
matching the repo — platform services and every model and team."*

```bash
# ACK: 55 AWS service controllers usable from Kubernetes.
kubectl api-resources | grep "services.k8s.aws" | awk -F'/' '{print $1}' | awk '{print $NF}' | sort -u

# KRO: the custom APIs the platform exposes.
kubectl get resourcegraphdefinitions
```

**Say:** *"KRO lets us define our own Kubernetes APIs. Three of them are the entire
self-service surface: `VLLMEndpoint` to serve a model (or `LLMDEndpoint` /
`LLMDDisaggEndpoint` for the llm-d scale tiers), `AITeam` to onboard a team. A
user writes a few lines of YAML; KRO expands it into the full resource graph — a
vLLM deployment + service, GPU workers, and a CloudWatch log group created via
ACK, with litellm-sync registering it on the gateway."*

```bash
kubectl get vllmendpoints -n inference \
  -o custom-columns=NAME:.metadata.name,READY:.status.ready,ENDPOINT:.status.endpoint
```

---

## Act 3 — One API, every model (3 min)

```bash
LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d)
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_KEY" | jq -r '.data[].id'
```

**Say:** *"Bedrock's Opus, the self-hosted Qwen, my fine-tuned model — all behind one
`/v1/chat/completions` endpoint. Anything that speaks OpenAI just works."*

→ **Open WebUI:** show the model dropdown; chat with `qwen3-3b` (now a GPU-backed
model) right after Opus — *"same API, different backend; the user can't tell."*

```bash
# The GPU nodes behind the self-hosted models — Bottlerocket, scale-to-zero when idle.
kubectl get nodes -l workload-type=gpu-inference \
  -o custom-columns=NAME:.metadata.name,INSTANCE:.metadata.labels.node\\.kubernetes\\.io/instance-type,GPU:.status.allocatable.nvidia\\.com/gpu
```

---

## Act 4 — Deploy a model, live, by git push (5–7 min) — *the "wow"*

**Say:** *"Watch how a developer ships a model. Same loop as shipping code."*

```bash
cat > workloads/models/inference/llama32-1b.yaml << 'EOF'
apiVersion: kro.run/v1alpha1
kind: VLLMEndpoint
metadata:
  name: llama32-1b
  namespace: inference
spec:
  model: "meta-llama/Llama-3.2-1B-Instruct"
  gpuCount: 1
  minReplicas: 1
  maxReplicas: 2
EOF

git add workloads/models/llama32-1b.yaml
git commit -m "feat: deploy Llama 3.2 1B Instruct"
git push origin main
```

**Say:** *"That's the whole interface. Now ArgoCD sees the commit…"*

→ **ArgoCD UI:** watch the `models` app sync.

```bash
kubectl get pods -n inference -l ray.io/cluster=llama32-1b -w   # Ctrl+C when worker is Running
```

**While it provisions, narrate the cold-start story** (you pre-warmed a node in
Act 0, so this is fast): *"Karpenter is provisioning a GPU node on demand. We keep
cold starts to about two minutes with Bottlerocket, SOCI lazy image loading, and an
S3 weight cache — and the node scales back to zero when idle."*

```bash
# ACK created a CloudWatch log group for the model automatically.
kubectl get loggroups -n inference
```

→ When `RUNNING`: refresh **Open WebUI**, `llama32-1b` appears — chat with it.

**Land it:** *"From `git push` to a live, observable, OpenAI-compatible endpoint —
no ticket, no console clicking."*

---

## Act 5 — The money demo: small + fine-tuned beats big (5 min) — *the payoff*

**Say:** *"Frontier models are great, but expensive at scale. The platform's job is
to let you prove when a small, fine-tuned model is good enough — with data, not
vibes."*

```bash
./platformctl compare \
  --dataset ops/sample-data/support-eval.jsonl \
  --models claude-opus-4-8,qwen3-3b,qwen3-support-tuned \
  --langfuse-dataset support-voice-eval \
  --self-hosted-model qwen3-support-tuned \
  --self-hosted-hf-id Qwen/Qwen2.5-3B-Instruct
```

**Say while it runs:** *"Same held-out questions through three contenders: the
frontier model, the base 3B, and a 3B fine-tuned on our support voice — served
here from S3 via `modelSource`, a `git push` just like any other model deploy."*

→ **Langfuse:** open the `support-voice-eval` dataset → **compare runs side-by-side**.
Point to: the tuned 3B matching Opus on voice/quality, with **far lower cost and
latency** per call.

**Land it — read the crossover the script prints:**

> *"The script computes the cost crossover — the daily request volume above which
> the self-hosted tuned model is cheaper per request than Bedrock. Below it, route
> to Opus; above it, route to the tuned 3B. Same API, same `AITeam` budgets — flip
> a model name. That's the decision this platform exists to make."*

> **No fine-tuned model handy?** Drop `qwen3-support-tuned` and run
> `--models claude-opus-4-8,qwen3-3b`. You still get the side-by-side + crossover;
> you just frame it as base-vs-frontier.

---

## Act 6 — Multi-tenant governance (4 min)

**Say:** *"This is shared infrastructure, so governance is built in. One `AITeam`
YAML creates a namespace, RBAC, a resource quota, a network policy, and a scoped
LiteLLM key with a budget and rate limit."*

```bash
cat workloads/teams/dev-team.yaml
kubectl get resourcequota -n team-dev
kubectl get networkpolicy -n team-dev
```

```bash
DS_KEY=$(kubectl get secret data-science-api-key -n team-data-science -o jsonpath='{.data.api-key}' | base64 -d)
DEV_KEY=$(kubectl get secret dev-api-key -n team-dev -o jsonpath='{.data.api-key}' | base64 -d)

# data-science (models: '*') → ALLOWED
echo ">>> data-science → claude-opus-4-8 (ALLOWED)"
curl -s http://localhost:4000/v1/chat/completions -H "Authorization: Bearer $DS_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-8","messages":[{"role":"user","content":"What is Kubernetes? One sentence."}]}' \
  | jq -r '.choices[0].message.content'

# dev-team (models: []) → BLOCKED at the gateway
echo ">>> dev-team → claude-opus-4-8 (BLOCKED)"
curl -s http://localhost:4000/v1/chat/completions -H "Authorization: Bearer $DEV_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-8","messages":[{"role":"user","content":"Hello"}]}' \
  | jq -r '.error.message'
```

**Say:** *"Same key model the cloud providers use — but it's your gateway, your
models, your budgets."* Then show spend:

```bash
curl -s http://localhost:4000/team/list -H "Authorization: Bearer $LITELLM_KEY" \
  | jq '[.[] | {team: .team_alias, spend: .spend, budget: .max_budget, models, rpm_limit}]'
```

→ **Cluster Dashboard** (`:9090`): show the live topology — nodes, GPU slots,
deployed models, recent activity. *"This is the operator's single pane of glass."*

---

## Act 7 — Optional finale: self-healing (4 min)

> Only if the **Platform Health Agent** is enabled (ships with cluster-dashboard
> but idles until a Kiro key is provisioned — see
> [its README](../platform/services/cluster-dashboard/PLATFORM-HEALTH-AGENT.md)).
> Skip otherwise.

**Say:** *"Day-two operations. The platform can watch itself, investigate failures
with an LLM, and propose a fix for one-click approval — no Slack, no PagerDuty."*

```bash
# Inject a realistic failure. 'configmap' is the best LLM-correlation demo.
./ops/demo/demo-failure.sh configmap
```

→ **Cluster Dashboard** (`:9090`): wait for the `🔔 N pending` badge (~90s). Open the
panel — show the **root cause, the proposed fix commands, and the severity**.

**Say:** *"The agent correlated the symptom to a ConfigMap key mismatch and wrote the
fix. Nothing is applied until a human clicks Approve — and RBAC blocks anything
destructive."* Click **Approve** → show the pod recover.

```bash
./ops/demo/demo-failure.sh cleanup    # tidy up after
```

---

## Closing (1 min)

**Say:**

- *"Frontier model live with **zero GPUs**, day one."*
- *"Deploy any model — and fine-tune one — by **`git push`**. ArgoCD does the rest."*
- *"**One OpenAI API** over Bedrock, self-hosted, and tuned models — with per-team
  budgets, keys, and rate limits."*
- *"We **proved** a tuned 3B matches a frontier model on a narrow task for a fraction
  of the cost — traced in Langfuse."*
- *"Built entirely on **EKS Managed Capabilities** — ArgoCD, KRO, ACK — plus Karpenter
  and vLLM. No bespoke control plane to maintain."*

> Repo: this README. Try it yourself: [`docs/quickstart.md`](quickstart.md).

```bash
# Reset the repo for the next run.
git rm workloads/models/llama32-1b.yaml && git commit -m "chore: remove demo model" && git push origin main
```

---

## When the demo gods are unkind

| Symptom | Recovery |
|---------|----------|
| GPU node won't provision in Act 4 | You pre-warmed in Act 0, so this is rare. Keep talking through the cold-start slide; if it truly stalls, pivot to *"here's one I deployed earlier"* and chat with `qwen3-3b` in Open WebUI. |
| `git push` blocked / no network | Apply locally: `kubectl apply -f workloads/models/llama32-1b.yaml`. Same KRO expansion; narrate that GitOps would normally drive it. |
| A model shows `NOT READY` | `./platformctl preflight` lists exactly what's wrong. Switch to a model that *is* ready — the API story is identical. |
| Langfuse trace not visible yet | Refresh; traces land within a second or two. Filter by `model` or by team metadata. |
| Bedrock call fails | Almost always model access not enabled in-account. `./platformctl preflight` prints the exact one-time console fix. Run preflight in Act 0 so you never hit this live. |
| Running short on time | Cut to the 10-min path (Acts 0→1→3→5). The hook and the payoff are what people remember. |

**Golden rule:** if `./platformctl preflight` was green in Act 0, Acts 1, 3, 5, and 6
cannot fail — they only call models that already answered. Lean on them.

---

## Likely audience questions

- **"Is the model serving production-grade?"** — vLLM with tensor parallelism
  (`gpuCount`), the llm-d scale tier for autoscaling replicas + KV/prefix-aware
  routing, and GPU time-slicing (`shared: true`) for small models. Karpenter
  reclaims GPU nodes when idle.
- **"How do cold starts not wreck UX?"** — Three layers: EBS image snapshots (0s
  pull), SOCI lazy loading, and an S3 weight cache (~15s load). ~2 min worst case;
  scale-to-zero is opt-in per workload.
- **"Can I bring my own model / data?"** — Any HuggingFace ID for serving; any
  JSONL (chat/ShareGPT/Alpaca) for fine-tuning. Fine-tuned weights land in S3 and
  deploy via `modelSource`.
- **"What's the lock-in?"** — It's your repo, your cluster, standard open components
  (vLLM, Ray, LiteLLM, ArgoCD, Langfuse). Fork it: change one repo URL.
- **"Cost control?"** — Per-team budgets + rate limits at the gateway, scale-to-zero
  GPUs, time-slicing, and the cost-crossover analysis to route traffic to the
  cheapest model that's good enough.
