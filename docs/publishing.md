# Publishing to open source — compliance checklist

Reconciled with the internal guide *"Publish to aws-samples"* (Andy Hopper) and
*Open Source / Posting Sample Code → Third-party Inclusions* (updated Mar 2026).
This file is the working checklist; the internal OpenSourcerer tool is the system
of record for the self-certification + repo creation.

## 0. Is this the right home? (decide first)

The guide says aws-samples is for **code that demonstrates AWS services for a
blog/presentation/workshop**, and explicitly **NOT** for *"a tool, library, SDK,
client, or code that replicates significant AWS product functionality."*

**Open question for a DevEx Advocate / the #open-source channel:** this project is
a substantial self-service AI platform, and it already extends *"Automated
Provisioning of Application-Ready Amazon EKS Clusters"* from the **AWS Solutions
Library**. So the better home may be **`aws-solutions-library-samples`** (or
`aws-ia`) rather than `aws-samples`. Confirm the target org before anything else —
it changes the template, naming, and review path.

## 1. Self-certification (OpenSourcerer → "AWS Sample Code")

Creates a SIM ticket (save the ID); all 6 checks must pass before going public:

1. **Names & Logos** — repo must start with **`sample-`** (e.g.
   `sample-ai-platform-on-eks`). No logos.
2. **Security** — always required (AppSec review; PCSR if SMGS). See §4.
3. **Content & IP** — no confidential info, customer data, or internal details;
   if any code came from another Amazon team, get their leadership's approval. See §3.
4. **Third-party Inclusions** — `THIRD-PARTY-LICENSES` for non-Amazon *included*
   assets. Done (see §2). **Note:** the guide says *including* anything not created
   by Amazon **requires review**, and prefers referencing over vendoring.
5. **Third-party Dependencies** — evaluate the license of every *referenced*
   dependency. See §2(B).
6. **Datasets & Models** — public datasets/ML models need Dataset Group review.
   We don't bundle models; templates *reference* HF model IDs (e.g. Qwen) pulled
   at deploy time. Confirm this "reference, not include" reading with the reviewer.

## 2. Third-party licensing — status

`THIRD-PARTY-LICENSES` (next to `LICENSE`, and a `NOTICE` if required) splits by
the guide's *included* vs *referenced* distinction:

- **(A) Included / vendored → attribution + full license text + REVIEW.** One file:
  `platform/services/inference-gateway/gie-crds.yaml` (Gateway API Inference
  Extension CRDs, Apache-2.0). Because it is non-Amazon content *included* in the
  repo, the guide says the project **requires review**, and it prefers we **not
  vendor** it. **Decision needed (see §5.3):** reference the CRDs from the upstream
  release instead of bundling, or keep + attribute + accept the review.
- **(B) Referenced (images/charts pulled at deploy) → evaluate + attribute.**
  vLLM, LiteLLM, oauth2-proxy, GPU Operator, Argo CD, KRO, Karpenter, llm-d, EKS
  Blueprints, curl, Langfuse, Open WebUI. All attributed in `THIRD-PARTY-LICENSES`.

Before publish: replace the abbreviated Apache-2.0 body in `THIRD-PARTY-LICENSES`
with the full upstream text; add a `NOTICE` file if the reviewer requires one.

## 3. Content & IP scrub (mechanical)

- [ ] Scrub git **history** (not just HEAD) for internal hostnames, account IDs,
      ARNs, and any real `*.tfvars` (gitignored now — confirm never committed).
- [ ] Confirm example values (region, ARNs, repo URLs) are placeholders.
- [ ] Commit the CI workflow (`.github/workflows/ci.yaml`, currently untracked)
      and confirm it passes.
- [ ] The import to the public repo is typically a **clean/squashed** history, not
      the internal development history.

## 4. Security review

Required before the repo goes public: AppSec review (or PCSR for SMGS). The repo
already ships secure defaults (internal ALB, SSO, network policies, no committed
secrets) — surface those in the review.

## 5. LEGAL REVIEW REQUIRED — not decisions to make unilaterally

The guide is explicit: *"Only licenses that are pre-approved for distribution can
be approved without engaging a lawyer."*

1. **Open WebUI (highest priority) → likely needs a lawyer.** Since v0.6.6 it's
   **BSD-3-Clause + a branding-protection clause** — NOT a standard OSI license.
   Removing/altering "Open WebUI" branding is a material breach for 50+ user
   deployments. An AWS-published sample featuring a non-OSI, branding-restricted
   component almost certainly trips the "not pre-approved → lawyer" rule. Options:
   keep + document prominently, make it opt-in, or swap the chat UI.
2. **Langfuse** — MIT core + commercially-licensed `ee` features. Confirm the
   deployed config enables only MIT functionality.
3. **Vendored GIE CRDs** — the guide prefers referencing over bundling. Decide:
   reference the upstream CRD release (removes an "included" review item), or keep
   + attribute (accepts the review).
4. **License = MIT-0** — confirmed by the OpenSourcerer MIT-0 template. The repo
   currently ships plain **MIT** with an Amazon copyright; replace `LICENSE` with
   the MIT-0 text at/after repo creation.
5. **Trademarks** — confirm nominative/attribution-only use of the product names.

## Where we are

Repo hygiene + attribution are in place; the blockers are the **org decision (§0)**,
the **legal items (§5)** — Open WebUI especially — and the **internal
OpenSourcerer self-cert + security review**, none of which are code changes.
