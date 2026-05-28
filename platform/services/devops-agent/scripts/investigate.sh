#!/bin/sh
# investigate.sh — wrapper script run by Investigator Job pods.
#
# Flow:
#   1. Build the investigation prompt from EVENT_PAYLOAD env var.
#   2. Invoke kiro-cli chat --no-interactive with read-only tool set.
#      The LLM is instructed to write its findings to /results/findings.json.
#   3. Validate the output is well-formed JSON.
#   4. Hand off to persist_findings.py (writes findings to investigations
#      table; cluster-dashboard renders the approvals UI from there).
#
# On any failure: persist_findings.py error … is invoked; the
# investigation row is marked status='failed'.
#
# Read-only enforcement happens at the K8s RBAC layer
# (devops-agent-reader has only get/list/watch).
set -eu

[ -n "${INVESTIGATION_ID:-}" ] || { echo "INVESTIGATION_ID not set" >&2; exit 2; }
[ -n "${EVENT_PAYLOAD:-}"   ] || { echo "EVENT_PAYLOAD not set"   >&2; exit 2; }

mkdir -p /results
LOG=/results/kiro-stdout.log

post_error() {
    msg="$1"
    echo "[investigate] $msg" >&2
    python3 /scripts/persist_findings.py error "$INVESTIGATION_ID" "$msg" || true
    exit 1
}

# Setup kubeconfig for in-cluster access (kiro-cli will shell out to kubectl).
export KUBECONFIG=/tmp/kubeconfig
KUBE_TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
KUBE_CA=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
KUBE_API="https://${KUBERNETES_SERVICE_HOST:-kubernetes.default.svc}:${KUBERNETES_SERVICE_PORT:-443}"

cat > "$KUBECONFIG" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: in-cluster
  cluster:
    server: ${KUBE_API}
    certificate-authority: ${KUBE_CA}
contexts:
- name: in-cluster
  context: { cluster: in-cluster, user: sa, namespace: default }
current-context: in-cluster
users:
- name: sa
  user: { token: ${KUBE_TOKEN} }
EOF

# Build the prompt. We do NOT trust env interpolation into the prompt body
# beyond the controlled variables — the LLM is instructed to read EVENT_PAYLOAD
# from a file rather than receive it inline (limits prompt-injection surface).
echo "$EVENT_PAYLOAD" > /tmp/event.json
ALLOWED_NS_PATTERN='^(inference|team-.+)$'

cat > /tmp/prompt.txt <<EOF
You are an autonomous EKS incident investigator running inside cluster
"${CLUSTER_NAME}" in region "${AWS_REGION}".

An automated event watcher detected a problem and dispatched you to investigate.
The triggering event is in /tmp/event.json — read it first.

Use kubectl (already configured for in-cluster read-only access; see KUBECONFIG)
to gather context. Read-only operations only: get, describe, logs, top.
Do NOT attempt: delete, patch, edit, apply, scale, rollout, exec, port-forward.
Any write attempt will be rejected by RBAC and will count as a failed investigation.

Investigation procedure:
1. Read /tmp/event.json. Identify the affected resource: kind, namespace, name.
2. \`kubectl describe\` the affected resource and walk its owner chain
   (Pod → ReplicaSet → Deployment, or Pod → RayCluster → RayService, etc.).
3. \`kubectl logs --tail=100 --all-containers=true\` on the affected pod (or sibling pods if the affected pod is gone).
4. \`kubectl get events -n <namespace> --sort-by=.lastTimestamp | tail -50\` for the affected namespace.
5. If the resource is a Pod, check its node:
   \`kubectl describe node <node-name>\` — look at conditions, capacity, allocatable.
6. Check resource quotas: \`kubectl get resourcequota -n <namespace>\`.
7. If the resource lives in 'inference' or matches '${ALLOWED_NS_PATTERN}',
   inspect parent KRO custom resources:
     - \`kubectl get inferenceendpoint -n inference -o yaml <name>\` (if applicable)
     - \`kubectl get aiteam -n ai-platform -o yaml\` (find owning team)
   Read these for context only — do NOT propose modifying them.

Determine \`out_of_scope\`:
- TRUE if the only reasonable fix is to modify an InferenceEndpoint or AITeam
  custom resource, OR any resource in: ai-platform, gpu-operator, kuberay,
  argocd, external-secrets, kube-system, amazon-cloudwatch, devops-agent.
- TRUE if the fix requires editing a file under platform/services or workloads/.
- FALSE if the fix can be applied via kubectl directly to a workload-level
  resource (Deployment, StatefulSet, Pod, ConfigMap, HPA) in 'inference' or a
  'team-*' namespace.

Write a SINGLE JSON object to /results/findings.json with EXACTLY these keys:
{
  "summary":            "2-3 sentence root-cause explanation",
  "severity":           "CRITICAL" | "HIGH" | "MEDIUM" | "LOW",
  "affected_resources": [{"kind": "...", "namespace": "...", "name": "..."}],
  "fix_commands": [
    {
      "description": "what this fix does, 1 sentence",
      "commands":    ["kubectl ... line 1", "kubectl ... line 2"]
    }
  ],
  "risk_assessment":        "what could go wrong if the fix is applied",
  "requires_manual_review": true | false,
  "out_of_scope":           true | false
}

If you cannot determine root cause confidently, emit the file with
severity="LOW", fix_commands=[], requires_manual_review=true,
and explain the uncertainty in the summary.

Do NOT print the JSON to stdout — only WRITE the file.
Do NOT modify any resources. Do NOT exit until /results/findings.json exists.
EOF

# Run kiro-cli. Trust set: fs_read, fs_write, execute_bash (kubectl).
echo "[investigate] running kiro-cli model=${KIRO_MODEL_INVESTIGATE}…"
if ! /tools/kiro-cli chat --no-interactive \
        --model "${KIRO_MODEL_INVESTIGATE}" \
        --trust-tools=fs_read,fs_write,execute_bash \
        "$(cat /tmp/prompt.txt)" > "$LOG" 2>&1; then
    post_error "kiro-cli exited non-zero. tail of log:
$(tail -20 "$LOG" | sed 's/[\\r\\n]/ /g; s/\"/\\\\\"/g')"
fi

# Validate the file.
[ -s /results/findings.json ] || post_error "kiro-cli did not produce /results/findings.json"
if ! python3 -c 'import json,sys; json.load(open("/results/findings.json"))'; then
    post_error "/results/findings.json is not valid JSON. tail of log:
$(tail -10 "$LOG")"
fi

# Persist findings to the DB. Cluster-dashboard renders the approvals UI from there.
echo "[investigate] persisting findings…"
python3 /scripts/persist_findings.py investigation "$INVESTIGATION_ID" /results/findings.json
echo "[investigate] done."
