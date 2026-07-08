# platform-controller image

A single pinned image that bakes the Python dependencies currently
`pip install`ed at pod startup by the platform's control-plane Deployments.
It replaces the `python:3.12-slim` + `install-pydeps` initContainer pattern in:

- `platform/services/litellm-sync/deployment.yaml` (`kubernetes==31.0.0`)
- `platform/services/cluster-dashboard/manifests.yaml` (`psycopg[binary]==3.2.3`)
- `platform/services/cluster-dashboard/pha-event-watcher.yaml`
  (`kubernetes==31.0.0` + `psycopg[binary]==3.2.3`)

Baking the deps removes the runtime PyPI dependency (reproducibility,
supply-chain, cold-start, air-gap). The `Dockerfile` here pins the exact same
versions, so it is a behavior-preserving drop-in.

## Status: Dockerfile ready; wiring is a scoped follow-up

The image is **not yet wired into the manifests**, on purpose. Doing so cleanly
requires two things this repo doesn't have in the default (self-hosted) path,
and a half-wired change would break the "fork and `up` anywhere" property:

1. **A build + push step.** There is no Docker on the Terraform apply host in
   the current flow (same reason `enable_fine_tuning` is documented as
   Docker-dependent). The build must run via Terraform on a Docker-capable host
   or in CI — mirror `terraform/30.eks/30.cluster/unsloth-image.tf` exactly:
   an `aws_ecr_repository` + a `docker build/push` (or CodeBuild) + surface the
   resulting image URI into the `platform-config` ConfigMap as
   `platformControllerImage` (next to `rayImage` / `unslothImage`).

2. **Per-install image-URI injection into the manifests.** These three
   Deployments are *raw* manifests synced by ArgoCD — Kubernetes cannot read a
   ConfigMap value into a container `image:` field. The ECR URI is
   account/region-specific, so it can't be hardcoded in a forkable template.
   Pick one:
   - **Kustomize `images:` transformer** (cluster-dashboard already uses a
     `kustomization.yaml`) with the ECR URI set per-install (e.g. via an ArgoCD
     ApplicationSet parameter or a Terraform-rendered kustomization), or
   - render these Deployments through KRO / Terraform so they can consume
     `platformConfig.data.platformControllerImage` the way the RGDs already do.

## Implementation checklist (when scheduled — this is a Phase-0 hardening item)

- [ ] `platform-controller-image.tf` — ECR repo + build/push (mirror `unsloth-image.tf`), gated like `enable_fine_tuning`.
- [ ] Add `platformControllerImage` to the `platform-config` ConfigMap.
- [ ] litellm-sync: drop the `install-pydeps` initContainer + `pydeps` emptyDir + `PYTHONPATH`; set `image` to the pinned image.
- [ ] cluster-dashboard + event-watcher: same, keeping the `db-init` (postgres) initContainer as-is.
- [ ] Verify each pod starts with no network egress to PyPI (e.g. NetworkPolicy test) and cold-start improves.
- [ ] Pin the base image by digest in the `Dockerfile`.

Until then, the runtime-install pattern remains in place and functional; the
dependency versions are already pinned, so the immediate risk is limited to
supply-chain/cold-start rather than version drift.
