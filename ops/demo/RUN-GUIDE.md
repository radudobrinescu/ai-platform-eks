# Demo Run Guide — Model Comparison + LLM-as-Judge + Teams API

Narrative: compare → judge → govern. Pick the cheapest model good enough for a
task, then enforce that choice with a team budget.

## Prerequisites (once, before demo)

```bash
# Tunnels (keep running in a separate terminal)
kubectl port-forward -n ai-platform svc/litellm 4000:4000 &
kubectl port-forward -n ai-platform svc/langfuse-web 3000:3000 &
kubectl port-forward -n ai-platform svc/open-webui 8080:8080 &

# Verify both models answer
./platformctl preflight
```

## Prep (once, ~20 min before demo — de-risks live timing)

1. Confirm the team key exists:
   ```bash
   kubectl get secret support-api-key -n team-support -o jsonpath='{.data.api-key}' | base64 -d; echo
   ```
2. Configure the Langfuse judge: follow `ops/demo/langfuse-evaluator-setup.md` steps 1-2.
3. Pre-run the comparison so the dataset + run exist as a fallback:
   ```bash
   ./platformctl compare \
     --dataset ops/demo/training-data/support-eval.jsonl \
     --models claude-opus-4-8,qwen3-4b-instruct-2507 \
     --langfuse-dataset support-eval
   ```
4. Apply the judge to the runs: `langfuse-evaluator-setup.md` steps 3-4.

---

## Live demo

### Act 1 — Onboard the team (Teams API)

```bash
# Show the YAML — the entire team interface
cat workloads/teams/support-team.yaml

# It's already deployed via ArgoCD. Show what it created:
kubectl get ns team-support
kubectl get resourcequota,networkpolicy -n team-support

# The scoped API key:
export TEAM_KEY=$(kubectl get secret support-api-key -n team-support -o jsonpath='{.data.api-key}' | base64 -d)

# Prove it works AND is scoped to allowed models + budget:
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $TEAM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen3-4b-instruct-2507","messages":[{"role":"user","content":"How do I reset my password?"}]}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['choices'][0]['message']['content'])"
```
Talking point: $50/mo budget, 60 rpm, only 2 allowed models — enforced at the gateway.

### Act 2 — Compare the models

```bash
./platformctl compare \
  --dataset ops/demo/training-data/support-eval.jsonl \
  --models claude-opus-4-8,qwen3-4b-instruct-2507 \
  --langfuse-dataset support-eval
```
Then in Langfuse (`http://localhost:3000`): **Datasets → support-eval → Runs**.
Talking point: same 10 questions, one endpoint, one key. Cost/latency/tokens traced automatically — Opus ~50x the per-request cost of qwen3-4b.

### Act 3 — Let the judge decide

Langfuse → **Datasets → support-eval → Compare Runs**.
Show side-by-side answers + `support-answer-quality` scores per model.
Talking point: on this narrow triage task, qwen3-4b scores within ~1 point of Opus — good enough — at a fraction of the cost. The judge decided, not us.

### Act 4 — Governance enforces it (optional punch)

```bash
# Show team usage / spend in LiteLLM (filtered by team)
# Or trigger the rate limit to prove the guardrail is real:
for i in $(seq 1 70); do
  curl -s -o /dev/null -w "%{http_code}\n" http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $TEAM_KEY" -H "Content-Type: application/json" \
    -d '{"model":"qwen3-4b-instruct-2507","messages":[{"role":"user","content":"hi"}]}'
done | sort | uniq -c
```
Talking point: 200s then 429s — the rpm limit is real. The model decision is enforced by policy, not trust.

---

## Reset between runs

```bash
# Re-running compare with the same dataset name appends a new run (fine).
# To clear rate-limit state, wait 60s. Budget resets per budgetDuration.
```
