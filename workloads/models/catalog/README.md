# Model catalog

Platform-shipped models that come up automatically on a fresh install — the
"models out of the box" half of the turnkey platform. ArgoCD syncs this
directory (the `models` ApplicationSet recurses into `workloads/models/`), so
anything here is deployed by default.

| File | Endpoint (LiteLLM alias) | What it is |
|------|--------------------------|------------|
| _(none shipped)_ | — | The catalog ships empty; add a small model to put a self-hosted contender next to Bedrock. |

Bedrock's `claude-opus-4-8` (the frontier contender) is a static entry in the
LiteLLM config (`platform/services/litellm/litellm.yaml`), since Bedrock models
have nothing to deploy or scale — so a fresh install serves against Bedrock with
no GPUs out of the box.

The money-demo's three-way comparison adds two self-hosted contenders on top:

| Contender | Source | Role |
|-----------|--------|------|
| `claude-opus-4-8` | Bedrock (LiteLLM config) | expensive generalist baseline |
| a small base model (e.g. Qwen2.5-3B) | drop an `InferenceEndpoint` here | cheap base small model (off-voice) |
| its fine-tuned variant (`*-tuned`) | a `FineTuneJob` with `autoDeploy: true` | the punchline — cheap *and* on-voice |

To run that demo, add a base small-model `InferenceEndpoint` (see below) and
fine-tune it with a `FineTuneJob`.

## Adding your own models

Drop a regular `InferenceEndpoint` YAML in `workloads/models/` (see
`workloads/models/TEMPLATE.yaml.example`). Use this `catalog/` subdir only for
models the platform ships by default.
