# Component compatibility matrix

This file is the human-readable half of the upgrade lifecycle. `renovate.json`
discovers version pins and proposes grouped PRs; this file records **which set of
versions was validated together, how, and why the groups are shaped the way they
are**. Update it whenever a validated set changes (the E2E run that proves a set
is the trigger, not the PR that proposes it).

Related: [`renovate.json`](renovate.json) (discovery + grouping),
[`.github/workflows/ci.yaml`](.github/workflows/ci.yaml) (static gate),
[`.github/workflows/addon-freshness.yaml`](.github/workflows/addon-freshness.yaml)
(EKS managed addons, report-only; out of Renovate's scope by design).

## Anchors

Compatibility is not a flat list. Nearly every coupling collapses onto two anchors;
everything else derives from them. **Anchor bumps are human commits** (Renovate does
not manage them) and should be done first, alone, with an E2E run.

| Anchor | Where it is set | What it gates |
|---|---|---|
| **EKS Kubernetes version** | `cluster_config.kubernetes_version` in your tfvars | Karpenter (chart ↔ CRD ↔ k8s window), AWS LB Controller, KEDA, metrics-server, EKS managed addons, the `kubernetes` Python client (±1 minor), `alpine/k8s` kubectl bundle (±1 minor) |
| **vLLM serving image** | `vllm_image_tag` in `terraform/30.eks/30.cluster/locals.tf` (single source → `platform-config` ConfigMap → KRO `externalRef`), mirrored as the schema default in `platform/config/kro/*.yaml` | NVIDIA GPU Operator (driver/CUDA ≥ vLLM's CUDA runtime), GIE CRDs + llm-d router (routing protocol), which HF architectures/quantizations load |

## Upgrade domains (one Renovate PR each)

| Domain | Anchor | Members | Acceptance test |
|---|---|---|---|
| `infra` | k8s version | Karpenter, LBC, KEDA, Terraform providers/modules, `kubernetes` pip, `alpine/k8s` | cluster + all addons Healthy; Karpenter provisions a node; ALB provisioned; EKS upgrade-readiness Insights clean |
| `inference` | vLLM | vLLM, gpu-operator, GIE CRDs, llm-d router, s5cmd, prometheus | a small model serves a completion on a GPU via `VLLMEndpoint`; DCGM metrics flow; `LLMDEndpoint` routes |
| `gateway` | LiteLLM | LiteLLM, litellm-sync libs, Postgres, Langfuse, Open WebUI, oauth2-proxy, python base image, aws-cli, curl | LiteLLM boots + migrates DB; litellm-sync registers/deregisters; a chat call lands a Langfuse trace; a `BedrockModel` answers on `/v1`; every seed user signs in via Cognito SSO to each UI with the right role |
| `gitops` | ArgoCD / KRO | argo-cd chart, kro chart (self-managed path) | ApplicationSets render; all apps Synced/Healthy; KRO expands a `VLLMEndpoint` |
| `tooling` | — | GitHub Actions, terraform CLI, gitleaks, ruff, pytest, kubeconform, yamllint | `ci.yaml` green |

## Validated sets

Newest first. "Validated" means the whole column ran together on a real cluster.

### Set 2026-10-20 — k8s 1.37 (validation: E2E gate, eu-central-1)

**E2E result:** E2E_RESULT_PLACEHOLDER

Moved from the baseline by the k8s anchor commit plus six Renovate group PRs
(#4 gateway, #5 inference, #6 infra, #7 tooling, #9 inference major,
#11 tooling major). Not in this set: #8 gateway major and #10 infra major (see
"Pending majors" below). The e2e env uses EKS Managed Capabilities, so the
ArgoCD and KRO versions are AWS-managed and the self-managed chart pins (used
only when `eks_capabilities = false`, e.g. the ESC partition) were not exercised.

| Component | Baseline (k8s 1.36) | This set |
|---|---|---|
| EKS Kubernetes | 1.36 | **1.37** |
| vLLM (`vllm/vllm-openai`) | v0.24.0 | **v0.32.0** |
| NVIDIA gpu-operator chart (dcgm-exporter) | v25.10.1 (4.4.2-4.7.0) | **v26.7.2** (4.6.1-4.8.4) |
| GIE CRDs | v1.5.0 | **v1.6.2** |
| llm-d router chart | v0.9.0 | **v1.0.0** |
| Karpenter chart | 1.14.0 | **1.15.0** |
| AWS Load Balancer Controller chart | 3.4.3 | **3.6.0** |
| KEDA chart | 2.20.2 | **2.21.0** |
| terraform-aws-modules/eks | 21.1.5 | **21.29.0** |
| terraform-aws-modules/vpc | 5.5.3 | **5.21.0** |
| hashicorp/aws provider | ~> 6.28.0 | **~> 6.69.0** |
| aws-ia/eks-blueprints-addons | ~> 1.24.0 | ~> 1.24.0 |
| LiteLLM | v1.81.9-stable.patch.1 | **v1.83.14-stable.patch.1** |
| Postgres | 16.14-alpine | **16.15-alpine** |
| Langfuse chart | 1.5.20 | **1.5.41** |
| Open WebUI | v0.10.2 | **v0.12.3** |
| oauth2-proxy | v7.15.3 | **v7.15.5** |
| Prometheus (autoscaling) | v2.55.1 | **v3.15.0** |
| s5cmd | v2.3.0 | v2.3.0 |
| alpine/k8s (kubectl) | 1.35.6 | **1.37.1** |
| python base image (incl. `PYTHON_IMAGE`) | 3.12-slim | **3.15-slim** |
| `kubernetes` (pip) | 31.0.0 | 31.0.0 |
| `psycopg[binary]` / `pyjwt[crypto]` / `pyyaml` (pip) | 3.2.3 / 2.9.0 / 6.0.2 | **3.3.6 / 2.15.1 / 6.0.3** |
| amazon/aws-cli, curlimages/curl | 2.36.8, 8.21.0 | **2.38.1, 8.23.0** |
| CI: terraform / ruff / pytest / kubeconform / yamllint | 1.9.8 / 0.6.9 / 8.3.3 / 0.6.7 / 1.35.1 | **1.16.6 / 0.17.1 / 9.1.1 / 0.8.0 / 1.38.0** |

What this set needed beyond the version bumps (each found by the gate, each
with a coupling note below): Karpenter inline controller policy, two DCGM fields
removed, psycopg pins in Python-built Jobs brought under Renovate.

### Set 2026-10 baseline — k8s 1.36 (validation: production use, pre-E2E-gate)

| Component | Version | Pin location |
|---|---|---|
| EKS Kubernetes | 1.36 | tfvars `cluster_config.kubernetes_version` |
| vLLM (`vllm/vllm-openai`) | v0.24.0 | `locals.tf` + 3 RGD schema defaults |
| NVIDIA gpu-operator chart | v25.10.1 | `argocd/bootstrap/templates/platform.yaml` |
| GIE CRDs (`gateway-api-inference-extension`) | v1.5.0 | `platform/services/inference-gateway/kustomization.yaml` |
| llm-d router chart (`llm-d-router-standalone`) | v0.9.0 | `routerChartVersion` default, LLMD* RGDs |
| Karpenter chart | 1.14.0 | `terraform/30.eks/30.cluster/karpenter.tf` |
| AWS Load Balancer Controller chart | 3.4.3 | `terraform/30.eks/35.addons/main.tf` |
| KEDA chart | 2.20.2 | `terraform/30.eks/35.addons/keda.tf` |
| KRO chart (self-managed) | 0.9.4 | `capabilities-selfmanaged.tf` |
| ArgoCD chart (self-managed) | 10.10.5 (app v3.5.5) | `capabilities-selfmanaged.tf` |
| terraform-aws-modules/eks | 21.1.5 | `main.tf`, `karpenter.tf` |
| aws-ia/eks-blueprints-addons | ~> 1.24.0 | `35.addons/main.tf`, `40.aws-native-observability/main.tf` |
| LiteLLM | v1.81.9-stable.patch.1 | `platform/services/litellm/litellm.yaml` |
| Postgres | 16.14-alpine | litellm.yaml, pha-event-watcher.yaml |
| Langfuse chart | 1.5.20 | `platform.yaml` |
| Open WebUI | v0.10.2 | `platform/services/open-webui/open-webui.yaml` |
| oauth2-proxy | v7.15.3 | `cluster-dashboard/oauth2-proxy.yaml` |
| Prometheus (autoscaling) | v2.55.1 | `platform/config/autoscaling-prometheus.yaml` |
| s5cmd | v2.3.0 | RGD init containers |
| alpine/k8s (kubectl) | 1.35.6 | RGDs, CronJobs, open-webui |
| python base image | 3.12-slim | litellm-sync, cluster-dashboard, `PYTHON_IMAGE` |
| `kubernetes` (pip) | 31.0.0 | litellm-sync, pha-event-watcher |
| `psycopg[binary]` / `pyjwt[crypto]` / `pyyaml` (pip) | 3.2.3 / 2.9.0 / 6.0.2 | cluster-dashboard |
| amazon/aws-cli, curlimages/curl | 2.36.8, 8.21.0 | litellm.yaml |

## Coupling notes (why the pins exist)

These used to live only as code comments. Keep them here and point to them from the
code.

- **AWS LB Controller v3.x**: chart version tracks app version since 3.0 (v2.x used chart 1.x). Helm does not upgrade CRDs: on a *live* cluster apply `kubectl apply -k "github.com/aws/eks-charts/stable/aws-load-balancer-controller/crds?ref=master"` before the chart bump. v3 also needs `ec2:DescribeRouteTables`, supplied via `lbc_v3_supplement` in `35.addons/main.tf` because the pinned blueprints module's policy predates it.
- **Karpenter controller IAM policy size** (found by the E2E gate, 2026-10-19): from `terraform-aws-modules/eks` v21.29 the Karpenter controller policy renders to ~6.2K characters, over the 6,144 limit for a managed IAM policy, so `CreatePolicy` fails with `LimitExceeded: PolicySize`. `karpenter.tf` sets `enable_inline_policy = true` (inline limit 10,240). The four extra actions this repo used to add itself are granted by the module since v21.29. On a live cluster the apply replaces the managed policy with an inline one in the same run.
- **dcgm-exporter field list** (found by the E2E gate, 2026-10-20): gpu-operator v26 ships dcgm-exporter 4.8.4, which refuses to start when the metrics CSV contains `DCGM_FI_DEV_PCIE_TX_THROUGHPUT` / `DCGM_FI_DEV_PCIE_RX_THROUGHPUT` ("no longer supported; use `DCGM_FI_PROF_PCIE_*_BYTES`") and CrashLoopBackOffs on every GPU node. The replacements are profiling fields, which `platform/services/gpu-operator/helm-values.yaml` excludes on purpose (profiling is unavailable with time-sliced GPUs), so both were removed. On any future gpu-operator major, diff the field list against the new dcgm-exporter. v26 also adds a 45s readiness delay to the exporter.
- **Python base image vs. pinned wheels** (found during validation): the dashboard and Platform Health Agent build two Jobs at runtime in Python (`backend.py`, `event_watcher.py`) that `pip install psycopg[binary]==…` on `PYTHON_IMAGE`. Those pins live in Python string literals; Renovate's pip regex manager now covers those files, and `tests/test_renovate_coverage.py` fails when one package is pinned to different versions anywhere under `platform/services/`. When the python image moves a minor, every pinned wheel must exist for it (psycopg-binary 3.2.3 has no cp315 wheel; 3.3.6 does). The `-slim` images have no compiler, so a missing wheel is a failed install, and the dashboard's init container swallows pip errors.
- **Open WebUI model visibility**: Open WebUI (v0.10 and v0.12 alike) shows a model that has no workspace access record only to admins. SSO users in `ai-platform-developers` / `ai-platform-users` sign in as role `user` and see an empty model list until an admin grants access in Workspace → Models, or `BYPASS_MODEL_ACCESS_CONTROL=true` is set. This is a platform policy choice; it is unchanged, and the E2E chat-attribution check signs in as the admin for that reason.
- **LiteLLM role names**: Cognito's `custom:role` carries `proxy_admin` / `internal_user` / `internal_user_view_only`; LiteLLM's UI session reports the last one as `internal_user_viewer` (enum value). Both are correct.
- **Karpenter**: same CRD caveat; v1.14 moved `CapacityBuffer` to `v1beta1`, so live upgrades apply CRDs first (`karpenter-crd` chart). Fresh clusters are unaffected. Re-verify the instance-family allow-list in `karpenter.tf` when the module version changes.
- **EKS managed addons** (vpc-cni, CoreDNS, kube-proxy, pod-identity, EBS CSI, metrics-server): `most_recent = true`; drift between applies is reported weekly by `addon-freshness`. EBS CSI once sat on a vulnerable pinned version; that is why nothing here is pinned.
- **vLLM**: one source of truth in `locals.tf`; the RGD schema defaults are a fallback for clusters where the ConfigMap key is absent and must match. GPU operator's driver must satisfy the CUDA runtime of the vLLM image. The llm-d router version is tested against a vLLM range; bump them together (same Renovate group).
- **`kubernetes` Python client**: supported within ±1 minor of the API server; `allowedVersions` in `renovate.json` enforces the ceiling and must be raised with the k8s anchor. Same for `alpine/k8s` (kubectl skew policy).
- **LiteLLM**: follow `-stable` tags only (enforced in `renovate.json`); litellm-sync and the cluster-dashboard use its admin API (`/model/new`, `/model/delete`, `/team/*`, `/key/*`), so check the changelog for admin-API changes on minors.
- **Postgres**: a major bump is a dump/restore of the shared LiteLLM DB; never a plain image tag change.
- **Langfuse chart v2 → v3**: ClickHouse becomes the trace store; it is a data migration, not a version bump. Also keep `additionalEnv`/`additionalEnvFrom` (the Terraform `langfuse-init` Secret) and the `NEXTAUTH_URL` ignoreDifferences rule working.
- **ArgoCD (self-managed path)**: pinned deliberately; an unpinned GitOps engine can break the mechanism used to recover from any other break. The managed EKS capability path is AWS-owned and out of scope.

## Pending majors (proposed by Renovate, not yet in a validated set)

| PR | Change | What blocks it |
|---|---|---|
| #8 gateway (major) | Langfuse chart 1.5 → **2.1.6**; Postgres 16 → **18** | Chart 2.x validation: with `redis.auth.existingSecret` set, the bundled Valkey sub-chart also needs `redis.auth.usersExistingSecret` (one key per ACL user), so `helm template` fails in CI. Postgres 18 is a dump/restore of the shared LiteLLM/dashboard database on existing clusters (fresh clusters are unaffected). |
| #10 infra (major) | terraform-aws-modules/iam 5 → **6**; hashicorp/kubernetes provider 2 → **3**; vpc module 5 → **6**; `kubernetes` pip 31 → **33** | `terraform validate` fails: iam 6.x removed the `iam-role-for-service-accounts-eks` submodule used by the EBS CSI IRSA role (`main.tf`, `ebs_csi_driver_irsa`). Provider 3.x and vpc 6.x need a review of their upgrade guides before re-validating. |

Each needs code changes, then its own E2E run, then a new validated set.

## The E2E gate

`./platformctl e2e <env> [--keep] [--skip-up] [--with-llmd] [--yes]` provisions
the env from the branch under test (ArgoCD tracks `gitops_revision` from the
env's tfvars), runs `ops/lib/e2e.py`, and tears down. The report lands in
`.platformctl/e2e/<timestamp>.md|json` with account ids, ARNs and AWS
hostnames redacted. The same command runs in GitHub Actions
(`.github/workflows/e2e.yaml`, label `e2e`, human-approved environment).

| Domain | Checks |
|---|---|
| gitops | every ArgoCD Application Synced + Healthy; every KRO RGD Active; ArgoCD UI login hands off to IAM Identity Center |
| infra | cluster version = tfvars; every EKS managed addon ACTIVE; every Ingress has an ALB; a Karpenter NodeClaim becomes Ready |
| gateway | LiteLLM ready; a `BedrockModel` (nova-lite) enrolls, answers on `/v1`, lands a Langfuse trace, and deregisters on delete; dashboard serves `/data.json`; Cognito seed users CONFIRMED with the right group and `custom:role`; each seed user signs in through the Cognito hosted UI to Open WebUI, the LiteLLM UI, Langfuse and the dashboard with the expected role (dashboard: admins + developers allowed, users denied); a wrong password is rejected; one password entry signs in to every UI; a chat through Open WebUI is attributed to that user's LiteLLM spend |
| inference | a `VLLMEndpoint` (Qwen2.5-0.5B-Instruct) becomes Ready on a GPU node and answers; dcgm-exporter serves GPU metrics; with `--with-llmd`, an `LLMDEndpoint` routes a request through llm-d |

Not automated: the interactive IAM Identity Center sign-in to ArgoCD (needs an
IdC user and MFA). On macOS the command holds a sleep assertion
(`caffeinate`); a sleeping host breaks the port-forwards the checks use.
Closing the lid on battery still sleeps the machine (the preflight warns).

If the host loses the network mid-apply, Terraform writes the unsaved state to
`terraform/<stage>/errored.tfstate` and leaves the S3 state lock held. To
recover: confirm no `terraform` process is running, `terraform force-unlock` the
lock id from the error, `terraform state push errored.tfstate` so state matches
what was created, then `./platformctl down <env> --yes`. Run it from CI
(`.github/workflows/e2e.yaml`) to avoid laptop sleep and network changes
altogether.

## Procedure

1. **Renovate PR arrives** (Monday schedule, one per domain). CI runs the static gate.
2. **Majors and anchors** appear as checkboxes on the Dependency Dashboard issue first; ticking one opens the PR. Read the coupling notes above before ticking.
3. **E2E gate**: label the PR `e2e` (or run `./platformctl e2e <env>` locally) to prove the set on a real cluster. Required for `infra`, `inference`, `gitops`; recommended for `gateway`; not needed for `tooling`.
4. **Merge** → ArgoCD syncs `main`. Rollback is `git revert` + sync, except CRD-bearing upgrades (LBC, Karpenter, GIE), which is why those are E2E-mandatory.
5. **Record** the new validated set above (copy the previous table, change what moved, note the E2E run).
6. **Anchor bump** (k8s or vLLM): human commit on its own PR, E2E, then raise the relevant `allowedVersions` ceilings in `renovate.json` and let the next Renovate run propose the dependents.

Forks that only consume release tags do none of this: move `gitops_revision` to the next tag after it has run on your staging cluster.
