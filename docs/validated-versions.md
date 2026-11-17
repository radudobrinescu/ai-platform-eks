# Validated versions

Each release is a set of component versions proven together on real clusters: a fresh
install and an upgrade from the previous release, each with the default profile
(self-managed Argo CD and kro) and with EKS Managed Capabilities. Every release listed
here passed all four runs before it was tagged. Upgrade with `./platformctl upgrade`
(see UPGRADING.md).

| Release | Date | Kubernetes | vLLM | LiteLLM | Karpenter |
|---|---|---|---|---|---|
| [v2026.11.7](releases/v2026.11.7.md) | 2026-11-17 | 1.36, 1.37 | `v0.33.0` | `v1.108.10-lts` | `1.15.2` |
| [v2026.11.6](releases/v2026.11.6.md) | 2026-11-17 | 1.36, 1.37 | `v0.33.0` | `v1.108.10-lts` | `1.15.2` |
| [v2026.11.5](releases/v2026.11.5.md) | 2026-11-16 | 1.36, 1.37 | `v0.33.0` | `v1.83.14-stable.patch.1` | `1.15.2` |
| [v2026.11.4](releases/v2026.11.4.md) | 2026-11-12 | 1.36 | `v0.33.0` | `v1.83.14-stable.patch.1` | `1.15.1` |
| [v2026.11.3](releases/v2026.11.3.md) | 2026-11-10 | 1.36 | `v0.32.0` | `v1.83.14-stable.patch.1` | `1.15.1` |
| [v2026.11.2](releases/v2026.11.2.md) | 2026-11-09 | 1.36 | `v0.32.0` | `v1.83.14-stable.patch.1` | `1.15.1` |
| [v2026.11.1](releases/v2026.11.1.md) | 2026-10-31 | 1.36 | `v0.32.0` | `v1.83.14-stable.patch.1` | `1.15.1` |
| [v2026.11.0](releases/v2026.11.0.md) | 2026-10-30 | 1.36 | `v0.32.0` | `v1.83.14-stable.patch.1` | `1.15.1` |
