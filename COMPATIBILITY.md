# Component compatibility matrix (internal)

The human-readable half of the update lifecycle. `renovate.json` (this branch)
finds version pins on `main` and proposes grouped branches; this file records
**why the groups are shaped the way they are, which couplings bite, and which
sets were validated together**. The public record of releases is
`docs/validated-versions.md` on `main`, written by `tools/release.py`.

Ported from v1 (`feat/upgrade-lifecycle`, COMPATIBILITY.md). v1 ran Renovate
against its own branch; v2 targets `main` and ships releases (see "Releases"
below).

## Anchors

Nearly every coupling collapses onto two anchors; everything else derives from
them. **Anchor bumps are deliberate**: the Kubernetes version is never proposed
by Renovate (it follows the EKS support calendar and the release manifest's
`kubernetes.supported`); the vLLM image needs a dashboard tick.

| Anchor | Where it is set on `main` | What it gates |
|---|---|---|
| **EKS Kubernetes version** | `cluster_config.kubernetes_version` in each env's tfvars (default in `example.tfvars`) | Karpenter (chart, CRD, k8s window), AWS LB Controller, KEDA, metrics-server, EKS managed addons, the `kubernetes` Python client (±1 minor), `alpine/k8s` kubectl (±1 minor) |
| **vLLM serving image** | `vllm_image_tag` in `terraform/30.eks/30.cluster/locals.tf` (single source -> `platform-config` ConfigMap -> kro `externalRef`), mirrored as the schema default in `platform/config/kro/*.yaml` | NVIDIA GPU Operator (driver/CUDA >= vLLM's CUDA runtime), GIE CRDs + llm-d router (routing protocol), which HF architectures/quantizations load |

## Upgrade domains (one Renovate branch each)

| Domain | Anchor | Members | Acceptance test |
|---|---|---|---|
| `infra` | k8s version | Karpenter, LBC, KEDA, Terraform providers/modules, `kubernetes` pip, `alpine/k8s` | cluster + all addons healthy; Karpenter provisions a node; ALB provisioned |
| `inference` | vLLM | vLLM, gpu-operator, GIE CRDs, llm-d router, s5cmd, prometheus | a small model serves a completion on a GPU via `VLLMEndpoint`; DCGM metrics flow; `LLMDEndpoint` routes |
| `gateway` | LiteLLM | LiteLLM, litellm-sync libs, Postgres, Langfuse, Open WebUI, oauth2-proxy, python base image, aws-cli, curl | LiteLLM boots + migrates DB; litellm-sync registers/deregisters; a chat lands a Langfuse trace; a `BedrockModel` answers on `/v1`; every seed user signs in via Cognito SSO to each UI with the right role |
| `gitops` | Argo CD / kro | argo-cd chart, kro chart (self-managed path) | ApplicationSets render; all apps Synced/Healthy; kro expands a `VLLMEndpoint` |
| `tooling` | none | GitHub Actions, Terraform CLI, ruff, pytest, kubeconform, yamllint | `main`'s `ci.yaml` green |

## How pins are found (no annotations on `main`)

`main` ships to the public product repo, so it carries no Renovate comments.
Built-in managers read Terraform (`helm_release`, modules, providers), plain
manifests (`image:`), kustomize (`?ref=`) and GitHub Actions (`uses:`). Regex
managers in `renovate.json` read the remaining shapes as they exist on `main`:

| Pin | Shape matched |
|---|---|
| vLLM (`locals.tf`) | `vllm_image_tag = "<v>"` (depName `vllm/vllm-openai`) |
| vLLM default in the 3 RGDs | `default=vllm/vllm-openai:<v>` |
| llm-d router (LLMD RGDs) | `routerChartVersion: "string \| default=<v>"` |
| LBC chart (`35.addons/main.tf`) | `chart_version = "<v>"` inside the `aws_load_balancer_controller` block |
| ApplicationSet charts | `chart:` / `chartRepo:` / `chartVersion:` triplets |
| `PYTHON_IMAGE` | `PYTHON_IMAGE: "python:<v>"` |
| pip pins in init containers and runtime-built Jobs | `name==<v>` in the five listed files |
| Terraform CLI in CI | `terraform_version: "<v>"` (the github-actions manager's own copy is disabled) |
| Python CI tools | `pip install ... <name>==<v>` in `.github/workflows/*.yaml` |
| kubeconform | `kubeconform/releases/download/v<v>/` |

`tests/test_renovate_coverage.py` holds the inventory and runs weekly against
`main` (`.github/workflows/coverage.yaml`). Verified with Renovate's own
extraction (`renovate --platform=local --dry-run=extract`, see README.md).

Known gaps on `main` (product fixes, not detection problems):
- `helm_release.argocd_selfmanaged` has no `version`: the self-managed Argo CD
  chart floats on every apply. v1 pinned it (baseline set below: 10.10.5).
- kustomize is installed from `master` in `main`'s CI (unpinned). v1 `ab353a8`
  pins a release tarball; once ported, add a manager for its `KUSTOMIZE_VERSION=`.
- `main` still carries v1's `renovate.json` (base branch `feat/upgrade-lifecycle`).
  It is inert (Renovate reads the default branch's config) but should be removed
  before the first release so nothing Renovate-related reaches the public repo.
- Observability chart versions in `40.observability/45.aws-oss-observability`
  are variable defaults (`helm_chart_version = optional(string, "...")`); not
  detected. The stage is opt-in and off by default.

## Validated sets

Newest first. "Validated" means the whole column ran together on a real cluster.

### Set 2026-10-20, k8s 1.37 (v1 branch only, not on `main`)

Validated on `feat/upgrade-lifecycle` by the v1 gate: 19 of 21 checks passed
(fresh cluster, `--with-llmd`). The two failures were platform defects fixed on
that branch (`e6490c3` LiteLLM memory request, `662741e` litellm-sync delete),
not yet proven on a cluster. **None of this is on `main`.** It is the reference
for the first v2 release: the versions below plus the coupled fixes listed in
"Coupling notes" must travel together.

| Component | Baseline (`main`, k8s 1.36) | This set |
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
| LiteLLM | v1.81.9-stable.patch.1 | **v1.83.14-stable.patch.1** |
| Postgres | 16.14-alpine | **16.15-alpine** |
| Langfuse chart | 1.5.20 | **1.5.41** |
| Open WebUI | v0.10.2 | **v0.12.3** |
| oauth2-proxy | v7.15.3 | **v7.15.5** |
| Prometheus (autoscaling) | v2.55.1 | **v3.15.0** |
| alpine/k8s (kubectl) | 1.35.6 | **1.37.1** |
| python base image (incl. `PYTHON_IMAGE`) | 3.12-slim | **3.15-slim** |
| `psycopg[binary]` / `pyjwt[crypto]` / `pyyaml` (pip) | 3.2.3 / 2.9.0 / 6.0.2 | **3.3.6 / 2.15.1 / 6.0.3** |
| amazon/aws-cli, curlimages/curl | 2.36.8, 8.21.0 | **2.38.1, 8.23.0** |
| CI: terraform / ruff / pytest / kubeconform / yamllint | 1.9.8 / 0.6.9 / 8.3.3 / 0.6.7 / 1.35.1 | **1.16.6 / 0.17.1 / 9.1.1 / 0.8.0 / 1.38.0** |

### Baseline, k8s 1.36 (what `main` runs today)

Production use on `ai-platform-dev`; first v2 E2E run (`fresh main`, both
profiles) pending (Unit 1b). Pins: see the left column above, plus kro chart
0.9.4 (self-managed), Argo CD chart unpinned (see "Known gaps"), s5cmd v2.3.0,
`kubernetes` pip 31.0.0.

## Coupling notes (why the pins exist)

- **AWS LB Controller v3.x**: chart version tracks app version since 3.0. Helm does not upgrade CRDs: on a *live* cluster apply the CRDs (`kubectl apply -k "github.com/aws/eks-charts/stable/aws-load-balancer-controller/crds?ref=master"`) before the chart bump. v3 also needs `ec2:DescribeRouteTables`, supplied via `lbc_v3_supplement` in `35.addons/main.tf`.
- **Karpenter controller IAM policy size** (found by the v1 gate): from `terraform-aws-modules/eks` v21.29 the Karpenter controller policy exceeds the 6,144-character managed-policy limit (`LimitExceeded: PolicySize`). Fix `044a6ef` (v1): `enable_inline_policy = true` in `karpenter.tf`. **Carry it with the eks module bump.**
- **dcgm-exporter field list** (found by the v1 gate): gpu-operator v26 ships dcgm-exporter 4.8.4, which refuses `DCGM_FI_DEV_PCIE_TX/RX_THROUGHPUT` and CrashLoopBackOffs on every GPU node. Fix `9dbfc1b` (v1) removes both. **Carry it with the gpu-operator v26 bump.** v26 also adds a 45 s exporter readiness delay (the DCGM check waits for Ready).
- **Python base image vs. pinned wheels**: the dashboard and the Platform Health Agent build Jobs at runtime that `pip install psycopg[binary]==...` on `PYTHON_IMAGE`; those pins live in Python string literals (`backend.py`, `event_watcher.py`), now read by the pip manager. Every pinned wheel must exist for a new python minor (psycopg-binary 3.2.3 has no cp315 wheel; 3.3.6 does). The coverage test fails when one package is pinned to two versions under `platform/services/`.
- **LiteLLM memory / litellm-sync delete**: v1 fixes `e6490c3` and `662741e` (see the 1.37 set). Carry them with the gateway bump.
- **Open WebUI model visibility**: a model with no workspace access record is visible only to admins; the chat-attribution check signs in as the admin for that reason. Platform policy, unchanged.
- **LiteLLM role names**: Cognito's `custom:role` is `internal_user_view_only`; LiteLLM's UI session reports `internal_user_viewer` (enum value). Both are correct.
- **Karpenter**: same CRD caveat; v1.14 moved `CapacityBuffer` to `v1beta1`, so live upgrades apply CRDs first (`karpenter-crd` chart).
- **EKS managed addons**: `most_recent = true`; drift is reported weekly by `addon-freshness` on `main`. Out of Renovate's scope by design.
- **vLLM**: one source of truth in `locals.tf`; the RGD schema defaults must match (same dep, one branch). The GPU driver must satisfy the image's CUDA runtime; bump the llm-d router with it.
- **`kubernetes` Python client / `alpine/k8s`**: ±1 minor of the API server; `allowedVersions` in `renovate.json` enforces the ceiling and must be raised with the Kubernetes anchor.
- **LiteLLM**: follow `-stable` tags only; litellm-sync and the dashboard use its admin API (`/model/*`, `/team/*`, `/key/*`), so read the changelog on minors.
- **Postgres**: a major bump is a dump/restore of the shared LiteLLM DB; never a plain tag change.
- **Langfuse chart v1 -> v2**: v2 needs `redis.auth.usersExistingSecret` alongside `existingSecret`; `helm template` fails otherwise (blocked in v1).
- **Argo CD (self-managed path)**: pin it; an unpinned GitOps engine can break the mechanism used to recover from any other break.

## Releases (v2)

1. Renovate opens grouped branches against `main` (Monday schedule, 7-day
   minimum release age, security updates exempt). Their PRs are never merged.
2. Majors and anchors appear on the Dependency Dashboard first; read the
   coupling notes above before ticking one.
3. Cut a release: `python3 tools/release.py --repo <product checkout> cut vYYYY.MM.N renovate/infra renovate/inference ...`;
   cherry-pick coupled fixes by hand onto `release/<version>`.
4. `python3 tools/release.py --repo <checkout> notes vYYYY.MM.N --base <previous tag or main>`
   writes the notes, the release manifest and the validated-versions row (E2E
   "not run"). The manifest must exist before E2E: the candidate's
   `./platformctl upgrade` refuses a target without one. Add
   `acknowledged_replacements` by hand if the upgrade plan guard needs them.
5. Run E2E on the release head: `fresh` and `upgrade` (base = previous release
   tag, or `main` for the first), both profiles: four commit statuses
   `e2e/{fresh,upgrade}-{default,managed}`, all required (BR1). Any push to the
   release branch needs a rerun (BR2).
6. Record the results: `notes ... --statuses-from-github OWNER/REPO` refreshes
   the row. That commit changes the head SHA, so under BR2 as written it needs a
   rerun; see README.md "Open question: recording E2E results".
7. Merge the release PR, tag the merge commit `vYYYY.MM.N`.
