# Self-Healing EKS Agent — Standalone Product Plan

**Status:** Draft for review
**Author:** Platform Team
**Date:** 2026-05-30
**Working product name:** **Kubernetes Reliability Agent (KRA)** — *placeholder; see Decision #1.* Must avoid the existing clash with AWS's "Platform Health" naming (already noted in the agent README).
**Scope:** Part 2 of a two-part strategy. Part 1 (turnkey platform, fine-tuning, agentic workflows) is in [`platform-evolution-plan.md`](./platform-evolution-plan.md).

---

## 0. Summary

The Platform Health Agent built into this repo is, underneath the platform-specific wiring, a **general-purpose autonomous incident-response engine for Kubernetes**: it watches the cluster for failure signals, dispatches an LLM-driven investigator with read-only access to diagnose root cause, proposes a bounded `kubectl` fix, and — after approval — runs a remediator with scoped write access that applies and verifies the fix. The security boundary is **Kubernetes RBAC, not the prompt** ("even if the LLM hallucinates `kubectl delete deployment kube-dns -n kube-system`, the API server rejects it with 403").

That engine is valuable on its own. This plan extracts it into a **standalone product that installs on any EKS cluster** (and ultimately any Kubernetes cluster), defaults to **Amazon Bedrock** for the LLM, ships its own state + console + hardened images, adds an **autonomy policy** (observe / approve / auto-heal), and is **listed on AWS Marketplace as a container offering** with usage-based metering.

The strategic fit is strong:
- It's **AWS-native** — Bedrock for reasoning, the `awslabs.eks-mcp-server` for cluster tools (already used here), Marketplace for billing, EKS add-on for distribution. **No data leaves the customer's account**, which is the #1 objection for AI-in-production at security-conscious buyers.
- It's **closed-loop** — most tools in this space diagnose; few safely *remediate*. The RBAC-as-boundary + autonomy-policy design lets it remediate responsibly.
- It **reuses ~70% of what already exists** (detection, the investigator/remediator pattern, the postgres state machine, the approvals UX, the impact analyzer).

---

## 1. The opportunity

**Problem:** Kubernetes failures (CrashLoopBackOff, OOMKilled, ImagePullBackOff, FailedScheduling, NodeNotReady, stuck rollouts) are constant, noisy, and burn on-call time. Diagnosis is repetitive ("read the events, read the logs, walk the owner chain, check the node"). Remediation is often a one-line patch the LLM can propose reliably.

**Today's tools (landscape):**
- **k8sgpt** — popular OSS diagnoser; explains issues, **does not remediate**, BYO-LLM.
- **Robusta / Komodor / Causely / PagerDuty AIOps** — alerting, troubleshooting, some automation; mostly **SaaS** (data leaves the account), subscription-priced, not AWS-native.
- **Generic LLM ops copilots** — chat-based, human drives every step.

**The gap we fill:** an **AWS-native, closed-loop, RBAC-safe, autonomy-configurable** agent that runs entirely in the customer's account, bills through their existing AWS relationship (Marketplace), and is one click to install (EKS add-on). That combination doesn't exist off-the-shelf today.

**Who buys it:**
- **SMBs / lean platform teams** — no SRE bench; want auto-healing of the boring 80% of incidents. Per-cluster pricing, observe-then-auto.
- **Enterprises** — many clusters; want a fleet view, audit trail, approval workflows, data-residency guarantees, and integration with their incident tooling. Per-node or contract pricing.

---

## 2. Decoupling matrix — what's reusable vs platform-coupled

Grounded in the actual code (`platform/services/platform-health-agent/` + the approvals code in `cluster-dashboard/scripts/backend.py`).

| Component | File(s) | Reusable as-is? | Coupling to remove |
|---|---|---|---|
| **Event detection** (CrashLoop/OOM/ImagePull/FailedSched/NodeNotReady/FailedMount; debounce; owner-chain dedup; concurrency + daily caps) | `scripts/event_watcher.py` (detect_* fns, `should_throttle`) | 🟢 **Fully generic** | None — this is product-ready logic |
| **Investigator/Remediator Job pattern** (read-only SA → investigate; scoped-write SA → remediate; activeDeadline; ttl) | `event_watcher.py` `build_investigator_job`, `backend.py` `build_remediator_job`, `investigate.sh`, `remediate.sh` | 🟢 Pattern is excellent | LLM is hardcoded to `kiro-cli`; image built by runtime init-container downloads |
| **Cluster tool surface** (`awslabs.eks-mcp-server`, read-only vs `--allow-write`) | `investigate.sh`, `remediate.sh` MCP config | 🟢 **Already AWS-native & generic** | None — keep; this is a strength |
| **State machine** (investigations table, statuses, debounce, daily_counters) | `scripts/ddl.sql`, `persist_findings.py` | 🟢 Schema is clean | Lives on the shared **`platform-db`**; product needs its own store |
| **Approvals + audit UX** (pending/history, approve/dismiss/delete, impact analyzer, post-approve polling) | `cluster-dashboard/scripts/backend.py`, HTML | 🟢 Great UX | Embedded in **this platform's dashboard**; product needs its own console |
| **RBAC safety model** (reader ClusterRole; writer ClusterRole bound per-namespace; `bind` verb anti-escalation) | `rbac.yaml` | 🟢 Security model is the moat | Writer scope hardcoded to `^(inference\|team-.+)$`; must be configurable |
| **StuckResource trigger** (polls CRs, fires if unhealthy > threshold) | `event_watcher.py` `watch_stuck_resources`, `_is_stuck_*` | 🟠 Concept generic | Hardcodes **`InferenceEndpoint`/`AITeam`/`RayService`** — generalize to config-driven GVK + health expression |
| **Namespace reconciler** (auto-creates writer RoleBindings in `team-*`) | `event_watcher.py` `reconcile_rolebindings_loop` | 🟠 Useful pattern | `ALLOWED_REMEDIATION_NAMESPACES_RE` hardcoded; make label/selector-driven |
| **Excludes / config** | `configmap.yaml` | 🟠 | Hardcodes platform namespaces (`gpu-operator`, `kuberay`, …) and `CLUSTER_NAME: ai-platform-cnd-demo`; ship neutral defaults |
| **LLM** | `kiro-cli` everywhere | 🔴 | Proprietary, needs Kiro key. Replace with **Bedrock default** + pluggable backends |
| **Image strategy** | init-containers download `kubectl`+`kiro-cli`+`pip install` at pod start | 🔴 | Marketplace requires **scanned, versioned ECR images**; runtime downloads are slow, fragile, and unscannable |
| **Terraform provisioning** | `30.cluster/platform-health-agent.tf` | 🔴 | Tied to this repo's Terraform; product ships as **Helm chart / EKS add-on** |

**Takeaway:** the engine and its security model are product-grade. The work is (1) swap the LLM to Bedrock, (2) cut the three platform umbilicals (DB, dashboard, Terraform), (3) generalize the platform-specific config, (4) harden into images + Helm, (5) add autonomy policy + integrations + metering.

---

## 3. Target architecture (standalone)

```
┌──────────────────────── Customer's EKS cluster ────────────────────────┐
│                                                                          │
│  ┌───────────────┐   K8s events / CR health / Prometheus alerts          │
│  │ event-watcher │◀──────────────────────────────────────────────       │
│  │  (Deployment) │                                                        │
│  │  reader SA    │── creates Job ──▶ ┌─────────────────────┐              │
│  └──────┬────────┘                   │ Investigator Job    │              │
│         │                            │ Bedrock + eks-mcp   │  reader SA   │
│         │ writes                     │ (read-only)         │  (RBAC=ro)   │
│         ▼                            └─────────┬───────────┘              │
│  ┌───────────────┐                             │ findings                 │
│  │  State store  │◀────────────────────────────┘                          │
│  │ DynamoDB (def)│                                                         │
│  │ or Postgres   │── autonomy policy ──┐                                   │
│  └──────┬────────┘                     │                                   │
│         │ poll/stream                  │ auto-heal (low risk)              │
│         ▼                              ▼                                   │
│  ┌───────────────┐  approve   ┌─────────────────────┐                     │
│  │  KRA Console  │───────────▶│ Remediator Job      │  writer SA          │
│  │ (bundled UI)  │            │ Bedrock + eks-mcp   │  (RBAC=scoped write)│
│  │  + /healthz   │            │ (--allow-write)     │                     │
│  └───────────────┘            └─────────┬───────────┘                     │
│         │                               │ verify + result                  │
│         │ also notify                   ▼                                   │
│         ├─▶ Slack / Teams / PagerDuty   (back to State store)              │
│         └─▶ GitHub/GitLab PR (GitOps-aware remediation)                    │
│                                                                            │
│  IRSA: Bedrock (InvokeModel/Converse) · Marketplace Metering · (opt) CW    │
└────────────────────────────────────────────────────────────────────────┘
         │ metered usage (MeterUsage)            │ traces/logs
         ▼                                        ▼
  AWS Marketplace Metering Service        CloudWatch / Langfuse (optional)
```

**Namespace:** installs into `kube-system`-adjacent `kra-system` (configurable). Nothing depends on this AI platform.

**Components:**
1. **event-watcher** — the existing detection engine, generalized (config-driven triggers, namespace scoping).
2. **Investigator/Remediator Jobs** — same two-SA pattern, but driven by a **Bedrock agent loop** over `eks-mcp-server` tools, from a **hardened image**.
3. **State store** — **DynamoDB by default** (serverless: nothing to run, scales to zero, multi-cluster-friendly), with a Postgres/RDS option for customers who prefer it. (Today's Postgres schema ports directly; DynamoDB needs a small access-layer rewrite.)
4. **KRA Console** — a standalone, single-binary web UI (extracted from the dashboard's approvals code) for pending/history/approve/dismiss + the impact analyzer. Optional — headless installs use chat-ops/PagerDuty instead.
5. **Autonomy policy engine** — decides observe / approve / auto-heal per the impact classification.
6. **Integrations** — Slack/Teams/PagerDuty notifications; GitHub/GitLab PRs for GitOps-managed clusters.
7. **Metering** — a CronJob/sidecar reporting usage to AWS Marketplace Metering Service.

---

## 4. LLM abstraction — Bedrock by default, pluggable

This is the most important decoupling. Replace `kiro-cli` with a thin **agent runner** that speaks to a pluggable backend, **defaulting to Amazon Bedrock**.

### Why Bedrock as default
- **AWS-native**: customers already have Bedrock access; no third-party key, no new vendor relationship.
- **In-account**: with a Bedrock VPC (PrivateLink) endpoint, inference traffic never leaves the customer's VPC — the data-residency story that wins enterprise deals.
- **Billing alignment**: model spend lands on the customer's AWS bill, separate from our Marketplace fee — clean and transparent.
- **Reuses Part 1's "Bedrock engine"** (the Converse + tool-use loop with IRSA), so the two products share a library.

### Design
- An **`agent-runner`** (Python; reuse the existing `eks-mcp-server` MCP integration). Backends behind one interface:
  - `bedrock` (**default**) — Converse API with tool use; models configurable (`anthropic.claude-sonnet-4-x` for investigation, a stronger model for remediation — mirrors today's sonnet/opus split).
  - `bedrock-agentcore` (optional) — run the investigator as an AgentCore Runtime agent for managed memory/observability (ties to Part 1 C4).
  - `openai` / `anthropic` (BYO API key) — for non-Bedrock shops.
  - `kiro` — keep as a backend for continuity with this platform.
  - `self-hosted` — point at any OpenAI-compatible endpoint (e.g., a LiteLLM/vLLM in-cluster), for air-gapped/cost-sensitive users.
- Tooling stays **`awslabs.eks-mcp-server`** — read-only for investigation, `--allow-write` for remediation. This is already proven here and keeps the safety model identical across backends.
- **Recommendation:** build the agent loop on the **Strands Agents SDK** (AWS's open-source agent framework) for the Bedrock path — it natively does Bedrock + MCP tool use and is the same framework Part 1's in-cluster agents use. The loop is small either way; Strands removes boilerplate and aligns the two products.

> **Why not keep kiro-cli?** It requires a proprietary key and credit-based billing that Marketplace customers won't have, and it's a third party in the data path. Bedrock removes both blockers. Keeping kiro as *a* backend preserves this platform's current behavior with zero regression.

---

## 5. Autonomy & safety model

The single biggest product lever: **let the customer choose how much the agent does on its own**, and make that choice safe.

### Three modes (per-cluster default, override per-namespace and per-trigger)
| Mode | Behavior | Use case |
|---|---|---|
| **observe** | Investigate + report; **never remediate**. | Trials, trust-building, regulated change windows |
| **approve** | Propose a fix; require human approval (today's behavior) | Most production |
| **auto** | Auto-apply fixes the policy deems low-risk; escalate the rest to approval | Mature teams, off-hours, dev clusters |

### Policy engine — built on the impact analyzer we already have
The dashboard already computes **reversibility** and **disruption** heuristically by parsing `kubectl` verbs (e.g., "rollout restart = fully reversible, brief disruption"). That is exactly the signal an auto-heal policy needs. Formalize it:

```yaml
autonomy:
  default: approve
  rules:
    - when: { trigger: [CrashLoopBackOff, OOMKilled], reversibility: full, disruption: brief }
      mode: auto                       # safe, common, reversible → auto-heal
    - when: { namespace: "prod-*" }
      mode: approve                    # never auto-touch prod
    - when: { verbs: [delete] , scope: "!pods" }
      mode: approve                    # deletes (except pod-restart) always need a human
    - when: { trigger: NodeNotReady }
      mode: observe                    # infra issues: report, don't act
  autoHeal:
    maxPerHour: 5                      # rate limit auto-actions
    requireVerification: true          # must verify healthy post-fix or auto-rollback
    rollbackOnVerifyFail: true
```

### Safety boundaries (keep + strengthen what's already proven)
- **RBAC is the boundary, not the prompt** — preserve the reader/writer split; writer scope is **customer-configured** (label selector / namespace list), not hardcoded.
- **`bind`-verb anti-escalation** — keep (the writer ClusterRole can only be bound by an SA with the explicit `bind` permission).
- **Bedrock Guardrails** (optional) on the agent's reasoning to prevent unsafe command generation — defense in depth atop RBAC.
- **Dry-run first** — remediator runs `kubectl --dry-run=server` (or MCP equivalent) and diff before applying; auto-heal requires a clean dry-run.
- **Verify-or-rollback** — auto-heal must confirm the resource reaches healthy state or it runs the rollback commands the remediator already produces.
- **Caps everywhere** — daily investigation/remediation budgets and concurrency caps already exist; auto-heal adds an hourly action cap.
- **Full audit** — every investigation, decision, approval, and action is recorded (the investigations table already does this) and exportable to CloudWatch/S3 for compliance.

---

## 6. Generalization work (make it cluster-agnostic)

| Item | Today | Product |
|---|---|---|
| **Namespace scope** | `watchNamespaces`/excludes partly configurable; **writer regex hardcoded** `^(inference\|team-.+)$` | `watchNamespaces`, `remediationNamespaces` as namespace lists **or label selectors**; reconciler creates writer RoleBindings to match |
| **Custom-resource health (StuckResource)** | Hardcoded `InferenceEndpoint`/`AITeam`/`RayService` with bespoke `_is_stuck_*` functions | Config-driven: list of `{group, version, kind, healthyWhen: <CEL/JSONPath>, thresholdSec}`. Ships built-in profiles for common stacks (Argo Rollouts, Flux Kustomization/HelmRelease, KEDA, cert-manager, Crossplane, RayService, KubeRay, Strimzi…) |
| **Triggers** | 6 K8s signals + StuckResource | Same + **Prometheus Alertmanager webhook** and **CloudWatch alarm** triggers (alert → investigation). Broadens from "pod crashed" to "SLO/symptom breached" |
| **Excludes / cluster name** | Platform namespaces hardcoded; `CLUSTER_NAME` literal | Neutral defaults (`kube-system`, `kube-node-lease`, …); cluster name auto-detected from EKS metadata |
| **GitOps awareness** | Flags fixes that require editing git as `out_of_scope` (good) | **Detect the GitOps controller** (ArgoCD/Flux). If a resource is GitOps-managed, *don't* fight selfHeal — instead **open a PR** with the fix (GitOps-native remediation) and link it in the console. Imperative apply only for non-GitOps-managed resources |
| **Runbooks** | None | Customer-supplied runbooks loaded as **MCP context / retrieval** so investigations follow the team's playbooks (the agent's own README lists this as future work). Strong enterprise feature |

The **GitOps-native remediation** point deserves emphasis: it turns the agent from "applies imperative fixes ArgoCD might revert" into "proposes the fix the right way for your delivery model" — imperative where safe, a reviewed PR where the cluster is GitOps-managed. That's the difference between a demo and something an enterprise platform team will actually run.

---

## 7. State, console, and notifications (cutting the platform umbilicals)

### State store
- **Default: DynamoDB.** Serverless, no DB to operate, pay-per-use, naturally multi-cluster (partition by cluster id). Best fit for a product a customer installs and forgets. Requires porting `persist_findings.py`/`backend.py` queries from SQL to a small DynamoDB access layer (the schema is simple: one `investigations` item collection + counters).
- **Option: Postgres/RDS.** For customers who want SQL/BI access. The existing `ddl.sql` ships unchanged as the RDS schema. (Bundled in-pod Postgres only for `dev` installs.)
- Abstract behind a `StateStore` interface so both are drop-in.

### Console
- Extract the dashboard's approvals UI (pending/history/approve/dismiss/delete, impact analyzer, post-approve polling — all in `backend.py` today) into a **standalone single-container console** (`kra-console`), served on its own Service/Ingress with the same ALB-allowlist security model.
- **Auth:** ship with OIDC/Cognito support (the current dashboard relies on ALB IP allowlist + `X-Forwarded-For` for best-effort audit; a product needs real authn for approvals). This is also listed as future work in the agent README.
- Console is **optional**: headless installs approve via Slack/Teams interactive messages (the *original* design doc's Slack flow — we ship it as one channel among several) or run in `auto`/`observe` mode with no human UI.

### Notifications / approvals channels
Pluggable notifier: **Console** (bundled), **Slack**, **MS Teams**, **PagerDuty/Opsgenie**, **email/SNS**. Approvals can come from the console or an interactive Slack/Teams message. This directly resurrects and generalizes the architecture doc's original Slack Block Kit design, now as one option rather than the only one.

---

## 8. Packaging — Helm chart, EKS add-on, hardened images

Marketplace and real-world installs both require proper packaging.

### Hardened container images (replace runtime downloads)
Today investigator/remediator pods **download `kubectl` + `kiro-cli` and `pip install` at startup** via init-containers (clever for a demo; unacceptable for a product — slow, fragile, unscannable, and won't pass Marketplace image scanning). Build **versioned, scanned images**:

- `kra-agent-runner` — Python + Strands + `awslabs.eks-mcp-server` + `kubectl` + AWS SDK, pinned and scanned. Used by investigator and remediator (read vs write decided by RBAC + MCP flag, as today).
- `kra-event-watcher` — the watcher with `kubernetes` + state-store client baked in.
- `kra-console` — the standalone UI.

Build via the same ECR pull-through + image-optimization machinery the platform already uses for the Ray image; multi-arch (amd64 **and arm64** — the agent README flags ARM64 as missing, and Graviton is a cost win for an always-on watcher).

### Helm chart
A single chart `kra` with values that expose everything above:
```yaml
llm:
  backend: bedrock                      # bedrock | bedrock-agentcore | openai | anthropic | kiro | self-hosted
  investigateModel: anthropic.claude-sonnet-4-6
  remediateModel: anthropic.claude-opus-4-x
  bedrock: { region: "", privateLink: false }
autonomy:
  mode: approve                          # observe | approve | auto
  rules: [...]
scope:
  watchNamespaces: ["*"]
  excludeNamespaces: ["kube-system", "kube-node-lease"]
  remediationNamespaces: []              # empty = none (observe-only by default until set)
triggers:
  k8s: { crashLoop: true, oom: true, imagePull: true, failedSched: true, nodeNotReady: true, failedMount: true }
  customResources: [ { group: argoproj.io, version: v1alpha1, kind: Rollout, healthyWhen: "...", thresholdSec: 300 } ]
  prometheus: { enabled: false, alertWebhook: "" }
state: { backend: dynamodb }             # dynamodb | postgres
console: { enabled: true, auth: oidc }
notifiers: { slack: {...}, pagerduty: {...} }
gitops: { mode: pr, provider: github }   # pr | imperative | off
metering: { marketplaceProductCode: "", dimension: "node-hour" }
serviceAccount: { bedrockRoleArn: "", meteringRoleArn: "" }
```

### EKS add-on
Package the Helm chart as an **EKS add-on** so it appears in the EKS console "Add-ons" tab and installs/updates with one click (and supports Marketplace-integrated add-ons). This is the lowest-friction distribution path for EKS customers and a strong differentiator vs SaaS competitors.

### IRSA / Pod Identity
Ship the IAM policy documents (least-privilege Bedrock invoke, optional Marketplace metering, optional CloudWatch) and wire via **EKS Pod Identity** (newer, simpler than IRSA trust policies) with IRSA as fallback. The platform's existing IRSA pattern (`capabilities.tf` `inference_worker`) is the template.

---

## 9. AWS Marketplace — container offering

### Product type & delivery
- **AWS Marketplace container product**, delivered as a **Helm chart** and/or **EKS add-on**, with images hosted in Marketplace-managed ECR. Customers launch from the Marketplace listing into their own EKS cluster.
- Images must pass **Marketplace scanning** (another reason for §8's hardened images).
- Listing assets: overview, architecture diagram, the security/RBAC story, install guide, the autonomy/safety explainer, and a free trial.

### Pricing & metering
Container products support **free, BYOL, contract (annual), and usage-based** pricing via the **AWS Marketplace Metering Service** (`MeterUsage`/`BatchMeterUsage`).

Recommended model — **hybrid, value-aligned**:

| Dimension | Why |
|---|---|
| **Per managed node-hour** (primary, metered) | Scales with cluster size = with value and with the customer's own infra spend; familiar (matches how Datadog/observability bill); easy for buyers to predict |
| **Free tier** (e.g., ≤ 10 nodes or observe-only) | Removes adoption friction; lets teams trust it in `observe` mode before paying for `auto` |
| **Annual contract** (enterprise, fleet) | Predictable for procurement; bundles fleet console + priority support |

Avoid pure per-remediation pricing as the *primary* meter — it perversely incentivizes the vendor toward more remediations and makes spend unpredictable. Per-node-hour aligns incentives (we make money keeping clusters healthy, not by churning fixes). *Optionally* expose remediation counts as a reported (non-billed) usage dimension for transparency.

**Metering implementation:** a small **metering reporter** (CronJob, hourly) reads the node count (and active mode) and calls `MeterUsage` with the Marketplace product code, via an IRSA/Pod-Identity role granting `aws-marketplace:MeterUsage`. The agent README's cost section and daily-counter tables show the team already thinks in these terms.

### Seller mechanics (checklist)
- Register as an AWS Marketplace seller (AWS Marketplace Management Portal), complete tax/banking.
- Create the container product; define usage dimensions + product code.
- Push scanned images to the Marketplace ECR repos; submit Helm chart / EKS add-on artifacts.
- Add the metering IAM permissions to the install docs/chart.
- Provide a **free trial** + a **CloudFormation/EKS-add-on quick launch**.
- Self-service buyer experience: subscribe → deploy to cluster → metered automatically.

---

## 10. Multi-cluster / fleet (enterprise upsell)

- **Per-cluster install** is the default (each cluster runs its own agent; DynamoDB partitioned by cluster id).
- **Fleet console** (paid tier): a central console aggregates investigations/approvals across clusters from the shared DynamoDB (or a central account via cross-account access). One pane for "what's failing across my 40 clusters, what did the agent fix, what needs approval."
- **Fleet policy**: define autonomy rules once, apply across the fleet (e.g., `auto` in dev clusters, `approve` in prod).
- This is a natural **annual-contract** differentiator and the reason an enterprise pays beyond per-node metering.

---

## 11. Competitive positioning

| | k8sgpt | SaaS AIOps (Komodor/Robusta/PagerDuty) | **KRA (this)** |
|---|---|---|---|
| Diagnose | ✅ | ✅ | ✅ |
| **Safe closed-loop remediation** | ❌ | partial / runbook automations | ✅ RBAC-bounded + autonomy policy + verify/rollback |
| **Data stays in account** | ✅ (BYO-LLM) | ❌ (SaaS) | ✅ (Bedrock in-account, optional PrivateLink) |
| **AWS-native billing** | n/a | subscription | ✅ Marketplace metered, on the AWS bill |
| **One-click install** | Helm | varies | ✅ EKS add-on + Marketplace |
| **GitOps-native fixes (PRs)** | ❌ | rare | ✅ detects ArgoCD/Flux, opens PRs |
| Autonomy choice (observe/approve/auto) | ❌ | partial | ✅ first-class policy |

**One-line positioning:** *"The AWS-native, closed-loop self-healing agent for EKS — diagnoses and safely fixes cluster incidents using Bedrock, entirely inside your account, with the autonomy you choose. One click from the EKS console."*

---

## 12. Phased roadmap

| Phase | Deliverable | Acceptance test |
|---|---|---|
| **S0. Spike: Bedrock backend** | Swap `kiro-cli` → Strands+Bedrock agent runner over `eks-mcp-server`; keep RBAC + Job pattern | On this cluster, an OOMKilled pod is investigated + (on approve) remediated using Bedrock, with no kiro key |
| **S1. Decouple state** | `StateStore` interface + DynamoDB default (Postgres optional) | Agent runs with **zero dependency** on `platform-db`; investigations persist in DynamoDB |
| **S2. Hardened images + Helm chart** | Scanned multi-arch `kra-*` images; `kra` Helm chart with §8 values | `helm install kra` on a **vanilla EKS cluster** (no AI platform) yields a working watcher + investigator |
| **S3. Standalone console + auth** | Extract approvals UI into `kra-console` with OIDC/Cognito | Approve/dismiss/history works without the cluster-dashboard; login required |
| **S4. Generalize triggers + scope** | Config-driven CR health, namespace selectors, neutral defaults; Prometheus/CloudWatch alert triggers | Watches a non-AI workload (e.g., a generic web app) and a custom CRD defined purely in values |
| **S5. Autonomy policy** | observe/approve/auto modes + policy rules + verify/rollback + auto caps | A reversible CrashLoop fix auto-heals; a `delete` in prod escalates to approval; verify-fail triggers rollback |
| **S6. GitOps-native remediation** | Detect ArgoCD/Flux; open PRs for managed resources | A fix to an ArgoCD-managed Deployment opens a PR instead of being reverted by selfHeal |
| **S7. Notifiers** | Slack/Teams/PagerDuty + interactive approvals | Approve a fix from a Slack message; PagerDuty incident created on CRITICAL |
| **S8. Marketplace listing** | Seller registration, container product, metering reporter, free trial, EKS add-on | Subscribe via Marketplace → deploy to cluster → `MeterUsage` reports node-hours; bill appears |
| **S9. Fleet (enterprise)** | Central multi-cluster console + fleet policy | Two clusters' incidents/approvals visible in one console; one policy governs both |

**Recommended order:** S0 → S1 → S2 → S4 → S5 → S3 → S6/S7 → S8 → S9. Rationale: prove Bedrock + cut the DB + ship installable-on-any-cluster first (S0–S2, S4); then the autonomy policy that's the product's headline (S5); then console/integrations; then Marketplace; fleet last.

A **shippable MVP is S0–S5 + S8** — Bedrock-powered, installable on any EKS cluster, autonomy-configurable, on the Marketplace. S6/S7/S9 are fast-follows.

---

## 13. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| LLM proposes a harmful fix | Med | High | **RBAC is the boundary** (proven); auto-heal only for policy-allowed low-risk classes; server-side dry-run; verify-or-rollback; Bedrock Guardrails; caps |
| Auto-heal fights a GitOps controller (ArgoCD selfHeal reverts) | Med | Med | GitOps detection (S6) → PR instead of imperative apply for managed resources; today's `out_of_scope` flag is the interim guard |
| Customers distrust autonomy | High (early) | High (adoption) | Ship **observe** mode as default for trials; build trust with a visible audit trail before enabling `auto`; free tier in observe |
| Bedrock model/region availability or cost | Med | Med | Model + region configurable; cost caps; per-investigation cost shown; `self-hosted` backend for cost-sensitive/air-gapped |
| Marketplace metering integration complexity | Med | Med | Standard `MeterUsage` pattern; start with a simple per-node-hour dimension; the team already tracks daily counters |
| Image scanning / Marketplace approval delays | Med | Med | Hardened, pinned, minimal images from day one (S2); no runtime downloads |
| DynamoDB rewrite introduces bugs vs proven SQL path | Low | Med | Keep Postgres path as reference; `StateStore` interface with a shared test suite across both backends |
| Non-EKS Kubernetes demand (GKE/AKS/on-prem) | Med | Med | Architecture is mostly portable; `eks-mcp-server` is EKS-specific — abstract the tool layer later (a generic `kubectl`/k8s-MCP backend) for non-EKS, post-MVP |
| Naming clash / brand | Low | Low | Pick a distinct name early (Decision #1); the README already flags the "Platform Health" clash |

---

## 14. Decision log

| # | Decision | Rationale | Alternative |
|---|---|---|---|
| 1 | Standalone product name = **TBD** (placeholder "KRA") | Must avoid the AWS "Platform Health" clash already noted in the README | Keep internal name — confusing externally |
| 2 | **Bedrock** is the default LLM; backend is pluggable | AWS-native, in-account, no third-party key, reuses Part 1's Bedrock engine | kiro-only (needs proprietary key — non-starter for Marketplace) |
| 3 | **DynamoDB** default state store (Postgres optional) | Serverless, nothing to operate, multi-cluster-native | Bundled Postgres — another stateful thing for the customer to run |
| 4 | **Autonomy policy** (observe/approve/auto) is the headline feature | It's the differentiator vs diagnose-only tools, and the trust on-ramp | Approval-only (what exists) — undersells the value |
| 5 | **RBAC remains the safety boundary**, not the prompt | Already proven here; honest, auditable, model-agnostic | Prompt-based guardrails alone — not trustworthy |
| 6 | Distribute as **EKS add-on + Helm** via **Marketplace container product** | Lowest-friction install for EKS users; billing on the AWS bill | SaaS — breaks the in-account/data-residency value prop |
| 7 | Primary meter = **per-node-hour** + free tier; contracts for enterprise | Aligns vendor incentive with cluster health, predictable for buyers | Per-remediation — perverse incentive, unpredictable spend |
| 8 | **GitOps-native remediation** (PRs) for managed resources | Enterprises run GitOps; imperative fixes get reverted and erode trust | Imperative-only — fights ArgoCD/Flux |
| 9 | **Hardened, scanned, multi-arch images** (no runtime downloads) | Marketplace requirement; reliability; Graviton cost win | Keep init-container downloads — fails scanning, slow, fragile |
| 10 | Reuse the existing engine (detection, Job pattern, state schema, approvals UX) | ~70% is already product-grade; fastest path to a real product | Rewrite from scratch — wasteful, throws away proven safety model |

---

## Appendix — relationship to Part 1

The two products **share a Bedrock engine** (Converse + MCP tool-use loop, IRSA, optional PrivateLink): Part 1 uses it to *run business-process agents*; Part 2 uses it to *run cluster-reliability agents*. Building it once, well, serves both. The self-healing agent can also ship **inside** the AI platform (as it does today) *and* stand alone on Marketplace — same code, two distribution channels. A customer who buys the standalone agent and later wants model serving + fine-tuning + business agents is a natural upsell into the full platform (Part 1).

*End of Part 2.*
