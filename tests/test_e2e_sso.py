"""Static tests for the SSO/UI login checks (ops/lib/e2e_sso.py). No network.

The live behaviour is proven by the E2E gate itself; these keep the pieces that
are easy to break by accident honest: the Cognito hosted-UI form parser, the URL
encoding that real browsers do and urllib does not, the role expectations that
mirror terraform/30.eks/30.cluster/cognito.tf, and the registry wiring.
"""

import os
import re

import e2e
import e2e_sso

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

COGNITO_FORM = """
<form name="cognitoSignInForm" method="post"
      action="/login?client_id=abc&amp;redirect_uri=http%3A%2F%2Flocalhost%3A8080%2Foauth%2Foidc%2Fcallback&amp;response_type=code&amp;scope=openid+email+profile&amp;state=xyz">
  <input name="_csrf" type="hidden" value="csrf-token-1"/>
  <input name="username" type="text"/>
  <input name="password" type="password"/>
  <input name="signInSubmitButton" type="Submit" value="Sign in"/>
</form>
"""


def test_login_form_parser_finds_action_and_hidden_fields():
    p = e2e_sso._LoginForm()
    p.feed(COGNITO_FORM)
    form = next(f for f in p.forms if {"username", "password"} <= f["names"])
    assert form["action"].startswith("/login?client_id=abc&redirect_uri=")
    assert form["fields"] == {"_csrf": "csrf-token-1"}


def test_safe_url_encodes_spaces_but_keeps_query_structure():
    u = e2e_sso._safe_url("https://x.auth.eu-central-1.amazoncognito.com/login?scope=openid email profile&state=a%2Fb")
    assert " " not in u and "scope=openid%20email%20profile" in u and "state=a%2Fb" in u


def test_jwt_claims_decodes_payload():
    import base64
    import json
    payload = base64.urlsafe_b64encode(json.dumps({"user_role": "proxy_admin"}).encode()).rstrip(b"=").decode()
    assert e2e_sso._jwt_claims(f"h.{payload}.s") == {"user_role": "proxy_admin"}


def test_role_maps_match_terraform_cognito_config():
    with open(os.path.join(REPO_ROOT, "terraform/30.eks/30.cluster/cognito.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    for group, role in e2e_sso.LITELLM_ROLES.items():
        assert re.search(rf'"{group}"\s*=\s*"{role}"', tf), f"{group}->{role} not in cognito.tf"
    for email, group in e2e_sso.SEED_GROUPS.items():
        assert re.search(rf'"{re.escape(email)}"\s*=\s*"{group}"', tf), f"seed user {email} not in cognito.tf"
    with open(os.path.join(REPO_ROOT, "platform/services/cluster-dashboard/oauth2-proxy.yaml"), encoding="utf-8") as fh:
        proxy = fh.read()
    assert set(re.findall(r"--allowed-group=(\S+)", proxy)) == e2e_sso.DASHBOARD_ALLOWED


def test_ui_callbacks_match_terraform():
    with open(os.path.join(REPO_ROOT, "terraform/30.eks/30.cluster/cognito.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    for name, (origin, _svc, _port) in e2e_sso.UIS.items():
        port = origin.split(":")[1]
        assert f"port = {port}" in tf, f"{name}: localhost:{port} not a registered callback port"


def test_sso_checks_are_registered_and_never_echo_passwords():
    names = {n for _, n, _, _ in e2e.CHECKS}
    for n in ("sso-seed-users-provisioned", "sso-login-open-webui", "sso-login-litellm-ui",
              "sso-login-langfuse", "sso-login-dashboard-oauth2-proxy", "sso-wrong-password-rejected",
              "sso-single-sign-on-across-uis", "sso-openwebui-chat-cost-attributed",
              "argocd-ui-identity-center-redirect"):
        assert n in names, n
    with open(os.path.join(REPO_ROOT, "ops/lib/e2e_sso.py"), encoding="utf-8") as fh:
        src = fh.read()
    # Passwords are only ever passed into the login form, never formatted into output.
    assert not re.search(r"f\"[^\"]*\{(pw|password)[^}]*\}", src)
    assert "verify_mode = ssl.CERT_NONE" not in src and "check_hostname = False" not in src
