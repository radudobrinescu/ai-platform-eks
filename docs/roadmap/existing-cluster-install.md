# Install onto an existing EKS cluster (bring-your-own-cluster)

**Status**: **Not delivered** — the platform provisions its own cluster today · **Updated**: 2026-07-16
**Priority**: High — this is a stated goal ("enable ArgoCD/ACK/KRO capabilities and
bootstrap the platform on an existing EKS in your own VPC")
**Date added**: 2026-07-16

## Goal

A forker with an **existing EKS cluster** enables the ArgoCD, ACK, and KRO EKS
Managed Capabilities, points the platform at their cluster + VPC, and bootstraps
the whole platform on top — without the platform creating a new VPC or cluster.

## Current reality (why it doesn't hold yet)

`platformctl up` runs the full Terraform chain — `10.networking` (creates the
VPC), `20.iam-roles-for-eks`, `30.eks/30.cluster`, `35.addons`, `40.observability`
— and **`30.cluster` creates the cluster itself** (`module "eks"`, no
`data "aws_eks_cluster"` path).

The GitOps layer is **not** separable from that Terraform substrate. `30.cluster`
provisions, on the cluster, everything the ArgoCD apps need to start:

- **~11 Kubernetes secrets/configmaps** — LiteLLM master key + env, platform DB
  credentials, Cognito/SSO secrets, Langfuse secrets/init/keys, the ArgoCD
  cluster secret, dashboard links, platform config.
- **~23 IAM roles + pod-identity/IRSA** — Karpenter, the three capabilities, the
  inference worker (S3 model-cache access), the dashboard, etc.
- **Karpenter** (EKS Karpenter module) + the `karpenter.sh/discovery` SG tag.
- The **three EKS Capabilities** themselves (+ their IAM roles + ArgoCD's Identity
  Center config), **Cognito**, the **S3 model-cache bucket**, the **ALB frontend
  SG**, Bedrock config, and image-optimization.

So "enable the capabilities and apply the manifests" on a bare existing cluster
fails immediately — the apps are missing their secrets, IRSA/pod-identity,
Karpenter NodePools, the capabilities, Cognito, and the model-cache bucket. The
design is "Terraform provisions the substrate → GitOps runs the workloads," which
is the opposite of drop-onto-any-EKS.

## What it would take

1. **Optional cluster creation.** A `create_cluster` / `existing_cluster_name`
   variable that swaps `module "eks"` for `data "aws_eks_cluster"` +
   `data "aws_eks_cluster_auth"`, and sources OIDC provider, VPC id, and subnets
   from the existing cluster instead of the networking stage.
2. **Substrate decoupled from the cluster module.** The secrets, IRSA/pod-identity,
   Karpenter, Cognito, S3, and SG-tag resources take `(cluster_name, vpc_id,
   subnets, oidc_provider_arn)` as inputs rather than reading `module.eks.*`
   outputs — so they can target an existing cluster.
3. **Capabilities: use-or-create.** Detect the ArgoCD/ACK/KRO capabilities the
   forker already enabled (or create them), rather than always creating them.
4. **Networking optional.** Skip `10.networking` when a VPC/subnets are supplied.
5. **A `platformctl` path + docs** for the BYO-cluster flow, and CI/validation for it.

## Interim honesty

Until the above ships, the README and docs describe the **full-provision** path
only (which is what the code does). Do not claim existing-cluster install as a
supported feature; it lives here as a roadmap item.
