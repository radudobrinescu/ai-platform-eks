# Validated versions

Each release is a set of component versions proven together on a real cluster: a fresh
install and an upgrade from the previous release, each with the default profile
(self-managed Argo CD and kro) and with EKS Managed Capabilities. Upgrade with
`./platformctl upgrade` (see UPGRADING.md), one release at a time.

| Release | Date | Kubernetes | vLLM | LiteLLM | Karpenter | E2E (fresh / upgrade) |
|---|---|---|---|---|---|---|
| [v2026.11.1](releases/v2026.11.1.md) | 2026-10-31 | 1.36 | `v0.32.0` | `v1.83.14-stable.patch.1` | `1.15.1` | default pass, managed pass / default pass, managed pass |
| [v2026.11.0](releases/v2026.11.0.md) | 2026-10-30 | 1.36 | `v0.32.0` | `v1.83.14-stable.patch.1` | `1.15.1` | default pass, managed pass / default pass, managed pass |
