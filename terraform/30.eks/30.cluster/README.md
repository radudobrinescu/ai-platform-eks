# 30.eks/30.cluster

This stage creates the EKS cluster `<resources_prefix>-<env>` and everything the
platform needs from AWS before Argo CD takes over. It has four parts.

The cluster itself: the control plane with its managed add-ons (VPC CNI with
network policy enforcement, kube-proxy, CoreDNS, Pod Identity agent, EBS CSI
driver, metrics-server), a small tainted managed node group for system add-ons,
access entries for the four IAM roles of `20.iam-roles-for-eks`, and a default
`gp3` storage class. With `cluster_config.eks_auto_mode = true` EKS Auto Mode
provides compute, networking and storage instead.

Node provisioning: Karpenter (controller, CRDs, node role) and the NodePools and
EC2NodeClasses in [`karpenter/`](./karpenter): `default` for general workloads
and the GPU pools for model serving. GPU nodes boot from an EBS snapshot that
already holds the vLLM serving image; `image-optimization.tf` builds that
snapshot once per image, account and region (its header describes how).

GitOps: Argo CD and kro, either as EKS Managed Capabilities
(`cluster_config.capabilities.eks_capabilities = true`, needs IAM Identity
Center) or as Helm releases (`false`), and the single Argo CD `bootstrap`
Application that points at `gitops_repo_url`. From there Argo CD deploys the
platform services from `platform/` and the workloads from `workloads/`.

What those services need and cannot create themselves: the `ai-platform` and
`inference` namespaces, generated secrets (LiteLLM master key, database and
Langfuse credentials, the Langfuse first-boot user and project keys), the
`platform-config` ConfigMap, the S3 bucket for cached model weights with the
IAM role of the serving pods, the IAM role that lets LiteLLM call Amazon
Bedrock (`enable_bedrock`), a Cognito user pool with three seed users for
single sign-on (`enable_sso`), the security group of the shared internal load
balancer, and, opt-in, CloudFront distributions in front of that load balancer
(`enable_cloudfront_edge`). With Docker Hub credentials in the environment it
also creates an ECR pull-through cache for Docker Hub images.

The model-weight cache bucket is not filled automatically. The serving pods'
init container only reads `s3://<bucket>/hf/<org>/<model>/`; a model starts from
the cache only if its weights were put there, and is downloaded from Hugging
Face otherwise.

## Deploy

`./platformctl up <env>` applies this stage after `10.networking` and
`20.iam-roles-for-eks`, whose state it reads. This stage alone, from the
repository root:

```bash
AWS_REGION=<region> make -C terraform plan  MODULE=30.eks/30.cluster ENVIRONMENT=<env>
AWS_REGION=<region> make -C terraform apply MODULE=30.eks/30.cluster ENVIRONMENT=<env>
```

`apply` does not ask for confirmation. For this stage it runs two applies: a
targeted one for the cluster (`module.eks` and the EBS CSI role), so that the
Kubernetes, Helm and kubectl providers have an endpoint to connect to, then the
full one. `make` reads `terraform/00.global/vars/<env>.tfvars` and stops if it
is missing; unlike `platformctl` it does not read `region` from that file, so
pass `AWS_REGION`. The apply also runs `aws eks update-kubeconfig` for the new
cluster on the machine it runs on.

## Inputs

Set in `terraform/00.global/vars/<env>.tfvars`; the documented template, with
the meaning of every value, is
[`example.tfvars`](../../00.global/vars/example.tfvars) (and
[`example-esc.tfvars`](../../00.global/vars/example-esc.tfvars) for the AWS
European Sovereign Cloud).

- `shared_config.resources_prefix` and the environment name form the cluster name.
- `cluster_config` - `kubernetes_version`, `private_eks_cluster`,
  `eks_auto_mode`, `create_mng_system`, the `capabilities` switches, and
  `capabilities_config` (Identity Center settings for the managed Argo CD).
- `cluster_endpoint_public_access_cidrs` - who may reach the public API
  endpoint. Required unless `private_eks_cluster = true`; `0.0.0.0/0` is refused.
- `gitops_repo_url`, `gitops_revision`, `gitops_workloads_repo_url` - the
  repository Argo CD syncs from.
- `enable_bedrock`, `enable_sso`, `enable_cloudfront_edge`, `sso_public_urls`.
- `gpu_data_volume_snapshot_id`, `gpu_capacity_reservation_ids`,
  `gpu_node_volume_size_gib` - optional GPU node settings.
- `ack_service_controllers` - a top-level variable (not a key of
  `cluster_config`): ACK controllers to install when Argo CD and kro are
  self-managed and `capabilities.ack = true`.
- `langfuse_init_user_email`, `kms_key_admin_roles`, `tags`.

Docker Hub credentials for the ECR pull-through cache are never written to a
tfvars file; export `TF_VAR_docker_hub_username` and
`TF_VAR_docker_hub_access_token`. The cache has fixed names per account and
region, so set them for one environment per account and region only.

## Outputs

Read them with
`TF_WORKSPACE=<env> terraform -chdir=terraform/30.eks/30.cluster output`.

- `cluster_name`, `cluster_endpoint`, `cluster_certificate_authority_data`,
  `kubernetes_version`, `oidc_provider_arn`, `control_plane_subnet_ids` - read
  by the later stages.
- `configure_kubectl` - the command that points kubectl at the cluster.
- `next_steps` - what to do after the first apply.
- `argocd_url` - the managed Argo CD URL; null when Argo CD is self-managed.
- `langfuse_admin_email`, `langfuse_admin_password` (sensitive).
- `sso_hosted_ui_url`, `sso_issuer_url`, `sso_user_pool_id`,
  `sso_seed_user_passwords` (sensitive).
- `edge_cloudfront_urls` - public URL per UI when the CloudFront edge is on.
