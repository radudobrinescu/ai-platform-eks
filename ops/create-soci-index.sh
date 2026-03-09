#!/usr/bin/env bash
# Build and push a SOCI index for an ECR image using a temporary EC2 instance.
# Usage: ./ops/create-soci-index.sh <ecr-image-uri>
# Example: ./ops/create-soci-index.sh 802019299867.dkr.ecr.eu-central-1.amazonaws.com/docker-hub/anyscale/ray-llm:2.54.0-py311-cu128
set -euo pipefail

IMAGE="${1:?Usage: $0 <ecr-image-uri>}"
REGION=$(echo "$IMAGE" | grep -oE '[a-z]+-[a-z]+-[0-9]+')
ACCOUNT=$(echo "$IMAGE" | grep -oE '^[0-9]+')
CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' | awk -F/ '{print $NF}')

# Reuse the Karpenter node role (has ECR access + SSM)
INSTANCE_PROFILE=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:eks:cluster-name,Values=$CLUSTER_NAME" "Name=instance-state-name,Values=running" \
  --query "Reservations[0].Instances[0].IamInstanceProfile.Arn" --output text | awk -F/ '{print $NF}')
SUBNET=$(kubectl get node -l role=system -o jsonpath='{.items[0].metadata.labels.topology\.kubernetes\.io/zone}' | \
  xargs -I{} aws ec2 describe-subnets --region "$REGION" \
    --filters "Name=tag:Name,Values=*${CLUSTER_NAME}*private*" "Name=availability-zone,Values={}" \
    --query "Subnets[0].SubnetId" --output text)
SG=$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query "cluster.resourcesVpcConfig.clusterSecurityGroupId" --output text)

echo "→ Launching temporary AL2023 instance for SOCI index build..."
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --instance-type m5.large \
  --subnet-id "$SUBNET" \
  --security-group-ids "$SG" \
  --iam-instance-profile Name="$INSTANCE_PROFILE" \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":100,"VolumeType":"gp3","Encrypted":true}}]' \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=soci-index-builder},{Key=Purpose,Value=temporary}]" \
  --query "Instances[0].InstanceId" --output text)
echo "  Instance: $INSTANCE_ID"

cleanup() {
  echo "→ Terminating temporary instance $INSTANCE_ID..."
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION" > /dev/null
  echo "  Done."
}
trap cleanup EXIT

echo "→ Waiting for instance to be ready..."
aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID" --region "$REGION"

echo "→ Installing tools and building SOCI index..."
CMD_ID=$(aws ssm send-command --instance-ids "$INSTANCE_ID" --region "$REGION" \
  --document-name "AWS-RunShellScript" \
  --timeout-seconds 1800 \
  --parameters "commands=[
    \"set -ex\",
    \"yum install -y nerdctl soci-snapshotter\",
    \"aws ecr get-login-password --region $REGION | nerdctl login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com\",
    \"nerdctl pull --platform linux/amd64 $IMAGE\",
    \"soci create $IMAGE\",
    \"soci push $IMAGE\",
    \"echo SOCI_INDEX_COMPLETE\"
  ]" \
  --query "Command.CommandId" --output text)

while true; do
  sleep 15
  STATUS=$(aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" --region "$REGION" \
    --query "Status" --output text 2>/dev/null || echo "Pending")
  case "$STATUS" in
    Success)
      echo "✓ SOCI index created and pushed for $IMAGE"
      exit 0 ;;
    Failed|TimedOut)
      echo "✗ Failed. Output:"
      aws ssm get-command-invocation --command-id "$CMD_ID" --instance-id "$INSTANCE_ID" --region "$REGION" \
        --query "StandardErrorContent" --output text
      exit 1 ;;
    *)
      echo "  $(date +%H:%M:%S) $STATUS..." ;;
  esac
done
