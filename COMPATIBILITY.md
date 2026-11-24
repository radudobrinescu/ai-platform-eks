# Component compatibility matrix (internal)

The human-readable half of the update lifecycle. `renovate.json` (this branch)
finds version pins on `main` and proposes grouped branches; this file records
**why the groups are shaped the way they are, which couplings bite, and which
pins must move together**. Which versions were validated together is not kept
here: every release writes its row to `docs/validated-versions.md` on `main`
(`tools/release.py notes`), and that table is the record. How the factory cuts,
tests and publishes is in README.md.

## Anchors, and what waits

Nearly every coupling collapses onto two anchors; everything else derives from
them. Nobody is assigned to tick a dashboard, so only one kind of update waits
for a person. The others that E2E cannot fully judge (they affect what users
run, or cannot be undone) get their own branch and wait for time instead:

| Update | Why E2E alone is not enough | How it gets in | Branch |
|---|---|---|---|
| every **major** | breaking changes by definition; often code changes or a data migration | a tick on the Dependency Dashboard (the only one left) | `renovate/major-<group>` |
| **EKS Kubernetes version** | one-way; replaces every node; gates the components below | by itself, 60 days after the minor's release | `renovate/eks-kubernetes` |
| every **vLLM** change | it runs every user's model; under 0.x a minor can drop architectures or flags users pass in `extraArgs`; E2E serves one small model | by itself, when the version is 14 days old | `renovate/vllm` |
| **LiteLLM** minors | the gateway for every request, key and budget; minors change the database schema | by itself, when the version is 14 days old | `renovate/litellm-minor` |
| **kro** and **llm-d router** minors | 0.x APIs users write manifests against (`VLLMEndpoint`, `LLMDEndpoint`) or route through | by itself, at once: their registries (registry.k8s.io, ghcr.io) give Renovate no release dates, and an age without a date would hold them forever | `renovate/kro-minor`, `renovate/llm-d-router-minor` |

The age is Renovate's `minimumReleaseAge` on those rules only (Docker Hub and
endoflife.date give release dates; a version without a date is held, never
proposed early). On top of it: the factory takes at most one of vLLM and the
EKS version per cycle; every update is tested in every weekly cycle after it
went in; and forkers get it only with the next published release, weeks later.
The label `hold` keeps any PR out.

Everything else, including Karpenter, Argo CD, Prometheus, the GPU operator and
kro/router patches, flows with its group: E2E exercises each of them on real
clusters (Karpenter provisions GPU nodes and drains them; Argo CD converges on
both profiles and upgrades in place; KEDA scales on Prometheus; DCGM metrics
flow). Each update in the table has its own branch, so it never holds
back the rest of its group, and a failure blames it alone. The Kubernetes
version is read from endoflife.date (`amazon-eks`), one minor per release (see
"Kubernetes upgrade" below).

| Anchor | Where it is set on `main` | What it gates |
|---|---|---|
| **EKS Kubernetes version** | `cluster_config.kubernetes_version` in each env's tfvars (default in `example.tfvars`) | Karpenter (chart, CRD, k8s window), AWS LB Controller, KEDA, metrics-server, EKS managed addons, the `kubernetes` Python client (±1 minor), `alpine/k8s` kubectl (±1 minor) |
| **vLLM serving image** | `vllm_image_tag` in `terraform/30.eks/30.cluster/locals.tf` (single source -> `platform-config` ConfigMap -> kro `externalRef`), mirrored as the schema default in `platform/config/kro/*.yaml` | NVIDIA GPU Operator (driver/CUDA >= vLLM's CUDA runtime), GIE CRDs + llm-d router (routing protocol), which HF architectures/quantizations load |

### Kubernetes upgrade (one minor at a time)

1. Check the gated components on `main` against the target minor: Karpenter,
   AWS LB Controller, KEDA, the EKS Terraform module, `alpine/k8s`, the
   `kubernetes` Python client.
2. Sixty days after the minor's release Renovate opens `renovate/eks-kubernetes`: it changes `kubernetes_version`
   in `example.tfvars` and `example-esc.tfvars` (the default for new clusters).
3. On the `lifecycle` branch, raise the `alpine/k8s` and `kubernetes`
   `allowedVersions` ceilings to the new minor + 1
   (`tests/test_renovate_coverage.py` fails until they match `main`).
4. The next release includes the PR. `release.py notes` writes the manifest as
   supported = previous + new minor, recommended = new minor. E2E: fresh jobs
   build on the new minor; upgrade jobs upgrade on the old minor, then raise
   `kubernetes_version` and `platformctl up` (the `k8s-bump/` checks).
5. Existing environments do not move. After upgrading to the release, their
   owners raise `kubernetes_version` in their tfvars and run `platformctl up`
   (UPGRADING.md, "Kubernetes versions"). The dev canary does the same.

## Upgrade domains (one Renovate branch each)

| Domain | Anchor | Members | Acceptance test |
|---|---|---|---|
| `infra` | k8s version | Karpenter, LBC, KEDA, Terraform providers/modules, `kubernetes` pip, `alpine/k8s` | cluster + all addons healthy; Karpenter provisions a node; ALB provisioned; KEDA scales an llm-d pool (inference check) |
| `inference` | vLLM | vLLM, gpu-operator, GIE CRDs, llm-d router, s5cmd, prometheus | a small model serves a completion on a GPU via `VLLMEndpoint`; DCGM metrics flow; `LLMDEndpoint` routes; under load KEDA scales the llm-d pool 1 -> 2 on vLLM queue depth and Karpenter brings the second GPU replica up |
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
| EKS Kubernetes version | `kubernetes_version = "<v>"` in the example tfvars (source: endoflife.date) |
| Karpenter, kro, AWS LB Controller charts | `karpenter_chart_version`, `kro_chart_version`, `lbc_chart_version = "<v>"`: one local each, read by the chart and by its CRDs |
| Argo CD's Redis image | `argocd_redis_image = "<tag>@<digest>"` |
| Langfuse sub-chart images (Valkey, SeaweedFS) | `repository:` + `tag: "<tag>@<digest>"` in the chart values |
| ApplicationSet charts | `chart:` / `chartRepo:` / `chartVersion:` triplets |
| `PYTHON_IMAGE` | `PYTHON_IMAGE: "python:<v>"` (with an optional digest) |
| pip pins in init containers and runtime-built Jobs | `name==<v>` in the listed files |
| Terraform CLI in CI | `terraform_version: "<v>"` (the github-actions manager's own copy is disabled) |
| Python CI tools | `pip install ... <name>==<v>` in `.github/workflows/*.yaml` |
| kubeconform, kustomize in CI | `kubeconform/releases/download/v<v>/`, `KUSTOMIZE_VERSION="<v>"` |

`tests/test_renovate_coverage.py` holds the inventory and runs weekly against
`main` (`.github/workflows/coverage.yaml`). Verified with Renovate's own
extraction (`renovate --platform=local --dry-run=extract`, see README.md).

Not detected, on purpose: the chart versions of the opt-in observability stage
(`40.observability/45.aws-oss-observability`) are variable defaults
(`helm_chart_version = optional(string, "...")`); the stage is off by default
and no E2E job builds it. EKS managed add-ons are not pins at all (see the
coupling notes).

## Coupling notes (why the pins exist)

- **Charts and their CRDs** (Karpenter, kro, AWS LB Controller): Helm does not upgrade CRDs, so each has one version local on `main` that both the chart and the CRD apply read. Never bump one without the other; Renovate cannot, because there is one literal.
- **AWS LB Controller v3.x**: the chart version tracks the app version since 3.0, and v3 needs `ec2:DescribeRouteTables`, supplied by `lbc_v3_supplement` in `35.addons/main.tf`.
- **Karpenter controller IAM policy size**: from `terraform-aws-modules/eks` v21.29 the controller policy exceeds the 6,144-character managed-policy limit (`LimitExceeded: PolicySize`); `enable_inline_policy = true` in `karpenter.tf` is what keeps it working. Do not remove it.
- **dcgm-exporter field list**: dcgm-exporter 4.8.4 (gpu-operator v26) refuses `DCGM_FI_DEV_PCIE_TX/RX_THROUGHPUT` and crash-loops on every GPU node; both fields are removed from the custom metrics on `main`. A new exporter version can refuse other fields the same way: the DCGM check is what catches it.
- **vLLM**: one source of truth in `locals.tf`; the RGD schema defaults must match (same dep, one branch). The GPU driver must satisfy the image's CUDA runtime, and the llm-d router and GIE CRDs must speak its routing protocol.
- **`kubernetes` Python client / `alpine/k8s`**: within one minor of the API server. `allowedVersions` in `renovate.json` holds the ceiling, (EKS default minor + 2) exclusive, and is raised by hand with the Kubernetes anchor (`tests/test_renovate_coverage.py` fails until it matches `main`).
- **Python base image vs. pinned wheels**: the dashboard and the Platform Health Agent build Jobs at runtime that `pip install psycopg[binary]==...` on `PYTHON_IMAGE`; those pins live in Python string literals (`backend.py`, `event_watcher.py`), read by the pip manager. Every pinned wheel must exist for a new python minor. The coverage test fails when one package is pinned to two versions under `platform/services/`.
- **LiteLLM**: follows the long-term-support tags (`vX.Y.Z-lts`). litellm-sync and the dashboard use its admin API (`/model/*`, `/team/*`, `/key/*`), and minors change the database schema, which is why minors wait 14 days on their own branch.
- **LiteLLM role names**: Cognito's `custom:role` is `internal_user_view_only`; LiteLLM's UI session reports `internal_user_viewer` (enum value). Both are correct.
- **Open WebUI model visibility**: a model with no workspace access record is visible only to admins; the chat-attribution check signs in as the admin for that reason. Platform policy.
- **Postgres**: a major bump is a dump/restore of the shared database (LiteLLM, Langfuse, the dashboard); never a plain tag change. A new major cannot start on the old data directory. `renovate.json` therefore keeps Postgres on its current major (`allowedVersions`), so the next one is not proposed until `platformctl upgrade` has a migration step; `tests/test_renovate_coverage.py` fails if `main` moves to another major without the rule.
- **Langfuse**: chart v2 and later need `redis.auth.usersExistingSecret` alongside `existingSecret` (`helm template` fails otherwise). Its ClickHouse stays on LTS lines (x.3, x.8). Read the chart's notes on a major: v2 to v3 moved the data store to ClickHouse.
- **Argo CD (self-managed path)**: pinned, with its Redis image pinned by digest. An unpinned GitOps engine can break the mechanism used to recover from any other break; the public repository's state before the first release left it unpinned.
- **EKS managed add-ons**: `most_recent = true`, so every apply takes the newest build for the cluster's Kubernetes version; drift is reported weekly by `addon-freshness` on `main`. Out of Renovate's scope by design.
- **Release age**: only the updates in the table at the top wait for an age. A 7-day age on everything was tried first and dropped: it froze whole group branches while one update was pending (so they were not rebased after a release), and Docker Hub re-pushes of official images (postgres, python) restarted the clock every few days.

## Renovate branches, majors, and the dev canary

How a cycle is cut, tested, merged, released and published is in README.md
("The factory: cycles and releases"); `tools/release_candidate.py` holds the
rules. What that description assumes about the branches:

- Renovate opens grouped branches against `main` (Monday window) and rebases
  them whenever `main` moves. Their PRs are never merged and never closed: a
  cycle's `release/<version>` branch takes the branches in. `release.py cut`
  refuses a branch that does not contain the newest release tag (Renovate did
  not run, or someone pushed to it).
- Majors exist as PRs only after a tick on the Dependency Dashboard; read the
  coupling notes first. An open major PR therefore means "approved for the next
  cycle". To take that back, close the PR: Renovate offers it again on the
  dashboard. The label `hold` keeps any PR out of the cycles without closing it.
- A fix that must travel with an update (a coupled fix) goes to `main` as an
  ordinary PR first. The next cycle tests `main` and the update together; if
  the update cannot pass without the fix, the fix is what makes it pass.
- The manifest of a release must exist before its E2E: the candidate's
  `./platformctl upgrade` refuses a target without one. `release.py notes`
  writes it; add `acknowledged_replacements` by hand when the upgrade plan
  guard needs them (hand edits are carried into the next cycle's manifest).

The dev canary is the one step after a release that a person does: in the
long-lived dev environment's checkout (branch `env/dev`),
`UPGRADE_REMOTE=origin ./platformctl upgrade dev --plan-only`, then without
`--plan-only`, then `make -C terraform plan-all ENVIRONMENT=dev` (expect no
changes). It is the same command a fork runs against the public repository.
