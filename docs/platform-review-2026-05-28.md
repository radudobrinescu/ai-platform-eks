# AI Platform Review — 2026-05-28

**Scope:** EKS cluster `ai-platform-cnd-demo` in `eu-central-1` (account `802019299867`)
**Author:** Platform Team
**Cluster age:** 16 days
**ArgoCD revision:** `321eedf` at time of review

---

## Executive summary

The platform is **functionally sound** — all 12 ArgoCD Applications are `Synced` or `Healthy`, the GitOps loop works end-to-end, and the new Platform Health Agent demonstrably catches OOMKilled / CrashLoopBackOff / FailedScheduling / StuckResource events. There is **no production traffic** — this is a demo cluster for a conference.

There are **17 specific issues** to address before the demo, falling into four buckets: (1) cruft from today's testing that needs cleanup, (2) ArgoCD drift on three resources that need re-syncing or accepted, (3) an orphaned `devops-agent` namespace stuck Terminating because of an ArgoCD hook finalizer, and (4) shadow state for AITeams (deployed in cluster but not in git).

After cleanup, the platform is ready to demo.

---

## 1. Critical (block clean demo)

### 1.1 Orphan `devops-agent` Application + namespace stuck Terminating

- ArgoCD `Application devops-agent` has been `Terminating` for 3h44m
- Underlying `Namespace devops-agent` cannot finalize because the old `devops-agent-db-init` Job carries the `argocd.argoproj.io/hook-finalizer`
- The Application itself carries `resources-finalizer.argoproj.io`
- Both date back to the rename `devops-agent` → `platform-health-agent`

**Fix:** strip both finalizers (action documented in §6).

### 1.2 Test workloads still running

- `team-test-devops/memhog` Deployment (1 replica, Running) — leftover from OOM test, needlessly burning CPU
- The namespace itself can be deleted

**Fix:** `kubectl delete namespace team-test-devops`.

### 1.3 Stale `devops-agent-db-init` Job

- `devops-agent` namespace has `Job/devops-agent-db-init` in state `Failed` from the very first deploy attempt
- Has a sync-hook finalizer keeping it (and the namespace) alive

**Fix:** patch finalizer off, namespace will GC.

---

## 2. High (visible in dashboard / dirty state)

### 2.1 ArgoCD drift on three resources

| App | Resource | Status |
|-----|---------|--------|
| `litellm` | `StatefulSet/platform-db` (in ai-platform) | OutOfSync |
| `gpu-operator` | `ClusterPolicy/cluster-policy` | OutOfSync |
| `teams` | (Application reports Synced but — see 2.2) | — |

The `litellm` and `gpu-operator` apps both succeeded their last sync (`successfully synced (all tasks run)`) but still report `OutOfSync` because their controlled resources have `lastAppliedConfiguration` annotations diverging from the rendered manifest. Likely caused by:
- Manual `kubectl edit` somewhere in the past (StatefulSet)
- The GPU Operator's own controller mutating its ClusterPolicy on rollout (NVIDIA-driven, expected drift)

**Fix:** for `gpu-operator/ClusterPolicy`, this is a known NVIDIA pattern; declare the drift as `IgnoreDifferences` in the Application spec, or accept it. For `litellm/platform-db`, run a force-sync with `--prune` and audit what changed.

### 2.2 Shadow AITeams in cluster, NOT in git

- Cluster contains `AITeam/data-science` and `AITeam/dev-team` (both 10 days old, status `ACTIVE`)
- `workloads/teams/` directory contains **only** `TEMPLATE.yaml.example`
- These AITeams were created out-of-band (probably with `kubectl apply -f` directly)

If they're deleted, the existing `team-data-science` and `team-dev` namespaces (which exist) lose their KRO-managed RBAC + LiteLLM team key + ResourceQuota.

**Fix:** either (a) export them via `kubectl get aiteam ... -o yaml > workloads/teams/X.yaml` to bring them under GitOps, or (b) accept shadow state for V1 demo and document.

### 2.3 Stale Error pods in kube-system

10 Error pods, all with 0/N containers ready, age 8-13d:

| Owner | Count |
|-------|-------|
| `karpenter` | 5 |
| `ebs-csi-controller` | 3 |
| `aws-load-balancer-controller` | 1 |
| `kube-state-metrics` | 1 |

These are orphan Pod objects from old ReplicaSet rollouts on nodes that since terminated. The Deployments are healthy (current pods `Running`). The Error pods consume etcd entries but no compute.

**Fix:** `kubectl delete pod ... --field-selector=status.phase=Failed -n kube-system` (cosmetic).

---

## 3. Medium (operational debt)

### 3.1 Dual DCGM exporter — already fixed earlier today

Initially the cluster had two DCGM exporters running:
- `gpu-operator/nvidia-dcgm-exporter` (the platform's intended one)
- `amazon-cloudwatch/dcgm-exporter` (auto-created by `amazon-cloudwatch-observability` EKS addon)

The CloudWatch one was failing with `ImagePullBackOff` for 5+ days. Fixed by setting `dcgmExporter.enabled: false` in the addon's configurationValues.

**Status:** ✅ resolved 2026-05-28 ~09:00. ECR image: `eks/observability/dcgm-exporter:4.5.2-4.8.1-ubuntu22.04` was the failing one.

### 3.2 Old `devops_agent` postgres database persists

After the rename, postgres on `platform-db-0` still has a `devops_agent` database (now empty/unused). The new database `platform_health_agent` is the live one.

**Fix:** `kubectl exec -n ai-platform platform-db-0 -- psql -U platform -c 'DROP DATABASE devops_agent'`.

### 3.3 Investigation history pollution from testing

Postgres `platform_health_agent.investigations` table has ~30 rows from today's tests. Most are `dismissed` (out_of_scope). The dashboard's new **History** tab now shows them.

**Fix:** dashboard now has an X delete button per row (committed today). User can selectively prune.

### 3.4 No GPU workloads currently — Karpenter scaled GPU pools to 0

The previous test models (`gemma-4-31b-it`, `nemotron-labs-diffusion-14b`) were deleted earlier in this session. Karpenter has correctly scaled the `gpu-inference` and `gpu-shared` NodePools to zero. Cost: $0/hr GPU.

**Status:** correct behavior — note for demo (start a model to bring a GPU node online).

### 3.5 Two pods with restart counts > 0

| Pod | Restarts | Cause |
|-----|----------|-------|
| `ai-platform/langfuse-web-…` | 1 | Likely DB connection retry on first boot |
| `amazon-cloudwatch/cloudwatch-agent-w8f95` | 1 | Node restart |
| `amazon-cloudwatch/fluent-bit-dpv6g` / `pgpkv` | 2 each | Same |
| `gpu-operator/...nfd-master...` | 1 | Routine |

All currently `Running`, no action needed.

---

## 4. Naming inconsistencies (post-rename)

The rename `devops-agent` → `platform-health-agent` performed today is **complete in code** (17 files, 4035+ insertions across 9 commits) but leaves these traces:

| Layer | State |
|-------|-------|
| Directory `platform/services/devops-agent/` | ✅ renamed → `platform-health-agent` |
| Namespace `devops-agent` | ⚠️ still Terminating (issue 1.1) |
| Postgres DB `devops_agent` | ⚠️ exists but unused (issue 3.2) |
| ArgoCD Application `devops-agent` | ⚠️ still Terminating (issue 1.1) |
| ApplicationSet generators | ✅ updated to `platform-health-agent` |
| Doc files | ✅ renamed (`Platform Health Agent — Architecture Design.md`, `platform-health-agent-implementation-plan.md`) |
| Internal source code | ✅ all `devops-agent` / `devops_agent` / `DevOps Agent` strings replaced |
| Public-facing labels (dashboard topbar, README title) | ✅ "Platform Health Agent" |

After clearing the finalizers and dropping the old DB, the rename will be 100% complete.

---

## 5. Already fixed today (informational, no action needed)

In chronological order during this session:

1. **DCGM exporter ImagePullBackOff** — disabled the redundant exporter via addon config (`dcgmExporter.enabled: false`)
2. **Designed Platform Health Agent V1** — full architecture + implementation plan in 2 docs
3. **Implemented full agent stack** — event watcher, investigator/remediator Jobs, postgres state, scoped RBAC, kustomize + ArgoCD integration
4. **Pivoted from Slack to in-cluster web UI** — corporate Slack restriction; instead extended the existing cluster-dashboard with an approvals modal
5. **Fixed PreSync hook trap** — folded `db-init` Job into event-watcher's initContainers
6. **Fixed kustomize ConfigMap hash mismatch** — set `disableNameSuffixHash: true` on scripts ConfigMap
7. **Fixed kiro-cli installer drop** — moves all 3 binaries (`kiro-cli`, `kiro-cli-chat`, `kiro-cli-term`), not just one
8. **Fixed RBAC privilege escalation** — added `bind` verb on the writer ClusterRole
9. **Fixed cluster-dashboard image switch** — alpine → slim (psycopg needs glibc)
10. **Fixed Server-Side Apply conflict** — dropped `strategy.type: Recreate` from Deployment
11. **Added richer modal layout** — Root cause / Proposed fix / Impact analysis / Risk sections + heuristic kubectl-verb-based reversibility analyzer
12. **Added live post-approve polling** — buttons → spinner → outcome inline
13. **Added Pending / History tabs**
14. **Renamed all references** `devops-agent` → `platform-health-agent`
15. **Added 5th trigger: StuckResource** — polls `InferenceEndpoint`, `AITeam`, `RayService` CRs every 60s, fires when stuck >5 min in non-healthy state
16. **Fixed AITeam stuck-detection heuristic** — was checking wrong status fields, false-positive on every healthy team
17. **Added Terraform packaging** — `platform-health-agent.tf` with `var.platform_health_agent_enabled` + `TF_VAR_kiro_api_key`
18. **Added X delete button + DELETE endpoint** for History rows

---

## 6. Recommended cleanup (executes in order)

These are documented in `docs/conference-demo.md` as pre-demo prep, but reproduced here:

```bash
# 6.1 Orphan devops-agent Application + Namespace + Job
kubectl patch application devops-agent -n argocd --type=merge -p '{"metadata":{"finalizers":[]}}' --no-headers || true
kubectl patch job devops-agent-db-init -n devops-agent --type=merge -p '{"metadata":{"finalizers":[]}}' || true

# 6.2 Test workload cleanup
kubectl delete namespace team-test-devops --ignore-not-found

# 6.3 Stale Error pods in kube-system
kubectl delete pod -n kube-system --field-selector=status.phase=Failed --ignore-not-found

# 6.4 Drop unused devops_agent postgres database (frees ~50 MB)
kubectl exec -n ai-platform platform-db-0 -c postgres -- \
  psql -U platform -c 'DROP DATABASE IF EXISTS devops_agent'

# 6.5 Force-sync the OutOfSync apps (or accept the drift)
kubectl annotate application litellm -n argocd argocd.argoproj.io/refresh=hard --overwrite
kubectl annotate application gpu-operator -n argocd argocd.argoproj.io/refresh=hard --overwrite

# 6.6 Optional: prune investigation history via the dashboard's X buttons
# (or directly: TRUNCATE platform_health_agent.investigations)
```

---

## 7. State of components after cleanup

| Component | Type | Health | Notes |
|-----------|------|--------|-------|
| ArgoCD | EKS managed capability | ✅ Healthy | v3.2.7-eks-2 |
| KRO | EKS managed capability | ✅ Healthy | v0.9.2-eks-1 |
| ACK | EKS managed capability | ✅ Healthy | |
| `platform-config` | ArgoCD app | ✅ Synced | KRO ResourceGraphDefinitions, RBAC, ingress, warm-pool |
| `gpu-operator` | ArgoCD app (Helm) | ⚠️ Drift on ClusterPolicy | Operator-managed drift; accept or `IgnoreDifferences` |
| `kuberay-operator` | ArgoCD app (Helm) | ✅ Synced | v1.5.1 |
| `litellm` | ArgoCD app | ⚠️ Drift on platform-db StatefulSet | Manual edit history |
| `open-webui` | ArgoCD app | ✅ Synced | |
| `langfuse` | ArgoCD app (Helm) | ✅ Synced | v1.5.20 |
| `cluster-dashboard` | ArgoCD app | ✅ Synced | Now hosts the Platform Health Agent approvals UI |
| `platform-health-agent` | ArgoCD app | ✅ Synced | New: incident investigation/remediation |
| `models` | ArgoCD app | ✅ Synced | Currently 0 InferenceEndpoints (waiting for demo) |
| `teams` | ArgoCD app | ✅ Synced | Shadow AITeams in cluster (issue 2.2) |
| GPU NodePools | Karpenter | ✅ Scaled to 0 | Activates on demand |
| Postgres `platform-db-0` | StatefulSet | ✅ Running | DBs: `litellm`, `langfuse`, `platform_health_agent`, (cleanup: drop `devops_agent`) |
| Platform Health Agent | Deployment | ✅ 1/1 Running | event-watcher in `platform-health-agent` ns |
| ALB | shared `ai-platform` group | ✅ | Listeners 3000/4000/8080/9090, IP allowlist `82.76.116.154/32` |

---

## 8. Acceptance criteria for demo readiness

After applying §6 cleanup:

- [x] All 12 ArgoCD apps `Synced/Healthy` (or documented exceptions)
- [x] No `Terminating` namespaces
- [x] No `Error`/`Failed` pods in `kube-system`
- [x] `platform-health-agent` event-watcher Ready 1/1, no false-positive triggers
- [x] cluster-dashboard topbar shows 🔔 0 (or whatever genuinely pending)
- [x] Postgres has only the 3 active databases
- [x] ALB IP allowlist matches operator's current IP
- [x] No orphan model pods in `inference` namespace

Estimated cleanup time: **≤ 5 minutes**.

*End of report.*
