################################################################################
# Inference ingress / routing — Envoy AI Gateway + Gateway API Inference
# Extension (GIE)
#
# Two-tier model (see docs/platform-product-report.md):
#   NLB -> Envoy AI Gateway -> LiteLLM (governance: keys/budgets/Bedrock)
#                           \-> InferencePool + EPP (self-hosted pod picking)
#
# INCREMENT 1 (this file): install the gateway CONTROL PLANE only —
#   - Envoy Gateway            (base Gateway API implementation)
#   - Envoy AI Gateway         (CRDs + controller, builds on Envoy Gateway)
#   - GIE CRDs                 (InferencePool / InferenceObjective)
# It changes NOTHING in the serving path: no Gateway/NLB and no HTTPRoutes are
# created yet, and InferenceEndpoints keep routing=service. Turning the flag on
# just makes the controllers available.
#
# INCREMENT 2 (next): a Gateway + EnvoyProxy (internet-facing NLB, IP-allowlist,
# HTTP — TLS is a drop-in once a domain/ACM cert exists) and the per-model
# InferencePool/HTTPRoute/EPP rendered by the inference-endpoint RGD, plus the
# LiteLLM re-point when routing=gateway.
#
# Version matrix (verified against Envoy AI Gateway v1.0 compatibility matrix):
#   Envoy AI Gateway v1.0.x  ->  Envoy Gateway v1.8.1+  ->  K8s v1.32+  ->  GwAPI v1.5.x
#   Cluster is EKS 1.36 (>=1.32) ✓ ;  GIE latest v1.5.0.
#
# Installed via terraform helm_release (mirrors karpenter.tf) so it deploys with
# `terraform apply` on the test cluster. Task 5 migrates it to an ArgoCD app for
# GitOps parity.
################################################################################

locals {
  # Pinned component versions (bump deliberately; see compatibility matrix above).
  envoy_gateway_version    = "v1.8.1"
  envoy_ai_gateway_version = "v1.0.0"
  gie_version              = "v1.5.0"

  inference_gateway_namespace = "envoy-ai-gateway-system"
}

# --- Base: Envoy Gateway (Gateway API implementation the AI Gateway extends) --
resource "helm_release" "envoy_gateway" {
  count            = local.capabilities.inference_gateway ? 1 : 0
  name             = "envoy-gateway"
  repository       = "oci://docker.io/envoyproxy"
  chart            = "gateway-helm"
  version          = local.envoy_gateway_version
  namespace        = "envoy-gateway-system"
  create_namespace = true
  # wait for the controller so the AI Gateway install below has its CRDs/webhooks.
  wait    = true
  timeout = 600

  depends_on = [module.eks]
}

# --- Envoy AI Gateway CRDs (AIGatewayRoute, AIServiceBackend, ...) ------------
resource "helm_release" "envoy_ai_gateway_crds" {
  count            = local.capabilities.inference_gateway ? 1 : 0
  name             = "aieg-crd"
  repository       = "oci://docker.io/envoyproxy"
  chart            = "ai-gateway-crds-helm"
  version          = local.envoy_ai_gateway_version
  namespace        = local.inference_gateway_namespace
  create_namespace = true
  wait             = true
  timeout          = 300

  depends_on = [helm_release.envoy_gateway]
}

# --- Envoy AI Gateway controller ----------------------------------------------
resource "helm_release" "envoy_ai_gateway" {
  count      = local.capabilities.inference_gateway ? 1 : 0
  name       = "aieg"
  repository = "oci://docker.io/envoyproxy"
  chart      = "ai-gateway-helm"
  version    = local.envoy_ai_gateway_version
  namespace  = local.inference_gateway_namespace
  wait       = true
  timeout    = 600

  depends_on = [helm_release.envoy_ai_gateway_crds]
}

# NOTE: Gateway API Inference Extension CRDs (InferencePool / InferenceObjective,
# GIE ${local.gie_version}) and the Gateway/EnvoyProxy (NLB) are installed in
# increment 2, where they're needed to render per-model InferencePools + EPP.
# Kept out of increment 1 so this step is a pure, low-risk controller install.
