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
| `gateway` | LiteLLM | LiteLLM, litellm-sync libs, Postgres, Langfuse, Open WebUI, oauth2-proxy, python base image, aws-cli, curl | LiteLLM boots + migrates DB; litellm-sync registers/deregisters; a chat call lands a Langfuse trace; a `BedrockModel` answers on `/v1` |
| `gitops` | ArgoCD / KRO | argo-cd chart, kro chart (self-managed path) | ApplicationSets render; all apps Synced/Healthy; KRO expands a `VLLMEndpoint` |
| `tooling` | — | GitHub Actions, terraform CLI, gitleaks, ruff, pytest, kubeconform, yamllint | `ci.yaml` green |

## Validated sets

Newest first. "Validated" means the whole column ran together on a real cluster.

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
- **Karpenter**: same CRD caveat; v1.14 moved `CapacityBuffer` to `v1beta1`, so live upgrades apply CRDs first (`karpenter-crd` chart). Fresh clusters are unaffected. Re-verify the instance-family allow-list in `karpenter.tf` when the module version changes.
- **EKS managed addons** (vpc-cni, CoreDNS, kube-proxy, pod-identity, EBS CSI, metrics-server): `most_recent = true`; drift between applies is reported weekly by `addon-freshness`. EBS CSI once sat on a vulnerable pinned version; that is why nothing here is pinned.
- **vLLM**: one source of truth in `locals.tf`; the RGD schema defaults are a fallback for clusters where the ConfigMap key is absent and must match. GPU operator's driver must satisfy the CUDA runtime of the vLLM image. The llm-d router version is tested against a vLLM range; bump them together (same Renovate group).
- **`kubernetes` Python client**: supported within ±1 minor of the API server; `allowedVersions` in `renovate.json` enforces the ceiling and must be raised with the k8s anchor. Same for `alpine/k8s` (kubectl skew policy).
- **LiteLLM**: follow `-stable` tags only (enforced in `renovate.json`); litellm-sync and the cluster-dashboard use its admin API (`/model/new`, `/model/delete`, `/team/*`, `/key/*`), so check the changelog for admin-API changes on minors.
- **Postgres**: a major bump is a dump/restore of the shared LiteLLM DB; never a plain image tag change.
- **Langfuse chart v2 → v3**: ClickHouse becomes the trace store; it is a data migration, not a version bump. Also keep `additionalEnv`/`additionalEnvFrom` (the Terraform `langfuse-init` Secret) and the `NEXTAUTH_URL` ignoreDifferences rule working.
- **ArgoCD (self-managed path)**: pinned deliberately; an unpinned GitOps engine can break the mechanism used to recover from any other break. The managed EKS capability path is AWS-owned and out of scope.

## Procedure

1. **Renovate PR arrives** (Monday schedule, one per domain). CI runs the static gate.
2. **Majors and anchors** appear as checkboxes on the Dependency Dashboard issue first; ticking one opens the PR. Read the coupling notes above before ticking.
3. **E2E gate**: label the PR `e2e` (or run `./platformctl e2e <env>` locally) to prove the set on a real cluster. Required for `infra`, `inference`, `gitops`; recommended for `gateway`; not needed for `tooling`.
4. **Merge** → ArgoCD syncs `main`. Rollback is `git revert` + sync, except CRD-bearing upgrades (LBC, Karpenter, GIE), which is why those are E2E-mandatory.
5. **Record** the new validated set above (copy the previous table, change what moved, note the E2E run).
6. **Anchor bump** (k8s or vLLM): human commit on its own PR, E2E, then raise the relevant `allowedVersions` ceilings in `renovate.json` and let the next Renovate run propose the dependents.

Forks that only consume release tags do none of this: move `gitops_revision` to the next tag after it has run on your staging cluster.
