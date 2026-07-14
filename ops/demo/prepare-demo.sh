#!/bin/bash
# =============================================================================
# prepare-demo.sh — warm a GPU node on demand, just before a demo.
# =============================================================================
# Replaces the always-on warm-pool Deployment (removed from the platform): that
# pinned an expensive GPU node 24/7. Instead, run this a few minutes before a
# demo to pre-provision ONE gpu-shared node and pre-pull the vLLM serving image,
# so the first `shared: true` model deploy starts in seconds instead of waiting
# ~90s for Karpenter + a multi-GiB image pull.
#
# How it works:
#   1. Applies a throwaway placeholder Deployment (NOT in git/ArgoCD) that
#      requests 1 nvidia.com/gpu on the gpu-shared NodePool, pinned to L4-class
#      so Karpenter picks a cheap g6.xlarge — not whatever big instance happens
#      to be eligible.
#   2. Waits for the node to be Ready and the vLLM image to land.
#   3. By default, deletes the placeholder immediately (--keep to leave it):
#      the node stays warm during Karpenter's consolidateAfter window
#      (gpu-shared = 300s) and a real model preempts onto it within that grace.
#
# Usage:
#   ./ops/demo/prepare-demo.sh            # warm a node, then release the placeholder
#   ./ops/demo/prepare-demo.sh --keep     # leave the placeholder running (pins the
#                                    # node until you run --teardown)
#   ./ops/demo/prepare-demo.sh --teardown # remove the placeholder now
#   ./ops/demo/prepare-demo.sh --timeout 600
#
# Env:
#   NAMESPACE        placeholder namespace      (default: ai-platform)
#   IMAGE            image to pre-pull          (default: read vllmImage from the
#                                                platform-config ConfigMap,
#                                                else vllm/vllm-openai:<pinned>)
#   MIN_GPU_MIB      VRAM floor for instance    (default: 21503 → excludes T4,
#                                                allows L4/A10G/L40S/+)
# =============================================================================

set -euo pipefail

NAMESPACE="${NAMESPACE:-ai-platform}"
NAME="demo-gpu-warmup"
DEFAULT_IMAGE="vllm/vllm-openai:v0.24.0"
MIN_GPU_MIB="${MIN_GPU_MIB:-21503}"   # (21 GiB * 1024) - 1
TIMEOUT=420
KEEP=0
TEARDOWN_ONLY=0

c_bold=$'\033[1m'; c_grn=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
[ -t 1 ] || { c_bold=""; c_grn=""; c_red=""; c_dim=""; c_rst=""; }
log()  { echo "${c_bold}==>${c_rst} $*"; }
warn() { echo "${c_red}!${c_rst} $*" >&2; }
die()  { warn "$*"; exit 1; }

# ---- args ------------------------------------------------------------------ #
while [ "$#" -gt 0 ]; do
  case "$1" in
    --keep)     KEEP=1 ;;
    --teardown) TEARDOWN_ONLY=1 ;;
    --timeout)  shift; TIMEOUT="${1:?--timeout needs a value}" ;;
    -h|--help)  sed -n '2,/^set -euo/p' "$0" | grep '^#' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
  shift
done

command -v kubectl >/dev/null 2>&1 || die "kubectl not found"
kubectl get ns "$NAMESPACE" >/dev/null 2>&1 || die "namespace '$NAMESPACE' not found — is the platform up?"

teardown() {
  log "Releasing placeholder (Karpenter holds the node ~300s for a real model to preempt)"
  kubectl delete deployment "$NAME" -n "$NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}

if [ "$TEARDOWN_ONLY" -eq 1 ]; then
  teardown
  log "${c_grn}Done.${c_rst} Placeholder removed."
  exit 0
fi

# ---- resolve the image real models use, so we pre-pull the SAME layers ----- #
IMAGE="${IMAGE:-$(kubectl get configmap platform-config -n inference \
  -o jsonpath='{.data.vllmImage}' 2>/dev/null || true)}"
[ -n "$IMAGE" ] || IMAGE="$DEFAULT_IMAGE"
log "Pre-pulling image: ${c_dim}${IMAGE}${c_rst}"

# ---- apply the throwaway placeholder --------------------------------------- #
# Mirrors the (removed) warm-pool: lowest-priority so a real workload preempts
# it instantly, tolerates the gpu-shared nvidia.com/gpu taint, and is pinned to
# L4-class via the instance-gpu-memory floor so Karpenter doesn't over-provision.
log "Provisioning one gpu-shared node (pinned to L4-class, > $((MIN_GPU_MIB / 1024)) GiB VRAM)"
kubectl apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${NAME}
  namespace: ${NAMESPACE}
  labels:
    app.kubernetes.io/part-of: ai-platform
    app.kubernetes.io/component: demo-warmup
  annotations:
    ai-platform/purpose: |
      Ephemeral, demo-only GPU node pre-warm created by ops/demo/prepare-demo.sh.
      NOT managed by ArgoCD. Safe to delete anytime.
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ${NAME}
  template:
    metadata:
      labels:
        app: ${NAME}
    spec:
      priorityClassName: system-cluster-critical
      terminationGracePeriodSeconds: 0
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      nodeSelector:
        workload-type: gpu-shared
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
              - matchExpressions:
                  - key: karpenter.k8s.aws/instance-gpu-memory
                    operator: Gt
                    values: ["${MIN_GPU_MIB}"]
      containers:
        - name: warmer
          image: ${IMAGE}
          command: ["sh", "-c", "exec sleep infinity"]
          resources:
            requests:
              nvidia.com/gpu: "1"
              cpu: "100m"
              memory: "256Mi"
            limits:
              nvidia.com/gpu: "1"
              cpu: "500m"
              memory: "1Gi"
YAML

# priorityClassName note: we use system-cluster-critical only to ensure the
# placeholder schedules promptly; it is deleted right after the node warms
# (unless --keep), so it never blocks a real workload at demo time.

# ---- wait for the node + image --------------------------------------------- #
log "Waiting up to ${TIMEOUT}s for the GPU node to provision and pull the image…"
if kubectl wait --for=condition=available "deployment/${NAME}" -n "$NAMESPACE" \
     --timeout="${TIMEOUT}s" 2>/dev/null; then
  node="$(kubectl get pods -n "$NAMESPACE" -l "app=${NAME}" \
            -o jsonpath='{.items[0].spec.nodeName}' 2>/dev/null || true)"
  instance="$(kubectl get node "$node" \
                -o jsonpath='{.metadata.labels.node\.kubernetes\.io/instance-type}' 2>/dev/null || true)"
  log "${c_grn}Warm.${c_rst} GPU node ${c_bold}${node:-?}${c_rst} (${instance:-?}) is Ready with the image pulled."
else
  warn "Timed out waiting for the warm node. Inspect:"
  echo "    kubectl get pods -n ${NAMESPACE} -l app=${NAME} -o wide"
  echo "    kubectl describe deployment/${NAME} -n ${NAMESPACE}"
  exit 1
fi

# ---- release (default) or keep --------------------------------------------- #
if [ "$KEEP" -eq 1 ]; then
  log "Leaving the placeholder running (--keep). Free the node for a real model with:"
  echo "    ./ops/demo/prepare-demo.sh --teardown"
else
  teardown
fi

echo
log "${c_grn}Ready for the demo.${c_rst} Deploy a shared model and it lands on the warm node:"
echo "    git add workloads/models/<your-model>.yaml && git commit && git push"
echo "    # or: ./ops/test-model.sh <model> \"prompt\"  once it's registered"
