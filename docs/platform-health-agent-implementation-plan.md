# Autonomous Platform Health Agent — Implementation Plan

**Status:** Plan (awaiting approval)
**Author:** Platform Team
**Date:** 2026-05-28
**Companion to:** [platform-health-agent-architecture-design.md](./platform-health-agent-architecture-design.md)
**Module path:** `platform/services/platform-health-agent/`

---

## v2 Pivot — 2026-05-28 (Slack → cluster-dashboard)

**Reason:** Corporate Slack workspace doesn't allow custom app creation
(no Bot User OAuth Token possible). Pivoted approvals UX from Slack DMs
to the existing in-cluster `cluster-dashboard` (already exposed via the
shared ALB at `:9090` with IP allowlist).

**What changed:**

- **D1 (transport):** Slack Socket Mode → in-cluster web UI inside cluster-dashboard.
  Approve/Dismiss buttons in a modal, opened by a topbar 🔔 badge.
- **D7 (interaction surface):** Slack DM → dashboard modal in same browser tab as topology.
- **D9 (notifier):** kiro-cli + post-to-slack → kiro-cli + persist-to-DB.
  No external API calls from the agent. Dashboard polls postgres every 2s and
  surfaces pending count in the topbar.
- **D15 (secret):** 5 keys (KIRO_API_KEY + 4× SLACK_*) → 1 key (KIRO_API_KEY only).
- **No new component:** no separate `slack-handler` Deployment. The cluster-dashboard
  backend (`scripts/backend.py`) extended to handle approvals.

**What stayed the same:**

- Architecture (event-watcher, Investigator/Remediator Jobs, postgres state DB)
- RBAC scoping (D3: inference + team-* only)
- Concurrency caps (D10), cost guards (D11), architecture target (D12)
- Database schema, debounce logic, daily counters
- The kiro-cli prompts in investigate.sh / remediate.sh (no changes needed)

**Files removed in pivot:**
- `platform/services/platform-health-agent/slack-handler.yaml`
- `platform/services/platform-health-agent/scripts/slack_handler.py`
- `platform/services/platform-health-agent/scripts/post_to_slack.py`

**Files added in pivot:**
- `platform/services/platform-health-agent/scripts/persist_findings.py` (replaces post_to_slack.py)
- `platform/services/cluster-dashboard/scripts/backend.py` (extracted from inline ConfigMap, extended)

**Files modified in pivot:**
- `platform/services/cluster-dashboard/manifests.yaml` (psycopg initContainer, jobs RBAC, DB env, image → slim)
- `platform/services/cluster-dashboard/cluster-topology.html` (topbar badge + approvals modal)
- `platform/services/cluster-dashboard/kustomization.yaml` (generates backend.py ConfigMap)
- `platform/services/platform-health-agent/event_watcher.py` (no Slack env in spawned Job spec)
- `platform/services/platform-health-agent/configmap.yaml` (no BOT_DISPLAY_NAME / BOT_ICON_EMOJI)
- `platform/services/platform-health-agent/kustomization.yaml` (no slack-handler resource)
- `platform/services/platform-health-agent/README.md` (rewritten — no Slack setup)
- `platform/services/platform-health-agent/scripts/{investigate,remediate}.sh` (call persist_findings)

The rest of this document reflects v1 (Slack-based). Sections affected by the pivot
are §0 (decisions table — see overrides below), §1 (prereqs), §10 (rollout). Other
sections (RBAC, DB schema, watcher logic, kiro-cli prompts) are unchanged.

---

## 0. Decisions Locked With User

These differ from / pin down ambiguities in the architecture doc:

| # | Topic | Decision |
|---|-------|---------|
| D1 | Slack transport | **Socket Mode** (no HTTPS ingress, no domain, no ACM cert needed) |
| D2 | Kiro auth | **Hosted Kiro CLI API key** from kiro.dev, supplied via env var `KIRO_API_KEY` |
| D3 | Remediation scope | **`inference/*` and `team-*` namespaces only.** Cannot modify `InferenceEndpoint` / `AITeam` CRs, nor anything in `ai-platform`, `gpu-operator`, `kuberay`, `argocd`, `external-secrets`, `kube-system`, `amazon-cloudwatch` |
| D4 | Approver identity | **Slack user IDs in ConfigMap** (e.g. `U01ABCD2EF3`) |
| D5 | Container image | **No custom image** — `python:3.12-slim` base + initContainer that downloads `kiro-cli` and `kubectl` at pod start |
| D6 | ArgoCD integration | **Add to existing `argocd/bootstrap/platform.yaml` ApplicationSet** as a new list element. Disable = remove the element + push. |
| D7 | Slack interaction surface | **DM-based** — bot DMs a single recipient user. No public channel needed for V1. |
| D8 | State storage | **New PostgreSQL database `platform_health_agent`** on the existing `platform-db-0` StatefulSet |
| D9 | Notifier | **Single-container shell wrapper** in the Investigator/Remediator Job — kiro-cli runs, then a Python script posts to Slack on the same exit path |
| D10 | Concurrency cap | **Max 3 concurrent investigations**, semaphore in postgres |
| D11 | Cost guard | `max_investigations_per_day: 50`, `max_remediations_per_day: 20`, counters in postgres |
| D12 | Architecture | `nodeSelector: kubernetes.io/arch: amd64` (until ARM64 build of kiro-cli is verified) |
| D13 | MCP servers | `awslabs.eks-mcp-server` only |
| D14 | Models | `claude-sonnet-4.6` (1.3x credits) for investigations, `claude-opus-4.6` (2.2x) for remediations. ConfigMap-overridable. |
| D15 | Secret storage | **Direct K8s Secret manually created** (matches existing `hf-token`, `langfuse-litellm-keys` pattern). ESO not used in V1. |
| D16 | Job lifecycle | `activeDeadlineSeconds: 600`, `ttlSecondsAfterFinished: 3600`. Approval expires after 24h. |

---

## 1. Prerequisites — User Actions Before First Deploy

You'll need to complete these once. I can't do them for you.

### 1.1 Create the Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**.
2. App name: `EKS Platform Health Agent`. Workspace: your workspace.
3. **OAuth & Permissions** → Bot Token Scopes — add:
   - `chat:write` (post messages)
   - `chat:write.customize` (post as bot with custom name/icon)
   - `im:write` (open DMs)
   - `im:history` (read DM context if needed)
   - `users:read` (resolve display names)
4. **Socket Mode** → toggle ON → **Generate App-Level Token** with scope `connections:write` → copy the `xapp-…` token.
5. **Interactivity & Shortcuts** → toggle ON. Request URL **leave blank** (Socket Mode handles it). This step is required to enable interactive messages even though we won't expose an HTTPS endpoint.
6. **Event Subscriptions** → not needed for V1 (we don't subscribe to events; we only post and react to button clicks).
7. **Install App** → install to workspace → copy the bot token (`xoxb-…`).
8. **Basic Information** → copy the **Signing Secret** (used as defense-in-depth even though Socket Mode doesn't strictly require it).
9. DM the bot once to ensure it can reach you (find it in your Slack DMs sidebar). Get your own Slack user ID: in Slack click your avatar → Profile → ⋯ → Copy member ID. It looks like `U01ABCD2EF3`.

### 1.2 Get the Kiro API key

1. <https://kiro.dev/> → log in → API keys → create a key with headless-mode scope.
2. Copy the key value (one-time, not retrievable later).

### 1.3 Create the K8s Secret

After the namespace is created (Step 1 of rollout, see §10), run:

```bash
kubectl -n platform-health-agent create secret generic platform-health-agent-secrets \
  --from-literal=KIRO_API_KEY='kr-…' \
  --from-literal=SLACK_BOT_TOKEN='xoxb-…' \
  --from-literal=SLACK_APP_TOKEN='xapp-…' \
  --from-literal=SLACK_SIGNING_SECRET='…' \
  --from-literal=SLACK_RECIPIENT_USER_ID='U01ABCD2EF3'
```

This secret is excluded from git (it lives only in the cluster, like `hf-token`).

---

## 2. Architecture (As Built)

```
EKS Cluster (<your-cluster>)
│
├── platform-health-agent namespace
│   │
│   ├── event-watcher (Deployment, 1 replica)
│   │     watches Pod/Node/Event API → filters → spawns Job
│   │     RBAC: platform-health-agent-reader (cluster-wide get/list/watch)
│   │
│   ├── slack-handler (Deployment, 1 replica)
│   │     opens WebSocket to Slack (Socket Mode)
│   │     handles button clicks → looks up fix in postgres → spawns Remediator Job
│   │     RBAC: ServiceAccount with create on jobs in this namespace
│   │
│   ├── investigator-jobs (ephemeral Jobs, max 3 concurrent)
│   │     SA: platform-health-agent-reader (read-only across cluster)
│   │     Wraps: kiro-cli investigation prompt → posts findings to Slack
│   │
│   ├── remediator-jobs (ephemeral Jobs, created post-approval)
│   │     SA: platform-health-agent-writer (scoped: inference + team-* namespaces only)
│   │     Wraps: kiro-cli remediation prompt → posts result to Slack thread
│   │
│   └── db-init (one-time Job, run by ArgoCD on first sync)
│         creates platform_health_agent database + schema on platform-db-0
│
└── ai-platform namespace (unchanged)
    └── platform-db-0  ← agent connects here for state
```

### State stored in postgres (`platform_health_agent` database):

| Table | Purpose |
|-------|---------|
| `investigations` | One row per spawned investigation. Stores event context, status, slack `thread_ts`, fix commands JSON, approver, timestamps. |
| `debounce` | `(namespace, kind, name)` → `last_investigated_at`. Used to skip duplicate triggers. |
| `daily_counters` | Today's investigation/remediation counts vs configured limits. |

Schema in §6.

---

## 3. File Layout

Following the cluster-dashboard convention — single `manifests.yaml` with inline scripts in ConfigMaps where practical, a few separate files where things get long.

```
platform/services/platform-health-agent/
├── README.md                       # operator-facing docs (setup, how it works, troubleshooting)
├── kustomization.yaml              # required by ArgoCD directory mode
├── namespace.yaml                  # platform-health-agent namespace
├── rbac.yaml                       # ServiceAccounts + ClusterRoles + bindings (reader, writer)
├── configmap.yaml                  # agent-config + authorized-approvers + scripts
├── db-init-job.yaml                # one-time CREATE DATABASE + DDL
├── event-watcher.yaml              # Deployment + Service (no Service strictly required, but useful for metrics later)
├── slack-handler.yaml              # Deployment
├── job-templates.yaml              # PodTemplate-shaped ConfigMap entries used by event-watcher / slack-handler to spawn Jobs
└── scripts/                        # source for the inline ConfigMap content (kept as separate files for readability + future extraction)
    ├── event_watcher.py
    ├── slack_handler.py
    ├── investigate.sh              # the shell wrapper (kiro-cli + post-to-slack)
    ├── remediate.sh                # the shell wrapper (kiro-cli + post-to-slack thread reply)
    ├── post_to_slack.py            # shared library used by both wrappers
    ├── db_init.sh                  # creates DB, runs DDL
    └── ddl.sql                     # schema
```

Note: ArgoCD's `directory` source mode applies all `*.yaml` files at the path. The `scripts/` subdir is **for source-of-truth readability only** — its contents get inlined into `configmap.yaml` at commit time. We keep both so diffs are reviewable. (A small `Makefile` target in this directory will regenerate the ConfigMap from `scripts/` to keep them in sync — running `make sync` produces a deterministic `configmap.yaml`.)

Why not pure inline like cluster-dashboard? Total Python/SQL lines here are ~600. Inlining a 600-line YAML is unreviewable. The Makefile pattern keeps GitOps purity (only YAML is applied) without sacrificing reviewability.

---

## 4. Component Details

### 4.1 Event Watcher

**Image:** `python:3.12-slim`
**Initialization:** initContainer downloads `kubectl` to a shared `emptyDir` (kiro-cli not needed here — pure Python/k8s API).
**Replicas:** 1 (single-writer; uses leader-election if ever scaled).
**Resources:** `requests: 100m/128Mi, limits: 500m/256Mi`.

**Logic** (`event_watcher.py`):

```python
# pseudocode — full file in scripts/event_watcher.py
1. Connect to in-cluster K8s API via service account token.
2. Connect to postgres platform_health_agent DB.
3. Start two informers:
   - core/v1 Pods (filter: status.containerStatuses.lastTerminationState.reason)
   - core/v1 Events (filter: type=Warning, reason in [FailedScheduling, FailedMount, Failed, BackOff])
   - core/v1 Nodes (filter: status.conditions.type=Ready,status=False for >60s)
4. For each actionable signal:
   a. Compute key = (namespace, kind, name).
   b. Check debounce table: skip if last_investigated_at within window (default 600s).
   c. Check namespace allowlist (config: watch_namespaces).
   d. Check daily counter: skip + DM-warn user if >= max_investigations_per_day.
   e. Check concurrency: count investigations in 'running' state; skip if >= 3.
   f. INSERT into investigations table (status=pending, payload=event JSON).
   g. Spawn Investigator Job using batch/v1 API with that investigation_id as env var.
   h. UPDATE investigations.status=running, debounce.last_investigated_at=now().
5. Liveness probe: GET /health → checks K8s API + postgres connection.
6. Prometheus metrics (later): events_processed_total, investigations_spawned_total, debounce_skips_total.
```

**Actionable signals (V1):**

| Signal | Detection rule |
|--------|---------------|
| `CrashLoopBackOff` | Pod with `containerStatuses[*].lastTerminationState.terminated.reason == 'Error'` AND `restartCount > 3` in last 10 min |
| `OOMKilled` | `lastTerminationState.terminated.reason == 'OOMKilled'` (immediate, debounced) |
| `ImagePullBackOff` | Pod `Waiting.reason in ['ImagePullBackOff', 'ErrImagePull']` for >60s |
| `FailedScheduling` | Event `reason='FailedScheduling'` for >120s on the same Pod |
| `NodeNotReady` | Node `Ready` condition `False` for >60s |
| `FailedMount` | Event `reason='FailedMount'` (immediate) |

### 4.2 Slack Handler

**Image:** `python:3.12-slim` + `pip install slack-bolt psycopg[binary]` at startup (small layers, ~5s install).
**Replicas:** 1 (single Socket Mode WebSocket).
**Resources:** `requests: 100m/128Mi, limits: 200m/256Mi`.

**Logic** (`slack_handler.py`):

```python
# pseudocode — full file in scripts/slack_handler.py
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

app = App(token=os.environ["SLACK_BOT_TOKEN"], signing_secret=os.environ["SLACK_SIGNING_SECRET"])

@app.action("approve_fix")
def handle_approve(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    investigation_id = body["actions"][0]["value"]
    # 1. Authorize: must be SLACK_RECIPIENT_USER_ID (V1: single approver).
    if user_id != os.environ["SLACK_RECIPIENT_USER_ID"]:
        client.chat_postMessage(channel=user_id, text="❌ Not authorized.")
        return
    # 2. Look up investigation.
    inv = db.fetch_one("SELECT * FROM investigations WHERE id=%s AND status='awaiting_approval'", investigation_id)
    if not inv:
        client.chat_postMessage(channel=body["channel"]["id"], thread_ts=body["message"]["thread_ts"], text="⏰ Approval expired or not found.")
        return
    # 3. Daily remediation cap.
    if remediations_today() >= MAX_REMEDIATIONS_PER_DAY:
        client.chat_postMessage(...); return
    # 4. Spawn Remediator Job with investigation_id env var.
    spawn_remediator_job(investigation_id)
    # 5. Update DB + original message.
    db.execute("UPDATE investigations SET status='remediating', approved_by=%s, approved_at=now() WHERE id=%s", user_id, investigation_id)
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"], blocks=approved_blocks(inv, user_id))

@app.action("dismiss_fix")
def handle_dismiss(ack, body, client):
    # similar, but status='dismissed', no Job spawn
    ...

if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
```

The Slack Handler does **not** need access to the K8s API except to create Jobs in its own namespace (`batch/v1` create on `jobs.batch` in `platform-health-agent`). RBAC scoped accordingly.

### 4.3 Investigator Job (`investigate.sh`)

Triggered with env vars: `INVESTIGATION_ID`, `EVENT_PAYLOAD` (JSON), `CLUSTER_NAME`, `AWS_REGION`.

```bash
#!/bin/sh
set -eu

# 1. Run kiro-cli with read-only tool set + EKS MCP server.
#    Output is captured to /results/findings.json via the prompt instruction.
mkdir -p /results

cat > /results/prompt.txt <<EOF
You are an EKS incident investigator running in cluster $CLUSTER_NAME ($AWS_REGION).
The following event triggered an investigation:

$EVENT_PAYLOAD

Use kubectl (already configured for in-cluster access) and the eks-mcp-server tools to:
1. Describe the affected resource and its owner chain (Deployment/StatefulSet/Job).
2. Read the last 100 log lines from each container in the affected pod.
3. List warning events in the affected namespace from the last 30 minutes.
4. Check resource quotas, limits, and the affected node's conditions.
5. If the resource is in a 'inference' or 'team-*' namespace, also check the parent
   InferenceEndpoint or AITeam status.

Output ONLY a single JSON object to /results/findings.json with these keys:
  summary           : string (2-3 sentence root cause)
  severity          : "CRITICAL" | "HIGH" | "MEDIUM" | "LOW"
  affected_resources: list of {kind, namespace, name}
  fix_commands      : list of {description, commands: [list of kubectl/yaml commands]}
                      Each command must be runnable with kubectl. Multi-line YAML allowed.
  risk_assessment   : string (what could go wrong)
  requires_manual_review: boolean (true for any change that could cause downtime)
  out_of_scope      : boolean (true if the fix would require touching ArgoCD-managed
                      namespaces or modifying KRO custom resources)
Do not write any other files. Do not run any commands that modify state.
EOF

kiro-cli chat --no-interactive \
  --model "${KIRO_MODEL:-claude-sonnet-4.6}" \
  --trust-tools=fs_read,fs_write,execute_bash \
  "$(cat /results/prompt.txt)"

# 2. Validate findings.json was produced and is valid JSON.
if ! jq empty /results/findings.json 2>/dev/null; then
  echo "Investigation failed to produce valid findings.json" >&2
  python3 /scripts/post_to_slack.py error "$INVESTIGATION_ID" "Investigation failed — Kiro CLI did not produce valid output."
  exit 1
fi

# 3. Persist findings + post DM to Slack.
python3 /scripts/post_to_slack.py investigation "$INVESTIGATION_ID" /results/findings.json
```

**Key safety properties:**
- `--trust-tools=fs_read,fs_write,execute_bash` lets kiro-cli read files, write its single output file, and run commands. The **RBAC** prevents the kubectl calls from doing anything destructive — `platform-health-agent-reader` has only `get/list/watch`. So even if the LLM hallucinated `kubectl delete pod foo`, it would 403.
- `out_of_scope=true` short-circuits in the post-to-slack script: instead of an approve button, the user gets a "📌 Out-of-scope, manual fix required" DM with the suggested fix as text only.

### 4.4 Remediator Job (`remediate.sh`)

```bash
#!/bin/sh
set -eu

# 1. Fetch fix commands from postgres.
FIX_JSON=$(psql -X -A -t -c "SELECT fix_commands FROM investigations WHERE id='$INVESTIGATION_ID'")
THREAD_TS=$(psql -X -A -t -c "SELECT slack_thread_ts FROM investigations WHERE id='$INVESTIGATION_ID'")

cat > /results/prompt.txt <<EOF
You are an EKS remediator running in cluster $CLUSTER_NAME ($AWS_REGION).
Investigation $INVESTIGATION_ID was approved by $APPROVED_BY at $APPROVED_AT.

The approved fix is:
$FIX_JSON

Apply it using kubectl. After applying:
1. Wait 30 seconds.
2. Verify the affected resources reached a healthy state (check pod phase, events).
3. If verification fails, do NOT attempt additional remediation. Report the failure.

Output ONLY a single JSON object to /results/result.json:
  applied           : boolean
  verification_pass : boolean
  post_fix_status   : list of {kind, namespace, name, status}
  rollback_commands : list of strings (commands to undo this fix if it caused harm)
  error_summary     : string | null
EOF

kiro-cli chat --no-interactive \
  --model "${KIRO_MODEL:-claude-opus-4.6}" \
  --trust-tools=fs_read,fs_write,execute_bash \
  "$(cat /results/prompt.txt)"

# 2. Post threaded reply to original Slack DM.
python3 /scripts/post_to_slack.py remediation "$INVESTIGATION_ID" /results/result.json "$THREAD_TS"
```

**Safety properties:**
- `platform-health-agent-writer` ClusterRole binds via `RoleBinding` (not `ClusterRoleBinding`) only to `inference` and to a `Role` that's templated per-team-namespace. **Effective scope: `inference` namespace + each `team-*` namespace.**
- Forbidden verbs/resources still 403 even if the LLM tries them.

### 4.5 PostgreSQL state DB (`db-init-job.yaml`)

A one-time Job that connects to `platform-db-0` as the postgres superuser, runs `CREATE DATABASE platform_health_agent`, then runs DDL. Idempotent (uses `CREATE TABLE IF NOT EXISTS`).

ArgoCD runs this as a `PreSync` hook so it executes before the Deployments come up.

---

## 5. RBAC Specifications

### 5.1 Reader (used by Event Watcher and Investigator Jobs)

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-health-agent-reader
rules:
- apiGroups: [""]
  resources: ["pods", "pods/log", "events", "nodes", "services", "configmaps",
              "persistentvolumeclaims", "namespaces", "endpoints", "serviceaccounts"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["batch"]
  resources: ["jobs", "cronjobs"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["kro.run"]
  resources: ["inferenceendpoints", "aiteams"]
  verbs: ["get", "list", "watch"]            # READ ONLY — agent can see KRO state for context
- apiGroups: ["ray.io"]
  resources: ["rayservices", "rayclusters"]
  verbs: ["get", "list", "watch"]
```

Bound cluster-wide to `ServiceAccount platform-health-agent-reader` in `platform-health-agent` namespace.

### 5.2 Writer (used by Remediator Jobs ONLY)

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-health-agent-writer
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["delete"]                          # restart by deletion
- apiGroups: ["apps"]
  resources: ["deployments", "statefulsets"]
  verbs: ["get", "patch", "update"]          # rollout restart, scale, image bump
- apiGroups: ["apps"]
  resources: ["deployments/scale", "statefulsets/scale"]
  verbs: ["update", "patch"]
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get", "patch"]
- apiGroups: ["autoscaling"]
  resources: ["horizontalpodautoscalers"]
  verbs: ["get", "patch"]
```

**Bound only via `RoleBinding` per allowed namespace.** A `clusterrolebinding` would give cluster-wide write — that violates D3.

The full list of allowed namespaces is computed by the Slack Handler at the moment of remediation:

```python
allowed_namespaces = ['inference'] + [
    ns.metadata.name for ns in k8s.list_namespace().items
    if ns.metadata.name.startswith('team-')
]
```

For each allowed namespace, a `RoleBinding` is pre-created via a small `namespace-watcher` reconciler inside the Slack Handler (or via the Event Watcher — keep it in one place; let's say Event Watcher since it's already running an informer). When a new `team-*` namespace appears, the watcher creates the RoleBinding. When one disappears, it deletes the RoleBinding.

Alternative simpler approach: **list all current `team-*` namespaces at agent boot, create a RoleBinding for each in a one-shot Job, and rely on a periodic refresh every 5 min.** This avoids leader-election/concurrency complexity. Going with this for V1.

### 5.3 Job-spawner SA (used by Slack Handler)

```yaml
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["create", "get", "list"]
  resourceNames: []  # any in own namespace
# scoped to platform-health-agent namespace via Role+RoleBinding
```

---

## 6. Database Schema

```sql
-- Run by db-init Job inside the new `platform_health_agent` database.

CREATE TABLE IF NOT EXISTS investigations (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  status             TEXT NOT NULL CHECK (status IN
                     ('pending','running','awaiting_approval','remediating','done','dismissed','expired','failed')),
  trigger_kind       TEXT NOT NULL,                 -- e.g. 'CrashLoopBackOff'
  resource_kind      TEXT NOT NULL,
  resource_namespace TEXT NOT NULL,
  resource_name      TEXT NOT NULL,
  event_payload      JSONB NOT NULL,                -- raw event/condition that triggered
  findings           JSONB,                          -- written by Investigator
  fix_commands       JSONB,                          -- extracted from findings
  out_of_scope       BOOLEAN NOT NULL DEFAULT false,
  slack_message_ts   TEXT,                           -- top-level DM message id
  slack_thread_ts    TEXT,                           -- thread root for replies
  approved_by        TEXT,                           -- Slack user id
  approved_at        TIMESTAMPTZ,
  remediation_result JSONB,                          -- written by Remediator
  completed_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS investigations_status_idx ON investigations(status);
CREATE INDEX IF NOT EXISTS investigations_created_idx ON investigations(created_at DESC);

CREATE TABLE IF NOT EXISTS debounce (
  resource_kind      TEXT NOT NULL,
  resource_namespace TEXT NOT NULL,
  resource_name      TEXT NOT NULL,
  last_seen          TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (resource_kind, resource_namespace, resource_name)
);

CREATE TABLE IF NOT EXISTS daily_counters (
  day                DATE PRIMARY KEY,
  investigations     INTEGER NOT NULL DEFAULT 0,
  remediations       INTEGER NOT NULL DEFAULT 0
);
```

**Connection string** (env var on Event Watcher, Slack Handler, all Jobs):
```
postgres://platform_health_agent:<password>@platform-db.ai-platform.svc.cluster.local:5432/platform_health_agent
```

The `platform_health_agent` user + password is created by the db-init Job and stored in the `platform-health-agent-secrets` K8s Secret as `DB_PASSWORD`. The Job uses the existing `platform-db-credentials` (postgres superuser) for the one-time provisioning.

---

## 7. Configuration ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: platform-health-agent-config
  namespace: platform-health-agent
data:
  CLUSTER_NAME: "ai-platform"
  AWS_REGION: "us-east-1"

  # Watch / debounce
  WATCH_NAMESPACES: "*"                          # comma-list or "*"
  EXCLUDE_NAMESPACES: "kube-system,kube-node-lease,argocd,external-secrets,gpu-operator,kuberay,amazon-cloudwatch,platform-health-agent"
  DEBOUNCE_WINDOW_SEC: "600"

  # Triggers (boolean toggle each)
  TRIGGER_CRASHLOOP: "true"
  TRIGGER_OOMKILLED: "true"
  TRIGGER_IMAGEPULL: "true"
  TRIGGER_FAILEDSCHED: "true"
  TRIGGER_NODENOTREADY: "true"
  TRIGGER_FAILEDMOUNT: "true"

  # Concurrency + cost
  MAX_CONCURRENT_INVESTIGATIONS: "3"
  MAX_INVESTIGATIONS_PER_DAY: "50"
  MAX_REMEDIATIONS_PER_DAY: "20"

  # Models
  KIRO_MODEL_INVESTIGATE: "claude-sonnet-4.6"
  KIRO_MODEL_REMEDIATE: "claude-opus-4.6"

  # Remediation scope (informational; enforced by RBAC)
  ALLOWED_NAMESPACES_PATTERN: "^(inference|team-.+)$"
```

---

## 8. Slack Message Templates

**Investigation finding (initial DM):**

```
Bot DM to U01ABCD2EF3:

🚨 [HIGH] CrashLoopBackOff in `inference/llama-7b-worker-2`

*Root cause*
Container OOMKilled: workerMemory=12Gi but model load peaks at 14.2Gi during weight init.

*Affected resources*
• Pod inference/llama-7b-worker-2
• RayService inference/llama-7b
• InferenceEndpoint inference/llama-7b (status: Degraded)

*Suggested fix*
```yaml
kubectl patch inferenceendpoint llama-7b -n inference \
  --type=merge -p '{"spec":{"workerMemory":"16Gi"}}'
```

⚠️  *This is out of scope for the agent — would modify a KRO CR managed by ArgoCD via workloads/models/.*
*Manual action required:* edit `workloads/models/llama-7b.yaml` and push.
```

If `out_of_scope=false`, the message ends with two buttons instead:
`[ ✅ Approve Fix ]   [ ❌ Dismiss ]`

**Remediation result (threaded reply):**

```
✅ Fix applied successfully
Approved by <@U01ABCD2EF3> at 14:23:45 UTC
Verification: pod healthy after 22s
Rollback: kubectl rollout undo deployment/foo -n team-search
```

---

## 9. ApplicationSet Diff

`argocd/bootstrap/platform.yaml` — add one element under `# --- Platform Services (user-facing) ---`:

```yaml
          - name: platform-health-agent
            namespace: platform-health-agent
            type: directory
            path: platform/services/platform-health-agent
            tier: platform
```

Place it after `cluster-dashboard` so it's the last platform service. **No other ArgoCD changes needed** — the existing `templatePatch` handles directory-mode apps automatically, and `CreateNamespace=true + ServerSideApply=true` are already set globally.

**Disable the agent:** delete that block + push. ArgoCD will prune the entire stack (pods, jobs, RBAC, ConfigMap, Secrets — except the manually-created `platform-health-agent-secrets` which has no owner reference, so it persists; and the postgres `platform_health_agent` database persists, which is intentional for audit replay if re-enabled).

---

## 10. Rollout Sequence

Each step has explicit verification before proceeding to the next.

### Step 1 — Land the manifests (no behaviour change yet)

1. Create branch `feat/platform-health-agent`.
2. Add all files under `platform/services/platform-health-agent/`.
3. **Do not yet add the entry to `argocd/bootstrap/platform.yaml`** — this keeps ArgoCD from picking it up.
4. Open PR. Validate with `kubectl --dry-run=client apply -k platform/services/platform-health-agent/` locally.

### Step 2 — Bootstrap secrets and DB (manual, one-time)

```bash
# Namespace
kubectl create namespace platform-health-agent

# Secret (see §1.3)
kubectl -n platform-health-agent create secret generic platform-health-agent-secrets ...

# Verify
kubectl -n platform-health-agent get secret platform-health-agent-secrets -o jsonpath='{.data}' | base64 -d 2>&1 | head -c 200
```

### Step 3 — Enable in ArgoCD (the one-line switch)

1. Add the new element to `argocd/bootstrap/platform.yaml` (§9).
2. Push.
3. Watch:
   ```bash
   kubectl get applications -n argocd platform-health-agent -w
   kubectl get pods -n platform-health-agent -w
   ```
4. Expected sequence:
   - `db-init` Job runs and completes (logs: "database platform_health_agent created", "DDL applied").
   - `event-watcher` and `slack-handler` Deployments come up Ready 1/1.

### Step 4 — Smoke test (read-only path)

1. Bring up a dummy CrashLoopBackOff in a sandbox namespace:
   ```bash
   kubectl create namespace team-test-devops
   kubectl apply -n team-test-devops -f - <<EOF
   apiVersion: apps/v1
   kind: Deployment
   metadata: {name: crasher}
   spec:
     replicas: 1
     selector: {matchLabels: {app: crasher}}
     template:
       metadata: {labels: {app: crasher}}
       spec:
         containers:
         - name: c
           image: busybox:latest
           command: ["sh", "-c", "exit 1"]
   EOF
   ```
2. Within ~3 minutes, expect a DM in Slack with the investigation finding.
3. **Click `Dismiss`** (don't approve yet — we're testing the read-only path).
4. Verify in postgres: `SELECT id, status, resource_name FROM investigations ORDER BY created_at DESC LIMIT 5;`
   The most recent row should show `status='dismissed'`.

### Step 5 — End-to-end test (write path)

1. Trigger a real fixable issue:
   ```bash
   # Pod stuck on bad image
   kubectl -n team-test-devops set image deployment/crasher c=busybox:nonexistent-tag
   ```
2. Receive DM, **click `Approve`**.
3. Watch:
   - `kubectl get jobs -n platform-health-agent -w` — expect a `remediator-…` Job.
   - DM thread reply within 1-2 min.
4. Verify the deployment was actually patched:
   ```bash
   kubectl -n team-test-devops get deployment crasher -o jsonpath='{.spec.template.spec.containers[0].image}'
   ```

### Step 6 — Tear down test, document

1. `kubectl delete namespace team-test-devops`
2. Update `README.md` with operator notes from anything learned.
3. Squash-merge to `main`.

### Step 7 — Soak (1 week)

Let the agent run during normal cluster activity. Monitor:
- Daily counter usage vs. limits
- Spurious investigations on benign events (if any → tighten triggers)
- Kiro API credit burn (visible at kiro.dev) → adjust models if needed

---

## 11. Verification Plan (per component)

| Component | Test | Pass criterion |
|-----------|------|---------------|
| `db-init` Job | Run twice manually | Second run is no-op; tables not duplicated |
| `event-watcher` | Bring up obvious CrashLoopBackOff | Investigation row created in postgres within 60s |
| Debounce | Trigger same crash twice within 5 min | Only one investigation row created |
| Concurrency cap | Manually `INSERT` 3 'running' rows, trigger event | Event ignored; log shows "concurrency cap reached" |
| Cost guard | Set `MAX_INVESTIGATIONS_PER_DAY=1`, trigger 2 events | First spawns Job, second is suppressed with DM warning |
| Investigator (read-only) | Run Job by hand with `kubectl create job --from=cronjob` (or template) | Produces valid `findings.json`; no kubectl write attempts in logs |
| Slack handler auth | Have a different Slack user click Approve | DM "Not authorized"; no Job spawned |
| Approval expiry | Manually set `created_at = now() - 25h`, click Approve | DM "Approval expired"; no Job spawned |
| Remediator scope | Manually craft a fix that targets `ai-platform` namespace, force into postgres, click Approve | kubectl 403; remediation result `applied=false, error_summary` populated |
| Out-of-scope detection | Trigger event whose only fix would touch a KRO CR | DM shows fix as text only (no buttons), `out_of_scope=true` in DB |
| Disable | Remove ApplicationSet entry, push | ArgoCD prunes all manifests within 3 min; secrets persist |

---

## 12. Operational Notes

- **Backfill**: agent only sees events fired AFTER it starts. No replay of historical events. If an issue exists at boot, the watcher's initial Pod list scan catches it (informers list-then-watch).
- **Kiro CLI failure modes**: API timeout, 429 rate limit, model deprecation. The wrapper catches non-zero exit and posts a "investigation failed" DM with the raw error. Counts toward daily counter (so a runaway error doesn't flood Slack).
- **Postgres connection loss**: Event Watcher reconnects with exponential backoff up to 60s. Slack Handler same. If postgres is down for longer, both Deployments crash → ArgoCD restarts them; Slack Handler reconnects to Slack on next start.
- **Slack rate limits**: bot DMs are limited to 1 message/sec. With cap of 50 DMs/day, no risk.
- **Audit log**: all approvals/dismissals/remediations are in postgres `investigations` table forever. Consider exporting to CloudWatch Logs in V2.
- **Kill switch**: `kubectl scale deployment event-watcher -n platform-health-agent --replicas=0` stops new investigations without touching ArgoCD config (ArgoCD will scale it back up within ~3min — for a true kill, comment out the ApplicationSet entry).

---

## 13. README content (operator-facing)

The `README.md` in `platform/services/platform-health-agent/` will be the operator runbook. Outline:

1. What this is (1 paragraph).
2. How to enable / disable.
3. Required setup (link to §1 of this doc).
4. How to read the postgres state (`kubectl exec -n ai-platform platform-db-0 -- psql -U platform_health_agent -d platform_health_agent -c 'SELECT …'`).
5. Troubleshooting:
   - "I'm not getting any DMs": check Slack Handler logs, verify Socket Mode WS is connected, verify bot was DM'd at least once.
   - "The agent investigated something silly": tune `EXCLUDE_NAMESPACES` or set the relevant `TRIGGER_*=false`.
   - "Approve button does nothing": check Slack Handler logs; common cause is expired investigation (>24h).
   - "Remediator gets 403": check the `team-*` RoleBinding watcher logs; the namespace may be missing a RoleBinding.
6. Links to architecture + this implementation plan.

---

## 14. Future Work (V2 candidates — not in scope)

Pulled forward from the architecture doc + emerged during this design:

- ESO-backed secrets (rotation)
- Multi-approver mode for CRITICAL events
- Webhook-based remediation that commits a fix to git as a PR (true GitOps remediation)
- HTTPS + ALB endpoint as alternative to Socket Mode (multi-cluster)
- ARM64 image support (re-test once kiro-cli ships an arm64 binary)
- CloudWatch Insights export of investigations table
- Prometheus metrics + Grafana dashboard
- Per-namespace remediation budgets (some teams more permissive than others)
- "Was this fix helpful?" feedback loop → prompt-tuning data collection
- GPU-specific signals: DCGM error counters, nvidia-smi crash patterns

---

## 15. Acceptance Criteria

V1 ships when ALL the following are green:

- [ ] All files in `platform/services/platform-health-agent/` reviewed and committed
- [ ] One-line addition to `argocd/bootstrap/platform.yaml` reviewed
- [ ] Slack app created with documented scopes; tokens stored in `platform-health-agent-secrets`
- [ ] `db-init` Job successfully runs, postgres `platform_health_agent` DB present with three tables
- [ ] Event Watcher and Slack Handler Deployments healthy 1/1
- [ ] Smoke test (Step 4) passes: investigation produced, dismissal logged
- [ ] End-to-end test (Step 5) passes: real fix applied, verified in cluster, threaded reply in Slack
- [ ] All twelve verification items in §11 pass
- [ ] Disable test (remove from ApplicationSet, ArgoCD prunes cleanly) passes
- [ ] `README.md` complete and links to architecture doc

---

*End of plan.*
