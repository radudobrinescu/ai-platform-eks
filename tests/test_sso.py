"""Static tests for the SSO/UI login checks (harness/sso.py). No network.

Ported from v1 tests/test_e2e_sso.py. The role expectations must keep matching
the subject's Cognito Terraform and oauth2-proxy config.
"""

import base64
import json
import os
import re

import checks
import sso

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    p = sso._LoginForm()
    p.feed(COGNITO_FORM)
    form = next(f for f in p.forms if {"username", "password"} <= f["names"])
    assert form["action"].startswith("/login?client_id=abc&redirect_uri=")
    assert form["fields"] == {"_csrf": "csrf-token-1"}


def test_safe_url_encodes_spaces_but_keeps_query_structure():
    u = sso._safe_url("https://x.auth.eu-central-1.amazoncognito.com/login?scope=openid email profile&state=a%2Fb")
    assert " " not in u and "scope=openid%20email%20profile" in u and "state=a%2Fb" in u


def test_jwt_claims_decodes_payload():
    payload = base64.urlsafe_b64encode(json.dumps({"user_role": "proxy_admin"}).encode()).rstrip(b"=").decode()
    assert sso._jwt_claims(f"h.{payload}.s") == {"user_role": "proxy_admin"}


def test_role_maps_match_subject_cognito_config(subject_root):
    with open(os.path.join(subject_root, "terraform/30.eks/30.cluster/cognito.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    for group, role in sso.LITELLM_ROLES.items():
        assert re.search(rf'"{group}"\s*=\s*"{role}"', tf), f"{group}->{role} not in cognito.tf"
    for email, group in sso.SEED_GROUPS.items():
        assert re.search(rf'"{re.escape(email)}"\s*=\s*"{group}"', tf), f"seed user {email} not in cognito.tf"
    with open(os.path.join(subject_root, "platform/services/cluster-dashboard/oauth2-proxy.yaml"), encoding="utf-8") as fh:
        proxy = fh.read()
    assert set(re.findall(r"--allowed-group=(\S+)", proxy)) == sso.DASHBOARD_ALLOWED


def test_ui_callbacks_match_subject_terraform(subject_root):
    with open(os.path.join(subject_root, "terraform/30.eks/30.cluster/cognito.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    for name, (origin, _svc, _port) in sso.UIS.items():
        port = origin.split(":")[1]
        assert f"port = {port}" in tf, f"{name}: localhost:{port} not a registered callback port"


def test_sso_checks_are_registered_and_never_echo_passwords():
    names = {n for _, n, _, _ in checks.CHECKS}
    for n in ("sso-seed-users-provisioned", "sso-login-open-webui", "sso-login-litellm-ui",
              "sso-login-langfuse", "sso-login-dashboard-oauth2-proxy", "sso-wrong-password-rejected",
              "sso-single-sign-on-across-uis", "sso-openwebui-chat-cost-attributed",
              "argocd-ui-identity-center-redirect"):
        assert n in names, n
    with open(os.path.join(ROOT, "harness", "sso.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert not re.search(r"f\"[^\"]*\{(pw|password)[^}]*\}", src)
    assert "verify_mode = ssl.CERT_NONE" not in src and "check_hostname = False" not in src


def test_open_webui_login_returns_the_user_id():
    """Survival compares Open WebUI user ids before and after the upgrade."""
    with open(os.path.join(ROOT, "harness", "sso.py"), encoding="utf-8") as fh:
        assert '"id": u.get("id")' in fh.read()
