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

  # Operator IP allowlist for the gateway's internet-facing NLB — mirrors the
  # current ALB inbound-cidrs (platform/config/ingress.yaml). TODO: source from
  # cluster_config once the ALB is retired at cutover; hardcoded for parity now.
  inference_gateway_allow_cidrs = "82.76.116.134/32"
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

  # REQUIRED for Envoy AI Gateway: base values wire Envoy Gateway's
  # extensionManager to the AI Gateway controller (+ enableBackend); the addon
  # registers InferencePool (inference.networking.k8s.io/v1) as a backend type.
  # Without these, AIGatewayRoute/InferencePool routing does not work. Vendored +
  # pinned at v1.0.0 under inference-gateway/. Merged in order (base then addon).
  values = [
    file("${path.module}/inference-gateway/envoy-gateway-values.yaml"),
    file("${path.module}/inference-gateway/envoy-gateway-values-inferencepool-addon.yaml"),
  ]

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

# --- Gateway API Inference Extension CRDs (GIE ${local.gie_version}) -----------
# InferencePool (inference.networking.k8s.io, GA) + InferenceObjective /
# InferenceModelRewrite / InferencePoolImport (inference.networking.x-k8s.io).
# Vendored + pinned at inference-gateway/gie-crds-v1.5.0.yaml (mirrors how the
# karpenter/*.yaml manifests are vendored — reproducible, no apply-time network,
# offline-applyable). CRDs only; harmless before any InferencePool exists (the
# inference-endpoint RGD renders pools/EPP per model in the routing=gateway path).
data "kubectl_file_documents" "gie_crds" {
  count   = local.capabilities.inference_gateway ? 1 : 0
  content = file("${path.module}/inference-gateway/gie-crds-v1.5.0.yaml")
}

resource "kubectl_manifest" "gie_crds" {
  count             = local.capabilities.inference_gateway ? length(data.kubectl_file_documents.gie_crds[0].documents) : 0
  yaml_body         = element(data.kubectl_file_documents.gie_crds[0].documents, count.index)
  server_side_apply = true

  depends_on = [helm_release.envoy_ai_gateway]
}

# NOTE: the Gateway + EnvoyProxy (internet-facing NLB, IP-allowlist, HTTP) and the
# per-model InferencePool/HTTPRoute/EPP are added next in increment 2, alongside
# the inference-endpoint RGD's routing=gateway path.

################################################################################
# Gateway data plane — GatewayClass + EnvoyProxy (NLB) + Gateway + buffer policy
#
# Mirrors the AWS ai-on-eks Envoy AI Gateway blueprint, with two changes:
#   - nlb-target-type: ip   (route straight to Envoy pods; matches the ALB today)
#   - aws-load-balancer-source-ranges: operator allowlist (IP-restricted, HTTP —
#     TLS is a drop-in once a domain/ACM cert exists).
# Creating the Gateway makes Envoy Gateway provision the Envoy data-plane
# Deployment + a LoadBalancer Service (-> internet-facing NLB via the AWS LB
# Controller). No HTTPRoutes/InferencePools exist yet, so nothing is routed and
# the current serving path (ALB -> LiteLLM -> ClusterIP) is unaffected.
################################################################################

# EnvoyProxy: configures the generated LB Service as an IP-allowlisted, internet-
# facing NLB. Referenced by the GatewayClass parametersRef below.
resource "kubectl_manifest" "gateway_envoyproxy" {
  count     = local.capabilities.inference_gateway ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: gateway.envoyproxy.io/v1alpha1
    kind: EnvoyProxy
    metadata:
      name: ai-gateway
      namespace: ${local.inference_gateway_namespace}
    spec:
      provider:
        type: Kubernetes
        kubernetes:
          envoyService:
            # IP allowlist -> propagates to Service.spec.loadBalancerSourceRanges,
            # which the AWS LB Controller enforces on the NLB security group.
            # (There is no aws-load-balancer-source-ranges annotation; that was an
            # ALB-ism. Use the native field.)
            loadBalancerSourceRanges:
              - ${local.inference_gateway_allow_cidrs}
            annotations:
              service.beta.kubernetes.io/aws-load-balancer-type: external
              service.beta.kubernetes.io/aws-load-balancer-nlb-target-type: ip
              service.beta.kubernetes.io/aws-load-balancer-scheme: internet-facing
              service.beta.kubernetes.io/aws-load-balancer-healthcheck-port: traffic-port
  YAML

  depends_on = [helm_release.envoy_ai_gateway]
}

# GatewayClass (cluster-scoped): uses the Envoy Gateway controller + our EnvoyProxy.
resource "kubectl_manifest" "gateway_class" {
  count     = local.capabilities.inference_gateway ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: gateway.networking.k8s.io/v1
    kind: GatewayClass
    metadata:
      name: ai-gateway
    spec:
      controllerName: gateway.envoyproxy.io/gatewayclass-controller
      parametersRef:
        group: gateway.envoyproxy.io
        kind: EnvoyProxy
        name: ai-gateway
        namespace: ${local.inference_gateway_namespace}
  YAML

  depends_on = [kubectl_manifest.gateway_envoyproxy]
}

# Gateway: single HTTP listener. Envoy Gateway provisions Envoy pods + the NLB.
resource "kubectl_manifest" "gateway" {
  count     = local.capabilities.inference_gateway ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: gateway.networking.k8s.io/v1
    kind: Gateway
    metadata:
      name: ai-gateway
      namespace: ${local.inference_gateway_namespace}
    spec:
      gatewayClassName: ai-gateway
      listeners:
        - name: http
          port: 80
          protocol: HTTP
          allowedRoutes:
            namespaces:
              from: All
  YAML

  depends_on = [kubectl_manifest.gateway_class, kubectl_manifest.gie_crds]
}

# Raise the request-body buffer for large LLM payloads (matches the blueprint).
resource "kubectl_manifest" "gateway_buffer_policy" {
  count     = local.capabilities.inference_gateway ? 1 : 0
  yaml_body = <<-YAML
    apiVersion: gateway.envoyproxy.io/v1alpha1
    kind: ClientTrafficPolicy
    metadata:
      name: ai-gateway-buffer-limit
      namespace: ${local.inference_gateway_namespace}
    spec:
      targetRefs:
        - group: gateway.networking.k8s.io
          kind: Gateway
          name: ai-gateway
      connection:
        bufferLimit: 50Mi
  YAML

  depends_on = [kubectl_manifest.gateway]
}

# NOTE (remaining increment-2 step): extend platform/config/kro/inference-endpoint.yaml
# with a routing field (default 'service') that renders an InferencePool + HTTPRoute
# + EPP against this Gateway and re-points the LiteLLM register at the gateway when
# routing=gateway. Until then the Gateway is provisioned but routes nothing.
