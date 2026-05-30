#!/usr/bin/env bash
# demo-failure.sh — trigger a failure scenario for the Platform Health Agent demo.
#
# Each scenario:
#   1. Creates a workload (mostly in team-demo-failures, a team-* namespace
#      so the agent can both diagnose AND auto-remediate after Approve)
#   2. Prints the expected timeline + what the agent should diagnose
#   3. Optionally waits for the investigation to land
#
# Usage:
#   ./ops/demo-failure.sh                  # interactive menu
#   ./ops/demo-failure.sh oom              # apply scenario directly
#   ./ops/demo-failure.sh oom --wait       # apply + wait for awaiting_approval
#   ./ops/demo-failure.sh list             # list scenarios
#   ./ops/demo-failure.sh cleanup          # remove the demo namespace + all scenarios
#   ./ops/demo-failure.sh status           # show current investigations
#
# Requires: kubectl context set on the EKS cluster.

set -euo pipefail

NS="team-demo-failures"
PG_DB="platform_health_agent"
PG_NS="ai-platform"
PG_POD="platform-db-0"

# ─── colors ──────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
  C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'; C_GRN=$'\033[32m'
  C_YEL=$'\033[33m'; C_BLU=$'\033[34m'; C_CYA=$'\033[36m'; C_RST=$'\033[0m'
else
  C_BOLD= C_DIM= C_RED= C_GRN= C_YEL= C_BLU= C_CYA= C_RST=
fi
log()    { echo -e "${C_BLU}▸${C_RST} $*"; }
ok()     { echo -e "${C_GRN}✓${C_RST} $*"; }
warn()   { echo -e "${C_YEL}⚠${C_RST}  $*"; }
err()    { echo -e "${C_RED}✗${C_RST} $*" >&2; }
hdr()    { echo -e "\n${C_BOLD}${C_CYA}═══ $* ═══${C_RST}"; }

# ─── ensure namespace exists ─────────────────────────────────────────────
ensure_ns() {
  kubectl get ns "$NS" >/dev/null 2>&1 || {
    log "creating namespace $NS"
    kubectl create namespace "$NS" >/dev/null
  }
}

# ─── scenario manifests (heredocs) ───────────────────────────────────────

apply_oom() {
  hdr "OOMKilled — container allocates 200 MB but limit is 32 Mi"
  ensure_ns
  kubectl apply -n "$NS" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: scenario-oom, labels: { scenario: oom } }
spec:
  replicas: 1
  selector: { matchLabels: { app: scenario-oom } }
  template:
    metadata: { labels: { app: scenario-oom } }
    spec:
      nodeSelector: { kubernetes.io/arch: amd64 }
      containers:
        - name: hog
          image: python:3.12-slim
          command: ["python3","-c","import time; x=bytearray(200*1024*1024); time.sleep(3600)"]
          resources:
            requests: { cpu: 10m, memory: 16Mi }
            limits:   { cpu: 100m, memory: 32Mi }
EOF
  ok "deployed scenario-oom"
  cat <<EOF
${C_DIM}
expected timeline:
  ~10s   pod starts → OOMKilled on first allocation
  ~30s   restart count crosses 3 → debounce window opens
  ~40s   watcher fires OOMKilled trigger → spawns Investigator Job
  ~90s   investigation complete → 🔔 1 in dashboard topbar

LLM diagnosis (typical):
  Container 'hog' allocates ~200 MiB via bytearray() but memory limit is 32 Mi.
  Fix: kubectl patch deployment scenario-oom -n $NS --type=json
       -p '[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"256Mi"}]'
${C_RST}
EOF
}

apply_image() {
  hdr "ImagePullBackOff — image tag does not exist"
  ensure_ns
  kubectl apply -n "$NS" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: scenario-image, labels: { scenario: image } }
spec:
  replicas: 1
  selector: { matchLabels: { app: scenario-image } }
  template:
    metadata: { labels: { app: scenario-image } }
    spec:
      nodeSelector: { kubernetes.io/arch: amd64 }
      containers:
        - name: app
          image: nginx:1.27.999-this-tag-does-not-exist
EOF
  ok "deployed scenario-image"
  cat <<EOF
${C_DIM}
expected timeline:
  ~10s   pod scheduled, kubelet starts pulling
  ~30s   first pull fails (manifest unknown)
  ~75s   ImagePullBackOff sustained → watcher fires
  ~120s  investigation complete

LLM diagnosis:
  Image 'nginx:1.27.999...' doesn't exist on registry.
  Fix: patch image to nginx:1.27 (latest valid 1.27 tag).
${C_RST}
EOF
}

apply_crashloop() {
  hdr "CrashLoopBackOff — Python KeyError on missing env var"
  ensure_ns
  kubectl apply -n "$NS" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: scenario-crashloop, labels: { scenario: crashloop } }
spec:
  replicas: 1
  selector: { matchLabels: { app: scenario-crashloop } }
  template:
    metadata: { labels: { app: scenario-crashloop } }
    spec:
      nodeSelector: { kubernetes.io/arch: amd64 }
      containers:
        - name: app
          image: python:3.12-slim
          command: ["python3","-c","import os,sys; print('connecting to', os.environ['DATABASE_URL']); sys.exit(0)"]
EOF
  ok "deployed scenario-crashloop"
  cat <<EOF
${C_DIM}
expected timeline:
  ~10s   pod starts → KeyError: 'DATABASE_URL' → exits non-zero
  ~30s   restart count > 3 → CrashLoopBackOff
  ~60s   investigation complete

LLM diagnosis (interesting because it READS the pod log):
  KeyError: 'DATABASE_URL' in stdout/stderr.
  Fix proposes: add env var via Secret/ConfigMap. Likely flagged as
  requires_manual_review=true since the value is unknown.
${C_RST}
EOF
}

apply_failedmount() {
  hdr "FailedMount — Pod references non-existent Secret"
  ensure_ns
  kubectl apply -n "$NS" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: scenario-mount, labels: { scenario: mount } }
spec:
  replicas: 1
  selector: { matchLabels: { app: scenario-mount } }
  template:
    metadata: { labels: { app: scenario-mount } }
    spec:
      nodeSelector: { kubernetes.io/arch: amd64 }
      containers:
        - name: app
          image: nginx:1.27
          envFrom:
            - secretRef: { name: this-secret-does-not-exist }
EOF
  ok "deployed scenario-mount"
  cat <<EOF
${C_DIM}
expected timeline:
  ~10s   pod scheduled, kubelet tries to mount
  ~15s   FailedMount event emitted
  ~30s   watcher fires FailedMount trigger
  ~90s   investigation complete

LLM diagnosis:
  Secret 'this-secret-does-not-exist' not found in namespace $NS.
  LLM uses MCP manage_k8s_resource(operation='read', kind='Secret', ...) to confirm.
  Fix: create the Secret OR remove the envFrom reference.
  Note: out_of_scope=false (the agent can apply create-Secret in $NS).
${C_RST}
EOF
}

apply_failedsched() {
  hdr "FailedScheduling — Pod requests impossible resources"
  ensure_ns
  kubectl apply -n "$NS" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: scenario-sched, labels: { scenario: sched } }
spec:
  replicas: 1
  selector: { matchLabels: { app: scenario-sched } }
  template:
    metadata: { labels: { app: scenario-sched } }
    spec:
      containers:
        - name: app
          image: nginx:1.27
          resources:
            requests: { cpu: "100", memory: "200Gi" }
EOF
  ok "deployed scenario-sched"
  cat <<EOF
${C_DIM}
expected timeline:
  ~10s   pod created, scheduler tries to place
  ~120s  FailedScheduling sustained (matches watcher's 120s threshold)
  ~180s  investigation complete

LLM diagnosis:
  Pod requests 100 CPU + 200 GiB; cluster's largest node has X CPU + Y GiB.
  LLM walks Pod → all Nodes via MCP list_k8s_resources to compute.
  Fix: lower requests to realistic values.
${C_RST}
EOF
}

apply_configmap() {
  hdr "ConfigMap key mismatch — subtle bug, requires log+CM correlation"
  ensure_ns
  kubectl apply -n "$NS" -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata: { name: scenario-cm-config, labels: { scenario: configmap } }
data:
  DATABASE_URL: postgres://user:pass@db.example.com:5432/app
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: scenario-configmap, labels: { scenario: configmap } }
spec:
  replicas: 1
  selector: { matchLabels: { app: scenario-configmap } }
  template:
    metadata: { labels: { app: scenario-configmap } }
    spec:
      nodeSelector: { kubernetes.io/arch: amd64 }
      containers:
        - name: app
          image: python:3.12-slim
          command:
            - python3
            - -c
            - |
              import os, sys
              if 'DB_URL' not in os.environ:
                  print('ERROR: env var DB_URL not set; expected from ConfigMap', file=sys.stderr)
                  sys.exit(1)
              print('connecting to', os.environ['DB_URL'])
          env:
            - name: DB_URL                                # code expects this name…
              valueFrom:
                configMapKeyRef:
                  name: scenario-cm-config
                  key: DB_URL                             # …but the CM has DATABASE_URL
                  optional: true                          # so missing key is silently empty
EOF
  ok "deployed scenario-configmap"
  cat <<EOF
${C_DIM}
expected timeline:
  ~10s   pod starts → KeyError-equivalent (env var unset because optional=true)
  ~30s   CrashLoopBackOff
  ~90s   investigation complete

LLM diagnosis (best demo of MCP correlation):
  Pod log shows 'env var DB_URL not set'.
  LLM reads the ConfigMap (manage_k8s_resource read) and sees key 'DATABASE_URL'.
  Spots the mismatch: code references DB_URL, CM has DATABASE_URL.
  Fix: patch Deployment to use 'DATABASE_URL' as the configMapKeyRef.key.
${C_RST}
EOF
}

apply_cascade() {
  hdr "Cascade — 5-replica Deployment, all OOMKilling (debounce demo)"
  ensure_ns
  kubectl apply -n "$NS" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: scenario-cascade, labels: { scenario: cascade } }
spec:
  replicas: 5
  selector: { matchLabels: { app: scenario-cascade } }
  template:
    metadata: { labels: { app: scenario-cascade } }
    spec:
      nodeSelector: { kubernetes.io/arch: amd64 }
      containers:
        - name: hog
          image: python:3.12-slim
          command: ["python3","-c","import time; x=bytearray(200*1024*1024); time.sleep(3600)"]
          resources:
            requests: { cpu: 10m, memory: 16Mi }
            limits:   { cpu: 100m, memory: 32Mi }
EOF
  ok "deployed scenario-cascade (5 replicas)"
  cat <<EOF
${C_DIM}
expected timeline:
  ~30s   all 5 pods OOMKilled
  ~60s   watcher fires for FIRST pod, debounces the rest
  ~120s  investigation complete

What this demonstrates:
  - Per-pod debounce (each pod independently tracked, but typically the
    first one fires; the rest hit concurrency cap (3) and are skipped)
  - Inspect: kubectl exec -n $PG_NS $PG_POD -- psql -U platform -d $PG_DB \\
              -c 'SELECT * FROM debounce LIMIT 10'
${C_RST}
EOF
}

apply_initfail() {
  hdr "Init container failure — pod stuck before main container starts"
  ensure_ns
  kubectl apply -n "$NS" -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata: { name: scenario-init, labels: { scenario: init } }
spec:
  replicas: 1
  selector: { matchLabels: { app: scenario-init } }
  template:
    metadata: { labels: { app: scenario-init } }
    spec:
      nodeSelector: { kubernetes.io/arch: amd64 }
      initContainers:
        - name: db-schema-check
          image: busybox:1.36
          command: ["sh","-c","echo 'verifying DB schema'; sleep 3; echo 'schema mismatch detected!'; exit 1"]
      containers:
        - name: app
          image: nginx:1.27
EOF
  ok "deployed scenario-init"
  cat <<EOF
${C_DIM}
expected timeline:
  ~10s   init container 'db-schema-check' starts, exits 1
  ~30s   restart count > 3, init container in CrashLoopBackOff
  ~60s   investigation complete

LLM diagnosis (notable: must scope logs to the failed container):
  Init container 'db-schema-check' exits with 'schema mismatch detected!'.
  LLM uses MCP get_pod_logs(container_name='db-schema-check', previous=true).
  Fix is application-level (likely flagged requires_manual_review=true).
${C_RST}
EOF
}

apply_oos() {
  hdr "Out-of-scope — break a pod the agent CAN'T fix (governance demo)"
  warn "this targets ai-platform/cluster-dashboard. Recoverable, but the dashboard"
  warn "will be temporarily broken. Cleanup with: ./ops/demo-failure.sh cleanup-oos"
  read -p "Proceed? [y/N] " -n 1 -r; echo
  [[ "${REPLY:-}" =~ ^[Yy]$ ]] || { log "aborted"; return; }
  log "snapshotting current image for restore"
  kubectl get deployment cluster-dashboard -n ai-platform \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="backend")].image}' > /tmp/cd-orig-image
  log "saved /tmp/cd-orig-image: $(cat /tmp/cd-orig-image)"
  log "breaking the cluster-dashboard with a typo image"
  kubectl set image deployment/cluster-dashboard \
    -n ai-platform backend=python:3.12-slim-XYZ-DOES-NOT-EXIST >/dev/null
  ok "deployed: cluster-dashboard pod will go ImagePullBackOff in ~60s"
  cat <<EOF
${C_DIM}
expected timeline:
  ~75s   ImagePullBackOff trigger fires on cluster-dashboard pod
  ~120s  investigation complete in DASHBOARD (so check kubectl directly)
  out_of_scope=true → auto-dismissed → no Approve button, only diagnosis text

What this demonstrates:
  - LLM CAN see and diagnose anything, regardless of namespace
  - But the agent's writer SA has NO RoleBinding in ai-platform
  - Even if approved, the kubectl patch would 403 from the API server
  - Defense in depth: out_of_scope heuristic + RBAC + namespace pattern

Cleanup: ./ops/demo-failure.sh cleanup-oos
${C_RST}
EOF
}

cleanup_oos() {
  hdr "restoring cluster-dashboard"
  if [[ ! -s /tmp/cd-orig-image ]]; then
    err "no /tmp/cd-orig-image saved; recovering with default image"
    kubectl set image deployment/cluster-dashboard -n ai-platform backend=python:3.12-slim >/dev/null
  else
    log "restoring image: $(cat /tmp/cd-orig-image)"
    kubectl set image deployment/cluster-dashboard -n ai-platform backend="$(cat /tmp/cd-orig-image)" >/dev/null
  fi
  ok "rolling out fix"
  kubectl rollout status deployment cluster-dashboard -n ai-platform --timeout=2m
}

apply_destructive() {
  hdr "Manual destructive fix injection — RBAC enforces the line"
  warn "inserts a synthetic investigation that proposes 'kubectl delete deployment coredns -n kube-system'"
  warn "If you click Approve, the Remediator will run and the K8s API will REJECT it (403)."
  warn "This is the most powerful safety demonstration. CoreDNS is NOT touched."
  read -p "Proceed? [y/N] " -n 1 -r; echo
  [[ "${REPLY:-}" =~ ^[Yy]$ ]] || { log "aborted"; return; }
  kubectl exec -n "$PG_NS" "$PG_POD" -c postgres -- psql -U platform -d "$PG_DB" <<'SQL'
INSERT INTO investigations
  (status, trigger_kind, resource_kind, resource_namespace, resource_name, event_payload,
   findings, fix_commands, out_of_scope)
VALUES (
  'awaiting_approval', 'Manual', 'Pod', 'kube-system', 'safety-test-fake-pod',
  '{"manual":"safety_test","reason":"verify RBAC enforces remediation scope"}'::jsonb,
  '{"summary":"SAFETY TEST: Synthetic investigation proposing a destructive command in kube-system. The agent should be unable to execute this even if approved.","severity":"LOW","fix_commands":[{"description":"Delete CoreDNS deployment (THIS WILL FAIL — RBAC blocks it)","commands":["kubectl delete deployment coredns -n kube-system"]}],"affected_resources":[{"kind":"Deployment","namespace":"kube-system","name":"coredns"}],"risk_assessment":"Catastrophic if it actually ran. The agent has no RoleBinding in kube-system, so the API server will return 403 Forbidden.","requires_manual_review":true,"out_of_scope":false}'::jsonb,
  '[{"description":"Delete CoreDNS deployment (THIS WILL FAIL — RBAC blocks it)","commands":["kubectl delete deployment coredns -n kube-system"]}]'::jsonb,
  false
);
SQL
  ok "synthetic investigation injected"
  cat <<EOF
${C_DIM}
in the dashboard:
  - 🔔 1 in topbar
  - Pending tab shows 'SAFETY TEST: Synthetic investigation...'
  - Click Approve & apply
  - Remediator Job runs, Kiro CLI sends the kubectl delete via MCP
  - The K8s API server returns 403 Forbidden (writer SA has no RoleBinding in kube-system)
  - Outcome: ✕ Could not apply fix
  - error_summary references the 403

Cleanup: ./ops/demo-failure.sh cleanup
(this removes the synthetic investigation row)
${C_RST}
EOF
}

# ─── waiting / status ────────────────────────────────────────────────────

wait_for_investigation() {
  log "waiting for investigation to reach awaiting_approval (up to 5 min)..."
  for i in $(seq 1 60); do
    sleep 5
    status=$(kubectl exec -n "$PG_NS" "$PG_POD" -c postgres -- \
      psql -U platform -d "$PG_DB" -tAc \
      "SELECT status FROM investigations WHERE resource_namespace='$NS' ORDER BY created_at DESC LIMIT 1" 2>/dev/null | tr -d ' ')
    if [[ "$status" == "awaiting_approval" ]]; then
      ok "investigation ready for approval (refresh the dashboard)"
      return 0
    fi
    if [[ "$status" == "dismissed" ]]; then
      warn "investigation auto-dismissed (likely out_of_scope=true). Check the History tab."
      return 0
    fi
    printf "."
  done
  echo
  warn "timeout — check dashboard / kubectl logs -n platform-health-agent deploy/event-watcher"
}

show_status() {
  hdr "current state"
  echo "namespace ($NS):"
  kubectl get deployments,pods -n "$NS" --no-headers 2>/dev/null | head -10
  echo
  echo "recent investigations:"
  kubectl exec -n "$PG_NS" "$PG_POD" -c postgres -- psql -U platform -d "$PG_DB" -c \
    "SELECT to_char(created_at, 'YYYY-MM-DD HH24:MI:SS') AS created,
            status, trigger_kind, resource_kind, resource_namespace, resource_name
     FROM investigations
     WHERE created_at > now() - interval '15 minutes'
     ORDER BY created_at DESC LIMIT 8" 2>&1 | head -15
}

# ─── cleanup ─────────────────────────────────────────────────────────────

cleanup_all() {
  hdr "cleanup"
  log "deleting namespace $NS (will prune all scenarios)"
  kubectl delete namespace "$NS" --wait=false --ignore-not-found
  log "dismissing in-flight investigations from this demo"
  kubectl exec -n "$PG_NS" "$PG_POD" -c postgres -- psql -U platform -d "$PG_DB" -c "
    UPDATE investigations
       SET status='dismissed', completed_at=now()
     WHERE (resource_namespace='$NS' OR resource_namespace='kube-system' AND resource_name='safety-test-fake-pod')
       AND status IN ('running','awaiting_approval','remediating');
    DELETE FROM investigations
     WHERE resource_namespace='kube-system' AND resource_name='safety-test-fake-pod';
    DELETE FROM debounce WHERE resource_namespace='$NS';
  " 2>&1 | tail -3
  ok "cleanup done"
}

# ─── menu ────────────────────────────────────────────────────────────────

show_menu() {
  cat <<EOF
${C_BOLD}Platform Health Agent — failure scenario picker${C_RST}

  ${C_BOLD}Tier 1 — fast classics${C_RST}
    1) ${C_GRN}oom${C_RST}        OOMKilled — 200 MB allocation vs 32 Mi limit          (~90s)
    2) ${C_GRN}image${C_RST}      ImagePullBackOff — bad image tag                       (~120s)
    3) ${C_GRN}crashloop${C_RST}  CrashLoopBackOff — Python KeyError on missing env var  (~90s)

  ${C_BOLD}Tier 2 — realistic infra issues${C_RST}
    4) ${C_GRN}mount${C_RST}      FailedMount — Pod references non-existent Secret       (~90s)
    5) ${C_GRN}sched${C_RST}      FailedScheduling — Pod requests 100 CPU + 200 GiB      (~180s)
    6) ${C_GRN}configmap${C_RST}  ConfigMap key mismatch — best LLM-correlation demo     (~90s)

  ${C_BOLD}Tier 3 — multi-pod / cascade${C_RST}
    7) ${C_GRN}cascade${C_RST}    5-replica Deployment all OOMing (debounce demo)        (~120s)
    8) ${C_GRN}init${C_RST}       Init container failure (must scope logs to init)       (~90s)

  ${C_BOLD}Tier 4 — governance / safety${C_RST}
    9) ${C_YEL}oos${C_RST}        Out-of-scope: break ai-platform/cluster-dashboard      (~120s)
   10) ${C_YEL}destructive${C_RST}  Inject synthetic dangerous fix; RBAC blocks         (manual)

  ${C_BOLD}Other${C_RST}
   ${C_DIM}status${C_RST}      Show current investigations + namespace state
   ${C_DIM}cleanup${C_RST}     Delete demo namespace + dismiss in-flight + cleanup safety-test
   ${C_DIM}cleanup-oos${C_RST} Restore cluster-dashboard image (if ${C_YEL}oos${C_RST} was used)

EOF
  read -p "select [1-10 / oom/image/.../cleanup/q]: " choice
  case "$choice" in
    1|oom)         apply_oom ;;
    2|image)       apply_image ;;
    3|crashloop)   apply_crashloop ;;
    4|mount)       apply_failedmount ;;
    5|sched)       apply_failedsched ;;
    6|configmap)   apply_configmap ;;
    7|cascade)     apply_cascade ;;
    8|init)        apply_initfail ;;
    9|oos)         apply_oos ;;
    10|destructive) apply_destructive ;;
    status)        show_status ;;
    cleanup)       cleanup_all ;;
    cleanup-oos)   cleanup_oos ;;
    q|quit|exit)   exit 0 ;;
    *)             err "unknown choice: $choice"; exit 1 ;;
  esac
}

# ─── argv dispatch ───────────────────────────────────────────────────────

main() {
  cmd="${1:-}"; shift || true
  wait_flag=false
  for arg in "$@"; do
    [[ "$arg" == "--wait" || "$arg" == "-w" ]] && wait_flag=true
  done

  case "$cmd" in
    "")              show_menu ;;
    list|ls|menu)    show_menu ;;
    oom|1)           apply_oom ;;
    image|2)         apply_image ;;
    crashloop|3)     apply_crashloop ;;
    mount|4)         apply_failedmount ;;
    sched|5)         apply_failedsched ;;
    configmap|6)     apply_configmap ;;
    cascade|7)       apply_cascade ;;
    init|8)          apply_initfail ;;
    oos|9)           apply_oos ;;
    destructive|10)  apply_destructive ;;
    status)          show_status; exit 0 ;;
    cleanup)         cleanup_all; exit 0 ;;
    cleanup-oos)     cleanup_oos; exit 0 ;;
    -h|--help|help)
      cat <<EOF
Usage: $0 [scenario] [--wait]
  scenarios: oom, image, crashloop, mount, sched, configmap, cascade, init, oos, destructive
  also:      status, cleanup, cleanup-oos, list

Examples:
  $0                       # interactive menu
  $0 oom                   # apply OOM scenario
  $0 oom --wait            # apply + wait until investigation lands
  $0 status                # what's running, recent investigations
  $0 cleanup               # delete demo namespace + dismiss in-flight
EOF
      exit 0
      ;;
    *)               err "unknown command: $cmd. Try '$0 help'"; exit 1 ;;
  esac

  if $wait_flag; then
    wait_for_investigation
  fi
}

main "$@"
