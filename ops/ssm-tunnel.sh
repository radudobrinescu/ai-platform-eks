#!/usr/bin/env bash
# Tunnel to Kubernetes services via SSM through an EKS node.
# Forwards directly to ClusterIPs, bypassing the ALB and its IP allowlist.
set -euo pipefail

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
DASHBOARD_IP=$(kubectl get svc cluster-dashboard -n "$NAMESPACE" -o jsonpath='{.spec.clusterIP}')
echo "  open-webui:        $OPENWEBUI_IP:8080"
echo "  litellm:           $LITELLM_IP:4000"
echo "  langfuse-web:      $LANGFUSE_IP:3000"
echo "  cluster-dashboard: $DASHBOARD_IP:9090"

echo ""
echo "→ Starting SSM tunnels (via ClusterIP — bypasses ALB allowlist)..."
echo "  Open WebUI:  http://localhost:8080"
echo "  LiteLLM:     http://localhost:4000"
echo "  Langfuse:    http://localhost:3000"
echo "  Dashboard:   http://localhost:9090"
echo ""

aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$OPENWEBUI_IP\"],\"portNumber\":[\"8080\"],\"localPortNumber\":[\"8080\"]}" &

aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$LITELLM_IP\"],\"portNumber\":[\"4000\"],\"localPortNumber\":[\"4000\"]}" &

aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$LANGFUSE_IP\"],\"portNumber\":[\"3000\"],\"localPortNumber\":[\"3000\"]}" &

aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$DASHBOARD_IP\"],\"portNumber\":[\"9090\"],\"localPortNumber\":[\"9090\"]}" &

trap 'kill $(jobs -p) 2>/dev/null; exit' INT TERM
wait
