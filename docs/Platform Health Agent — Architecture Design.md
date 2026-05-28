# Autonomous Platform Health Agent — Architecture Design

**Status:** Draft  
**Author:** Platform Team  
**Date:** 2026-05-28  
**Module path:** `platform/services/platform-health-agent/`

---

## 0. Summary

An optional, EKS-native platform service that autonomously investigates cluster incidents using Kiro CLI headless mode, notifies the team via Slack with root-cause analysis and a proposed fix, and applies the remediation only after explicit human approval in Slack.

No additional infrastructure (ECS, Step Functions) is required — the entire pipeline runs as Kubernetes workloads alongside the existing platform services, deployed via ArgoCD.

---

## 1. Goals & Non-Goals

### Goals (V1 = MVP)

- Event-driven: automatically detect actionable Kubernetes events (CrashLoopBackOff, OOMKilled, FailedScheduling, node NotReady)
- Investigate using `kiro-cli chat --no-interactive` with full in-cluster context (kubectl, logs, metrics)
- Post structured Slack notifications with: error summary, root cause, suggested fix, risk assessment
- Human-in-the-loop: remediation only executes after Slack approval by an authorized user
- Optional deployment: teams can enable/disable via a single ArgoCD Application toggle
- GitOps-native: all configuration in YAML, deployed through the existing ArgoCD app-of-apps pattern

### Non-Goals (V1)

- Proactive anomaly detection (baseline learning) — deferred to V2
- Multi-cluster federation (single cluster per deployment)
- Integration with PagerDuty/OpsGenie (Slack-only for V1)
- Auto-remediation without human approval (by design — safety boundary)
- Custom LLM backends (Kiro CLI handles model selection)

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        EKS Cluster                              │
│                                                                 │
│  ┌──────────────┐     ┌──────────────────┐                     │
│  │ Event Watcher│────▶│ Investigator Job  │                     │
│  │ (Deployment) │     │ (kiro-cli headless│                     │
│  │              │     │  --trust-tools=   │                     │
│  │ Watches:     │     │  read,grep)       │                     │
│  │ - Pod events │     └────────┬─────────┘                     │
│  │ - Node conds │              │ findings JSON                  │
│  │ - HPA issues │              ▼                                │
│  └──────────────┘     ┌──────────────────┐                     │
│                       │  Slack Notifier   │                     │
│                       │  (sidecar/step)   │──────┐              │
│                       └──────────────────┘      │              │
│                                                  │              │
│  ┌──────────────────┐                           │              │
│  │ Slack Handler    │◀──────────────────────────┘              │
│  │ (Deployment)     │                                           │
│  │ /approve endpoint│     ┌──────────────────┐                 │
│  │                  │────▶│ Remediator Job   │                 │
│  │ Validates user   │     │ (kiro-cli headless│                 │
│  │ Creates Job      │     │  --trust-tools=   │                 │
│  └──────────────────┘     │  read,write,grep) │                 │
│                           └──────────────────┘                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │   Slack Channel   │
                    │  #ops-incidents   │
                    │                   │
                    │ 🚨 Error summary  │
                    │ 📋 Root cause     │
                    │ 🔧 Suggested fix  │
                    │ [✅ Approve] [❌] │
                    └───────────────────┘
```

---

## 3. Components

### 3.1 Event Watcher

**Type:** Deployment (1 replica)  
**Image:** Custom lightweight Go/Python binary  
**RBAC:** `ClusterRole` with read access to Events, Pods, Nodes, Deployments, ReplicaSets, HPA  

**Responsibilities:**
- Watches Kubernetes Events API using informers/watch
- Filters for actionable signals (debounced — no duplicate investigations for the same pod within 10 min)
- Creates an Investigator Job with event context injected as environment variables

**Actionable event patterns (V1):**

| Signal | Source | Filter |
|--------|--------|--------|
| CrashLoopBackOff | Pod status | `restartCount > 3` within 10 min |
| OOMKilled | Container termination reason | Immediate |
| FailedScheduling | Event reason | After 2 min unscheduled |
| NodeNotReady | Node condition | `condition.status = False` for > 60s |
| ImagePullBackOff | Pod status | After 3 failures |
| FailedMount | Event reason | Immediate |

**Configuration (ConfigMap):**
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: platform-health-agent-config
data:
  watch_namespaces: "*"              # or "production,staging"
  exclude_namespaces: "kube-system,kube-node-lease"
  debounce_window: "600"            # seconds — no re-investigation within this window
  severity_threshold: "WARNING"     # CRITICAL, WARNING, INFO
  slack_channel: "ops-incidents"
```

### 3.2 Investigator Job

**Type:** Job (created per incident, `ttlSecondsAfterFinished: 3600`)  
**Image:** `<ecr>/kiro-agent:latest` (Kiro CLI + kubectl + awscli)  
**RBAC:** `ServiceAccount: platform-health-agent-reader` — read-only cluster access  

**Execution:**
```bash
kiro-cli chat --no-interactive --trust-tools=read,grep --require-mcp-startup \
  "An EKS incident occurred in namespace $NAMESPACE:
   Resource: $RESOURCE_KIND/$RESOURCE_NAME
   Event: $EVENT_REASON — $EVENT_MESSAGE
   Timestamp: $EVENT_TIMESTAMP
   
   Investigate:
   1. Describe the affected resource and its owner chain
   2. Check container logs (last 100 lines)
   3. Check recent events on the resource
   4. Check node conditions if relevant
   5. Check resource quotas and limits
   
   Output a JSON file to /results/investigation.json with:
   - summary: 2-3 sentence root cause
   - severity: CRITICAL/HIGH/MEDIUM/LOW
   - affected_resources: list of resource names
   - suggested_fix: exact kubectl/yaml commands to remediate
   - risk_assessment: what could go wrong applying the fix
   - requires_manual_review: boolean (true if fix could cause downtime)"
```

**Output:** Writes `/results/investigation.json` → Slack notifier sidecar picks it up.

### 3.3 Slack Notifier

**Type:** Sidecar container in the Investigator Job (or a completion hook)  
**Responsibility:** Reads investigation results and posts to Slack using Block Kit

**Slack message structure:**
- Header: 🚨 severity + affected resource
- Section: Root cause summary
- Section: Suggested fix (in code block)
- Section: Risk assessment
- Context: timestamp, namespace, cluster name
- Actions: `[✅ Approve Fix]` `[❌ Dismiss]` `[🔍 View Full Report]`

**Button payload** encodes: `investigation_id`, `fix_commands`, `approved_namespaces`

### 3.4 Slack Handler

**Type:** Deployment (1 replica) with a `Service` + `Ingress`  
**Image:** Lightweight Python (Flask/FastAPI)  
**Endpoint:** `POST /slack/interactions` — Slack Interactivity Request URL  

**Responsibilities:**
1. Verify Slack request signature (`x-slack-signature`)
2. Validate approver is in the authorized users list (ConfigMap)
3. On "Approve": create a Remediator Job with the fix commands
4. On "Dismiss": update Slack message, log decision, no action
5. Update the original Slack message with approval status + approver name

**Authorization model:**
```yaml
# ConfigMap
authorized_approvers:
  - rdobrine
  - platform-oncall    # Slack user group
min_approval_count: 1  # V2: support multi-approval for CRITICAL
```

### 3.5 Remediator Job

**Type:** Job (created only after Slack approval)  
**Image:** Same `<ecr>/kiro-agent:latest`  
**RBAC:** `ServiceAccount: platform-health-agent-writer` — scoped write access  

**Execution:**
```bash
kiro-cli chat --no-interactive --trust-tools=read,write,grep \
  "Apply the following remediation to the EKS cluster.
   Approved by: $APPROVED_BY at $APPROVAL_TIMESTAMP
   Investigation ID: $INVESTIGATION_ID
   
   Fix commands:
   $FIX_COMMANDS
   
   Steps:
   1. Apply the fix
   2. Wait 30 seconds
   3. Verify the fix worked (check pod status, events)
   4. Output results to /results/remediation.json with:
      - applied: boolean
      - verification: pass/fail
      - post_fix_status: resource state after fix
      - rollback_commands: commands to undo if needed"
```

**Post-execution:** Posts confirmation to Slack thread (success ✅ or failure ⚠️ with rollback instructions).

---

## 4. RBAC Design

Two separate ServiceAccounts enforce the principle of least privilege:

```yaml
# Read-only — used by Event Watcher + Investigator
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-health-agent-reader
rules:
- apiGroups: [""]
  resources: ["pods", "pods/log", "events", "nodes", "services",
              "configmaps", "persistentvolumeclaims", "namespaces"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
  verbs: ["get", "list"]
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get", "list"]
- apiGroups: ["batch"]
  resources: ["jobs", "cronjobs"]
  verbs: ["get", "list"]
```

```yaml
# Write — used by Remediator ONLY (post-approval)
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-health-agent-writer
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["delete"]               # for pod restart
- apiGroups: ["apps"]
  resources: ["deployments", "statefulsets"]
  verbs: ["get", "patch", "update"]  # for rollout restart, scale, image update
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "patch"]          # for config fixes
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get", "patch"]          # for HPA adjustments
```

**Namespace scoping (optional):** Use `RoleBinding` per namespace instead of `ClusterRoleBinding` to restrict which namespaces the agent can modify.

---

## 5. Container Image

Single image for both Investigator and Remediator:

```dockerfile
FROM debian:bookworm-slim

# Kiro CLI
RUN curl -fsSL https://cli.kiro.dev/install | bash

# kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -sL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl && mv kubectl /usr/local/bin/

# AWS CLI (for CloudWatch Logs, Secrets Manager)
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip \
    && unzip awscliv2.zip && ./aws/install && rm -rf aws awscliv2.zip

# Python (for Slack posting scripts)
RUN apt-get update && apt-get install -y python3 python3-pip \
    && pip3 install requests

# MCP config for kiro-cli (kubectl access via in-cluster ServiceAccount)
COPY mcp-config.json /root/.kiro/mcp.json

WORKDIR /workspace
```

**MCP config** enables Kiro CLI to use kubectl within the cluster:
```json
{
  "mcpServers": {
    "kubectl": {
      "command": "kubectl",
      "args": ["--in-cluster"]
    }
  }
}
```

---

## 6. Secrets Management

All sensitive values stored as Kubernetes `ExternalSecret` (AWS Secrets Manager):

| Secret Key | Used By | Purpose |
|------------|---------|---------|
| `kiro-api-key` | Investigator, Remediator | Kiro CLI headless authentication |
| `slack-bot-token` | Notifier, Slack Handler | Post messages, update messages |
| `slack-signing-secret` | Slack Handler | Verify inbound Slack requests |

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: platform-health-agent-secrets
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: aws-secrets-manager
    kind: ClusterSecretStore
  target:
    name: platform-health-agent-secrets
  data:
  - secretKey: kiro-api-key
    remoteRef:
      key: /platform/platform-health-agent/kiro-api-key
  - secretKey: slack-bot-token
    remoteRef:
      key: /platform/platform-health-agent/slack-bot-token
  - secretKey: slack-signing-secret
    remoteRef:
      key: /platform/platform-health-agent/slack-signing-secret
```

---

## 7. Enabling/Disabling the Module

### Option A: ArgoCD Application toggle (recommended)

```yaml
# argocd/apps/platform-health-agent.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: platform-health-agent
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "3"
  labels:
    platform.ai/optional: "true"
spec:
  project: platform
  source:
    repoURL: <this-repo>
    path: platform/services/platform-health-agent
    targetRevision: HEAD
  destination:
    server: https://kubernetes.default.svc
    namespace: platform-health-agent
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

To disable: delete or comment out `argocd/apps/platform-health-agent.yaml` and push.

### Option B: Helm values (if using ApplicationSet)

```yaml
# cluster-config/values.yaml
platform:
  devopsAgent:
    enabled: true
    slackChannel: "ops-incidents"
    watchNamespaces: ["production", "staging"]
    authorizedApprovers: ["rdobrine", "platform-oncall"]
```

---

## 8. Observability

The module itself is observable through the same platform stack:

| Signal | Mechanism |
|--------|-----------|
| Event Watcher health | Liveness/readiness probes + Prometheus metrics (events_processed, jobs_created) |
| Investigation duration | Job completion time → CloudWatch metric |
| Remediation success rate | Custom metric: remediations_applied{status=success/failure} |
| Slack delivery | Slack API response codes logged to CloudWatch |
| Audit trail | All approval/rejection events logged with: user, timestamp, investigation_id, fix_commands |

---

## 9. Sequence Diagram

```
User Workload          Event Watcher        Investigator         Slack           Slack Handler      Remediator
     │                      │                    │                  │                  │                │
     │── Pod crashes ──────▶│                    │                  │                  │                │
     │                      │── creates Job ────▶│                  │                  │                │
     │                      │                    │── kiro-cli ──┐   │                  │                │
     │                      │                    │              │   │                  │                │
     │                      │                    │◀─ findings ──┘   │                  │                │
     │                      │                    │── post msg ─────▶│                  │                │
     │                      │                    │                  │── [Approve] ────▶│                │
     │                      │                    │                  │                  │── creates Job ▶│
     │                      │                    │                  │                  │                │── kiro-cli
     │                      │                    │                  │                  │                │── apply fix
     │◀─── pod healthy ─────┼────────────────────┼──────────────────┼──────────────────┼────────────────│
     │                      │                    │                  │◀── confirmation ─┼────────────────│
```

---

## 10. File Layout

```
platform/services/platform-health-agent/
├── kustomization.yaml
├── namespace.yaml
├── rbac/
│   ├── reader-clusterrole.yaml
│   ├── reader-binding.yaml
│   ├── writer-clusterrole.yaml
│   └── writer-binding.yaml
├── secrets/
│   └── external-secret.yaml
├── config/
│   ├── agent-config.yaml          # ConfigMap: namespaces, thresholds, Slack channel
│   └── authorized-approvers.yaml  # ConfigMap: who can approve
├── event-watcher/
│   ├── deployment.yaml
│   ├── service-account.yaml
│   └── hpa.yaml                   # optional autoscaling
├── investigator/
│   └── job-template.yaml          # template used by event-watcher to spawn Jobs
├── remediator/
│   └── job-template.yaml          # template used by slack-handler to spawn Jobs
├── slack-handler/
│   ├── deployment.yaml
│   ├── service.yaml
│   ├── ingress.yaml               # or Gateway API route
│   └── service-account.yaml
└── docker/
    ├── Dockerfile
    ├── mcp-config.json
    └── scripts/
        ├── post_to_slack.py
        └── post_confirmation.py
```

---

## 11. Security Considerations

| Concern | Mitigation |
|---------|-----------|
| Kiro CLI could hallucinate destructive commands | Writer RBAC strictly scopes allowed verbs/resources |
| Unauthorized user clicks "Approve" | Slack Handler validates user against allowlist |
| Secrets exposure | ExternalSecrets + IRSA, no plaintext in repo |
| Runaway investigation Jobs | `activeDeadlineSeconds: 300` on all Jobs |
| Namespace escape | Optional: `RoleBinding` per namespace instead of `ClusterRoleBinding` |
| Network access | NetworkPolicy: investigator egress only to API server + Kiro API |
| Replay attacks on Slack handler | Verify `x-slack-request-timestamp` (reject > 5 min) |

---

## 12. Future Enhancements (V2)

- **Proactive mode:** Baseline learning from normal metrics → alert before users notice
- **Multi-approval for CRITICAL:** Require 2+ approvers for high-risk remediations
- **Runbook integration:** Load team-specific runbooks as MCP context for kiro-cli
- **PagerDuty/OpsGenie:** Forward to incident management alongside Slack
- **Cost attribution:** Track investigation/remediation compute cost per namespace
- **Multi-cluster:** Deploy via ApplicationSet across fleet, centralize Slack to single channel
- **Feedback loop:** "Was this fix helpful?" button → improves future prompt engineering

---

## 13. Decision Log

| # | Decision | Rationale | Alternative considered |
|---|----------|-----------|----------------------|
| 1 | EKS-native (K8s Jobs) over ECS Fargate | Already have EKS; no additional infra; in-cluster kubectl is simpler | ECS + cross-cluster auth |
| 2 | Kiro CLI headless over Platform Health Agent webhook | Full prompt customization; can apply fixes; MCP extensibility | Platform Health Agent managed webhooks |
| 3 | Slack Block Kit over email/PagerDuty | Interactive approval buttons; team already lives in Slack | Email with approval links |
| 4 | Separate reader/writer RBAC | Principle of least privilege; write only after human approval | Single broad ServiceAccount |
| 5 | ExternalSecrets over sealed-secrets | Consistent with existing platform pattern; rotation support | SealedSecrets, SOPS |
| 6 | Optional ArgoCD Application | Zero impact on teams that don't want it; easy to enable/disable | Helm subchart with flag |
