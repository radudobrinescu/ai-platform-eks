#!/bin/bash
# Scale up AI platform — re-enables ArgoCD auto-sync.
# ArgoCD reconciles all resources to their desired state automatically.

set -euo pipefail

SYNC_POLICY='{"spec":{"syncPolicy":{"automated":{"prune":true,"selfHeal":true},"syncOptions":["CreateNamespace=true","ServerSideApply=true"]}}}'

echo "=== Scaling up AI Platform ==="
echo ""

# Re-enable auto-sync on all generated Applications.
# Skip 'bootstrap' (managed by Terraform, has its own syncPolicy).
echo "Re-enabling ArgoCD auto-sync..."
for app in $(kubectl get applications -n argocd -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  [[ "$app" == "bootstrap" ]] && continue
  kubectl patch application "$app" -n argocd --type merge -p "$SYNC_POLICY" 2>/dev/null && echo "  ✓ $app" || true
done
echo ""

# Wait for key services to come up
echo "Waiting for ArgoCD to reconcile..."
echo -n "  platform-db: "
kubectl wait --for=jsonpath='{.status.readyReplicas}'=1 statefulset/platform-db -n ai-platform --timeout=180s 2>/dev/null && echo "✓" || echo "⏳"
echo -n "  LiteLLM: "
kubectl wait --for=condition=available deploy/litellm -n ai-platform --timeout=180s 2>/dev/null && echo "✓" || echo "⏳"
echo -n "  Open WebUI: "
kubectl wait --for=condition=available deploy/open-webui -n ai-platform --timeout=180s 2>/dev/null && echo "✓" || echo "⏳"

echo ""
echo "=== Scale up complete ==="
echo ""
echo "  Access services:  ./ops/ssm-tunnel.sh"
echo "  ArgoCD will sync models automatically — GPU nodes provision in ~5-10 min."
