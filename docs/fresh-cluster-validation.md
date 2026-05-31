# Fresh-Cluster Validation Runbook

Prove the platform is genuinely **turnkey** on a clean `terraform apply` — before
you rely on it for a demo. Every check here maps to a real failure mode that can
silently break the "value on first boot" promise. Run it once after provisioning a
new cluster.

> **One command covers most of this:** `./platformctl preflight` now asserts
> Bedrock access **and** the full Langfuse tracing path (keys authenticate + a
> test trace lands). The steps below are the manual equivalent + the few things
> preflight can't see (headless-init timing, GitOps fork correctness).

## 0. Prerequisites checked before you trust "turnkey"

The single most common turnkey breaker is an **incomplete fork**. ArgoCD
ApplicationSet generators can't read a Terraform variable, so the git repo URL
lives in **three** places that must all agree:

```bash
# All three must point at YOUR fork — no YOUR-ORG/YOUR-REPO placeholders left.
grep -rn 'YOUR-ORG/YOUR-REPO' argocd/bootstrap/ terraform/00.global/vars/<env>.tfvars
# Expect: no matches. Any match = ArgoCD will fail to sync every child app.
```

If that grep finds anything, fix it before applying (see the fork checklist in
`argocd/bootstrap/platform.yaml`). This is the bug that, left half-done, makes
*nothing* sync — Langfuse included.

## 1. GitOps actually synced

```bash
kubectl get applications -n argocd \
  -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status
```

**Pass:** every app `Synced` / `Healthy`. **Fail signature:** apps stuck
`Unknown` with `ComparisonError: failed to get git client for repo
.../YOUR-ORG/YOUR-REPO.git` → the fork wasn't completed (step 0).

## 2. Langfuse headless-init actually ran

The turnkey promise is that Terraform generates the project keys and Langfuse
creates the org/project/admin **on first boot**. Verify the project exists —
not just that the pod is up:

```bash
# The DB must show one org, one project, one API key, one admin user.
kubectl exec -n ai-platform platform-db-0 -- \
  psql -U platform -d langfuse -c \
  "SELECT 'orgs' t,count(*) FROM organizations
   UNION ALL SELECT 'projects',count(*) FROM projects
   UNION ALL SELECT 'api_keys',count(*) FROM api_keys
   UNION ALL SELECT 'users',count(*) FROM users;"
```

**Pass:** all counts ≥ 1. **Fail signature:** all zeros → headless init never
ran. Root cause is almost always **timing**: the `langfuse-web` container booted
*before* Terraform created the `langfuse-init` Secret. The platform now guards
against this — the init env uses `secretKeyRef` (not optional `envFrom`), so a
pod scheduled too early **blocks** (`CreateContainerConfigError`) and retries
until the Secret lands, then inits correctly. If you still see zeros, force a
re-init once the Secret exists:

```bash
kubectl get secret langfuse-init -n ai-platform >/dev/null && \
  kubectl rollout restart deploy/langfuse-web -n ai-platform
```

## 3. The keys LiteLLM holds match the project (and authenticate)

```bash
PK=$(kubectl get secret langfuse-litellm-keys -n ai-platform -o jsonpath='{.data.LANGFUSE_PUBLIC_KEY}' | base64 -d)
INIT_PK=$(kubectl get secret langfuse-init -n ai-platform -o jsonpath='{.data.LANGFUSE_INIT_PROJECT_PUBLIC_KEY}' | base64 -d)
[ "$PK" = "$INIT_PK" ] && echo "keys match" || echo "MISMATCH — LiteLLM will trace into the wrong/nonexistent project"
```

## 4. The full trace write path is healthy

Tracing dies silently if the **blob store** (MinIO) is down or **ClickHouse** is
full — the model call still returns 200, the trace just never appears. Preflight
exercises this end to end:

```bash
./platformctl preflight
# Expect, in the Langfuse section:
#   [ok] Langfuse reachable
#   [ok] Langfuse project keys — authenticated (project: AI Platform)
#   [ok] Langfuse trace write path — ingestion accepted the trace (write path healthy)
```

If the write-path check fails, inspect the two usual culprits:

```bash
kubectl get deploy langfuse-s3 -n ai-platform        # MinIO must be 1/1 (not 0/0)
kubectl exec -n ai-platform langfuse-clickhouse-shard0-0 -- df -h /bitnami/clickhouse
```

> **Operational note (not a fresh-boot issue):** `ops/scale-down.sh` zeros every
> Deployment; `ops/scale-up.sh` now explicitly restores `langfuse-s3` because
> ArgoCD won't self-heal its replica count (the chart omits the field).
> ClickHouse's own `system.trace_log` is capped with a 3-day TTL so it can't fill
> the disk over time. Both are handled — listed here only so you know where to
> look if tracing degrades on a long-lived cluster.

## 5. End-to-end: a real model call is traced

```bash
LITELLM_KEY=$(kubectl get secret litellm-secrets -n ai-platform -o jsonpath='{.data.master-key}' | base64 -d)
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" -H "Content-Type: application/json" \
  -d '{"model":"claude-opus-4-8","messages":[{"role":"user","content":"validation"}],"max_tokens":8}' >/dev/null

# Wait ~30s for the async pipeline (LiteLLM flush → ingestion → worker → ClickHouse),
# then confirm the trace landed:
PK=$(kubectl get secret langfuse-litellm-keys -n ai-platform -o jsonpath='{.data.LANGFUSE_PUBLIC_KEY}' | base64 -d)
SK=$(kubectl get secret langfuse-litellm-keys -n ai-platform -o jsonpath='{.data.LANGFUSE_SECRET_KEY}' | base64 -d)
curl -s -u "$PK:$SK" "http://localhost:3000/api/public/traces?limit=5" | python3 -m json.tool
```

**Pass:** at least one `litellm-acompletion` trace with model, tokens, and cost.
That's the money-demo's observability working from a cold provision.

## Sign-off

All five green = the platform is turnkey on this cluster. If you want this gated
in CI, run `./platformctl preflight` as a post-apply step and fail the pipeline on
a non-zero exit.
