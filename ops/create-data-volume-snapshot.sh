#!/usr/bin/env bash
# Create an EBS snapshot of a Bottlerocket data volume with pre-pulled container images.
#
# The snapshot can be referenced in Karpenter EC2NodeClass blockDeviceMappings
# (snapshotID field) so new GPU nodes boot with images already on disk —
# eliminating the multi-minute pull for large images like Ray LLM (~13 GiB).
#
# Approach: launches a temporary Bottlerocket instance that joins the EKS cluster
# with a NoSchedule taint. A short-lived pod scheduled ON that node (via nodeName)
# forces kubelet to pull the target image into containerd's content store on
# /dev/xvdb. Once pulled, the instance is stopped and /dev/xvdb is snapshotted.
#
# Usage:
#   ./ops/create-data-volume-snapshot.sh [options] <image1> [image2] ...
#
# Options:
#   -r REGION           AWS region (default: from AWS_REGION or cluster context)
#   -n CLUSTER_NAME     EKS cluster name (default: from current kubectl context)
#   -i INSTANCE_TYPE    Builder instance type (default: c5.xlarge for fast network)
#   -s VOLUME_SIZE      Data volume size in GiB (default: 200)
#   --fsr AZ1[,AZ2]    Enable Fast Snapshot Restore in specified AZs
#   --write-tfvars      Write snapshot ID to terraform/30.eks/30.cluster/snapshot.auto.tfvars
#
# Examples:
#   # Using ECR pull-through cache URI:
#   ./ops/create-data-volume-snapshot.sh <account-id>.dkr.ecr.<region>.amazonaws.com/docker-hub/anyscale/ray-llm:2.54.0-py311-cu128
#
#   # Short form (auto-prefixes with account ECR pull-through cache):
#   ./ops/create-data-volume-snapshot.sh anyscale/ray-llm:2.54.0-py311-cu128
#
#   # With FSR and auto-tfvars:
#   ./ops/create-data-volume-snapshot.sh --fsr eu-central-1a,eu-central-1b --write-tfvars anyscale/ray-llm:2.54.0-py311-cu128
#
# Output: prints snapshot ID (snap-xxx) as the last line on success.
set -euo pipefail

INSTANCE_TYPE="c5.xlarge"
VOLUME_SIZE=200
FSR_AZS=""
WRITE_TFVARS=false
REGION=""
CLUSTER_NAME=""
IMAGES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -r) REGION="$2"; shift 2 ;;
    -n) CLUSTER_NAME="$2"; shift 2 ;;
    -i) INSTANCE_TYPE="$2"; shift 2 ;;
    -s) VOLUME_SIZE="$2"; shift 2 ;;
    --fsr) FSR_AZS="$2"; shift 2 ;;
    --write-tfvars) WRITE_TFVARS=true; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed 's/^# \?//'; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; exit 1 ;;
    *) IMAGES+=("$1"); shift ;;
  esac
done

if [[ ${#IMAGES[@]} -eq 0 ]]; then
  echo "Error: at least one container image URI required." >&2
  echo "Run with --help for usage." >&2
  exit 1
fi

# --- Input validation ---------------------------------------------------------
if ! [[ "$VOLUME_SIZE" =~ ^[0-9]+$ ]] || [[ "$VOLUME_SIZE" -lt 50 || "$VOLUME_SIZE" -gt 16384 ]]; then
  echo "Error: volume size must be an integer between 50-16384 GiB" >&2
  exit 1
fi

# Validate image URIs contain only expected characters (prevent injection via YAML/JSON embedding)
IMAGE_PATTERN='^[a-zA-Z0-9._:/@-]+$'
for img in "${IMAGES[@]}"; do
  if [[ ! "$img" =~ $IMAGE_PATTERN ]]; then
    echo "Error: invalid characters in image URI: $img" >&2
    echo "  Image URIs must match: $IMAGE_PATTERN" >&2
    exit 1
  fi
done

# --- Resolve cluster context --------------------------------------------------
if [[ -z "$CLUSTER_NAME" ]]; then
  CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' 2>/dev/null | awk -F/ '{print $NF}')
fi
if [[ -z "$REGION" ]]; then
  REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
fi
if [[ -z "$REGION" ]]; then
  echo "Error: cannot determine region. Pass -r or set AWS_REGION." >&2
  exit 1
fi

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if [[ -z "$ACCOUNT" || "$ACCOUNT" == "None" ]]; then
  echo "Error: failed to get AWS account ID. Check credentials and IAM permissions." >&2
  exit 1
fi

# Expand short image references to full ECR pull-through cache URIs
FULL_IMAGES=()
for img in "${IMAGES[@]}"; do
  if [[ "$img" != *".dkr.ecr."* ]]; then
    img="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/docker-hub/${img}"
  fi
  FULL_IMAGES+=("$img")
done

echo "╭──────────────────────────────────────────────────────────────────╮"
echo "│  Bottlerocket Data Volume Snapshot Builder                       │"
echo "╰──────────────────────────────────────────────────────────────────╯"
echo "  Region:   $REGION"
echo "  Cluster:  $CLUSTER_NAME"
echo "  Instance: $INSTANCE_TYPE"
echo "  Volume:   ${VOLUME_SIZE} GiB"
echo "  Images:"
for img in "${FULL_IMAGES[@]}"; do
  echo "    - $img"
done
echo ""

# --- Resolve networking -------------------------------------------------------
echo "→ Resolving cluster networking..."
SUBNET=$(aws ec2 describe-subnets --region "$REGION" \
  --filters "Name=tag:karpenter.sh/discovery,Values=$CLUSTER_NAME" \
  --query "Subnets[0].SubnetId" --output text)
if [[ -z "$SUBNET" || "$SUBNET" == "None" ]]; then
  echo "Error: no subnet found with tag karpenter.sh/discovery=$CLUSTER_NAME in $REGION" >&2
  exit 1
fi

SG=$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query "cluster.resourcesVpcConfig.clusterSecurityGroupId" --output text)
if [[ -z "$SG" || "$SG" == "None" ]]; then
  echo "Error: cannot resolve cluster security group for $CLUSTER_NAME" >&2
  exit 1
fi

K8S_VERSION=$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query "cluster.version" --output text)

# Resolve the Karpenter node role's instance profile
ROLE_NAME="KarpenterNode-${CLUSTER_NAME}"
INSTANCE_PROFILE=$(aws iam list-instance-profiles-for-role \
  --role-name "$ROLE_NAME" --region "$REGION" \
  --query "InstanceProfiles[0].InstanceProfileName" --output text 2>/dev/null || true)

if [[ -z "$INSTANCE_PROFILE" || "$INSTANCE_PROFILE" == "None" ]]; then
  echo "Error: cannot find instance profile for role $ROLE_NAME" >&2
  exit 1
fi

echo "  Subnet:     $SUBNET"
echo "  SG:         $SG"
echo "  K8s:        $K8S_VERSION"
echo "  Profile:    $INSTANCE_PROFILE"

# --- Resolve Bottlerocket AMI -------------------------------------------------
# Use the standard (non-NVIDIA) Bottlerocket AMI for the builder. The data volume
# layout (/dev/xvdb containerd content store) is identical between NVIDIA and
# non-NVIDIA variants — only /dev/xvda differs (kernel modules). Since we only
# snapshot /dev/xvdb, a cheaper non-GPU instance works fine.
echo "→ Resolving Bottlerocket AMI..."
BR_AMI=$(aws ssm get-parameter --region "$REGION" \
  --name "/aws/service/bottlerocket/aws-k8s-${K8S_VERSION}/x86_64/latest/image_id" \
  --query "Parameter.Value" --output text)
echo "  AMI: $BR_AMI (bottlerocket aws-k8s-${K8S_VERSION})"

# --- Build Bottlerocket userdata (TOML) ---------------------------------------
API_SERVER=$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query 'cluster.endpoint' --output text)
CA_DATA=$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query 'cluster.certificateAuthority.data' --output text)

# Write userdata to a temp file — AWS CLI v2 auto-base64-encodes --user-data,
# so we pass raw TOML. Using a file avoids shell quoting issues with the
# multi-line certificate data. Cleaned up by the EXIT trap.
USERDATA_FILE=$(mktemp)
cat > "$USERDATA_FILE" <<TOML
[settings.kubernetes]
cluster-name = "${CLUSTER_NAME}"
api-server = "${API_SERVER}"
cluster-certificate = "${CA_DATA}"

[settings.kubernetes.node-labels]
"node.kubernetes.io/purpose" = "snapshot-builder"

[settings.kubernetes.node-taints]
"snapshot-builder" = "true:NoExecute"

[settings.container-runtime]
snapshotter = "soci"
TOML

# --- Launch builder instance --------------------------------------------------
echo "→ Launching builder instance..."
INSTANCE_ID=$(aws ec2 run-instances --region "$REGION" \
  --image-id "$BR_AMI" \
  --instance-type "$INSTANCE_TYPE" \
  --subnet-id "$SUBNET" \
  --security-group-ids "$SG" \
  --iam-instance-profile Name="$INSTANCE_PROFILE" \
  --user-data "file://${USERDATA_FILE}" \
  --block-device-mappings "[
    {\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":4,\"VolumeType\":\"gp3\",\"Encrypted\":true}},
    {\"DeviceName\":\"/dev/xvdb\",\"Ebs\":{\"VolumeSize\":${VOLUME_SIZE},\"VolumeType\":\"gp3\",\"Iops\":6000,\"Throughput\":500,\"Encrypted\":true}}
  ]" \
  --tag-specifications "ResourceType=instance,Tags=[
    {Key=Name,Value=${CLUSTER_NAME}-snapshot-builder},
    {Key=Purpose,Value=data-volume-snapshot}
  ]" \
  --query "Instances[0].InstanceId" --output text)

echo "  Instance: $INSTANCE_ID"

cleanup() {
  echo ""
  echo "→ Cleaning up..."
  rm -f "$USERDATA_FILE" 2>/dev/null || true
  kubectl delete pods -n kube-system -l app=snapshot-puller --ignore-not-found 2>/dev/null || true
  NODE_TO_DELETE=$(kubectl get nodes -l node.kubernetes.io/purpose=snapshot-builder \
    --no-headers -o custom-columns=':metadata.name' 2>/dev/null | head -1)
  if [[ -n "$NODE_TO_DELETE" ]]; then
    kubectl delete node "$NODE_TO_DELETE" --ignore-not-found 2>/dev/null || true
  fi
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$REGION" > /dev/null 2>&1 || true
  echo "  Builder instance terminated."
}
trap cleanup EXIT

# --- Wait for node to join cluster --------------------------------------------
echo "→ Waiting for builder node to join the cluster..."
NODE_NAME=""
for attempt in $(seq 1 72); do
  NODE_NAME=$(kubectl get nodes -l node.kubernetes.io/purpose=snapshot-builder \
    --no-headers -o custom-columns=':metadata.name' 2>/dev/null | head -1)
  if [[ -n "$NODE_NAME" ]]; then
    break
  fi
  sleep 5
done

if [[ -z "$NODE_NAME" ]]; then
  echo "Error: builder node did not join within 6 minutes." >&2
  echo "  Debug: aws ec2 get-console-output --instance-id $INSTANCE_ID --region $REGION" >&2
  exit 1
fi
echo "  Node: $NODE_NAME"

kubectl wait --for=condition=Ready "node/$NODE_NAME" --timeout=120s 2>/dev/null
echo "  ✓ Node ready"

# --- Pull images by scheduling pods on the builder node -----------------------
# kubelet pulls images into containerd's content store (on /dev/xvdb) when
# creating a pod. We schedule pods that reference each target image, tolerating
# the NoExecute taint, pinned to the builder node.
echo "→ Pulling images onto builder node..."
for i in "${!FULL_IMAGES[@]}"; do
  img="${FULL_IMAGES[$i]}"
  pod_name="snapshot-puller-${i}"
  echo "  [$((i+1))/${#FULL_IMAGES[@]}] $img"

  kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${pod_name}
  namespace: kube-system
  labels:
    app: snapshot-puller
spec:
  nodeName: ${NODE_NAME}
  tolerations:
    - operator: Exists
  terminationGracePeriodSeconds: 0
  containers:
    - name: pull
      image: ${img}
      command: ["sh", "-c", "echo IMAGE_PULLED && sleep 3"]
      resources:
        requests:
          cpu: "50m"
          memory: "64Mi"
        limits:
          cpu: "100m"
          memory: "128Mi"
  restartPolicy: Never
EOF
done

# Wait for all puller pods to complete (indicates images are pulled)
echo "→ Waiting for image pulls to complete (this takes 3-8 min for large images)..."
ALL_PULLED=true
for i in "${!FULL_IMAGES[@]}"; do
  pod_name="snapshot-puller-${i}"
  img="${FULL_IMAGES[$i]}"

  if kubectl wait --for=jsonpath='{.status.phase}'=Succeeded \
    "pod/$pod_name" -n kube-system --timeout=900s 2>/dev/null; then
    echo "  ✓ $(basename "$img")"
  else
    PHASE=$(kubectl get pod "$pod_name" -n kube-system -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
    if [[ "$PHASE" == "Running" || "$PHASE" == "Succeeded" ]]; then
      echo "  ✓ $(basename "$img") (phase: $PHASE)"
    else
      echo "  ✗ $(basename "$img") (phase: $PHASE — image may not be fully cached)"
      ALL_PULLED=false
    fi
  fi
done

if [[ "$ALL_PULLED" != "true" ]]; then
  echo "Warning: not all images pulled successfully. Snapshot may be incomplete." >&2
fi

# --- Clean up pods and stop instance ------------------------------------------
echo "→ Cleaning up puller pods..."
kubectl delete pods -n kube-system -l app=snapshot-puller --ignore-not-found 2>/dev/null

echo "→ Removing builder node from cluster..."
kubectl delete node "$NODE_NAME" --ignore-not-found 2>/dev/null

echo "→ Stopping instance (flushing writes to EBS)..."
aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" > /dev/null
aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" --region "$REGION"
echo "  ✓ Instance stopped"

# --- Create snapshot ----------------------------------------------------------
echo "→ Creating snapshot of /dev/xvdb..."
VOLUME_ID=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$REGION" \
  --query "Reservations[0].Instances[0].BlockDeviceMappings[?DeviceName=='/dev/xvdb'].Ebs.VolumeId" \
  --output text)
if [[ -z "$VOLUME_ID" || "$VOLUME_ID" == "None" ]]; then
  echo "Error: could not find /dev/xvdb volume on instance $INSTANCE_ID" >&2
  exit 1
fi

TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)
IMAGE_LIST=$(IFS=,; echo "${IMAGES[*]}")

SNAPSHOT_ID=$(aws ec2 create-snapshot --region "$REGION" \
  --volume-id "$VOLUME_ID" \
  --description "Bottlerocket data volume - pre-pulled GPU images for ${CLUSTER_NAME}" \
  --tag-specifications "ResourceType=snapshot,Tags=[
    {Key=Name,Value=${CLUSTER_NAME}-gpu-data-volume-${TIMESTAMP}},
    {Key=Cluster,Value=${CLUSTER_NAME}},
    {Key=Purpose,Value=bottlerocket-data-volume},
    {Key=Images,Value=${IMAGE_LIST}},
    {Key=CreatedBy,Value=ops/create-data-volume-snapshot.sh},
    {Key=CreatedAt,Value=${TIMESTAMP}}
  ]" \
  --query "SnapshotId" --output text)

echo "  Snapshot: $SNAPSHOT_ID"
echo "→ Waiting for snapshot to complete (2-5 min for 200 GiB volume)..."
aws ec2 wait snapshot-completed --snapshot-ids "$SNAPSHOT_ID" --region "$REGION"
echo "  ✓ Snapshot ready"

# --- Fast Snapshot Restore (optional) -----------------------------------------
if [[ -n "$FSR_AZS" ]]; then
  echo "→ Enabling Fast Snapshot Restore in: $FSR_AZS"
  IFS=',' read -ra AZ_ARRAY <<< "$FSR_AZS"
  aws ec2 enable-fast-snapshot-restores --region "$REGION" \
    --source-snapshot-ids "$SNAPSHOT_ID" \
    --availability-zones "${AZ_ARRAY[@]}" > /dev/null
  echo "  ✓ FSR enabled (optimization takes 15-60 min)"
fi

# --- Write tfvars (optional) --------------------------------------------------
if [[ "$WRITE_TFVARS" == "true" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
  TFVARS_FILE="${SCRIPT_DIR}/../terraform/30.eks/30.cluster/snapshot.auto.tfvars"
  cat > "$TFVARS_FILE" <<EOF
# Auto-generated by ops/create-data-volume-snapshot.sh
# Created: ${TIMESTAMP}
# Images: ${IMAGE_LIST}
# To regenerate: ./ops/create-data-volume-snapshot.sh --write-tfvars ${IMAGES[*]}
gpu_data_volume_snapshot_id = "${SNAPSHOT_ID}"
EOF
  echo "  ✓ Written to terraform/30.eks/30.cluster/snapshot.auto.tfvars"
fi

# --- Done (trap still fires to terminate instance) ----------------------------
echo ""
echo "╭──────────────────────────────────────────────────────────────────╮"
echo "│  Snapshot created successfully                                   │"
echo "╰──────────────────────────────────────────────────────────────────╯"
echo ""
echo "  Snapshot ID:  $SNAPSHOT_ID"
echo "  Volume size:  ${VOLUME_SIZE} GiB"
echo "  Images:       ${IMAGE_LIST}"
echo ""
echo "  Next steps:"
echo "    1. Add to tfvars:  gpu_data_volume_snapshot_id = \"$SNAPSHOT_ID\""
echo "    2. Apply:          make ENVIRONMENT=<env> MODULE=./30.eks/30.cluster apply"
echo "    3. New GPU nodes will boot with images pre-cached (0s image pull)"
echo ""
echo "$SNAPSHOT_ID"
