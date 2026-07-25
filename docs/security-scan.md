# Security scan report

**Date:** 2026-07-25
**Scope:** Python source (Bandit), Node/npm dependencies (npm audit), and all
referenced container images (Trivy).

Reproduce:

```bash
# Python
pip install bandit && python3 -m bandit -r ops platform tools
# Containers (per image)
trivy image --scanners vuln --severity HIGH,CRITICAL <image:tag>
```

---

## 1. Python — Bandit 1.9.4

26 Python files scanned (`ops/`, `platform/services/*/scripts/`, `tools/`,
`tests/`). **No high-severity findings.**

| Severity | Count (app code) | Notes |
|---|---|---|
| MEDIUM | 8 | 4× `B310` urlopen, 4× `B108` `/tmp` path |
| LOW | 21 | subprocess, try/except/pass, 1 false positive |
| (tests) | 49× `B101` | `assert` in tests — expected, ignored |

Triage:

- **`B310` (urlopen)** — `model.py`, `backend.py` (×2), `litellm_sync.py`. URLs
  are internally constructed (Hugging Face API, in-cluster Kubernetes / LiteLLM
  endpoints), not attacker-controlled. Low real risk.
- **`B108` (`/tmp`)** — dashboard scripts; single-tenant pod, low risk.
- **`B105` "hardcoded password"** (`backend.py`) — **false positive**: it is the
  Kubernetes service-account **token file path** (`/var/run/secrets/.../token`),
  not a secret.
- **`B603`/`B404` (subprocess)** — `gitops.py` git calls use an argument list
  (no `shell=True`); safe.
- **`B110`/`B112` (try/except/pass|continue)** — intentional best-effort blocks.

## 2. Node / npm — not applicable

There is **no `package.json`/lock file** anywhere in the repository. The only
browser JavaScript is the cluster dashboard's vanilla `cluster-topology.html`,
which has no npm dependencies. `npm audit` has nothing to scan.

## 3. Containers — Trivy 0.72.0 (HIGH/CRITICAL, vuln-only)

All images are referenced by tag; the repository builds none. Findings are
upstream base-image OS/library CVEs. Tags were bumped to the latest patched
version within a compatible line (durable in the GitOps manifests, so every
future cluster inherits them).

| Image | Before (CRIT/HIGH) | After (CRIT/HIGH) | Action |
|---|---|---|---|
| `quay.io/oauth2-proxy/oauth2-proxy` | 5 / 38 | **0 / 2** | `v7.7.1` → `v7.15.3` |
| `amazon/aws-cli` | 0 / 87 | **0 / 0** | `2.24.5` → `2.36.8` |
| `curlimages/curl` | 2 / 20 | **0 / 1** | `8.11.0` → `8.21.0` |
| `postgres` | 7 / 75 | **1 / 14** | `16.6-alpine` & `16-alpine` → `16.14-alpine` |
| `litellm/litellm` | 10 / 142 | 10 / 142 | **held at `v1.81.9-stable.patch.1`** — `v1.83.14` regressed (OOM at 1Gi, liveness failures at 2Gi); revisit with validation |
| `alpine/k8s` | 7 / 309 | **4 / 207** | `1.35.4` → `1.35.6` (same k8s minor) |
| `ghcr.io/open-webui/open-webui` | 38 / 471 | 38 / 471 | already latest release (`v0.10.2`) |
| `prom/prometheus` | 4 / 76 | 4 / 76 | already latest 2.x (`v2.55.1`) |
| `peakcom/s5cmd` | 3 / 33 | 3 / 33 | already latest (`v2.3.0`) |
| `python` | 4 / 19 | 4 / 19 | rolling `3.12-slim` (auto-patches on pull) |

**Totals:** CRITICAL 81 → 64, HIGH 1284 → 965.

### Residual findings (not fixable by a tag bump)

- **`open-webui:v0.10.2`** (38 CRIT / 471 HIGH) dominates the residual. It is
  already the latest release; the CVEs are in its large bundled Python/CUDA
  dependency set. Mitigations: it sits behind auth (Cognito/oauth2-proxy) and an
  internal ALB by default; track upstream releases and rebuild-on-pull.
- **`prometheus` / `s5cmd`** — remaining CRITs are Go-stdlib CVEs fixed only in a
  major rebuild by the upstream maintainer; already on the newest tag in line.
- **`python:3.12-slim`** — rolling tag; a fresh pull picks up Debian patches as
  they are published. No more-patched 3.12 tag exists to pin.

Base-image CVEs are the responsibility of the upstream image maintainers; the
platform mitigates exposure by keeping tags current (this report) and by not
exposing these services publicly by default (internal ALB + SSO).
