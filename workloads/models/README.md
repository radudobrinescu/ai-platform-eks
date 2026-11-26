# Models — self-service deployment

Deploy a model with GitOps: add a `VLLMEndpoint` YAML and `git push`. ArgoCD
(the `workloads-models` ApplicationSet) discovers it, and the platform provisions
a GPU node, loads the model on vLLM, and registers it with the LiteLLM gateway
(centrally, via `litellm-sync`). Removal is `git rm`.

## Directory = namespace

`workloads/models/<namespace>/` — **each subdirectory is the target namespace**
the models inside it deploy into:

- `workloads/models/inference/` — the models of the platform (the `inference`
  namespace). **This is the only namespace that can serve models in this release.**
- `workloads/models/team-<name>/` — reserved for a team's own models. Argo CD
  already maps the directory to the team's namespace, but a model placed there
  does not start yet: its pods need the `inference-worker` service account and the
  `platform-config` ConfigMap, which exist only in `inference`. Until that is
  added, serve the model from `inference/` and give the team access to it with
  `./platformctl onboard-team <name> --models <model>`.

Do **not** set `metadata.namespace` in the model YAML — it inherits the
directory's namespace. Copy `TEMPLATE.yaml.example` into `workloads/models/inference/`,
e.g. `workloads/models/inference/qwen3-3b.yaml`.

For the scale / disaggregation tiers, use `workloads/scale-models/`
(`LLMDEndpoint` / `LLMDDisaggEndpoint`).

## Separate workloads repo (multi-team)

By default, models live in this repo. For real multi-team self-service, point
`var.gitops_workloads_repo_url` at a separate, tenant-owned repo: teams get write
access to the **workloads repo only** — never the platform repo. The
directory-per-namespace convention above is unchanged. `./platformctl new-model
--deploy` and `onboard-team --deploy` commit to this checkout, so with a separate
workloads repo you copy the printed manifest there yourself.

## Bedrock — self-service enrollment (no GPUs)

Amazon Bedrock models are enrolled with `./platformctl new-model --source bedrock
<model> --deploy`, which commits a `BedrockModel` CR (no GPUs, no serving pod) and
litellm-sync registers it on the `/v1` API. Nothing ships by default — list what's
invokable in your region/partition with `--source bedrock --list-available-models`.
To serve any open-source model instead, point a `VLLMEndpoint` at a HuggingFace
model ID — including a model you've fine-tuned and pushed to HF (private repos need
a token). The platform serves models; it does not train them.

## Tool / function calling

`platformctl new-model` auto-detects tool/function-calling support from the
model's architecture and writes `toolCallParser: <parser>` into the generated
`VLLMEndpoint` (e.g. `gemma4`, `hermes` for Qwen, `mistral`, `llama3_json`). The
model then answers `tool_choice: auto` requests — from Open WebUI or the API —
out of the box instead of erroring. It's written explicitly so you can see and
change it: delete the line for chat-only, or adjust it after a platform release
moves vLLM to a version that renames the parser (parser names are
vLLM-version-specific). Models from unrecognized
families deploy chat-only (no `toolCallParser`) — add one if the model
supports tools. For any other vLLM flag, use `--extra-arg`.
