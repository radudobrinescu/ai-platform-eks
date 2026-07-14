#!/bin/bash
# Scale down AI platform for off-hours / cost savings.
# Suspends ArgoCD auto-sync, deletes workloads, scales platform to zero.
# GPU nodes are reclaimed by Karpenter after workloads are removed.
# Core components (Karpenter, CoreDNS, CNI) remain running.

set -euo pipefail

echo "=== Scaling down AI Platform ==="
echo ""

# Stop port-forwards
pkill -f 'port-forward.*(litellm|langfuse|open-webui|head-svc)' 2>/dev/null || true
pkill -f 'ssm.*StartPortForwardingSession' 2>/dev/null || true
echo "✓ Port-forwards stopped"

# Suspend ArgoCD auto-sync on all generated Applications.
# Skip 'teams' (keep team configs live) and 'bootstrap' (managed by Terraform).
echo "Suspending ArgoCD auto-sync..."
for app in $(kubectl get applications -n argocd -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  [[ "$app" == "teams" || "$app" == "bootstrap" ]] && continue
  kubectl patch application "$app" -n argocd --type merge \
    -p '{"spec":{"syncPolicy":null}}' 2>/dev/null && echo "  ✓ $app" || true
done
echo "✓ Auto-sync suspended"

# Delete all serving endpoints (vLLM + llm-d scale/disaggregation), across all
# namespaces (models can live in per-team `team-*` namespaces). Removes GPU
# workers (and, for llm-d, the router Applications via KRO GC) so Karpenter
# reclaims the GPU nodes.
echo "Removing serving endpoints (vllm / llm-d / disagg)..."
kubectl delete vllmendpoints,llmdendpoints,llmddisaggendpoints --all -A --ignore-not-found
echo "✓ Serving endpoints removed (GPU nodes will be reclaimed)"

# Scale down everything in ai-platform namespace
echo "Scaling down platform..."
kubectl scale deploy -n ai-platform --all --replicas=0 2>/dev/null || true
kubectl scale statefulset -n ai-platform --all --replicas=0 2>/dev/null || true
kubectl scale deploy -n kuberay --all --replicas=0 2>/dev/null || true
echo "✓ Platform scaled to 0"

echo ""
echo "=== Scale down complete ==="
echo "Karpenter will consolidate empty nodes within 5 minutes."
echo "S3 model cache (s3://{cluster}-model-cache) is preserved — previously"
echo "deployed models will be fast on next scale-up."
echo "To restore: ./scale-up.sh"
