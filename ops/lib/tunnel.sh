#!/usr/bin/env bash
# Tunnel to Kubernetes services via SSM through an EKS node.
# Forwards directly to ClusterIPs, bypassing the ALB and its IP allowlist.
set -euo pipefail

# Pre-flight tool check — this script is runnable directly (not only via
# ./platformctl), so fail fast with a clear message instead of a cryptic
# "command not found" partway through.
for _tool in kubectl aws session-manager-plugin; do
  command -v "$_tool" >/dev/null 2>&1 || { echo "Error: missing required tool: ${_tool} (see README prerequisites)" >&2; exit 1; }
done

NAMESPACE="ai-platform"

CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' | awk -F/ '{print $NF}')
echo "→ Finding an EKS node in the criticaladdons node group (cluster: $CLUSTER_NAME)..."
INSTANCE_ID=$(aws ec2 describe-instances \
  --filters "Name=tag:eks:cluster-name,Values=$CLUSTER_NAME" \
            "Name=tag:eks:nodegroup-name,Values=*criticaladdons*" \
            "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].InstanceId" \
  --output text)
[ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ] && { echo "ERROR: No running EKS node found."; exit 1; }
echo "  Instance: $INSTANCE_ID"

echo ""
echo "→ Resolving Kubernetes service ClusterIPs..."
OPENWEBUI_IP=$(kubectl get svc open-webui -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
LITELLM_IP=$(kubectl get svc litellm -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
LANGFUSE_IP=$(kubectl get svc langfuse-web -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
# The dashboard's Approve/Dismiss mutations require the OIDC bearer token that
# oauth2-proxy injects (--set-authorization-header=true) before forwarding to
# cluster-dashboard:9090 — the same path the ALB ingress uses. Forwarding
# straight to the cluster-dashboard Service (port 9090) skips oauth2-proxy
# entirely: the read-only topology view still renders (no auth needed), but
# any mutation 401s with "missing bearer token" even though you're logged in,
# because the backend never receives the header. So we tunnel oauth2-proxy's
# port (4180) and map it to local :9090, keeping the same URL for the operator.
DASHBOARD_IP=$(kubectl get svc oauth2-proxy -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
DASHBOARD_REMOTE_PORT=$(kubectl get svc oauth2-proxy -n "$NAMESPACE" -o jsonpath='{.spec.ports[0].port}')
# ArgoCD lives in its own namespace. argocd-server serves TLS on 443, so this
# tunnel is HTTPS (self-signed cert — accept the browser warning).
ARGOCD_IP=$(kubectl get svc argocd-server -n argocd -o jsonpath='{.spec.clusterIP}')
echo "  open-webui:        $OPENWEBUI_IP:8080"
echo "  litellm:           $LITELLM_IP:4000"
echo "  langfuse-web:      $LANGFUSE_IP:3000"
echo "  cluster-dashboard: $DASHBOARD_IP:$DASHBOARD_REMOTE_PORT (via oauth2-proxy, so Approve/Dismiss are authenticated)"
echo "  argocd-server:     $ARGOCD_IP:443 (HTTPS)"

echo ""
echo "→ Starting SSM tunnels (via ClusterIP — bypasses ALB allowlist)..."
echo "  Open WebUI:  http://localhost:8080"
echo "  LiteLLM:     http://localhost:4000"
echo "  Langfuse:    http://localhost:3000"
echo "  Dashboard:   http://localhost:9090"
echo "  ArgoCD:      https://localhost:8090"
echo ""

# Each SSM port-forwarding session can go idle and exit on its own (default
# ~20min inactivity timeout). Worse, a session can also go stale WITHOUT exiting:
# the connection to SSM closes but session-manager-plugin keeps the local port
# bound, so browsers connect and then hang forever. The watchdog therefore
# probes each local port with a real request and restarts a forward that is
# dead OR stops answering, so a long demo keeps working without a manual restart.
# (Plain indexed arrays, not associative — this must run on bash 3.2, macOS's
# default /bin/bash, which has no `declare -A`.)
LOCAL_PORTS=(8080 4000 3000 9090 8090)
REMOTE_HOSTS=("$OPENWEBUI_IP" "$LITELLM_IP" "$LANGFUSE_IP" "$DASHBOARD_IP" "$ARGOCD_IP")
REMOTE_PORTS=(8080 4000 3000 "$DASHBOARD_REMOTE_PORT" 443)
SCHEMES=(http http http http https)
SSM_PIDS=(0 0 0 0 0)
FAILS=(0 0 0 0 0)

# Only one tunnel can own the local ports. A second one starts sessions that
# silently fail to bind, so stop any tunnel already running and take over.
# Match only processes that ARE this script ("bash .../ops/lib/tunnel.sh"), not
# anything that merely mentions it (an editor, a grep, a shell running a command).
is_tunnel_proc() {
  # shellcheck disable=SC2046  # split args on purpose
  set -- $(ps -o args= -p "$1" 2>/dev/null)
  [ "$#" -eq 2 ] || return 1
  case "$1" in bash|*/bash) ;; *) return 1 ;; esac
  case "$2" in */ops/lib/tunnel.sh|ops/lib/tunnel.sh) return 0 ;; esac
  return 1
}
for old in $(pgrep -f 'ops/lib/tunnel\.sh$' || true); do
  [ "$old" = "$$" ] && continue
  is_tunnel_proc "$old" || continue
  echo "→ Stopping the tunnel already running (pid $old)..."
  pkill -P "$old" 2>/dev/null || true
  kill "$old" 2>/dev/null || true
done
for port in "${LOCAL_PORTS[@]}"; do
  for pid in $(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true); do
    case "$(ps -o comm= -p "$pid" 2>/dev/null)" in
      *session-manager-plugin*) kill "$pid" 2>/dev/null || true ;;   # a leftover SSM forward on our port
      *) echo "ERROR: localhost:${port} is used by another program (pid ${pid}); stop it and re-run." >&2; exit 1 ;;
    esac
  done
done
sleep 1

start_forward() {
  local i="$1"
  aws ssm start-session --target "$INSTANCE_ID" \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters "{\"host\":[\"${REMOTE_HOSTS[$i]}\"],\"portNumber\":[\"${REMOTE_PORTS[$i]}\"],\"localPortNumber\":[\"${LOCAL_PORTS[$i]}\"]}" \
    >/dev/null 2>&1 &
  SSM_PIDS[$i]=$!
  FAILS[$i]=0
}

# Stop one forward: the aws CLI and its session-manager-plugin child (the
# process that actually holds the local port). A hung plugin can ignore
# SIGTERM, so whatever is still alive after 2 s gets SIGKILL.
stop_forward() {
  local pid="${SSM_PIDS[$1]}" kids
  [ "$pid" -gt 0 ] || return 0
  kids="$(pgrep -P "$pid" 2>/dev/null | tr '\n' ' ' || true)"
  # shellcheck disable=SC2086  # $kids is a space-separated pid list
  kill $kids "$pid" 2>/dev/null || true
  sleep 2
  # shellcheck disable=SC2086
  kill -KILL $kids "$pid" 2>/dev/null || true
}

# 0 if the local port answers an HTTP(S) request within 5 s (any status code).
port_answers() {
  local code
  code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "${SCHEMES[$1]}://localhost:${LOCAL_PORTS[$1]}/" 2>/dev/null || true)"
  [ -n "$code" ] && [ "$code" != "000" ]
}

for i in 0 1 2 3 4; do start_forward "$i"; done

cleanup() { for i in 0 1 2 3 4; do stop_forward "$i"; done; exit; }
trap cleanup INT TERM

while true; do
  sleep 30
  for i in 0 1 2 3 4; do
    if ! kill -0 "${SSM_PIDS[$i]}" 2>/dev/null; then
      echo "→ tunnel :${LOCAL_PORTS[$i]} died, restarting..." >&2
      start_forward "$i"
    elif port_answers "$i"; then
      FAILS[$i]=0
    else
      FAILS[$i]=$(( ${FAILS[$i]} + 1 ))
      if [ "${FAILS[$i]}" -ge 2 ]; then   # 2 misses in a row (~1 min), not one slow reply
        echo "→ tunnel :${LOCAL_PORTS[$i]} stopped answering, restarting..." >&2
        stop_forward "$i"
        start_forward "$i"
      fi
    fi
  done
done
