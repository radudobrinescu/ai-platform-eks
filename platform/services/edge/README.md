# CloudFront edge (opt-in) — public HTTPS for the platform UIs

This adds a public, HTTPS front for the platform UIs using **CloudFront via ACK**
(the CloudFront ACK CRDs ship with the managed `ack` capability — verified present
as `distributions.cloudfront.services.k8s.aws`). It gives each UI a free
`*.cloudfront.net` HTTPS URL with **no domain or ACM certificate**, which is what
makes browser-based SSO work publicly (OIDC needs HTTPS callbacks).

Without this, SSO still works out of the box via `./platformctl tunnel` (the
Cognito app-client callbacks default to `http://localhost:<port>`). CloudFront is
the enhancement for public access.

## Why it's opt-in
Each `Distribution` is a **real, billable** CloudFront distribution. This directory
is deliberately **not** in the platform ApplicationSet
(`argocd/bootstrap/platform.yaml`), so nothing is created until you opt in.

## Activate

1. **Add the app** to the `list` generator in `argocd/bootstrap/platform.yaml`:
   ```yaml
   - name: edge
     type: directory
     path: platform/services/edge
   ```
   Commit + push; ArgoCD creates the `Distribution` CRs and the `reconcile-edge` Job.

2. **The `reconcile-edge` Job** (reconcile-edge.yaml) runs automatically: it reads
   the ALB hostname off the shared `ai-platform` ingress, patches each
   Distribution's `origins[0].domainName`, waits for ACK to populate
   `.status.domainName` (the `*.cloudfront.net` domain), writes those domains into
   the `sso-secrets` `*-public-url` keys (so the apps emit correct OIDC redirects),
   and prints the `sso_public_urls` block for step 3.

3. **Link the CloudFront domains to Cognito callbacks** (Cognito is Terraform-owned):
   copy the printed block into your tfvars and re-apply:
   ```hcl
   sso_public_urls = {
     open-webui = "https://dXXXX.cloudfront.net"
     litellm    = "https://dYYYY.cloudfront.net"
     langfuse   = "https://dZZZZ.cloudfront.net"
   }
   ```
   `terraform apply` updates the Cognito app-client callback/logout URLs.

4. **Flip oauth2-proxy to secure cookies** (now that it's HTTPS): set
   `--cookie-secure=true` in `platform/services/cluster-dashboard/oauth2-proxy.yaml`.

5. Optionally retire the temporary ALB `inbound-cidrs` allowlist and restrict the
   ALB to the CloudFront managed prefix list.

## Notes
- All four distributions share the one internet-facing ALB (ingress group
  `ai-platform`), differing only by origin port (WebUI 8080 / LiteLLM 4000 /
  Langfuse 3000 / dashboard 9090).
- Cache is disabled (managed `CachingDisabled` + `AllViewer`), required for dynamic,
  authenticated apps.
- Teardown: delete the `Distribution` CRs (ACK disables + removes the CloudFront
  distributions — takes a few minutes each) before removing the app.
