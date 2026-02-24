#!/bin/bash
# Scale up AI platform workloads — restores from saved state
# Interactive: lets you choose which workloads to deploy

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
GITOPS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
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

# ============================================================
# Step 1: Interactive workload selection
# ============================================================
WORKLOAD_DIR="$GITOPS_DIR/workloads/examples"
WORKLOAD_FILES=()
SELECTED_FILES=()

if [ -d "$WORKLOAD_DIR" ] && ls "$WORKLOAD_DIR"/*.yaml &>/dev/null; then
  while IFS= read -r f; do
    WORKLOAD_FILES+=("$f")
  done < <(ls "$WORKLOAD_DIR"/*.yaml)
fi

if [ ${#WORKLOAD_FILES[@]} -gt 0 ]; then
  echo "========================================="
  echo "  Available Workloads"
  echo "========================================="
  echo ""
  for i in "${!WORKLOAD_FILES[@]}"; do
    f="${WORKLOAD_FILES[$i]}"
    name=$(basename "$f" .yaml)
    kind=$(grep "^kind:" "$f" | head -1 | awk '{print $2}')
    model=$(grep "model:" "$f" | head -1 | sed 's/.*model: *"\{0,1\}\([^"]*\)"\{0,1\}/\1/')
    gpu=$(grep "gpuCount:" "$f" | head -1 | awk '{print $2}')
    printf "  [%d] %-20s %-25s (model: %s, gpus: %s)\n" $((i+1)) "$name" "$kind" "$model" "${gpu:-1}"
  done
  echo ""
  echo "  [0] Skip — scale up infrastructure only"
  echo ""
  read -p "Select workloads (comma-separated, e.g. 1,2 or 'all'): " SELECTION

  if [ "$SELECTION" = "all" ]; then
    SELECTED_FILES=("${WORKLOAD_FILES[@]}")
  elif [ "$SELECTION" != "0" ]; then
    IFS=',' read -ra INDICES <<< "$SELECTION"
    for idx in "${INDICES[@]}"; do
      idx=$(echo "$idx" | tr -d ' ')
      if [ "$idx" -ge 1 ] && [ "$idx" -le ${#WORKLOAD_FILES[@]} ] 2>/dev/null; then
        SELECTED_FILES+=("${WORKLOAD_FILES[$((idx-1))]}")
      fi
    done
  fi

  echo ""
  if [ ${#SELECTED_FILES[@]} -gt 0 ]; then
    echo "Selected: $(for f in "${SELECTED_FILES[@]}"; do basename "$f" .yaml; done | tr '\n' ' ')"
  else
    echo "No workloads selected — scaling up infrastructure only."
  fi
  echo ""
else
  echo "No workload files found in $WORKLOAD_DIR"
  echo ""
fi

# ============================================================
# Step 2: Re-enable ArgoCD auto-sync
# ============================================================
SYNC_POLICY='{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true},"syncOptions":["CreateNamespace=true","ServerSideApply=true"]}}}'
echo "Re-enabling ArgoCD auto-sync..."
for app in litellm open-webui langfuse kuberay-operator workloads; do
  kubectl patch application "$app" -n argocd --type merge -p "$SYNC_POLICY" 2>/dev/null && echo "  ✓ $app" || true
done
echo ""

# ============================================================
# Step 3: Scale up infrastructure
# ============================================================
echo "=== Scaling up AI Platform ==="
echo ""

# Operators
echo "Scaling up KubeRay Operator..."
kubectl scale deploy -n kuberay --all --replicas=1
echo -n "  Waiting: "
kubectl wait --for=condition=available deploy -n kuberay -l app.kubernetes.io/name=kuberay-operator --timeout=120s 2>/dev/null && echo "✓ Ready" || echo "⏳ Still starting"

# Databases (must be up before apps)
echo "Scaling up databases..."
for sts in litellm-db langfuse-postgresql langfuse-redis-primary langfuse-zookeeper langfuse-clickhouse-shard0; do
  r=$(get_replicas "$sts" "$STS_STATE_FILE")
  kubectl scale statefulset "$sts" -n ai-platform --replicas="$r" 2>/dev/null || true
done
echo -n "  Waiting for PostgreSQL: "
kubectl wait --for=jsonpath='{.status.readyReplicas}'=1 statefulset/langfuse-postgresql -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"
echo -n "  Waiting for LiteLLM DB: "
kubectl wait --for=jsonpath='{.status.readyReplicas}'=1 statefulset/litellm-db -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"

# Platform deployments
echo "Scaling up platform apps..."
for deploy in langfuse-web langfuse-worker langfuse-s3 litellm open-webui; do
  r=$(get_replicas "$deploy" "$STATE_FILE")
  kubectl scale deploy "$deploy" -n ai-platform --replicas="$r" 2>/dev/null || true
done
echo "✓ Platform apps scaling up"

# ============================================================
# Step 3: Deploy selected workloads
# ============================================================
if [ ${#SELECTED_FILES[@]} -gt 0 ]; then
  echo ""
  echo "Deploying workloads..."
  for f in "${SELECTED_FILES[@]}"; do
    name=$(basename "$f" .yaml)
    kubectl apply -f "$f" 2>/dev/null && echo "  ✓ $name"
  done
fi

# ============================================================
# Step 4: Wait for readiness
# ============================================================
echo ""
echo "Waiting for services..."
echo -n "  LiteLLM: "
kubectl wait --for=condition=available deploy/litellm -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"
echo -n "  Langfuse: "
kubectl wait --for=condition=available deploy/langfuse-web -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"
echo -n "  OpenWebUI: "
kubectl wait --for=condition=available deploy/open-webui -n ai-platform --timeout=120s 2>/dev/null && echo "✓" || echo "⏳"

# ============================================================
# Step 5: Port-forwards
# ============================================================
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
echo "  LiteLLM:  http://localhost:4000"
echo "  OpenWebUI: http://localhost:8080"
echo "  Langfuse:  http://localhost:3000"
if [ ${#SELECTED_FILES[@]} -gt 0 ]; then
  echo ""
  echo "  Workloads deploying — GPU nodes provision in ~5-10 min."
fi
