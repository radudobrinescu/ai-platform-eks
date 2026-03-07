#!/bin/bash
# Scale up AI platform — restores from saved state
# ArgoCD handles workload deployment via auto-sync.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.scale-state-deploy.json"
STS_STATE_FILE="$SCRIPT_DIR/.scale-state-sts.json"

get_replicas() {
  local name=$1 file=$2 fallback=${3:-1}
  local r=""
  [ -f "$file" ] && r=$(jq -r --arg n "$name" '.[] | select(.name==$n) | .replicas' "$file" 2>/dev/null)
  r=${r:-$fallback}
  [ "$r" -eq 0 ] && r=$fallback
  echo "$r"
}

echo "=== Scaling up AI Platform ==="
echo ""

# Re-enable ArgoCD auto-sync
SYNC_POLICY='{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true},"syncOptions":["CreateNamespace=true","ServerSideApply=true"]}}}'
echo "Re-enabling ArgoCD auto-sync..."
for app in platform-config litellm open-webui langfuse kuberay-operator workloads; do
  kubectl patch application "$app" -n argocd --type merge -p "$SYNC_POLICY" 2>/dev/null && echo "  ✓ $app" || true
done
echo ""

# Operators
echo "Scaling up KubeRay Operator..."
kubectl scale deploy -n kuberay --all --replicas=1
echo -n "  Waiting: "
kubectl wait --for=condition=available deploy -n kuberay -l app.kubernetes.io/name=kuberay-operator --timeout=120s 2>/dev/null && echo "✓ Ready" || echo "⏳ Still starting"

# Databases first
echo "Scaling up databases..."
for sts in litellm-db langfuse-postgresql langfuse-redis-primary langfuse-zookeeper langfuse-clickhouse-shard0; do
  r=$(get_replicas "$sts" "$STS_STATE_FILE")
  kubectl scale statefulset "$sts" -n ai-platform --replicas="$r" 2>/dev/null || true
done
echo -n "  Waiting for PostgreSQL: "
kubectl wait --for=jsonpath='{.status.readyReplicas}'=1 statefulset/langfuse-postgresql -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"
echo -n "  Waiting for LiteLLM DB: "
kubectl wait --for=jsonpath='{.status.readyReplicas}'=1 statefulset/litellm-db -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"

# Platform apps
echo "Scaling up platform apps..."
for deploy in langfuse-web langfuse-worker langfuse-s3 litellm open-webui; do
  r=$(get_replicas "$deploy" "$STATE_FILE")
  kubectl scale deploy "$deploy" -n ai-platform --replicas="$r" 2>/dev/null || true
done
echo "✓ Platform apps scaling up"

# Wait for readiness
echo ""
echo "Waiting for services..."
echo -n "  LiteLLM: "
kubectl wait --for=condition=available deploy/litellm -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"
echo -n "  Langfuse: "
kubectl wait --for=condition=available deploy/langfuse-web -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"
echo -n "  OpenWebUI: "
kubectl wait --for=condition=available deploy/open-webui -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"

# Port-forwards
echo ""
echo "Starting port-forwards..."
pkill -f 'port-forward.*(litellm|langfuse|open-webui)' 2>/dev/null || true
sleep 1
kubectl port-forward -n ai-platform svc/litellm 4000:4000 &>/dev/null &
kubectl port-forward -n ai-platform svc/open-webui 8080:8080 &>/dev/null &
kubectl port-forward -n ai-platform svc/langfuse-web 3000:3000 &>/dev/null &

echo ""
echo "=== Scale up complete ==="
echo ""
echo "  LiteLLM:   http://localhost:4000"
echo "  OpenWebUI: http://localhost:8080"
echo "  Langfuse:  http://localhost:3000"
echo ""
echo "  ArgoCD will sync workloads automatically — GPU nodes provision in ~5-10 min."
