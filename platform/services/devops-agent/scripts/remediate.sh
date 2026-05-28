#!/bin/sh
# remediate.sh — wrapper script run by Remediator Job pods.
#
# Flow:
#   1. Pull approved fix_commands + approval metadata from postgres.
#   2. Invoke kiro-cli chat --no-interactive with write tool set.
#      The LLM applies the fix via kubectl, waits, verifies.
#   3. The LLM writes /results/result.json.
#   4. Hand off to persist_findings.py remediation (writes result back to DB).
#
# Write enforcement is RBAC: devops-agent-writer has scoped write access
# to inference + team-* namespaces only. Anything else 403s, which we
# capture as `applied=false` in the result.
set -eu

[ -n "${INVESTIGATION_ID:-}" ] || { echo "INVESTIGATION_ID not set" >&2; exit 2; }

mkdir -p /results
LOG=/results/kiro-stdout.log

post_error() {
    msg="$1"
    echo "[remediate] $msg" >&2
    python3 /scripts/persist_findings.py error "$INVESTIGATION_ID" "$msg" || true
    exit 1
}

# Pull fix_commands + approval metadata from postgres.
python3 - <<'PY' > /tmp/context.json
import json, os, sys
import psycopg
conn = psycopg.connect(
    host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT","5432")),
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
    password=os.environ["DB_PASSWORD"], autocommit=True, connect_timeout=10,
)
with conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
    cur.execute("""SELECT id, trigger_kind, resource_kind, resource_namespace, resource_name,
                          fix_commands, findings, approved_by, approved_at
                     FROM investigations WHERE id=%s""",
                (os.environ["INVESTIGATION_ID"],))
    row = cur.fetchone()
if not row:
    sys.exit(f"investigation {os.environ['INVESTIGATION_ID']} not found in DB")
row["approved_at"] = row["approved_at"].isoformat() if row["approved_at"] else None
print(json.dumps(row, default=str))
PY

[ -s /tmp/context.json ] || post_error "Could not load investigation context from DB"

APPROVED_BY=$(python3 -c 'import json; print(json.load(open("/tmp/context.json")).get("approved_by",""))')
APPROVED_AT=$(python3 -c 'import json; print(json.load(open("/tmp/context.json")).get("approved_at",""))')

# Setup kubeconfig (Remediator pod uses the writer SA).
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

cat > /tmp/prompt.txt <<EOF
You are an autonomous EKS remediator running inside cluster "${CLUSTER_NAME}"
in region "${AWS_REGION}".

A human approved a remediation. Investigation context (with the approved
fix_commands) is in /tmp/context.json — read it first.

Approval audit:
  Approved by:    ${APPROVED_BY}
  Approved at:    ${APPROVED_AT}
  Investigation:  ${INVESTIGATION_ID}

Write enforcement: your kubectl SA only has write access to namespaces matching
'^(inference|team-.+)\$'. Any other write WILL fail with 403.

Procedure:
1. Read /tmp/context.json. Note the fix_commands list — each item has
   {description, commands[]}.
2. Apply each command in order. Capture stdout/stderr.
3. After ALL commands have been attempted (continue past individual failures),
   sleep 30 seconds.
4. Verify the affected resources reached a healthy state:
   - For Pods: phase==Running and all conditions True
   - For Deployments: status.availableReplicas == status.replicas
   - For StatefulSets: status.readyReplicas == status.replicas
5. Do NOT attempt additional remediation beyond what was approved.
6. Compose rollback_commands: best-effort kubectl commands to undo each
   applied change (e.g. \`kubectl rollout undo deployment/foo -n bar\` for a
   rollout restart, or restoring a previous image tag).

Write a SINGLE JSON object to /results/result.json with EXACTLY these keys:
{
  "applied":           true | false,
  "verification_pass": true | false,
  "post_fix_status": [
    {"kind":"Deployment","namespace":"team-search","name":"reranker","status":"ready"}
  ],
  "rollback_commands": ["kubectl rollout undo deployment/reranker -n team-search"],
  "error_summary":     null | "short failure description"
}

Do NOT print the JSON to stdout — only WRITE the file.
Do NOT exit until /results/result.json exists.
EOF

echo "[remediate] running kiro-cli model=${KIRO_MODEL_REMEDIATE}…"
if ! /tools/kiro-cli chat --no-interactive \
        --model "${KIRO_MODEL_REMEDIATE}" \
        --trust-tools=fs_read,fs_write,execute_bash \
        "$(cat /tmp/prompt.txt)" > "$LOG" 2>&1; then
    post_error "kiro-cli exited non-zero during remediation. tail:
$(tail -20 "$LOG" | sed 's/[\\r\\n]/ /g; s/\"/\\\\\"/g')"
fi

[ -s /results/result.json ] || post_error "remediator did not produce /results/result.json"
if ! python3 -c 'import json,sys; json.load(open("/results/result.json"))'; then
    post_error "/results/result.json is not valid JSON. tail:
$(tail -10 "$LOG")"
fi

echo "[remediate] persisting result…"
python3 /scripts/persist_findings.py remediation "$INVESTIGATION_ID" /results/result.json
echo "[remediate] done."
