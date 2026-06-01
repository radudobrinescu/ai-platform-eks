# AI Platform on EKS

**A self-service AI gateway for your own AWS account.** One OpenAI-compatible API
fronts every model — Amazon Bedrock, any HuggingFace model, and your fine-tuned
ones — with per-team keys, budgets, and rate limits. Teams ship models the way
they ship code: commit a short YAML, `git push`, and the platform handles GPU
provisioning, serving, routing, and observability. A frontier model
(**Bedrock Claude Opus 4.8**) works on day one with **zero GPUs**.

**Two things make it work:**

- **One gateway, every model.** LiteLLM puts Bedrock, vLLM-served open models, and
  fine-tuned models behind a single `/v1/chat/completions` endpoint — with team
  isolation, budgets, and Langfuse tracing built in.
- **Proven, extendable templates.** Three [KRO](https://kro.run) resources
  (`InferenceEndpoint`, `AITeam`, `FineTuneJob`) capture the hard parts —
  tensor-parallelism, GPU sizing, scale-to-zero, fine-tune→deploy — as a few lines
  of YAML. They're the platform's API: fork and extend them, don't reinvent them.

**Stack:** EKS Managed Capabilities (ArgoCD · KRO · ACK) · Karpenter · Ray Serve · vLLM · LiteLLM · Langfuse

![Cluster dashboard — live topology of nodes, GPU slots, and deployed models](docs/img/cluster-dashboard.png)

---

## Architecture

```
git push → ArgoCD syncs → KRO expands your YAML into K8s + AWS resources
         → Karpenter provisions a GPU node → vLLM loads the model
         → LiteLLM registers it → available via API, Open WebUI, and Langfuse
```

The three custom resources **are** the self-service interface:

| Resource | What it does |
|---|---|
| **`InferenceEndpoint`** | Serve a model — HuggingFace ID, or a fine-tuned model from S3 |
| **`AITeam`** | Onboard a team: namespace, RBAC, budget, rate limits, scoped API key |
| **`FineTuneJob`** | QLoRA fine-tune (Unsloth), optionally `autoDeploy` the result |

```yaml
# That's the whole interface — e.g. serve a model:
apiVersion: kro.run/v1alpha1
kind: InferenceEndpoint
metadata: { name: qwen3-3b, namespace: inference }
spec:
  model: "Qwen/Qwen2.5-3B-Instruct"   # ungated — no token needed
  gpuCount: 1                          # 1/2/4/8 → vLLM tensor parallelism
  shared: false                        # true → time-slice one GPU across up to 4 small models
```

Bedrock models need no resource — they're a few lines of LiteLLM config, live the
moment the cluster is up. KRO definitions live in `platform/config/kro/`; extend
them there and every model/team inherits the change.

---

## Quick start

Full walkthrough — provision → use Opus 4.8 with zero GPUs → deploy a model →
fine-tune → prove the savings — is in **[docs/quickstart.md](docs/quickstart.md)**.
It covers the prerequisites that matter (forking, the IP allowlist, gated-model
tokens, the Langfuse login URL). The shape of it:

```bash
# 1. Configure: copy a tfvars, set your Identity Center ARN + gitops repo URL.
cd terraform/00.global/vars && cp example.tfvars dev.tfvars   # edit per quickstart.md

# 2. Provision everything (VPC → EKS + capabilities → Karpenter → secrets).
export AWS_REGION=eu-central-1
./platformctl up dev

# 3. Use it immediately — no GPUs yet.
./platformctl tunnel        # forward the UIs (WebUI / LiteLLM / Langfuse)
./platformctl preflight     # verify Bedrock + models answer AND Langfuse tracing works

# 4. Deploy a self-hosted model — commit a YAML and push.
cp workloads/models/TEMPLATE.yaml.example workloads/models/qwen3-3b.yaml
# edit: set name + model (e.g. Qwen/Qwen2.5-3B-Instruct), then:
git add workloads/models/qwen3-3b.yaml && git commit -m "feat: deploy qwen3-3b" && git push
kubectl get inferenceendpoints -n inference -w
```

`./platformctl` is a thin wrapper over `make` + `ops/` (`up · status · tunnel ·
preflight · compare · down`). The UIs sit behind one internet-facing ALB
(Open WebUI `:8080` · LiteLLM `:4000` · Langfuse `:3000` · Dashboard `:9090`),
IP-allowlisted — or reach them from anywhere with `./platformctl tunnel`.

---

## Beyond the basics

**Prove small + fine-tuned beats big.** Run the same eval set through the frontier
model, a base small model, and a fine-tuned small model — Langfuse shows the tuned
3B matching Opus 4.8 on a narrow task at a fraction of the cost, and the script
prints the **cost crossover** (daily volume above which self-hosting wins):

```bash
./platformctl compare    # → ops/compare-models.py, traced as a Langfuse dataset run
```

Fine-tune with the same `git push` loop (upload a dataset, commit a `FineTuneJob`,
`autoDeploy: true` → live endpoint): **[docs/fine-tuning-getting-started.md](docs/fine-tuning-getting-started.md)**.

**Fast cold starts.** New GPU deployments avoid the multi-minute cold start via
three layers: EBS image snapshots (0s image pull), SOCI lazy-loading, and an S3
model-weight cache (~15s load vs ~60s from HuggingFace). All automated by Terraform.

**Platform Health Agent.** The cluster dashboard can watch for failures, investigate
them with an LLM, and propose a one-click fix — idle until you provide a Kiro key.
See **[its guide](platform/services/cluster-dashboard/PLATFORM-HEALTH-AGENT.md)**.

**Cost control.** Karpenter right-sizes GPUs and **scales them to zero** when idle;
`shared: true` time-slices one physical GPU across up to 4 small models.

**Presenting it?** [docs/demo-walkthrough.md](docs/demo-walkthrough.md) is a timed
presenter's script (10/20/30-min cuts) with talk track and fallbacks.

---

## Repository layout

```
argocd/bootstrap/   ApplicationSets (platform services + self-service workloads)
platform/
  config/kro/       InferenceEndpoint · AITeam · FineTuneJob definitions (the API)
  services/         litellm, open-webui, langfuse, gpu-operator, kuberay,
                    cluster-dashboard (+ Platform Health Agent component)
workloads/          Self-service YAMLs: models/ · teams/ · fine-tuning/
ops/                Operational scripts (ops/demo/ holds demo-only scripts)
terraform/          Infrastructure modules (VPC → IAM → EKS → observability)
docs/               quickstart · fine-tuning · demo-walkthrough · platform-evolution-plan
```

## Acknowledgments

Infrastructure based on [Automated Provisioning of Application-Ready Amazon EKS Clusters](https://aws-solutions-library-samples.github.io/compute/automated-provisioning-of-application-ready-amazon-eks-clusters.html)
from the AWS Solutions Library, extended with EKS Managed Capabilities,
GPU-optimized Karpenter NodePools, and the self-service AI platform layer.
