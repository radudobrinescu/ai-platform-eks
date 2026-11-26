# 30.eks/35.addons

This stage installs the cluster add-ons the platform's Ingresses and
autoscaling depend on. The AWS Load Balancer Controller (with its CRDs and IAM
role) creates the shared internal load balancer from the platform's Ingresses;
it is installed unless `cluster_config.capabilities.loadbalancing = false` (the
default when EKS Auto Mode is on). KEDA scales the model-serving replicas,
including to zero;
it is installed unless `cluster_config.capabilities.autoscaling = false`. Three
more add-ons are installed only for the optional Amazon Managed Prometheus and
Grafana stack (`observability_configuration.aws_oss_tooling = true`): External
Secrets, and either AWS for Fluent Bit with its CloudWatch log group
`/aws/eks/<cluster>/aws-fluentbit-logs` or, when
`aws_oss_tooling_config.enable_adot_collector = true`, cert-manager.

Everything is installed by Terraform through Helm; you do not need the `helm`
CLI.

## Deploy

`./platformctl up <env>` applies this stage after `30.eks/30.cluster`, whose
state it reads (with that of `10.networking`). This stage alone, from the
repository root:

```bash
AWS_REGION=<region> make -C terraform plan  MODULE=30.eks/35.addons ENVIRONMENT=<env>
AWS_REGION=<region> make -C terraform apply MODULE=30.eks/35.addons ENVIRONMENT=<env>
```

`apply` does not ask for confirmation. `make` reads
`terraform/00.global/vars/<env>.tfvars` and stops if it is missing; unlike
`platformctl` it does not read `region` from that file, so pass `AWS_REGION`.

## Inputs

Set in `terraform/00.global/vars/<env>.tfvars`; the documented template is
[`example.tfvars`](../../00.global/vars/example.tfvars).

- `observability_configuration` - required. `aws_oss_tooling` and the
  `aws_oss_tooling_config` map select the observability add-ons above.
- `cluster_config.capabilities.loadbalancing`,
  `cluster_config.capabilities.autoscaling`, `cluster_config.eks_auto_mode`.
- `tags` - extra tags for every resource.

## Outputs

- `external_secrets_addon_output` - what the External Secrets add-on created,
  read by `45.aws-oss-observability`; null unless `aws_oss_tooling = true`.
