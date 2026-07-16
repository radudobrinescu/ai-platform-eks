# Publishing to open source (aws-samples) — compliance checklist

> **Process source of truth:** the internal guide
> `https://w.amazon.com/bin/view/Users/andyhop/Publish_to_aws_samples/`.
> That page is Amazon-internal and could not be read while preparing this doc, so
> the **internal workflow steps** below (repo request, Open Source Program Office
> review/approval, security review) are described from general knowledge and MUST
> be confirmed against that page. The **licensing/attribution** work has been done
> in the repo and is described in detail.

## A. Repository hygiene (in place)

- [x] `LICENSE` — present (**see the flag in section C about which license**).
- [x] `THIRD_PARTY_LICENSES` — attribution for vendored + deployed components (this change).
- [x] `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `README.md`.
- [x] No committed secrets (real tfvars are gitignored; `repo-secret.yaml` is a
      public OCI-registry declaration, not a credential).
- [x] No account/region/env pinning in tracked files (swept this cycle).
- [ ] CI workflow (`.github/workflows/ci.yaml`) — written, currently **untracked**;
      commit it as part of publishing (runs fmt/validate/lint/kubeconform/gitleaks).
- [ ] `.github/CODEOWNERS` + PR/issue templates — nice-to-have, not blocking.

## B. Third-party licensing — what was done and why

`THIRD_PARTY_LICENSES` splits obligations by how each dependency is consumed:

- **Vendored (copied into the repo) → full-text obligation.** The only vendored
  third-party artifact is `platform/services/inference-gateway/gie-crds.yaml`
  (Gateway API Inference Extension CRDs, **Apache-2.0**). Before publishing, drop
  the upstream Apache-2.0 `LICENSE` text **verbatim** into `THIRD_PARTY_LICENSES`
  where noted (the file currently abbreviates it with a pointer).
- **Deployed at runtime (referenced by image tag / chart version) → attribution.**
  vLLM (Apache-2.0), LiteLLM (MIT), oauth2-proxy (MIT), NVIDIA GPU Operator,
  Argo CD, KRO, Karpenter, llm-d, EKS Blueprints Addons (all Apache-2.0), curl,
  Langfuse and Open WebUI (see flags). The repo does not redistribute these
  images/charts, so attribution + a link is the standard treatment — but confirm
  the OSPO review agrees for the two flagged components.

## C. LEGAL REVIEW REQUIRED — do not publish until resolved

These have legal implications and need Amazon Open Source Program Office (OSPO)
and/or legal sign-off — they are **not** decisions to make unilaterally:

1. **Open WebUI license (highest priority).** Since v0.6.6 Open WebUI is
   **BSD-3-Clause + a branding-protection clause** — NOT a standard OSI license.
   Removing/altering the "Open WebUI" branding is a material breach for
   deployments over 50 users. Questions for legal: is it acceptable for an
   AWS-published sample to deploy and feature a component under a non-OSI,
   branding-restricted license? Does anything in this repo alter its branding?
   If it's a problem, options are: keep it but document the license prominently,
   make it optional/opt-in, or replace the chat UI.

2. **Langfuse licensing.** Core is MIT; some enterprise ("ee") features are under
   a separate commercial license. Confirm the deployed configuration enables only
   MIT-licensed functionality (no `ee` feature on by default).

3. **Which license for THIS repo.** The repo currently ships **MIT** with an
   Amazon copyright. aws-samples typically requires **MIT-0** (MIT without the
   attribution clause) or **Apache-2.0**. Confirm the required license with OSPO
   and switch `LICENSE` if needed (this also affects the header the process may
   require in source files).

4. **Trademarks / third-party names.** The README and docs reference product
   names (LiteLLM, Langfuse, Open WebUI, NVIDIA, Karpenter, etc.). Confirm usage
   is nominative/attribution only and matches each project's trademark guidance.

## D. Internal workflow (confirm against the internal guide)

Typical aws-samples path — **verify each step against the internal page**:
1. Request the public `aws-samples/<name>` repo through the AWS open-source
   process (OSPO intake).
2. OSPO / open-source review of license, THIRD_PARTY_LICENSES, and dependencies.
3. Security review / secret scan of the full history (not just HEAD).
4. Import the code (a clean, squashed history is usually preferred over the
   internal development history).
5. Final approval + publish.

## E. Pre-publish scrub (mechanical, before import)

- [ ] Scrub git history for internal hostnames, account IDs, ARNs, and the
      operator's real `*.tfvars` (they're gitignored now, but confirm they were
      never committed historically).
- [ ] Replace the abbreviated Apache-2.0 text in `THIRD_PARTY_LICENSES` with the
      full upstream text.
- [ ] Commit the CI workflow and confirm it passes.
- [ ] Confirm example values (region, ARNs, repo URLs) are placeholders.
