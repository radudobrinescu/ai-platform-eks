# Identity & Cost Attribution — SSO, RBAC, and per-user cost via Cognito

**Status**: Proposed — **design validated (Phase 0 complete)**, ready to build ·
**Priority**: High (completeness pillar: identity & access) · **Effort**: Medium–High
**Date added**: 2026-07-13

## Summary

Give the platform one identity plane: users sign in with SSO, access is driven by
role, and cost is attributed **per user** — correlated across the IdP, Open WebUI,
LiteLLM, and Langfuse. Ships working out of the box on **Amazon Cognito** (seed
users, no external IdP required); enterprises optionally federate their own IdP into
Cognito. Identity Center stays required **only** for ArgoCD SSO.

**The primary goal is cost *visibility* per user, not hard per-user limits.**

## Goals / non-goals

**In scope (v1):**
- Cognito user pool as the shipped OIDC provider (Hosted UI, seed users, groups).
- CloudFront (HTTPS) in front of the existing HTTP ALB, so OIDC works and the UIs
  are public-but-authenticated (no IP allowlist).
- SSO on **Open WebUI**, **LiteLLM admin UI**, **Langfuse**; **oauth2-proxy** to
  protect the **cluster dashboard** (it has no native auth).
- **Per-user cost attribution** for interactive traffic (Open WebUI → LiteLLM).
- Optional external-IdP federation into Cognito.
- Break-glass local admins retained on LiteLLM/Langfuse.

**Deferred (later):**
- Developer *scoped* Langfuse read (per-team projects).
- Programmatic per-user API via JWT.
- Per-user hard budgets / throttles (the same identity plumbing enables it later).
- Option B (ArgoCD on Cognito to drop the Identity Center requirement).

## Role model & access matrix

| Role | Cognito group | ArgoCD | LiteLLM UI | Langfuse | Open WebUI | Dashboard |
|---|---|---|---|---|---|---|
| **Platform admin** | `ai-platform-admins` | admin | admin (`proxy_admin`) | full | admin | ✅ |
| **Developer** | `ai-platform-developers` | — | — | (deferred: scoped read) | user | ✅ |
| **End user** | `ai-platform-users` | — | — | — | user | ❌ |

"Platform users" for the dashboard = **admins + developers** (the platform's
operators/builders), not end-users. The dashboard's allowed groups are a
configurable list (default: admins + developers).

## Architecture

```
                         ┌── OIDC ──> Cognito Hosted UI ──(optional SAML/OIDC)──> enterprise IdP
 users ── HTTPS ─> CloudFront ─(HTTP)─> ALB (group ai-platform) ─> Open WebUI / LiteLLM / Langfuse
                         │                                        └─> oauth2-proxy ─> cluster dashboard
 platform admins ─────────────────────> ArgoCD ──(existing)──> Identity Center ─> enterprise IdP
```

- **CloudFront**: one distribution per UI → ALB:port (WebUI 8080 / LiteLLM 4000 /
  Langfuse 3000 / dashboard 9090). Default `*.cloudfront.net` cert = free HTTPS, no
  domain/ACM needed. Cache **disabled** (CachingDisabled, forward all
  headers/cookies/query) — a CDN cache config breaks auth.
- **No IP allowlist**: access control is authentication. The current temporary
  `alb.ingress.kubernetes.io/inbound-cidrs` is removed. Lock the ALB to accept only
  CloudFront (managed prefix list or shared-secret header).
- **Cognito Hosted UI** provides the login page, MFA, forgot-password, and
  federated-IdP buttons out of the box (free prefix domain).

## Per-component design (validated)

| Component | Mechanism |
|---|---|
| **Open WebUI** | OIDC: `OPENID_PROVIDER_URL`, `OAUTH_CLIENT_ID/SECRET`, `ENABLE_OAUTH_SIGNUP`; roles: `OAUTH_ROLES_CLAIM=cognito:groups` + `OAUTH_ADMIN_ROLES=ai-platform-admins`; `WEBUI_URL=<cloudfront-url>`; **`ENABLE_FORWARD_USER_INFO_HEADERS=true`** |
| **Per-user cost** | Open WebUI forwards `X-OpenWebUI-User-Id/Email`; LiteLLM `litellm_settings.extra_spend_tag_headers: ["x-openwebui-user-id"]` captures it as a spend tag → per-user spend in `/spend/logs`, `/global/spend/report`, DB. No per-user keys. |
| **LiteLLM admin UI** | `GENERIC_CLIENT_ID/SECRET`, `GENERIC_AUTHORIZATION/TOKEN/USERINFO_ENDPOINT` (Cognito), `PROXY_BASE_URL=<cloudfront-url>`; map `ai-platform-admins` → `proxy_admin`; keep master key as break-glass |
| **Langfuse** | Helm `additionalEnv`: `AUTH_CUSTOM_CLIENT_ID/SECRET`, `AUTH_CUSTOM_ISSUER`, `AUTH_CUSTOM_SCOPE`, `AUTH_CUSTOM_ALLOW_ACCOUNT_LINKING=true` (merges SSO into the existing break-glass admin); `NEXTAUTH_URL=<cloudfront-url>` |
| **Cluster dashboard** | **oauth2-proxy** (Cognito OIDC, `--oidc-groups-claim=cognito:groups`, `--allowed-group=ai-platform-admins,ai-platform-developers`) in front of the dashboard Service; no native auth needed |
| **Cognito** | User pool + prefix domain + 3 app clients (+ 1 for oauth2-proxy) + 3 groups + 3 seed users (generated passwords as TF outputs); optional external IdP |

## Where it lives (Terraform + manifests)

- **`terraform/30.eks/30.cluster/cognito.tf`** (new): user pool, domain, app clients,
  groups, seed users (reuse the existing `random_password` + output pattern, as with
  `langfuse_init_user`), optional external-IdP federation variable. Write each app's
  client id/secret into K8s secrets — **co-located with the existing secret creation
  in `capabilities.tf`**.
- **`terraform/30.eks/30.cluster/edge.tf`** (new): CloudFront distributions per UI,
  origin = the shared ALB, CachingDisabled, output HTTPS URLs. (Optional WAF managed
  rules — **WebACL must be created in `us-east-1`, CLOUDFRONT scope**, via a
  `provider` alias; a Terraform gotcha to bake in.)
- **Manifests**: env additions to `open-webui.yaml`, `litellm.yaml` (+ `config.yaml`
  `extra_spend_tag_headers`), `langfuse/helm-values.yaml`; new `oauth2-proxy`
  deployment + ingress wiring for the dashboard; **remove** the temporary
  `inbound-cidrs`.
- **Opt-in flag**: follow the existing pattern — an `enable_sso` capability. Per the
  "works out of the box for all forkers" goal, default **on** (Cognito + seed users);
  external-IdP federation is the optional part.

## Things noticed (must handle, or it breaks)

1. **Public base URLs must be the CloudFront URLs**, not the internal ALB host:
   `WEBUI_URL`, Langfuse `NEXTAUTH_URL`, LiteLLM `PROXY_BASE_URL`, oauth2-proxy
   redirect. Langfuse currently reconciles `NEXTAUTH_URL` from the ingress hostname at
   runtime — that must point at CloudFront.
2. **The dashboard discovers the other UIs' URLs from the ingress hostname at
   runtime** — it must instead use the CloudFront URLs (feed them via config).
3. **Cognito callback URLs = the CloudFront URLs** → ordering: create CloudFront →
   read domains → set Cognito app-client callbacks + each app's redirect (one apply).
4. **CloudFront cache must be disabled** for these dynamic/auth apps.
5. **oauth2-proxy** needs a cookie secret + Cognito client secret (K8s secret via TF).
6. **LiteLLM SSO admin-only** and **Langfuse default org role** on auto-provision are
   the two detail-level configs to pin during the PoC (both feasible).
7. **LiteLLM SSO** has had opaque-token/JWT edge cases across versions; verify SSO on
   the pinned `v1.81.9` (Cognito returns JWT access tokens, so likely fine).

## Phased plan (PoC-gated)

1. **Terraform**: Cognito (pool, domain, clients, groups, seed users) + CloudFront
   (per-UI) + secrets; `enable_sso` flag.
2. **PoC gate**: Cognito + CloudFront + **Open WebUI only** → log in as the seed
   developer → **confirm per-user spend appears in LiteLLM**. Prove the cost path
   before rolling out.
3. **Extend**: LiteLLM UI SSO (admin-only) + Langfuse SSO + group→role.
4. **Dashboard**: oauth2-proxy (admins + developers).
5. **Enterprise toggle + docs**: external-IdP federation variable; document the
   Cognito-default vs bring-your-own-IdP paths.
6. **Dashboard cost view**: per-user cost from LiteLLM spend data.

## Teardown additions

CloudFront distributions (disable → delete, a few minutes each), the Cognito user
pool + domain, oauth2-proxy, and any WAF WebACL (us-east-1). Add to the README
teardown notes.

## Validation status

Phase 0 desk-validation complete: Open WebUI OIDC + forward-headers, LiteLLM UI
`GENERIC_*` SSO + `extra_spend_tag_headers`, Langfuse `AUTH_CUSTOM_*`, oauth2-proxy
Cognito groups, Cognito Hosted UI, and the shared-HTTP-ALB ingress model are all
confirmed. No pre-existing CloudFront/Cognito/WAF/SSO code in the repo — greenfield.

## Implementation status (2026-07-13)

**Built + code-validated (terraform validate / YAML + embedded-config parse):**
- `terraform/30.eks/30.cluster/cognito.tf` — pool, hosted-UI domain, 3 groups,
  4 app clients, 3 seed users (passwords as `sso_seed_user_passwords` output),
  `sso-secrets` + `oauth2-proxy-secrets`. Gated on `enable_sso` (default on).
- Open WebUI, LiteLLM (+ `extra_spend_tag_headers`), Langfuse (`AUTH_CUSTOM_*`)
  SSO wiring — all via **optional** secret refs (push-safe; apps keep local
  login until Terraform creates the secrets).
- `oauth2-proxy` Deployment/Service + dashboard ingress routed through it
  (admins + developers).
- `example.tfvars` documents `enable_sso` + `sso_public_urls`.

**Out-of-the-box behavior:** on a fresh `platformctl up`, Terraform creates the
Cognito secrets before ArgoCD syncs, so SSO activates automatically. Access is via
**`platformctl tunnel`** (Cognito permits `http://localhost` callbacks — the
app-client callback URLs default to the localhost tunnel ports). Per-user cost
appears in LiteLLM spend reports once users chat through Open WebUI.

**Remaining — CloudFront public HTTPS (opt-in, implemented via ACK):** the ALB is
created by the in-cluster LB controller *after* Terraform first runs, so CloudFront
can't reference it in the same apply. The edge is therefore **two-phase Terraform**:
the CloudFront VPC origins + distributions (in `terraform/30.eks/30.cluster/edge.tf`)
are gated behind `enable_cloudfront_edge` and applied *after* `up` — a `data "aws_lb"`
discovers the ALB by its ingress-group tags, so a targeted re-apply
(`./platformctl edge cloudfront`) picks up the live ALB. It is **opt-in** because each
distribution is billable. Terraform writes the resulting `*.cloudfront.net` domains
straight into the Cognito callback/logout URLs and the app `*-public-url` secrets —
no manual `sso_public_urls` step — and the ALB stays internal. (An earlier iteration
used ACK `Distribution` CRDs, but the managed ACK CloudFront controller can't create
VPC origins — `cloudfront:CreateVpcOrigin` is outside its permission set and isn't
extensible — so Terraform owns the edge.) Until activated, the temporary ALB
`inbound-cidrs` allowlist stays.

**Live end-to-end validation** (login flow + per-user spend) requires
`terraform apply` against a real account and cannot be exercised in this
environment; the code is structured and validated for a clean fresh-deploy.

## Update (2026-07-14) — per-user budgets/keys shipped + internal-ALB edge

Two items previously listed under *Deferred* are now implemented and (for the chat
path) validated live:

- **Per-user hard budgets + rate limits (chat path, no keys).** Open WebUI forwards
  the signed-in identity; LiteLLM `general_settings.user_header_mappings` maps
  `x-openwebui-user-email` to a LiteLLM "customer" (end-user), and
  `litellm_settings.max_end_user_budget_id` binds every end-user to a default budget
  tier ($10 / 30d + 60 rpm + 100k tpm) created idempotently by the
  `litellm-enduser-budget` Job (`POST /budget/new`). **Empirically proven** on the
  live proxy: with only the forwarded header (no body `user`), a second over-budget
  request returns `ExceededBudget: End User … over budget`, and `/customer/info`
  shows the mapped customer with tracked spend.
- **Per-user API keys (programmatic path).** SSO users are internal_users (role from
  `custom:role`); `max_internal_user_budget` + `internal_user_budget_duration` give
  each a default personal budget, and `upperbound_key_generate_params` caps any key
  they self-generate. (We deliberately do **not** set
  `default_internal_user_params.user_role`, which would override the working
  `custom:role` mapping and could downgrade the seed admin.)

**Networking correction vs. the original design above.** The platform now ships the
shared ALB as **`scheme: internal`** (no public IP) — the secure default — instead
of internet-facing + IP-allowlist. Consequently the CloudFront edge was rebuilt from
a **public domain origin** to a **CloudFront VPC origin** (four `VPCOrigin` resources,
one per ALB listener port) that reaches the private ALB from inside the VPC; the
`reconcile-edge` Job now resolves the ALB **ARN** (IRSA:
`elasticloadbalancing:DescribeLoadBalancers`), sets it on each `VPCOrigin`, and points
each `Distribution` at the VPC-origin id. The `inbound-cidrs` allowlist is **retained**
(not removed as the original design assumed) and, for the edge, is scoped to the VPC
so CloudFront's in-VPC network interface can reach the ALB. All edge manifests are
`kubectl apply --dry-run=server`-validated against the live ACK CRDs; a full public
end-to-end run creates billable CloudFront resources and hasn't been exercised here.
