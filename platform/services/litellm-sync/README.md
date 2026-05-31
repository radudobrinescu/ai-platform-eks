# litellm-sync

Keeps LiteLLM's model registry in sync with `InferenceEndpoint` custom
resources, so deleting a model actually removes it from LiteLLM's `/v1/models`.

## Why this exists

KRO's `-register` Job adds a model to LiteLLM (`POST /model/new`) when an
`InferenceEndpoint` is created. But KRO has **no delete-hook** — on instance
deletion it only tears down the sub-resources it created (RayService, the
register Job, the LogGroup) in reverse dependency order. Nothing deregisters
the model, so every removed model used to leave a dead entry behind, polluting
`/v1/models` and the Open WebUI / LiteLLM model pickers.

A finalizer is the only Kubernetes mechanism that runs cleanup *on delete*, and
a finalizer only clears if a controller removes it. This is that controller.

## How it works

A single-replica Deployment (no custom image — `python:3.12-slim` + an
initContainer that pip-installs the kubernetes client) running
[scripts/litellm_sync.py](scripts/litellm_sync.py):

1. **Watches** `inferenceendpoints.kro.run` in the `inference` namespace.
2. **Live endpoint, finalizer missing** → PATCH adds `litellm.ai-platform/deregister`.
3. **Endpoint with `deletionTimestamp`** → deregister the model from LiteLLM,
   then PATCH removes the finalizer so deletion completes. The finalizer is only
   dropped *after* deregistration succeeds; if LiteLLM is unreachable it stays put
   and retries.
4. **Reconcile every 10 min** (backstop): re-add finalizers to live endpoints and
   sweep orphaned DB-registered models whose endpoint no longer exists.

### Static models are protected

Deregistration only ever touches **DB-registered** models (`model_info.db_model ==
true`). Static config-file models declared in `litellm.yaml` — notably the Bedrock
`claude-opus-4-8` baseline, which has no `InferenceEndpoint` by design — report
`db_model: false` and are skipped. They can never be deleted by this controller.

## Operational notes

**Stuck `Terminating` (rare).** Because this uses a true finalizer, if the
controller is down at the exact moment an `InferenceEndpoint` is deleted, the
object stays in `Terminating` (and its ArgoCD app shows `Progressing`) until the
controller comes back — the reconcile loop and the Deployment's auto-restart make
this self-healing in practice. To force-unstick manually:

```bash
kubectl patch inferenceendpoint <name> -n inference \
  --type=json -p='[{"op":"remove","path":"/metadata/finalizers"}]'
```

(The reconcile sweep will then deregister the now-orphaned model on its next pass.)

**Relationship to `litellm-cleanup`.** This controller owns **model** consistency.
The hourly `litellm-model-cleanup` CronJob (`platform/config/litellm-cleanup.yaml`)
still owns **team** consistency (orphaned LiteLLM teams from deleted `AITeam`s) and
register-Job GC — it no longer touches models.

## Config (env vars, see `deployment.yaml`)

| Var | Default | Purpose |
| --- | --- | --- |
| `WATCH_NAMESPACE` | `inference` | Namespace of the InferenceEndpoints to watch |
| `LITELLM_BASE_URL` | `http://litellm.ai-platform.svc.cluster.local:4000` | LiteLLM admin API |
| `LITELLM_MASTER_KEY` | _(from `litellm-api-key` secret)_ | Bearer auth for the admin API |
| `FINALIZER` | `litellm.ai-platform/deregister` | Finalizer string the controller manages |
| `RECONCILE_INTERVAL_SEC` | `600` | Backstop reconcile cadence |
