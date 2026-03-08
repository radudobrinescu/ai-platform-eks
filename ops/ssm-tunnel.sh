#!/usr/bin/env bash
# Tunnel to the internal ALB via SSM through an EKS node.
# Creates 3 port forwards: Open WebUI (8080), LiteLLM (4000), Langfuse (3000)
set -euo pipefail

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

echo ""
echo "→ Starting SSM tunnels..."
echo "  Open WebUI:  http://localhost:8080"
echo "  LiteLLM:     http://localhost:4000"
echo "  Langfuse:    http://localhost:3000"
echo ""

# Start first two tunnels in background
aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$ALB_HOST\"],\"portNumber\":[\"8080\"],\"localPortNumber\":[\"8080\"]}" &

aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$ALB_HOST\"],\"portNumber\":[\"4000\"],\"localPortNumber\":[\"4000\"]}" &

aws ssm start-session --target "$INSTANCE_ID" \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters "{\"host\":[\"$ALB_HOST\"],\"portNumber\":[\"3000\"],\"localPortNumber\":[\"3000\"]}" &

# Wait for all background tunnels; clean up on Ctrl+C
trap 'kill $(jobs -p) 2>/dev/null; exit' INT TERM
wait
