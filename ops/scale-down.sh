#!/bin/bash
# Scale down AI platform workloads for off-hours / cost savings
# Preserves replica counts for scale-up restoration
# Core components (Karpenter, CoreDNS, CNI) remain running.
# GPU nodes are reclaimed by Karpenter after workloads are removed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.scale-state-deploy.json"
STS_STATE_FILE="$SCRIPT_DIR/.scale-state-sts.json"

echo "=== Scaling down AI Platform ==="
echo ""

# Save current replica counts
echo "Saving current state..."
kubectl get deploy -n ai-platform -o json | jq '[.items[] | select(.spec.replicas > 0) | {name: .metadata.name, replicas: .spec.replicas}]' > "${STATE_FILE}.new"
[ "$(jq 'length' "${STATE_FILE}.new")" -gt 0 ] && mv "${STATE_FILE}.new" "$STATE_FILE" || rm -f "${STATE_FILE}.new"

kubectl get statefulset -n ai-platform -o json | jq '[.items[] | select(.spec.replicas > 0) | {name: .metadata.name, replicas: .spec.replicas}]' > "${STS_STATE_FILE}.new"
[ "$(jq 'length' "${STS_STATE_FILE}.new")" -gt 0 ] && mv "${STS_STATE_FILE}.new" "$STS_STATE_FILE" || rm -f "${STS_STATE_FILE}.new"
echo "✓ State saved"

# Stop port-forwards
pkill -f 'port-forward.*(litellm|langfuse|open-webui|head-svc)' 2>/dev/null || true
echo "✓ Port-forwards stopped"

# Suspend ArgoCD auto-sync to prevent self-healing during scale-down
echo "Suspending ArgoCD auto-sync..."
for app in litellm open-webui langfuse kuberay-operator workloads; do
  kubectl patch application "$app" -n argocd --type merge \
    -p '{"spec":{"syncPolicy":null}}' 2>/dev/null && echo "  ✓ $app" || true
done
echo "✓ Auto-sync suspended"

# Delete InferenceEndpoints (removes RayServices → GPU workers → Karpenter reclaims GPU nodes)
echo "Removing InferenceEndpoints..."
kubectl delete inferenceendpoints --all -n inference --ignore-not-found
echo "✓ InferenceEndpoints removed (GPU nodes will be reclaimed)"

# Scale down platform apps
echo "Scaling down platform apps..."
kubectl scale deploy -n ai-platform --all --replicas=0
kubectl scale statefulset -n ai-platform --all --replicas=0
echo "✓ Platform apps scaled to 0"

# Scale down operators
echo "Scaling down operators..."
kubectl scale deploy -n kuberay --all --replicas=0
# GPU operator stays running (DaemonSets, can't scale to 0 meaningfully)
echo "✓ Operators scaled down"

echo ""
echo "=== Scale down complete ==="
echo "Karpenter will consolidate empty nodes within 5 minutes."
echo "To restore: ./scale-up.sh"
