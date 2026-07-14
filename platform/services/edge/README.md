# CloudFront edge (opt-in) — public HTTPS for the platform UIs

This adds a public, HTTPS front for the platform UIs using **CloudFront via ACK**,
connecting to the platform's **internal** ALB over a **CloudFront VPC origin**. It
gives each UI a free `*.cloudfront.net` HTTPS URL with **no domain or ACM
certificate**, which is what makes browser-based SSO work publicly (OIDC needs HTTPS
callbacks) — while the ALB itself stays private (no public IP).

Without this, the UIs are reachable via `./platformctl tunnel` (the Cognito
app-client callbacks default to `http://localhost:<port>`). CloudFront is the
enhancement for public access.

## Why a VPC origin
The platform ships the shared ALB as `scheme: internal` (see
`platform/config/ingress.yaml`) — it has no public DNS, so a classic CloudFront
custom origin can't reach it. A **VPC origin** lets CloudFront connect to the ALB
privately from inside the VPC, so the only public surface is CloudFront (HTTPS +
SSO). Each UI is on its own ALB port, so there are **four VPC origins** (one per
port: WebUI 8080 / LiteLLM 4000 / Langfuse 3000 / dashboard 9090), each paired with
its Distribution. (Verified against the live ACK CRDs with
`kubectl apply --dry-run=server`.)

## Why it's opt-in
Each `Distribution` + `VPCOrigin` is a **real, billable** resource. This directory
is deliberately **not** in the platform ApplicationSet
(`argocd/bootstrap/platform.yaml`), so nothing is created until you opt in.

## Activate

1. **Create the reconcile IAM role.** The `reconcile-edge` Job resolves the ALB ARN
   from its hostname, which needs `elasticloadbalancing:DescribeLoadBalancers`.
   Create a minimal IRSA role with that single (read-only) permission and a trust
   policy for the `reconcile-edge` ServiceAccount in `ai-platform`, then set its ARN
   in the `eks.amazonaws.com/role-arn` annotation in `reconcile-edge.yaml`.

2. **Add the app** to the `list` generator in `argocd/bootstrap/platform.yaml`:
   ```yaml
   - name: edge
     type: directory
     path: platform/services/edge
   ```
   Commit + push; ArgoCD creates the `VPCOrigin` + `Distribution` CRs and the
   `reconcile-edge` Job.

3. **The `reconcile-edge` Job** runs automatically: it reads the ALB hostname off the
   shared `ai-platform` ingress, resolves the ALB ARN, sets that ARN on each
   `VPCOrigin`, waits for CloudFront to assign a VPC-origin id (`.status.id`), points
   each `Distribution`'s origin at its VPC origin id + the ALB hostname, waits for ACK
   to publish the `*.cloudfront.net` domains, writes those into the `sso-secrets`
   `*-public-url` keys, and prints the `sso_public_urls` block for step 5.

4. **Open the ALB security group to the VPC origin.** CloudFront's VPC origin reaches
   the internal ALB from an elastic network interface inside your VPC, so the ALB
   security group must allow inbound on the listener ports (8080/4000/3000/9090) from
   that traffic. With the internal default, set the shared `inbound-cidrs` (all four
   ingresses) to your **VPC CIDR** (or the specific VPC-origin subnets) so the
   in-VPC network interface can reach the ALB. Keep the four values identical.

5. **Link the CloudFront domains to Cognito callbacks** (Cognito is Terraform-owned):
   copy the printed block into your tfvars and re-apply:
   ```hcl
   sso_public_urls = {
     open-webui = "https://dXXXX.cloudfront.net"
     litellm    = "https://dYYYY.cloudfront.net"
     langfuse   = "https://dZZZZ.cloudfront.net"
   }
   ```
   `terraform apply` updates the Cognito app-client callback/logout URLs.

6. **Flip oauth2-proxy to secure cookies** (now that it's HTTPS): set
   `--cookie-secure=true` in `platform/services/cluster-dashboard/oauth2-proxy.yaml`.

## Notes
- All four VPC origins point at the **same** internal ALB, differing only by port
  (WebUI 8080 / LiteLLM 4000 / Langfuse 3000 / dashboard 9090).
- Cache is disabled (managed `CachingDisabled` + `AllViewer`), required for dynamic,
  authenticated apps.
- Teardown: delete the `Distribution` CRs first, then the `VPCOrigin` CRs (ACK
  disables + removes the real CloudFront distributions and VPC origins — takes a few
  minutes each) before removing the app. A VPC origin can't be deleted while a
  distribution still references it.
