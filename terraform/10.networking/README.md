# 10.networking

This stage creates the network the cluster runs in: one VPC named
`<resources_prefix>-<env>`, spread over up to `num_azs` Availability Zones, with
three subnets per zone (private for nodes and pods, public for NAT and
internet-facing load balancers, and a small "control plane" subnet for the EKS
network interfaces) and NAT gateways for outbound traffic. The private subnets
are tagged for internal load balancers and for Karpenter discovery, the public
ones for internet-facing load balancers. When
`cluster_config.private_eks_cluster = true` it also creates VPC endpoints (an S3
gateway endpoint and interface endpoints for EKS and EKS Auth, EC2, Auto
Scaling, EBS, ECR, KMS, SSM, CloudWatch Logs, Amazon Managed Prometheus, Amazon
Managed Grafana and Bedrock runtime), so a private-only cluster reaches those
services without leaving the VPC. A CloudFormation stack with no real resources
records that the AWS guidance was deployed.

## Deploy

`./platformctl up <env>` applies this stage first, then the others in order.
This stage alone, from the repository root:

```bash
AWS_REGION=<region> make -C terraform plan  MODULE=10.networking ENVIRONMENT=<env>
AWS_REGION=<region> make -C terraform apply MODULE=10.networking ENVIRONMENT=<env>
```

`apply` does not ask for confirmation. `make` reads
`terraform/00.global/vars/<env>.tfvars` and stops if it is missing; unlike
`platformctl` it does not read `region` from that file, so pass `AWS_REGION`.
The state bucket must exist (`platformctl up` creates it; by hand:
`AWS_REGION=<region> make -C terraform bootstrap ENVIRONMENT=<env>`).

## Inputs

Set in `terraform/00.global/vars/<env>.tfvars`; the documented template is
[`example.tfvars`](../00.global/vars/example.tfvars).

- `vpc_cidr` - CIDR block of the VPC (default `10.0.0.0/16`).
- `shared_config.resources_prefix` - first part of every resource name.
- `cluster_config.private_eks_cluster` - `true` adds the VPC endpoints. If the
  key is omitted it counts as `true`, as it does in `30.eks/30.cluster`.
- `tags` - extra tags for every resource.

Not in the template, with defaults that rarely need changing: `num_azs` (3),
`private_subnets_cidr_prefix` (20), `public_subnets_cidr_prefix` (24),
`control_plane_subnets_cidr_prefix` (28).

## Outputs

Read by the later stages from this stage's state:

- `vpc_id`
- `private_subnet_ids`
- `public_subnet_ids`
- `intra_subnet_ids` - the control plane subnets
