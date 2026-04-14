#!/bin/bash
# Scale down AI platform for off-hours / cost savings.
# Suspends ArgoCD auto-sync, deletes workloads, scales platform to zero.
# GPU nodes are reclaimed by Karpenter after workloads are removed.
# Core components (Karpenter, CoreDNS, CNI) remain running.

set -euo pipefail

ARGOCD_APPS=(platform models)

echo "=== Scaling down AI Platform ==="
echo ""

# Stop port-forwards
pkill -f 'port-forward.*(litellm|langfuse|open-webui|head-svc)' 2>/dev/null || true
pkill -f 'ssm.*StartPortForwardingSession' 2>/dev/null || true
echo "✓ Port-forwards stopped"

# Suspend ArgoCD auto-sync on all managed apps
echo "Suspending ArgoCD auto-sync..."
for app in "${ARGOCD_APPS[@]}"; do
  kubectl patch application "$app" -n argocd --type merge \
    -p '{"spec":{"syncPolicy":null}}' 2>/dev/null && echo "  ✓ $app" || true
done
# Also suspend child apps managed by the platform App-of-Apps
for app in $(kubectl get applications -n argocd -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  [[ "$app" == "teams" ]] && continue  # skip teams if not in ARGOCD_APPS
  kubectl patch application "$app" -n argocd --type merge \
    -p '{"spec":{"syncPolicy":null}}' 2>/dev/null || true
done
echo "✓ Auto-sync suspended"

# Delete InferenceEndpoints (removes RayServices → GPU workers → Karpenter reclaims nodes)
echo "Removing InferenceEndpoints..."
kubectl delete inferenceendpoints --all -n inference --ignore-not-found
echo "✓ InferenceEndpoints removed (GPU nodes will be reclaimed)"

# Scale down everything in ai-platform namespace
echo "Scaling down platform..."
kubectl scale deploy -n ai-platform --all --replicas=0 2>/dev/null || true
kubectl scale statefulset -n ai-platform --all --replicas=0 2>/dev/null || true
kubectl scale deploy -n kuberay --all --replicas=0 2>/dev/null || true
echo "✓ Platform scaled to 0"

echo ""
echo "=== Scale down complete ==="
echo "Karpenter will consolidate empty nodes within 5 minutes."
echo "To restore: ./scale-up.sh"
