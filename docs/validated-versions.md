# Validated versions

Each release is a set of component versions proven together on real clusters: a fresh
install and an upgrade from the previous release, each with the default profile
(self-managed Argo CD and kro) and with EKS Managed Capabilities. Every release listed
here passed all four runs before it was tagged. Upgrade with `./platformctl upgrade`
(see UPGRADING.md).

| Release | Date | Kubernetes | vLLM | LiteLLM | Karpenter |
|---|---|---|---|---|---|
| [v2026.12.0](releases/v2026.12.0.md) | 2026-11-19 | 1.36, 1.37 | `v0.33.0` | `v1.108.10-lts` | `1.15.2` |
