################################################################################
# Identity plane — Amazon Cognito (SSO for the platform UIs + per-user cost)
#
# Ships a working OIDC identity provider out of the box. SSO works immediately
# via `platformctl tunnel` (Cognito permits http://localhost callback URLs);
# set var.sso_public_urls once a public HTTPS front (CloudFront) is deployed to
# add the public callbacks. Enterprise: federate an external IdP into this pool
# (not created here — an opt-in add-on). Identity Center stays required only for
# ArgoCD SSO.
#
# Everything here is gated on `local.enable_sso` and is inert until `apply`.
# The app manifests consume the `sso-secrets`/`oauth2-proxy-secrets` created
# below via OPTIONAL secret refs, so a cluster without SSO enabled still boots.
################################################################################

locals {
  enable_sso = local.capabilities.gitops && var.enable_sso

  # Per-app OIDC callback/logout paths + the local tunnel port. localhost
  # callbacks make SSO usable out of the box via `platformctl tunnel`.
  sso_apps = {
    "open-webui"   = { port = 8080, callback = "/oauth/oidc/callback", logout = "/" }
    "litellm"      = { port = 4000, callback = "/sso/callback", logout = "/" }
    "langfuse"     = { port = 3000, callback = "/api/auth/callback/custom", logout = "/" }
    "oauth2-proxy" = { port = 9090, callback = "/oauth2/callback", logout = "/" }
  }

  # callback/logout URL lists = localhost tunnel + optional public (CloudFront) URL.
  sso_callbacks = {
    for app, cfg in local.sso_apps : app => compact([
      "http://localhost:${cfg.port}${cfg.callback}",
      try("${var.sso_public_urls[app]}${cfg.callback}", ""),
    ])
  }
  sso_logouts = {
    for app, cfg in local.sso_apps : app => compact([
      "http://localhost:${cfg.port}${cfg.logout}",
      try("${var.sso_public_urls[app]}${cfg.logout}", ""),
    ])
  }

  sso_groups = ["ai-platform-admins", "ai-platform-developers", "ai-platform-users"]

  # Seed users (one per role). Passwords are generated + surfaced as outputs.
  # example.com (RFC 2606 documentation domain) is used deliberately: LiteLLM's
  # SSO email validator rejects reserved/special-use domains like .local, so the
  # seed emails must use a normal domain. No email is ever sent (admin-created
  # users with a permanent password + email_verified=true).
  sso_seed_users = {
    "admin@example.com"     = "ai-platform-admins"
    "developer@example.com" = "ai-platform-developers"
    "user@example.com"      = "ai-platform-users"
  }

  cognito_domain_prefix = local.enable_sso ? "${local.cluster_name}-${random_string.cognito_domain_suffix[0].result}" : ""
  cognito_issuer_url    = local.enable_sso ? "https://cognito-idp.${local.region}.amazonaws.com/${aws_cognito_user_pool.platform[0].id}" : ""
  cognito_hosted_ui_url = local.enable_sso ? "https://${aws_cognito_user_pool_domain.platform[0].domain}.auth.${local.region}.amazoncognito.com" : ""
}

# Cognito prefix domains are globally unique across all AWS accounts — a random
# suffix avoids collisions between forkers using the same resources_prefix.
resource "random_string" "cognito_domain_suffix" {
  count   = local.enable_sso ? 1 : 0
  length  = 6
  special = false
  upper   = false
}

resource "aws_cognito_user_pool" "platform" {
  count = local.enable_sso ? 1 : 0

  name                     = "${local.cluster_name}-ai-platform"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_uppercase = true
    require_numbers   = true
    require_symbols   = true
  }

  # Seed users are admin-created; forkers add more via the console/CLI.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  tags = local.tags
}

resource "aws_cognito_user_pool_domain" "platform" {
  count        = local.enable_sso ? 1 : 0
  domain       = local.cognito_domain_prefix
  user_pool_id = aws_cognito_user_pool.platform[0].id
}

resource "aws_cognito_user_group" "groups" {
  for_each = local.enable_sso ? toset(local.sso_groups) : toset([])

  name         = each.value
  user_pool_id = aws_cognito_user_pool.platform[0].id
  description  = "AI platform role group: ${each.value}"
}

resource "aws_cognito_user_pool_client" "apps" {
  for_each = local.enable_sso ? local.sso_apps : {}

  name         = "${local.cluster_name}-${each.key}"
  user_pool_id = aws_cognito_user_pool.platform[0].id

  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]

  callback_urls = local.sso_callbacks[each.key]
  logout_urls   = local.sso_logouts[each.key]

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # Expose the group membership on tokens so each app maps cognito:groups -> role.
  read_attributes = ["email", "email_verified"]
}

# Seed-user passwords (permanent, generated). Cognito-safe symbol set.
resource "random_password" "sso_seed_users" {
  for_each = local.enable_sso ? local.sso_seed_users : {}

  length           = 20
  min_lower        = 2
  min_upper        = 2
  min_numeric      = 2
  min_special      = 2
  override_special = "!@#$%^&*()-_=+"
}

resource "aws_cognito_user" "seed" {
  for_each = local.enable_sso ? local.sso_seed_users : {}

  user_pool_id = aws_cognito_user_pool.platform[0].id
  username     = each.key
  password     = random_password.sso_seed_users[each.key].result

  attributes = {
    email          = each.key
    email_verified = "true"
  }
}

resource "aws_cognito_user_in_group" "seed" {
  for_each = local.enable_sso ? local.sso_seed_users : {}

  user_pool_id = aws_cognito_user_pool.platform[0].id
  group_name   = aws_cognito_user_group.groups[each.value].name
  username     = aws_cognito_user.seed[each.key].username
}

# Cookie secret for oauth2-proxy (must be 16/24/32 bytes for AES).
resource "random_password" "oauth2_proxy_cookie" {
  count   = local.enable_sso ? 1 : 0
  length  = 32
  special = false
}

# SSO config consumed by Open WebUI / LiteLLM / Langfuse (via OPTIONAL refs, so
# clusters without SSO still boot). Client id/secret per app + issuer + domain.
resource "kubernetes_secret" "sso_secrets" {
  count = local.enable_sso ? 1 : 0

  metadata {
    name      = "sso-secrets"
    namespace = "ai-platform"
  }

  data = merge(
    {
      "issuer"            = local.cognito_issuer_url
      "hosted-ui-domain"  = local.cognito_hosted_ui_url
      "openid-config-url" = "${local.cognito_issuer_url}/.well-known/openid-configuration"
      "authorize-url"     = "${local.cognito_hosted_ui_url}/oauth2/authorize"
      "token-url"         = "${local.cognito_hosted_ui_url}/oauth2/token"
      "userinfo-url"      = "${local.cognito_hosted_ui_url}/oauth2/userInfo"
      "admin-group"       = "ai-platform-admins"
      # Public base URL per UI. Defaults to the localhost tunnel URL (so SSO
      # works out of the box via `platformctl tunnel`); overridden by CloudFront.
      "open-webui-public-url" = try(var.sso_public_urls["open-webui"], "http://localhost:8080")
      "litellm-public-url"    = try(var.sso_public_urls["litellm"], "http://localhost:4000")
      "langfuse-public-url"   = try(var.sso_public_urls["langfuse"], "http://localhost:3000")
    },
    { for app in keys(local.sso_apps) : "${app}-client-id" => aws_cognito_user_pool_client.apps[app].id },
    { for app in keys(local.sso_apps) : "${app}-client-secret" => aws_cognito_user_pool_client.apps[app].client_secret },
  )

  depends_on = [kubernetes_namespace.ai_platform]
}

# Dedicated secret for the dashboard's oauth2-proxy (client + cookie secret).
resource "kubernetes_secret" "oauth2_proxy_secrets" {
  count = local.enable_sso ? 1 : 0

  metadata {
    name      = "oauth2-proxy-secrets"
    namespace = "ai-platform"
  }

  data = {
    "client-id"     = aws_cognito_user_pool_client.apps["oauth2-proxy"].id
    "client-secret" = aws_cognito_user_pool_client.apps["oauth2-proxy"].client_secret
    "cookie-secret" = random_password.oauth2_proxy_cookie[0].result
    "issuer"        = local.cognito_issuer_url
    "redirect-url"  = "${try(var.sso_public_urls["oauth2-proxy"], "http://localhost:9090")}/oauth2/callback"
  }

  depends_on = [kubernetes_namespace.ai_platform]
}

################################################################################
# Outputs
################################################################################
output "sso_hosted_ui_url" {
  description = "Cognito Hosted UI (login page) base URL."
  value       = local.enable_sso ? local.cognito_hosted_ui_url : null
}

output "sso_issuer_url" {
  description = "Cognito OIDC issuer URL (OPENID_PROVIDER_URL / discovery base)."
  value       = local.enable_sso ? local.cognito_issuer_url : null
}

output "sso_user_pool_id" {
  description = "Cognito user pool ID."
  value       = local.enable_sso ? aws_cognito_user_pool.platform[0].id : null
}

output "sso_seed_user_passwords" {
  description = "Generated passwords for the SSO seed users (email -> password). Sign in at the Hosted UI or the platform UIs."
  value       = { for email in keys(local.sso_seed_users) : email => try(random_password.sso_seed_users[email].result, null) }
  sensitive   = true
}
