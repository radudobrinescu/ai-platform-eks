#!/usr/bin/env bash
# Update the ALB inbound-cidrs annotation with your current public IP.
# Usage: ./ops/update-ip.sh
set -euo pipefail

NEW_IP=$(curl -4 -s ifconfig.me)
[ -z "$NEW_IP" ] && { echo "ERROR: Could not determine public IP"; exit 1; }
CIDR="${NEW_IP}/32"

echo "Current IP: $NEW_IP"

FILES=(
  platform/config/ingress.yaml
  platform/services/cluster-dashboard/manifests.yaml
)

REPO_ROOT=$(git -C "$(dirname "$0")" rev-parse --show-toplevel)

for f in "${FILES[@]}"; do
  filepath="$REPO_ROOT/$f"
  if grep -q "alb.ingress.kubernetes.io/inbound-cidrs" "$filepath" 2>/dev/null; then
    sed -i '' "s|alb.ingress.kubernetes.io/inbound-cidrs:.*|alb.ingress.kubernetes.io/inbound-cidrs: ${CIDR}|g" "$filepath"
    echo "  Updated $f"
  fi
done

echo ""
cd "$REPO_ROOT"
git add -A
git commit -m "chore: update ALB allowlist to ${CIDR}"
git push
echo ""
echo "→ Triggering ArgoCD sync..."
kubectl patch application platform-config -n argocd --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
kubectl patch application cluster-dashboard -n argocd --type merge \
  -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'
echo "Done. ALB will update within ~30s."
