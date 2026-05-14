# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Self-service AI inference platform on Amazon EKS. Teams deploy LLM models by committing a short YAML to `workloads/` — the platform handles GPU provisioning (Karpenter), model serving (Ray Serve + vLLM), API routing (LiteLLM), and observability (Langfuse).

Built on EKS Managed Capabilities: ArgoCD (GitOps), KRO (custom resource expansion), ACK (AWS resource management).

## Key Architecture Concepts

**Two custom resources drive everything:**
- **InferenceEndpoint** (`platform/config/kro/inference-endpoint.yaml`) — teams specify a HuggingFace model ID; KRO expands it into a RayService with vLLM backend, GPU worker pods, a LiteLLM registration Job, and a CloudWatch Log Group
- **AITeam** (`platform/config/kro/team-onboarding.yaml`) — creates namespace, RBAC, ResourceQuota, NetworkPolicy, LiteLLM team + scoped API key

**GitOps flow:** `git push` → ArgoCD syncs → KRO expands custom resource → Karpenter provisions GPU node → vLLM loads model → LiteLLM registers endpoint

**ArgoCD bootstrap:** Terraform renders a single root `Application` pointing at `argocd/bootstrap/` in the git repo (URL from `var.gitops_repo_url`). That directory contains two ApplicationSets:
- `argocd/bootstrap/platform.yaml` — creates Applications for `platform-config`, `gpu-operator`, `kuberay-operator`, `litellm`, `open-webui`, `langfuse`
- `argocd/bootstrap/workloads.yaml` — creates Applications for `models` and `teams` (self-service)

**Karpenter NodePools:**
- `default` — x86 compute (c/m/r families) for system add-ons
- `gpu-inference` — dedicated GPU instances (Bottlerocket + SOCI), scales to 0 when idle
- `gpu-shared` — time-sliced GPU instances (1 physical GPU exposed as 4 virtual), for small models

## Terraform Commands

All terraform operations run from the `terraform/` directory via Make:

```bash
cd terraform

# Bootstrap state backend (S3 + DynamoDB)
make bootstrap

# Deploy all modules in order
make ENVIRONMENT=dev apply-all

# Single module operations
make ENVIRONMENT=dev MODULE=./30.eks/30.cluster plan
make ENVIRONMENT=dev MODULE=./30.eks/30.cluster apply

# List discovered modules
make print-modules

# Tear down (reverse order)
make ENVIRONMENT=dev destroy-all
```

Environment config lives in `terraform/00.global/vars/{environment}.tfvars`. Modules are auto-discovered by finding `backend.tf` files. Each module gets its own Terraform workspace matching the environment name.

ECR pull-through cache credentials are passed via env vars, never committed:
```bash
export TF_VAR_docker_hub_username="..."
export TF_VAR_docker_hub_access_token="..."
```

## Terraform Module Order

Modules deploy in directory sort order (this matters for dependencies):
1. `10.networking` — VPC, subnets, NAT gateways, VPC endpoints
2. `20.iam-roles-for-eks` — IAM roles for cluster access personas
3. `30.eks/30.cluster` — EKS cluster, managed node group, Karpenter NodePools, capabilities, platform secrets
4. `30.eks/35.addons` — EKS Blueprints add-ons (ALB controller, cert-manager, external secrets)
5. `40.observability/*` — AWS-native or OSS observability stacks

## Operational Scripts

```bash
./ops/test-model.sh gemma-4b "What is Kubernetes?"   # Quick model test
./ops/ssm-tunnel.sh                                   # Port-forward to services (WebUI :8080, LiteLLM :4000, Langfuse :3000)
./ops/scale-down.sh                                   # Suspend platform, reclaim GPU nodes
./ops/scale-up.sh                                     # Re-enable via ArgoCD sync
./ops/create-soci-index.sh <ecr-image-uri>            # Create SOCI index for faster cold starts
./ops/create-data-volume-snapshot.sh <image>          # EBS snapshot with pre-pulled images (fastest cold start)
./ops/recommend-instance.py <model-id>                # Recommend GPU instance type + VRAM for a model
./ops/seed-model-cache.py <model-id>                  # Pre-populate S3 model weight cache
./ops/demo.sh                                         # End-to-end demo flow (deploy model + query)
```

## Deploying a New Model

```bash
cp workloads/models/TEMPLATE.yaml.example workloads/models/my-model.yaml
# Edit: set spec.model to HuggingFace model ID, adjust gpuCount/replicas as needed
git add workloads/models/my-model.yaml && git commit && git push
kubectl get inferenceendpoints -n inference -w   # Track progress
```

`gpuCount` must be 1, 2, 4, or 8 (vLLM tensor parallelism constraint).

## Platform Services Layout

- `platform/config/kro/` — KRO ResourceGraphDefinitions (the core API abstractions)
- `platform/config/rbac/` — team-developer ClusterRole
- `platform/config/ingress.yaml` — Internal ALB routing
- `platform/config/warm-pool/` — GPU warm-pool placeholder (keeps one node pre-provisioned)
- `platform/services/litellm/` — LiteLLM proxy + shared PostgreSQL (raw manifests)
- `platform/services/open-webui/` — Open WebUI (raw manifests)
- `platform/services/kuberay/` — KubeRay operator (Helm values)
- `platform/services/gpu-operator/` — NVIDIA GPU Operator + DCGM exporter (Helm values, custom metrics)
- `platform/services/langfuse/` — Langfuse (Helm values)

## Key Configuration Sources

- **Ray LLM image version**: single source of truth in `terraform/30.eks/30.cluster/locals.tf` (`ray_image_tag`), propagated via `platform-config` ConfigMap. KRO reads it via `externalRef` with `.orValue()` fallback to Docker Hub.
- **Cluster capabilities** (ArgoCD, KRO, ACK, autoscaling): toggled in tfvars under `cluster_config.capabilities`
- **Platform secrets** (LiteLLM master key, DB password, Langfuse keys): generated by Terraform in `30.cluster`, stored as Kubernetes Secrets
- **HuggingFace token**: manually created `hf-token` Secret in `inference` namespace (required for gated models)
- **GPU data volume snapshot**: optional `gpu_data_volume_snapshot_id` tfvar in `30.cluster` — pre-pulled container images on EBS (see GPU Cold-Start Optimization)
- **EBS CSI driver**: addon configured with `snapshotter.forceEnable = false` (snapshot sidecar disabled since VolumeSnapshot CRDs aren't installed)
- **DCGM exporter**: custom metrics in `platform/services/gpu-operator/helm-values.yaml` — profiling fields excluded for time-slicing compatibility

## GPU Time-Slicing

Setting `shared: true` on an InferenceEndpoint schedules it on the `gpu-shared` NodePool, where NVIDIA time-slicing allows up to 4 models to share one physical GPU. Only suitable for small models — use the VRAM calculator to verify fit. Without `shared`, models get a dedicated `gpu-inference` node.

## GPU Cold-Start Optimization

Three layers reduce time-to-first-inference for new model deployments:

1. **EBS Data Volume Snapshot** (fastest): Pre-pulled Ray LLM image baked into an EBS snapshot. GPU nodes boot with the 13 GiB image already on disk → 0s image pull. **Automated by Terraform** — `null_resource` in `image-optimization.tf` triggers when `ray_image_tag` changes. Snapshot discovered by tags on subsequent applies.

2. **SOCI lazy-loading** (always active): Bottlerocket's containerd uses the SOCI snapshotter to stream image layers on demand. Falls back gracefully when no SOCI index exists or when the snapshot is stale. **Automated by Terraform** — SOCI index created alongside the snapshot.

3. **S3 model weight cache** (always active): An initContainer syncs model weights from S3 (~15s) instead of downloading from HuggingFace (~60s). Auto-populated by the worker sidecar after first load.

**Automation flow** (defined in `terraform/30.eks/30.cluster/image-optimization.tf`):
- Triggered by changes to `ray_image_tag` in `locals.tf`
- Requires ECR pull-through cache (`TF_VAR_docker_hub_username` set)
- First `terraform apply`: creates SOCI index + EBS snapshot (nodes use SOCI only)
- Second `terraform apply`: discovers snapshot, updates Karpenter NodeClasses (nodes boot with image on disk)

**Manual override**: set `gpu_data_volume_snapshot_id` in tfvars to pin a specific snapshot.

**Manual scripts** (for ad-hoc use or CI pipelines):
```bash
./ops/create-soci-index.sh <ecr-image-uri>
./ops/create-data-volume-snapshot.sh --write-tfvars <image>
```

## ArgoCD Bootstrap Structure

Terraform renders exactly one ArgoCD `Application` (named `bootstrap`) pointing at `argocd/bootstrap/` in the git repo. That directory contains two ApplicationSets:

- `argocd/bootstrap/platform.yaml` — ApplicationSet with six platform apps (`platform-config`, `gpu-operator`, `kuberay-operator`, `litellm`, `open-webui`, `langfuse`). Plain-directory and Helm-multi-source app shapes handled via a single templatePatch.
- `argocd/bootstrap/workloads.yaml` — ApplicationSet for the two self-service directories (`workloads/models/` → `models` app, `workloads/teams/` → `teams` app).

Adding a new platform service = add its manifests (in `platform/services/`) and a new element to the list generator in `argocd/bootstrap/platform.yaml`. Don't add new Application YAMLs anywhere else — the ApplicationSet owns them.

All Applications have `automated.prune: true` + `automated.selfHeal: true` — ArgoCD will revert manual kubectl changes. Use `ServerSideApply=true` to avoid ownership conflicts.

Forking = change `gitops_repo_url` in tfvars + edit the 3 literal URLs in `argocd/bootstrap/{platform,workloads}.yaml`.

## Observability Modules

Two mutually exclusive stacks in `terraform/40.observability/`:
- `40.aws-native-observability` — CloudWatch Container Insights, X-Ray
- `45.aws-oss-observability` — Amazon Managed Prometheus + Grafana, ADOT collector

Toggled via `observability_configuration` in tfvars. Both can be disabled.

## KRO YAML Gotchas

KRO ResourceGraphDefinitions use CEL expressions (`${...}`) that interact poorly with YAML:
- **Ternary expressions** containing `:` must be quoted — YAML interprets unquoted colons as key-value separators. Example: `"${expr ? 'a' : 'b'}"` not `${expr ? 'a' : 'b'}`
- **String conversion**: use `${string(schema.spec.intField)}` when embedding integers in string contexts (e.g. serveConfigV2 YAML)
- **Optional chaining with fallback**: `${obj.?field.orValue('default')}` — the `?` prevents null-pointer when the field doesn't exist

## Conventions

- Workload YAMLs in `workloads/` are the self-service interface — keep them minimal (teams should only need `spec.model` and optionally `gpuCount`)
- KRO definitions in `platform/config/kro/` are the platform's core logic — changes here affect all models/teams
- Registration Jobs are idempotent (check-then-create pattern)
- Team onboarding Jobs are idempotent (find existing team by alias, update on re-sync)
- All platform services run in the `ai-platform` namespace; inference workloads in the `inference` namespace; teams get `team-{teamName}` namespaces
- Karpenter NodePool GPU limit is 16 GPUs total (defined in `terraform/30.eks/30.cluster/karpenter/gpu-inference.yaml`)
- ArgoCD uses `ServerSideApply=true` on all Applications — avoid `kubectl apply` conflicts
