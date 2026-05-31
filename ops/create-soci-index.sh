#!/usr/bin/env bash
# Build and push a SOCI index for an ECR image using a temporary EC2 instance.
# Usage: ./ops/create-soci-index.sh [-p <instance-profile>] [-n <cluster>] [-r <region>] <ecr-image-uri>
# Example: ./ops/create-soci-index.sh <account-id>.dkr.ecr.<region>.amazonaws.com/docker-hub/anyscale/ray-llm:2.54.0-py311-cu128
#
# The temp instance must run under an instance profile that can BOTH pull and
# PUSH to ECR (soci push uploads the index as a referrer artifact). The EKS
# node role only has ECR *read*, so `soci push` 403s. Pass -p with a push-capable
# profile (Terraform provisions `<cluster>-soci-builder` for this); without -p we
# fall back to the first running node's profile (read-only — push will fail
# unless that role was granted ECR write).
#
# Networking + cluster name resolve HERMETICALLY from AWS APIs (no kubectl): the
# subnet is found by the `karpenter.sh/discovery=<cluster>` tag, matching
# create-data-volume-snapshot.sh. This avoids racing the host kubeconfig during a
# `terraform apply` on a brand-new cluster (where the host context may still
# point elsewhere). Pass -n/-r explicitly from Terraform; both fall back to the
# kubeconfig / AWS_REGION / the image URI when omitted (manual ad-hoc use).
set -euo pipefail

INSTANCE_PROFILE=""
CLUSTER_NAME=""
REGION=""
while getopts "p:n:r:h" opt; do
  case "$opt" in
    p) INSTANCE_PROFILE="$OPTARG" ;;
    n) CLUSTER_NAME="$OPTARG" ;;
    r) REGION="$OPTARG" ;;
    h) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option" >&2; exit 1 ;;
  esac
done
shift $((OPTIND - 1))

IMAGE="${1:?Usage: $0 [-p <instance-profile>] [-n <cluster>] [-r <region>] <ecr-image-uri>}"
# REGION precedence: -r flag > AWS_REGION env > parsed from the ECR image URI.
REGION="${REGION:-${AWS_REGION:-$(echo "$IMAGE" | grep -oE '[a-z]+-[a-z]+-[0-9]+')}}"
ACCOUNT=$(echo "$IMAGE" | grep -oE '^[0-9]+')

# Self-protect: if the target image isn't in ECR yet, there's nothing to index.
# This makes the script safe to call from Terraform on the no-Docker path — the
# Unsloth build is skipped (no image pushed), so we must NOT launch a builder and
# fail trying to pull a tag that doesn't exist. Exit 0 (success, no-op) instead.
# Only applies to ECR repo URIs (<acct>.dkr.ecr.<region>...); skip the check for
# any other registry.
if echo "$IMAGE" | grep -qE '\.dkr\.ecr\.'; then
  REPO_NAME="${IMAGE#*/}"; REPO_NAME="${REPO_NAME%%:*}"
  IMG_TAG="${IMAGE##*:}"
  if ! aws ecr describe-images --region "$REGION" \
        --repository-name "$REPO_NAME" --image-ids "imageTag=${IMG_TAG}" \
        >/dev/null 2>&1; then
    echo "⚠ Image not found in ECR: ${IMAGE} — skipping SOCI index (nothing to index)." >&2
    echo "  (Build/push the image first, then re-run; e.g. fine-tuning's Unsloth image" >&2
    echo "   is only present after a Docker-enabled apply.)" >&2
    exit 0
  fi
fi

# Cluster name: -n flag wins; otherwise fall back to the host kubeconfig (ad-hoc
# use). From Terraform we always pass -n so we never read the host context.
if [ -z "$CLUSTER_NAME" ]; then
  CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' 2>/dev/null | awk -F/ '{print $NF}')
fi
if [ -z "$CLUSTER_NAME" ]; then
  echo "Error: cannot determine cluster name. Pass -n <cluster>." >&2
  exit 1
fi

# Default to the running node's instance profile only if -p wasn't supplied.
# NOTE: the node profile has ECR read but NOT push — prefer -p <push-capable>.
if [ -z "$INSTANCE_PROFILE" ]; then
  INSTANCE_PROFILE=$(aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:eks:cluster-name,Values=$CLUSTER_NAME" "Name=instance-state-name,Values=running" \
    --query "Reservations[0].Instances[0].IamInstanceProfile.Arn" --output text | awk -F/ '{print $NF}')
  echo "⚠ No -p given; using node profile '$INSTANCE_PROFILE' (ECR read-only — 'soci push' may 403)." >&2
fi

# Subnet: resolved hermetically via the Karpenter discovery tag (no kubectl), the
# same way create-data-volume-snapshot.sh does — robust on a fresh-cluster apply.
SUBNET=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=tag:karpenter.sh/discovery,Values=$CLUSTER_NAME" \
  --query "Subnets[0].SubnetId" --output text)
if [ -z "$SUBNET" ] || [ "$SUBNET" = "None" ]; then
  echo "Error: no subnet found with tag karpenter.sh/discovery=$CLUSTER_NAME in $REGION." >&2
  exit 1
fi

SG=$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query "cluster.resourcesVpcConfig.clusterSecurityGroupId" --output text)

echo "→ Launching temporary AL2023 instance for SOCI index build..."
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --instance-type m5.large \
  --subnet-id "$SUBNET" \
  --security-group-ids "$SG" \
  --iam-instance-profile Name="$INSTANCE_PROFILE" \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":200,"VolumeType":"gp3","Encrypted":true}}]' \
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
    \"yum install -y containerd nerdctl soci-snapshotter\",
    \"systemctl start containerd\",
    \"aws ecr get-login-password --region $REGION | nerdctl login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com\",
    \"nerdctl pull --platform linux/amd64 $IMAGE\",
    \"export TMPDIR=/var/tmp\",
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
