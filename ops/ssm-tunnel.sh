#!/usr/bin/env bash
# Tunnel to the internal ALB via SSM through an EKS node.
# Usage: ./ops/ssm-tunnel.sh [local_port]
set -euo pipefail

LOCAL_PORT="${1:-8080}"
NAMESPACE="ai-platform"
INGRESS_NAME="ai-platform-litellm"

echo "→ Getting internal ALB hostname..."
ALB_HOST=$(kubectl get ingress "$INGRESS_NAME" -n "$NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
[ -z "$ALB_HOST" ] && { echo "ERROR: Could not find ALB hostname. Is the ingress provisioned?"; exit 1; }
echo "  ALB: $ALB_HOST"

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

echo "→ Starting SSM tunnel on localhost:$LOCAL_PORT → $ALB_HOST:80"
echo "  Open WebUI:  http://localhost:$LOCAL_PORT/"
echo "  LiteLLM API: http://localhost:$LOCAL_PORT/v1/chat/completions"
echo "  Langfuse:    http://localhost:$LOCAL_PORT/observe"
echo ""

aws ssm start-session \
  --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$ALB_HOST\"],\"portNumber\":[\"80\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}"
