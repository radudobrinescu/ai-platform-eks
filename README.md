# AI Platform on EKS

A self-service AI platform that lets teams deploy and serve LLMs on Amazon EKS through GitOps. Commit a short YAML, get a production-ready inference endpoint — the platform handles GPU provisioning, model serving, API routing, team isolation, observability, and **autonomous incident response**.

**Core stack:** EKS Managed Capabilities (ArgoCD, KRO, ACK) · Karpenter · Ray Serve · vLLM · LiteLLM · Langfuse · Platform Health Agent

## What Teams Get

| Capability | How it works |
|-----------|-------------|
| **Frontier model out of the box** | Amazon Bedrock **Claude Opus 4.8** behind the same API — zero GPUs on day one (`enable_bedrock`) |
| **Deploy any HuggingFace model** | Commit an `InferenceEndpoint` YAML → model is live in ~60s |
| **Preconfigured small model** | Qwen2.5-3B-Instruct (ungated) ships in the catalog and serves on first boot |
| **OpenAI-compatible API** | LiteLLM proxies all models behind `/v1/chat/completions` |
| **Chat UI** | Open WebUI for interactive testing |
| **Team isolation** | `AITeam` resource creates namespace, RBAC, budget, rate limits, scoped API key |
| **Auto GPU sizing** | Karpenter provisions the right GPU, scales to zero when idle |
| **Fast cold starts** | EBS snapshots (0s image pull) + S3 weight cache (~15s) |
| **Observability on first boot** | Langfuse tracing live on the first request — keys provisioned by Terraform, no manual setup |
| **Fine-tuning** | `FineTuneJob` resource — self-service QLoRA via Unsloth, `autoDeploy` to a live endpoint |
| **Model comparison** | `ops/compare-models.py` runs an eval set through several models → side-by-side Langfuse dataset run + cost crossover |
| **Cluster topology dashboard** | Live view of nodes, pods, models, and pending platform-health approvals |
| **Autonomous incident response** | Platform Health Agent watches the cluster, investigates failures with kiro-cli, proposes a fix, applies after one-click approval |

## How It Works

```
git push → ArgoCD syncs → KRO expands custom resource
         → Karpenter provisions GPU node (Bottlerocket + SOCI)
         → vLLM loads model → LiteLLM registers endpoint
         → model available via API and Open WebUI
```

## Architecture

```
EKS Cluster
├── Managed Capabilities (AWS-hosted)
│   ├── ArgoCD       → syncs platform + workloads from Git
│   ├── KRO          → expands custom resources into K8s objects
│   └── ACK          → manages AWS resources from Kubernetes
│
├── Karpenter NodePools
│   ├── default          → platform nodes (AL2023, Graviton)
│   ├── gpu-inference    → dedicated GPU nodes (Bottlerocket + SOCI)
│   └── gpu-shared       → time-sliced GPU nodes (4 models per GPU)
│
├── Platform Services
│   ├── GPU Operator         → NVIDIA device plugin + DCGM metrics
│   ├── KubeRay              → Ray cluster lifecycle
│   ├── LiteLLM              → OpenAI-compatible API gateway
│   ├── Open WebUI           → chat interface
│   ├── Platform DB          → shared PostgreSQL (litellm, langfuse, platform_health_agent)
│   ├── Langfuse             → LLM tracing and analytics
│   ├── Cluster Dashboard    → topology view + Platform Health Agent approvals UI
│   └── Platform Health Agent → autonomous incident investigation/remediation
│
└── Workloads (teams self-serve)
    ├── InferenceEndpoints
    └── AITeams
```

---

## Quick Start

> **Turnkey path:** the [quickstart guide](docs/quickstart.md) walks the whole
> journey — provision → use Sonnet with zero GPUs → fine-tune → prove it with a
> Langfuse comparison — using the thin `./platformctl` wrapper. The steps below
> are the underlying commands.

### 1. Deploy Infrastructure

```bash
cd terraform/00.global/vars/
cp example.tfvars your-env.tfvars
# Edit: set Identity Center ARN, VPC CIDR, gitops_repo_url, capabilities

cd terraform
export AWS_REGION=eu-central-1
make bootstrap ENVIRONMENT=your-env
make ENVIRONMENT=your-env apply-all
```

This creates VPC, IAM roles, EKS cluster with managed capabilities, Karpenter NodePools, and all platform secrets. Cluster name: `{resources_prefix}-{ENVIRONMENT}`.

### 2. (Optional) GPU Image Optimization

Enable ECR pull-through cache for faster image pulls:

```bash
export TF_VAR_docker_hub_username="your-dockerhub-username"
export TF_VAR_docker_hub_access_token="dckr_pat_XXXXXXXXXX"
```

Terraform automatically creates a SOCI index + EBS data volume snapshot on `apply`. Two-apply flow for new clusters — second apply wires the snapshot into Karpenter NodeClasses.

### 3. (Optional) Enable the Platform Health Agent

The agent is opt-in. To turn it on:

```bash
# In your tfvars:
platform_health_agent_enabled = true

# In your shell, before terraform apply:
export TF_VAR_kiro_api_key="kr-..."   # get from https://kiro.dev/
```

Terraform provisions the agent's namespace, the Kiro API key Secret, and a copy of `platform-db-credentials`. ArgoCD then deploys the agent itself from `platform/services/platform-health-agent/`. See [`platform/services/platform-health-agent/README.md`](platform/services/platform-health-agent/README.md) for design and operation.

### 4. Verify ArgoCD

ArgoCD is deployed as a managed capability — no manual bootstrap needed. Terraform renders a bootstrap Application pointing at `argocd/bootstrap/` with your `gitops_repo_url`.

```bash
aws eks update-kubeconfig --region $AWS_REGION --name ai-platform-your-env
kubectl get applications -n argocd
```

### 5. Create HuggingFace Token (for gated models)

```bash
kubectl create secret generic hf-token -n inference \
  --from-literal=token=hf_YOUR_TOKEN_HERE
```

### 6. Deploy a Model

```bash
cp workloads/models/TEMPLATE.yaml.example workloads/models/my-model.yaml
```

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: gemma-4b
  namespace: inference
spec:
  model: "google/gemma-3-4b-it"
  gpuCount: 1
```

```bash
git add workloads/models/my-model.yaml
git commit -m "feat: deploy gemma-4b"
git push
kubectl get inferenceendpoints -n inference -w
```

### 7. Access Services

The platform exposes services via an internet-facing ALB restricted by IP allowlist:

- `http://<alb>:8080` — Open WebUI (chat with models)
- `http://<alb>:4000` — LiteLLM API (OpenAI-compatible)
- `http://<alb>:3000` — Langfuse (tracing)
- `http://<alb>:9090` — Cluster Dashboard (live topology + Platform Health Agent approvals)

```bash
# Get ALB hostname
kubectl get ingress ai-platform-litellm -n ai-platform \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

# Or use SSM tunnel (works from anywhere, no public IP needed)
./ops/ssm-tunnel.sh    # localhost:8080 / :4000 / :3000 / :9090
```

Update the allowlist in `platform/config/ingress.yaml` (and `platform/services/cluster-dashboard/manifests.yaml`) when your IP changes.

### 8. Test

```bash
# qwen3-3b ships in the default catalog; or use the model you deployed above.
./ops/test-model.sh qwen3-3b "What is Kubernetes?"
```

---

## Fine-tuning

Self-service QLoRA via Unsloth, same `git push` loop. Upload a dataset, commit a
`FineTuneJob`, and (with `autoDeploy: true`) the tuned model becomes a live
LiteLLM endpoint. Enabled by `enable_fine_tuning` (default on).

```bash
DATASETS_BUCKET=$(kubectl get cm platform-config -n inference -o jsonpath='{.data.trainingDatasetsBucket}')
aws s3 cp support-transcripts.jsonl s3://$DATASETS_BUCKET/

cp workloads/fine-tuning/TEMPLATE.yaml.example workloads/fine-tuning/qwen3-support-tuned.yaml
# edit: dataset=s3://$DATASETS_BUCKET/support-transcripts.jsonl, autoDeploy: true
git add workloads/fine-tuning/qwen3-support-tuned.yaml && git commit -m "feat: support-voice tune" && git push
kubectl get finetunejobs -n inference -w
```

Full guide: [docs/fine-tuning-getting-started.md](docs/fine-tuning-getting-started.md).

## The money demo — compare models

Run the same eval set through Sonnet, the base small model, and the fine-tuned
small model, and let Langfuse show that a small fine-tuned model can match a
large commercial one at a fraction of the cost.

```bash
./ops/compare-models.py \
  --dataset ops/sample-data/support-eval.jsonl \
  --models claude-opus-4-8,qwen3-3b,qwen3-support-tuned \
  --langfuse-dataset support-voice-eval \
  --self-hosted-model qwen3-support-tuned --self-hosted-hf-id Qwen/Qwen2.5-3B-Instruct
```

Every call is traced (cost/latency/tokens) and tagged as a Langfuse **dataset
run**, so you compare runs side by side; configure an LLM-as-judge evaluator for
voice/policy/helpfulness scoring. The script prints the **cost crossover** — the
daily request volume above which the self-hosted tuned model is cheaper than
Sonnet (reusing `recommend-instance.py`'s pricing model). See
[docs/quickstart.md](docs/quickstart.md) and
[docs/platform-evolution-plan.md](docs/platform-evolution-plan.md).

`./ops/compare-models.py --preflight` checks connectivity and Bedrock model
access, printing the exact fix if Sonnet isn't enabled in your account.

---

## Cluster Dashboard

`http://<alb>:9090` shows a live (2s polling) view of the cluster:

- **Cluster topology** — nodes, pods, GPU allocation, deployed models, recent activity
- **🔔 Approvals badge** in the topbar — appears when the Platform Health Agent has investigations awaiting approval
- **Pending tab** — proposed fixes with severity, root cause, fix commands, impact analysis (parses kubectl verbs to predict reversibility/disruption), risk
- **History tab** — last 20 investigations with outcome chips (✓ Applied / ⚠ Verify failed / ✕ Failed / Dismissed). Each row expands to show full proposed fix, post-fix status, rollback commands, error messages. Includes an `×` button to permanently delete a row from the audit trail.

The dashboard is the primary operator UX. No external messaging system (Slack, email) required.

---

## GPU Instance Recommender

The built-in recommender reads model architecture from HuggingFace, estimates VRAM, models decode throughput, picks the cheapest GPU with the right parallelism strategy, and emits a ready-to-commit YAML:

```bash
# What GPU fits a 4B model?
./ops/recommend-instance.py google/gemma-3-4b-it

# 8B model, 16K context, 8 concurrent users
./ops/recommend-instance.py meta-llama/Llama-3.1-8B-Instruct --seq 16384 --users 8

# Quantised 32B — int4 halves VRAM
./ops/recommend-instance.py Qwen/Qwen2.5-32B-Instruct --quant int4

# 100 users with 25 tok/s SLO — auto-scales to fleet if needed
./ops/recommend-instance.py Qwen/Qwen2.5-7B-Instruct --users 100 --target-tok-s 25

# Gated model
HF_TOKEN=hf_... ./ops/recommend-instance.py google/gemma-3-27b-it
```

Key flags: `--quant`, `--kv-quant`, `--seq`, `--users`, `--tp`, `--target-tok-s`, `--max-price`, `--in-cluster-only`, `--json`, `--workload`. Run with `--help` for full documentation.

---

## Custom Resource Reference

### InferenceEndpoint

```yaml
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata:
  name: my-model
  namespace: inference
spec:
  model: "org/model-id"           # REQUIRED — HuggingFace model ID
  modelSource: ""                 # Optional — S3 prefix of a fine-tuned model (relative to the
                                  # model-cache bucket). When set, the init container syncs from
                                  # there and vLLM loads the local path instead of pulling `model`
                                  # from HF. Set automatically by FineTuneJob autoDeploy.
  gpuCount: 1                     # GPUs per worker (1, 2, 4, or 8)
  tensorParallelSize: 1           # TP — shards each layer's weights across GPUs (prefers NVLink)
  pipelineParallelSize: 1         # PP — assigns layer groups to pipeline stages (works on any interconnect)
  shared: false                   # Time-slice GPU with up to 4 models
  minReplicas: 1
  maxReplicas: 4
  maxModelLen: 8192               # Max sequence length
  minVramPerGpuGiB: 0             # Karpenter GPU sizing hint (0 = unconstrained)
  workerMemory: "12Gi"            # CPU memory per Ray worker
  workerCpu: "4"
```

KRO expands this into: RayService (vLLM backend), GPU worker pods, LiteLLM registration Job, CloudWatch Log Group.

**Parallelism** (or let `recommend-instance.py` choose):
- `gpuCount: 1` — single GPU (most ≤7B models)
- `gpuCount: 4, tensorParallelSize: 4` — NVLink node (A100/H100)
- `gpuCount: 4, pipelineParallelSize: 4` — PCIe node (L4/L40S/A10G)
- `gpuCount: 8, tensorParallelSize: 4, pipelineParallelSize: 2` — TP×PP

### AITeam

```yaml
apiVersion: kro.run/v1alpha1
kind: AITeam
metadata:
  name: team-search
  namespace: ai-platform
spec:
  teamName: search-ranking
  models: ["qwen3-3b", "claude-opus-4-8"]
  maxBudget: "50.0"
  budgetDuration: "30d"
  rpmLimit: 60
  tpmLimit: 50000
```

KRO creates: namespace, ResourceQuota, NetworkPolicy, RBAC, LiteLLM team + scoped API key, welcome ConfigMap.

### FineTuneJob

```yaml
apiVersion: kro.run/v1alpha1
kind: FineTuneJob
metadata:
  name: qwen3-support-tuned
  namespace: inference
spec:
  baseModel: "unsloth/Qwen2.5-3B-Instruct-bnb-4bit"   # default (ungated showcase base)
  dataset: "s3://<cluster>-training-datasets/training-data.jsonl"
  gpuCount: 1
  autoDeploy: true              # Deploy as InferenceEndpoint when done
```

Self-service fine-tuning via Unsloth — same GitOps pattern. Handles validation, QLoRA training, model export to S3, and optional auto-deployment.

---

## Platform Health Agent

Optional opt-in service that turns the cluster into a self-watching system. When pods crash, models fail to deploy, or KRO custom resources get stuck, the agent investigates with `kiro-cli` and surfaces a proposed `kubectl` fix in the cluster-dashboard's approvals UI. Click `Approve` and the fix applies; click `Dismiss` and it's logged but does nothing. Out-of-scope fixes (anything that requires editing git) are auto-flagged as text-only diagnoses.

**What triggers an investigation** (configurable in `platform/services/platform-health-agent/configmap.yaml`):

| Trigger | Detection |
|---------|-----------|
| `CrashLoopBackOff` | Pod restart count > 3 |
| `OOMKilled` | Container terminated with `OOMKilled` reason |
| `ImagePullBackOff` | Pod waiting for image > 60s |
| `FailedScheduling` | Pod unschedulable > 120s |
| `NodeNotReady` | Node `Ready=False` > 60s |
| `FailedMount` | Volume mount event `Warning/FailedMount` |
| `StuckResource` | InferenceEndpoint, AITeam, or RayService not reaching healthy state in 5 min |

**Safety boundaries:**
- Investigator pods run with **read-only** RBAC (cluster-wide `get/list/watch` only)
- Remediator pods run with **scoped write** RBAC — `RoleBinding`s in `inference` and `team-*` namespaces only
- Anything outside that scope returns 403 even if the LLM hallucinates a destructive command
- Daily caps: 50 investigations, 20 remediations
- Concurrency cap: 3 active investigations cluster-wide
- 24h approval expiry; rejected approvals never apply

See [`platform/services/platform-health-agent/README.md`](platform/services/platform-health-agent/README.md) for full design and [`docs/platform-health-agent-architecture-design.md`](docs/platform-health-agent-architecture-design.md) for the original design doc.

---

## GPU Time-Slicing

For models that don't need a full GPU, `shared: true` enables NVIDIA time-slicing — up to 4 models share one physical GPU, reducing costs by ~75%.

| Model size | Shared? |
|-----------|---------|
| ≤ 3B (FP16) | Yes |
| 3-4B (FP16) | Tight — reduce `maxModelLen` |
| ≥ 7B | No — use dedicated GPU |

```yaml
spec:
  model: "HuggingFaceTB/SmolLM3-3B"
  shared: true
  maxModelLen: 4096
```

---

## Cold-Start Optimization

Four layers reduce first-inference time from ~7 min to ~60s:

| Layer | Effect | How |
|-------|--------|-----|
| EBS data volume snapshot | 0s image pull | Terraform auto-creates on `ray_image_tag` change |
| SOCI lazy-loading | ~50% faster pull (fallback) | Terraform auto-creates alongside snapshot |
| S3 model weight cache | ~15s vs ~60s model load | Sidecar auto-warms after first deploy |
| GPU node pre-warm | Skip ~90s node provisioning | On demand before a demo: `./ops/prepare-demo.sh` |

**Pre-seed the cache** before a demo:

```bash
./ops/seed-model-cache.py HuggingFaceTB/SmolLM3-3B
./ops/seed-model-cache.py list
```

---

## Operational Scripts

```bash
./ops/recommend-instance.py <model>          # GPU sizing + fleet scaling
./ops/compare-models.py --dataset <jsonl> --models a,b,c   # 3-way Langfuse comparison + cost crossover
./ops/test-model.sh <name> "prompt"          # Quick model test
./ops/ssm-tunnel.sh                          # Port-forward via SSM (WebUI/LiteLLM/Langfuse/Dashboard)
./ops/seed-model-cache.py <model>            # Pre-populate S3 weight cache
./ops/scale-down.sh                          # Suspend platform, reclaim GPUs
./ops/scale-up.sh                            # Restore via ArgoCD sync
./ops/demo.sh                                # Copy-paste demo runbook (section by section)
./ops/demo-failure.sh <scenario>             # Trigger a Platform Health Agent failure demo
./platformctl up|status|tunnel|preflight|compare|down      # Turnkey wrapper over make + ops
```

---

## Repository Structure

```
argocd/bootstrap/                # ApplicationSets (platform + workloads)
platform/
  config/kro/                    # InferenceEndpoint + AITeam + FineTuneJob definitions
  config/rbac/                   # ClusterRoles
  config/ingress.yaml            # ALB routing
  services/                      # gpu-operator, kuberay, litellm, open-webui,
                                 # langfuse, cluster-dashboard, platform-health-agent
workloads/
  models/                        # InferenceEndpoint YAMLs (self-service)
  teams/                         # AITeam YAMLs (self-service)
  fine-tuning/                   # FineTuneJob YAMLs (self-service)
ops/                             # Operational scripts
terraform/                       # Infrastructure modules (VPC → IAM → EKS → Observability)
docs/
  quickstart.md                            # Turnkey path: provision → Sonnet → fine-tune → compare
  fine-tuning-getting-started.md           # Self-service QLoRA guide
  platform-evolution-plan.md               # Turnkey platform design (P1–P6)
  fine-tuning-implementation-plan-v2.md    # Fine-tuning design
  platform-health-agent-architecture-design.md
  platform-health-agent-implementation-plan.md
```

---

## Langfuse Tracing

Tracing is **live on the first model call — no manual setup**. Terraform
(`terraform/30.eks/30.cluster/langfuse-init.tf`) generates the project key pair,
provisions the org/project/admin user via Langfuse's headless init, and writes
the `langfuse-litellm-keys` Secret that LiteLLM's callback consumes.

For ALB or custom-domain access, set the `langfuse_nextauth_url` tfvar (default
`http://localhost:3000`, which works with `./ops/ssm-tunnel.sh`) — e.g.
`http://k8s-aiplatform-<hash>.<region>.elb.amazonaws.com:3000` or
`https://langfuse.<your-domain>`. Do **not** hand-edit the git-tracked
`helm-values.yaml`; the value is injected via the `langfuse-init` Secret.

Sign in at that URL with `langfuse_init_user_email` and the admin password from
`terraform output -raw langfuse_admin_password`.

---

## Cleanup

```bash
kubectl delete inferenceendpoints --all -n inference
cd terraform && make ENVIRONMENT=your-env destroy-all
```

---

## Acknowledgments

Infrastructure based on [Automated Provisioning of Application-Ready Amazon EKS Clusters](https://aws-solutions-library-samples.github.io/compute/automated-provisioning-of-application-ready-amazon-eks-clusters.html) from the AWS Solutions Library, extended with EKS Managed Capabilities, GPU-optimized Karpenter NodePools, the AI platform layer, and the Platform Health Agent.
