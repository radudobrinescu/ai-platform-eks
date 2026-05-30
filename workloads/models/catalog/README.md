# Model catalog

Platform-shipped models that come up automatically on a fresh install — the
"models out of the box" half of the turnkey platform. ArgoCD syncs this
directory (the `models` ApplicationSet recurses into `workloads/models/`), so
anything here is deployed by default.

| File | Endpoint (LiteLLM alias) | What it is |
|------|--------------------------|------------|
| [qwen3-3b.yaml](qwen3-3b.yaml) | `qwen3-3b` | Qwen2.5-3B-Instruct — ungated small model, the cheap self-hosted contender |

Bedrock's `claude-opus-4-8` (the frontier contender) is *not* here — it's a
static entry in the LiteLLM config (`platform/services/litellm/litellm.yaml`),
since Bedrock models have nothing to deploy or scale.

Together these two cover the money-demo's three-way comparison once a fine-tuned
variant is added:

| Contender | Source | Role |
|-----------|--------|------|
| `claude-opus-4-8` | Bedrock (LiteLLM config) | expensive generalist baseline |
| `qwen3-3b` | this catalog | cheap base small model (off-voice) |
| `qwen3-support-tuned` | a `FineTuneJob` with `autoDeploy: true` | the punchline — cheap *and* on-voice |

## Removing the default model

Don't want the small model running (e.g. a zero-GPU, Bedrock-only install)?
Delete `qwen3-3b.yaml` and commit — ArgoCD prunes the endpoint. The platform
still works against Bedrock with no GPUs.

## Adding your own models

Drop a regular `InferenceEndpoint` YAML in `workloads/models/` (see
`workloads/models/TEMPLATE.yaml.example`). Use this `catalog/` subdir only for
models the platform ships by default.
