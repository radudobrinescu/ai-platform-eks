# Security Policy

## Reporting a Vulnerability

If you discover a potential security issue in this project, we ask that you notify
AWS/Amazon Security via our
[vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/)
or directly via email to aws-security@amazon.com. Please do **not** create a public
GitHub issue.

## Deploying this project securely

This is a **sample**. It provisions real, billable infrastructure and, by design,
exposes UIs on the public internet behind an IP-allowlisted load balancer. Before you
deploy, review and harden the following for your environment:

- **Internet-facing ALB.** The platform UIs (Open WebUI, LiteLLM, Langfuse, dashboard)
  sit behind an internet-facing Application Load Balancer restricted by an **IP
  allowlist**. Set the allowlist to your own IP ranges before applying — **never leave
  it open to `0.0.0.0/0`**. Prefer `./platformctl tunnel` for private access where
  possible.
- **Secrets.** Provide model tokens, API keys, and other secrets via the mechanisms
  described in the README (tfvars / Kubernetes secrets), never by committing them.
  `*.tfvars` (except `example*.tfvars`) and state files are git-ignored — keep it that
  way.
- **Cost.** GPU nodes and the EKS control plane incur significant cost. Use the
  teardown steps in the README to remove everything when finished.
- **Least privilege.** Review the IAM roles and Kubernetes RBAC the platform creates
  and scope them down for production use.

These are starting points, not a complete production hardening guide. Review against
your own security and compliance requirements before any non-experimental use.
