#!/usr/bin/env bash
# Create an EBS snapshot of a Bottlerocket data volume with pre-pulled container images.
#
# The snapshot can be referenced in Karpenter EC2NodeClass blockDeviceMappings
# (snapshotID field) so new GPU nodes boot with images already on disk —
# eliminating the multi-minute pull for the large vLLM serving image.
#
# Approach: launches a temporary Bottlerocket instance that joins the EKS cluster
# with a NoSchedule taint. A short-lived pod scheduled ON that node (via nodeName)
# forces kubelet to pull the target image into containerd's content store on
# /dev/xvdb. Once pulled, the instance is stopped and /dev/xvdb is snapshotted.
#
# kubectl runs with a temporary kubeconfig that the script writes for the named
# cluster (-n, -r), never with your current context; your kubeconfig is not
# modified.
#
# Usage:
#   ./ops/image/create-data-volume-snapshot.sh [options] <image1> [image2] ...
#
# Options:
#   -r REGION           AWS region (default: from AWS_REGION or cluster context)
#   -n CLUSTER_NAME     EKS cluster name (default: from current kubectl context)
#   -i INSTANCE_TYPE    Builder instance type (default: c5.xlarge for fast network)
#   -s VOLUME_SIZE      Data volume size in GiB (default: 200)
#   --fsr AZ1[,AZ2]    Enable Fast Snapshot Restore in specified AZs
#   --write-tfvars      Write snapshot ID to terraform/30.eks/30.cluster/snapshot.auto.tfvars
#   --reuse             If this account already has a completed snapshot of the
#                       same images and volume size (tags Purpose + Images), use
#                       it instead of building a new one
#   --patch-nodeclasses NC1[,NC2]
#                       Set the snapshot on these Karpenter EC2NodeClasses' /dev/xvdb
#                       mapping right away (Terraform renders it on the next apply)
#
# Images: a reference with a registry host (docker.io/..., <acct>.dkr.ecr...)
# is pulled as given; docker.io images are pulled anonymously, so no Docker Hub
# account is needed. A short reference (vllm/vllm-openai:tag) is expanded to
# this account's ECR pull-through cache (docker-hub/...), as before.
#
# Examples:
#   # From Docker Hub, no credentials:
#   ./ops/image/create-data-volume-snapshot.sh docker.io/vllm/vllm-openai:v0.33.0
#
#   # Using ECR pull-through cache URI:
#   ./ops/image/create-data-volume-snapshot.sh <account-id>.dkr.ecr.<region>.amazonaws.com/docker-hub/vllm/vllm-openai:v0.33.0
#
#   # Short form (auto-prefixes with account ECR pull-through cache):
#   ./ops/image/create-data-volume-snapshot.sh vllm/vllm-openai:v0.33.0
#
#   # With FSR and auto-tfvars:
#   ./ops/image/create-data-volume-snapshot.sh --fsr eu-central-1a,eu-central-1b --write-tfvars vllm/vllm-openai:v0.24.0
#
# Output: prints snapshot ID (snap-xxx) as the last line on success.
set -euo pipefail

INSTANCE_TYPE="c5.xlarge"
VOLUME_SIZE=200
FSR_AZS=""
WRITE_TFVARS=false
REUSE=false
PATCH_NODECLASSES=""
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
    --reuse) REUSE=true; shift ;;
    --patch-nodeclasses) PATCH_NODECLASSES="$2"; shift 2 ;;
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

if [[ -n "$PATCH_NODECLASSES" && ! "$PATCH_NODECLASSES" =~ ^[a-z0-9.,-]+$ ]]; then
  echo "Error: --patch-nodeclasses takes comma-separated Kubernetes names" >&2
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
  CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}' 2>/dev/null | awk -F/ '{print $NF}') || true
fi
# EKS cluster names are letters, digits, "-" and "_". Checked because the name
# goes into EC2 tag filters, where "*" and "?" are wildcards: the leftover
# builder lookup below must match this cluster only.
if [[ ! "$CLUSTER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]]; then
  echo "Error: no valid EKS cluster name ('${CLUSTER_NAME}'). Pass -n <cluster-name>." >&2
  exit 1
fi
if [[ -z "$REGION" ]]; then
  REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
fi
if [[ -z "$REGION" ]]; then
  echo "Error: cannot determine region. Pass -r or set AWS_REGION." >&2
  exit 1
fi

# --- Builder lifecycle --------------------------------------------------------
# The only billable thing this script creates is the builder EC2 instance (its
# volumes are deleted with it). The EXIT trap is installed here, before the
# launch, and terminating the instance is the first thing it does: that step
# needs only the AWS CLI, never kubectl.
INSTANCE_ID=""
NODE_NAME=""
LAUNCH_STARTED=false
BUILDER_NAME_TAG="${CLUSTER_NAME}-snapshot-builder"
BUILDER_PURPOSE_TAG="data-volume-snapshot"
RETRY_SECONDS="${LAUNCH_RETRY_SECONDS:-10}"
WORK_DIR=$(mktemp -d)

# terminate_instances ID... — 0 once EC2 has accepted the termination (3 tries).
terminate_instances() {
  local attempt
  for attempt in 1 2 3; do
    if aws ec2 terminate-instances --instance-ids "$@" --region "$REGION" \
        >/dev/null 2>"$WORK_DIR/terminate.err"; then
      return 0
    fi
    if [[ "$attempt" -lt 3 ]]; then sleep "$RETRY_SECONDS"; fi
  done
  echo "ERROR: could not terminate builder instance(s) $* - still running, and billed." >&2
  sed 's/^/  /' "$WORK_DIR/terminate.err" >&2
  echo "  Terminate by hand:" >&2
  echo "    aws ec2 terminate-instances --instance-ids $* --region $REGION" >&2
  return 1
}

# Terminate the builders of THIS cluster that are still alive: the ones an
# interrupted earlier run left behind. Found by the tags launch_builder sets,
# as exact values, so another cluster's builder is never matched.
terminate_leftover_builders() {
  local out id ids=()
  if ! out=$(aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:Name,Values=${BUILDER_NAME_TAG}" \
                "Name=tag:Purpose,Values=${BUILDER_PURPOSE_TAG}" \
                "Name=instance-state-name,Values=pending,running,stopping,stopped" \
      --query "Reservations[].Instances[].InstanceId" --output text 2>"$WORK_DIR/describe.err"); then
    echo "  ⚠ could not look for leftover builder instances of $CLUSTER_NAME:" >&2
    sed 's/^/    /' "$WORK_DIR/describe.err" >&2
    return 1
  fi
  while read -r id; do
    if [[ "$id" =~ ^i-[0-9a-f]+$ ]]; then ids+=("$id"); fi
  done < <(tr -s '[:space:]' '\n' <<< "$out")
  if [[ ${#ids[@]} -eq 0 ]]; then return 0; fi
  echo "→ Terminating builder instance(s) of $CLUSTER_NAME left running: ${ids[*]}"
  terminate_instances "${ids[@]}"
}

# Terminate the builder, then remove its pods and node. Safe to call twice.
# Returns 1 if an instance may still be running.
release_builder() {
  local rc=0
  if [[ -n "$INSTANCE_ID" ]]; then
    echo ""
    echo "→ Cleaning up..."
    if terminate_instances "$INSTANCE_ID"; then
      echo "  Builder instance $INSTANCE_ID terminated."
    else
      rc=1
    fi
    INSTANCE_ID=""
    kubectl delete pods -n kube-system -l app=snapshot-puller --ignore-not-found \
      --wait=false --request-timeout=30s >/dev/null 2>&1 || true
    if [[ -n "$NODE_NAME" ]]; then
      kubectl delete node "$NODE_NAME" --ignore-not-found \
        --wait=false --request-timeout=30s >/dev/null 2>&1 || true
    fi
  elif [[ "$LAUNCH_STARTED" == "true" ]]; then
    # A launch was attempted but no instance id was recorded: look by tags.
    terminate_leftover_builders || rc=1
  fi
  LAUNCH_STARTED=false
  return "$rc"
}

cleanup() {
  local rc=$?
  set +e +o pipefail
  if ! release_builder && [[ "$rc" -eq 0 ]]; then rc=1; fi
  rm -rf "$WORK_DIR"
  exit "$rc"
}
trap cleanup EXIT

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if [[ -z "$ACCOUNT" || "$ACCOUNT" == "None" ]]; then
  echo "Error: failed to get AWS account ID. Check credentials and IAM permissions." >&2
  exit 1
fi

# An earlier run that was killed (closed terminal, CI timeout) may have left its
# builder running. A failure here is reported and does not stop this run.
terminate_leftover_builders || true

# Expand short image references to full ECR pull-through cache URIs, and drop
# any private-ECR image that isn't pushed yet — baking such an image would hang
# the puller pod in ImagePullBackOff, so the script skips images absent from ECR.
# Skipping it keeps the no-Docker path working (the vLLM image still bakes).
# FULL_IMAGES: what is pulled. BAKED_IMAGES: the same images as they were given
# on the command line — the spelling the Images tag and the lookup by it use.
FULL_IMAGES=()
BAKED_IMAGES=()
for img in "${IMAGES[@]}"; do
  requested="$img"
  # A first path component with a dot, a colon or "localhost" is a registry host.
  first="${img%%/*}"
  if [[ "$img" != */* || ( "$first" != *.* && "$first" != *:* && "$first" != "localhost" ) ]]; then
    img="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/docker-hub/${img}"
  fi
  # For private-ECR images (not the pull-through cache), verify the tag exists.
  if [[ "$img" == *".dkr.ecr."* && "$img" != *"/docker-hub/"* ]]; then
    repo="${img#*/}"; repo="${repo%%:*}"
    tag="${img##*:}"
    if ! aws ecr describe-images --region "$REGION" \
          --repository-name "$repo" --image-ids "imageTag=${tag}" >/dev/null 2>&1; then
      echo "  ⚠ skipping ${img} — not present in ECR (not built yet)." >&2
      continue
    fi
  fi
  FULL_IMAGES+=("$img")
  BAKED_IMAGES+=("$requested")
done

if [[ ${#FULL_IMAGES[@]} -eq 0 ]]; then
  echo "Error: none of the requested images are present in ECR — nothing to bake." >&2
  exit 1
fi

# Tag-safe form of the image list for the Images tag (see the snapshot step):
# space-joined, at most 255 characters. Reuse looks snapshots up by it, so it
# lists only the images that are baked — never one skipped above.
IMAGE_LIST_TAG="${BAKED_IMAGES[*]}"
IMAGE_LIST_TAG="${IMAGE_LIST_TAG:0:255}"

# --- kubectl talks to the named cluster only ----------------------------------
# kubectl never runs with the operator's kubeconfig: its current context can
# point at another cluster (a second environment, a switch in another terminal
# during the build), and the GPU EC2NodeClasses have the same names in every
# environment. The script writes its own kubeconfig for $CLUSTER_NAME, uses only
# that one, and removes it on exit; the operator's file and context are not
# touched. Stops the script when kubectl or the cluster is not usable.
KUBECONFIG_READY=false
use_cluster_kubeconfig() {
  if [[ "$KUBECONFIG_READY" == "true" ]]; then return 0; fi
  if ! command -v kubectl >/dev/null 2>&1; then
    echo "Error: kubectl not found on PATH. It is needed to bake the image and to" >&2
    echo "  update the EC2NodeClasses. Install kubectl and run this again." >&2
    exit 1
  fi
  if ! aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$REGION" \
      --kubeconfig "$WORK_DIR/kubeconfig" >/dev/null 2>"$WORK_DIR/kubeconfig.err"; then
    echo "Error: cannot write a kubeconfig for cluster $CLUSTER_NAME in $REGION:" >&2
    sed 's/^/  /' "$WORK_DIR/kubeconfig.err" >&2
    exit 1
  fi
  export KUBECONFIG="$WORK_DIR/kubeconfig"
  if ! kubectl get --raw /version --request-timeout=20s \
      >/dev/null 2>"$WORK_DIR/kubeconfig.err"; then
    echo "Error: cannot reach cluster $CLUSTER_NAME in $REGION with kubectl:" >&2
    sed 's/^/  /' "$WORK_DIR/kubeconfig.err" >&2
    exit 1
  fi
  KUBECONFIG_READY=true
}

# Point the given Karpenter EC2NodeClasses' /dev/xvdb volume at the snapshot.
# Terraform's next apply renders the same value, so this only makes it take
# effect in the apply that built the snapshot.
patch_nodeclasses() {
  local snap="$1" nc devices idx current
  [[ -z "$PATCH_NODECLASSES" ]] && return 0
  [[ "$snap" =~ ^snap-[0-9a-f]+$ ]] || { echo "  ⚠ not a snapshot id: $snap" >&2; return 0; }
  IFS=',' read -ra ncs <<< "$PATCH_NODECLASSES"
  for nc in "${ncs[@]}"; do
    if ! devices=$(kubectl get ec2nodeclass "$nc" \
        -o jsonpath='{range .spec.blockDeviceMappings[*]}{.deviceName}{"\n"}{end}' 2>/dev/null); then
      echo "  - EC2NodeClass $nc: not found, skipped"
      continue
    fi
    idx=$(awk '$0 == "/dev/xvdb" {print NR - 1; exit}' <<< "$devices")
    if [[ -z "$idx" ]]; then
      echo "  - EC2NodeClass $nc: no /dev/xvdb volume, skipped"
      continue
    fi
    current=$(kubectl get ec2nodeclass "$nc" -o jsonpath="{.spec.blockDeviceMappings[${idx}].ebs.snapshotID}" 2>/dev/null || true)
    if [[ "$current" == "$snap" ]]; then
      echo "  - EC2NodeClass $nc: already uses $snap"
      continue
    fi
    if kubectl patch ec2nodeclass "$nc" --type json \
        -p "[{\"op\":\"add\",\"path\":\"/spec/blockDeviceMappings/${idx}/ebs/snapshotID\",\"value\":\"${snap}\"}]" >/dev/null; then
      echo "  ✓ EC2NodeClass $nc now boots GPU nodes from $snap"
    else
      echo "  ⚠ EC2NodeClass $nc: patch failed (the next apply sets it)" >&2
    fi
  done
}

# Checked before anything else is done with the cluster, and before a builder
# is launched. (A reuse without --patch-nodeclasses needs no kubectl.)
if [[ -n "$PATCH_NODECLASSES" ]]; then
  use_cluster_kubeconfig
fi

if [[ "$REUSE" == "true" ]]; then
  EXISTING=$(aws ec2 describe-snapshots --region "$REGION" --owner-ids self \
    --filters "Name=tag:Purpose,Values=bottlerocket-data-volume" \
              "Name=tag:Images,Values=${IMAGE_LIST_TAG}" \
              "Name=volume-size,Values=${VOLUME_SIZE}" \
              "Name=status,Values=completed" \
    --query "reverse(sort_by(Snapshots, &StartTime))[0].SnapshotId" --output text 2>/dev/null || true)
  if [[ "$EXISTING" =~ ^snap-[0-9a-f]+$ ]]; then
    echo "→ Reusing snapshot $EXISTING (already holds: ${IMAGE_LIST_TAG})"
    patch_nodeclasses "$EXISTING"
    echo "$EXISTING"
    exit 0
  fi
  echo "→ No snapshot of ${IMAGE_LIST_TAG} yet: building one (about 15 minutes)."
fi

use_cluster_kubeconfig

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

# Resolve the Karpenter node role's instance profile. Karpenter creates it when
# it first reconciles the EC2NodeClasses, which in a fresh apply can be just
# after this starts (E2E: "cannot find instance profile"), so wait for it.
ROLE_NAME="KarpenterNode-${CLUSTER_NAME}"
INSTANCE_PROFILE=""
for _ in $(seq 1 "${LAUNCH_ATTEMPTS:-18}"); do
  INSTANCE_PROFILE=$(aws iam list-instance-profiles-for-role \
    --role-name "$ROLE_NAME" --region "$REGION" \
    --query "InstanceProfiles[0].InstanceProfileName" --output text 2>/dev/null || true)
  [[ -n "$INSTANCE_PROFILE" && "$INSTANCE_PROFILE" != "None" ]] && break
  echo "  no instance profile for $ROLE_NAME yet; retrying in 10s"
  sleep "$RETRY_SECONDS"
done

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
USERDATA_FILE="$WORK_DIR/userdata.toml"
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
# Karpenter creates the node role's instance profiles when it reconciles the
# EC2NodeClasses, seconds before this runs in a fresh apply, and IAM is
# eventually consistent: EC2 then answers "Invalid IAM Instance Profile name"
# for a while (seen in E2E 6 s after creation). Retry that error only.
launch_builder() {
  aws ec2 run-instances --region "$REGION" \
  --image-id "$BR_AMI" \
  --instance-type "$INSTANCE_TYPE" \
  --subnet-id "$SUBNET" \
  --security-group-ids "$SG" \
  --iam-instance-profile Name="$INSTANCE_PROFILE" \
  --user-data "file://${USERDATA_FILE}" \
  --block-device-mappings "[
    {\"DeviceName\":\"/dev/xvda\",\"Ebs\":{\"VolumeSize\":4,\"VolumeType\":\"gp3\",\"Encrypted\":true,\"DeleteOnTermination\":true}},
    {\"DeviceName\":\"/dev/xvdb\",\"Ebs\":{\"VolumeSize\":${VOLUME_SIZE},\"VolumeType\":\"gp3\",\"Iops\":6000,\"Throughput\":500,\"Encrypted\":true,\"DeleteOnTermination\":true}}
  ]" \
  --tag-specifications "ResourceType=instance,Tags=[
    {Key=Name,Value=${BUILDER_NAME_TAG}},
    {Key=Purpose,Value=${BUILDER_PURPOSE_TAG}}
  ]" \
  --query "Instances[0].InstanceId" --output text
}
# stderr goes to a file, not into the captured id: a warning printed on a
# successful launch must not hide the instance id (the instance would be lost).
LAUNCH_STARTED=true
for attempt in $(seq 1 "${LAUNCH_ATTEMPTS:-18}"); do
  if out=$(launch_builder 2>"$WORK_DIR/launch.err"); then
    cat "$WORK_DIR/launch.err" >&2
    if [[ "$out" =~ ^i-[0-9a-f]+$ ]]; then INSTANCE_ID="$out"; fi
    break
  fi
  out=$(cat "$WORK_DIR/launch.err")
  if [[ "$out" != *"iamInstanceProfile"* ]]; then
    echo "Error: run-instances failed: $out" >&2
    exit 1
  fi
  echo "  instance profile not visible to EC2 yet (attempt ${attempt}); retrying in 10s"
  sleep "$RETRY_SECONDS"
done
if [[ -z "$INSTANCE_ID" ]]; then
  echo "Error: builder instance not launched: ${out:-no output}" >&2
  exit 1
fi

echo "  Instance: $INSTANCE_ID"

# --- Wait for node to join cluster --------------------------------------------
# The node of the instance launched above, by its providerID
# (aws:///<az>/<instance-id>). Not "the first node with the builder label": that
# can be a node an earlier run left behind, and then the image is pulled there
# while this run's empty volume is snapshotted.
echo "→ Waiting for builder node to join the cluster..."
NODE_JOIN_ATTEMPTS="${NODE_JOIN_ATTEMPTS:-72}"
NODE_JOIN_SECONDS="${NODE_JOIN_SECONDS:-5}"
for _ in $(seq 1 "$NODE_JOIN_ATTEMPTS"); do
  NODE_NAME=$(kubectl get nodes -l node.kubernetes.io/purpose=snapshot-builder \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.providerID}{"\n"}{end}' 2>/dev/null \
    | awk -F'\t' -v id="$INSTANCE_ID" '{ n = split($2, part, "/"); if (part[n] == id) { print $1; exit } }') || true
  if [[ -n "$NODE_NAME" ]]; then
    break
  fi
  sleep "$NODE_JOIN_SECONDS"
done

if [[ -z "$NODE_NAME" ]]; then
  echo "Error: the node of builder instance $INSTANCE_ID did not join cluster $CLUSTER_NAME within $(( NODE_JOIN_ATTEMPTS * NODE_JOIN_SECONDS / 60 )) minutes." >&2
  echo "  Debug: aws ec2 get-console-output --instance-id $INSTANCE_ID --region $REGION" >&2
  exit 1
fi
echo "  Node: $NODE_NAME"

if ! kubectl wait --for=condition=Ready "node/$NODE_NAME" --timeout=120s; then
  echo "Error: builder node $NODE_NAME (instance $INSTANCE_ID) did not become Ready within 120 seconds." >&2
  exit 1
fi
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

  if ! kubectl apply -f - <<EOF
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
  then
    echo "Error: could not create the puller pod for $img on node $NODE_NAME." >&2
    exit 1
  fi
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
      REASON=$(kubectl get pod "$pod_name" -n kube-system \
        -o jsonpath='{.status.containerStatuses[0].state.waiting.reason}' 2>/dev/null || true)
      echo "  ✗ $img was not pulled (waited up to 15 min; pod phase: ${PHASE:-Unknown}${REASON:+, $REASON})" >&2
      ALL_PULLED=false
    fi
  fi
done

# A snapshot of a volume without the image would still carry the Images tag, and
# every cluster in this account and region finds snapshots by that tag: stop
# here. The EXIT trap terminates the builder; the next run builds again.
if [[ "$ALL_PULLED" != "true" ]]; then
  echo "Error: not every image was pulled onto the builder. No snapshot was created." >&2
  exit 1
fi

# --- Clean up pods and stop instance ------------------------------------------
# The images are on the volume by now: a failure to tidy the cluster here is
# reported and does not stop the build (the cleanup at exit tries again).
echo "→ Cleaning up puller pods..."
kubectl delete pods -n kube-system -l app=snapshot-puller --ignore-not-found --timeout=60s \
  || echo "  ⚠ could not delete the puller pods yet" >&2

echo "→ Removing builder node from cluster..."
kubectl delete node "$NODE_NAME" --ignore-not-found --timeout=60s \
  || echo "  ⚠ could not delete node $NODE_NAME yet" >&2

echo "→ Stopping instance (flushing writes to EBS)..."
if ! aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" > /dev/null; then
  echo "Error: could not stop builder instance $INSTANCE_ID." >&2
  exit 1
fi
# This waiter checks every 15 s, 40 times.
if ! aws ec2 wait instance-stopped --instance-ids "$INSTANCE_ID" --region "$REGION"; then
  echo "Error: builder instance $INSTANCE_ID did not reach the stopped state within 10 minutes." >&2
  exit 1
fi
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
# Comma-joined form for the human-readable summary + tfvars comment below.
IMAGE_LIST=$(IFS=,; echo "${BAKED_IMAGES[*]}")
# IMAGE_LIST_TAG (set above) is the tag-safe form for the Images tag: the AWS
# CLI shorthand --tag-specifications uses COMMA as its delimiter, so a
# comma-joined multi-image value corrupts the parse (only single-image runs
# worked before). Space-joined instead (spaces are valid inside a shorthand
# value) and capped at the 256-char tag-value limit.

SNAPSHOT_ID=$(aws ec2 create-snapshot --region "$REGION" \
  --volume-id "$VOLUME_ID" \
  --description "Bottlerocket data volume - pre-pulled GPU images for ${CLUSTER_NAME}" \
  --tag-specifications "ResourceType=snapshot,Tags=[
    {Key=Name,Value=${CLUSTER_NAME}-gpu-data-volume-${TIMESTAMP}},
    {Key=Cluster,Value=${CLUSTER_NAME}},
    {Key=Purpose,Value=bottlerocket-data-volume},
    {Key=Images,Value=${IMAGE_LIST_TAG}},
    {Key=CreatedBy,Value=ops/image/create-data-volume-snapshot.sh},
    {Key=CreatedAt,Value=${TIMESTAMP}}
  ]" \
  --query "SnapshotId" --output text)

if [[ ! "$SNAPSHOT_ID" =~ ^snap-[0-9a-f]+$ ]]; then
  echo "Error: create-snapshot returned no snapshot id ('${SNAPSHOT_ID}')." >&2
  exit 1
fi
echo "  Snapshot: $SNAPSHOT_ID"

# `aws ec2 wait snapshot-completed` gives up after 40 checks, 15 s apart: 10
# minutes. A first snapshot of a volume can take longer, so wait a few rounds.
SNAPSHOT_WAIT_ROUNDS="${SNAPSHOT_WAIT_ROUNDS:-4}"
echo "→ Waiting for snapshot to complete (typically a few minutes; waiting up to $(( SNAPSHOT_WAIT_ROUNDS * 10 )) min)..."
SNAP_STATE=""
for round in $(seq 1 "$SNAPSHOT_WAIT_ROUNDS"); do
  if aws ec2 wait snapshot-completed --snapshot-ids "$SNAPSHOT_ID" --region "$REGION" 2>"$WORK_DIR/wait.err"; then
    SNAP_STATE="completed"
    break
  fi
  SNAP_STATE=$(aws ec2 describe-snapshots --snapshot-ids "$SNAPSHOT_ID" --region "$REGION" \
    --query "Snapshots[0].State" --output text 2>/dev/null || true)
  if [[ "$SNAP_STATE" == "completed" || "$SNAP_STATE" == "error" ]]; then break; fi
  echo "  not complete yet (state: ${SNAP_STATE:-unknown}; wait ${round} of ${SNAPSHOT_WAIT_ROUNDS})"
done
if [[ "$SNAP_STATE" == "error" ]]; then
  echo "Error: snapshot $SNAPSHOT_ID failed (state: error). Deleting it; the next run builds a new one." >&2
  aws ec2 delete-snapshot --snapshot-id "$SNAPSHOT_ID" --region "$REGION" >/dev/null \
    || echo "  ⚠ not deleted. By hand: aws ec2 delete-snapshot --snapshot-id $SNAPSHOT_ID --region $REGION" >&2
  exit 1
fi
if [[ "$SNAP_STATE" != "completed" ]]; then
  echo "Error: snapshot $SNAPSHOT_ID is not complete after $(( SNAPSHOT_WAIT_ROUNDS * 10 )) minutes (state: ${SNAP_STATE:-unknown})." >&2
  sed 's/^/  /' "$WORK_DIR/wait.err" >&2
  echo "  It is left to complete. Once it has, the next apply finds it by its tags" >&2
  echo "  (as does this script with --reuse); nothing else is needed." >&2
  exit 1
fi
echo "  ✓ Snapshot ready"

# The builder is no longer needed: terminate it now rather than at exit. (The
# EXIT trap still covers every earlier way out.)
release_builder || exit 1

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
  # This script lives in ops/image/, so the repo's terraform/ is two levels up
  # (was ../terraform → ops/terraform, which does not exist and made --write-tfvars
  # die AFTER the snapshot was already created).
  TFVARS_FILE="${SCRIPT_DIR}/../../terraform/30.eks/30.cluster/snapshot.auto.tfvars"
  cat > "$TFVARS_FILE" <<EOF
# Auto-generated by ops/create-data-volume-snapshot.sh
# Created: ${TIMESTAMP}
# Images: ${IMAGE_LIST}
# To regenerate: ./ops/image/create-data-volume-snapshot.sh --write-tfvars ${IMAGES[*]}
gpu_data_volume_snapshot_id = "${SNAPSHOT_ID}"
EOF
  echo "  ✓ Written to terraform/30.eks/30.cluster/snapshot.auto.tfvars"
fi

patch_nodeclasses "$SNAPSHOT_ID"

# --- Done ---------------------------------------------------------------------
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
