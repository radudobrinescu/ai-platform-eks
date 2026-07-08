# platform-controller — pinned base image for the platform's long-running
# Python control-plane pods:
#   - litellm-sync            (platform/services/litellm-sync)
#   - cluster-dashboard       (platform/services/cluster-dashboard)
#   - platform-health-agent   (event-watcher, same dir)
#
# WHY THIS EXISTS
# Today those three Deployments run `python:3.12-slim` with an initContainer
# that `pip install`s deps into an emptyDir at pod startup (see the manifests'
# "install-pydeps" initContainers). That runtime install is:
#   - non-reproducible   (resolves PyPI at boot; drifts over time),
#   - a supply-chain risk (fetches + executes code from the network at runtime),
#   - slow               (adds seconds to every cold start),
#   - fragile air-gapped (the dashboard even wraps it in `|| true`).
# Baking the deps into one pinned image removes all four.
#
# Base pinned by tag; pin by digest for full reproducibility once you can
# resolve it in your build environment:
#   FROM python:3.12-slim@sha256:<digest>
FROM python:3.12-slim

# Versions match exactly what the manifests pip-install at boot today, so this
# image is a drop-in replacement (no behavior change, just baked at build time).
#   kubernetes      -> litellm-sync, event-watcher, dashboard (K8s API client)
#   psycopg[binary] -> dashboard, event-watcher (Platform Health Agent Postgres)
RUN pip install --no-cache-dir \
        kubernetes==31.0.0 \
        "psycopg[binary]==3.2.3" \
    && rm -rf /root/.cache/pip

# The scripts themselves stay mounted from their ConfigMaps (litellm_sync.py,
# backend.py, event_watcher.py) — only the dependencies are baked here, so
# script changes still ship via GitOps without an image rebuild.

# Matches the manifests' securityContext (runAsNonRoot / runAsUser: 65532).
USER 65532:65532
