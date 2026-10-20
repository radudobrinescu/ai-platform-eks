"""SSO / UI login checks for the E2E gate (imported by ops/lib/e2e.py).

What a person does in a browser, done over HTTP with a cookie jar: open a
platform UI, get redirected to the Cognito hosted login page, submit a seed
user's email + password, follow the OIDC authorization-code callback back to
the UI, and land signed in. No shortcuts: the Cognito app clients disable the
password grant (ALLOW_USER_PASSWORD_AUTH is off on purpose), so the hosted UI
form is the only way in, and that is exactly the path under test.

Callbacks are registered as http://localhost:<port>/... (the `platformctl
tunnel` ports). The client connects those to kubectl port-forwards on free
local ports while keeping the original Host header and cookie domain, so the
OIDC redirect_uri matches without binding the operator's real ports.

Seed-user passwords come from the Terraform output and are never logged.
"""
from __future__ import annotations

import base64
import http.client
import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

NS = "ai-platform"


def _tls_context() -> ssl.SSLContext:
    """Verified TLS for Cognito / ArgoCD. python.org builds on macOS ship
    without a CA bundle (urllib then fails every HTTPS call), so prefer
    certifi, then the OS bundle, then Python's defaults. Verification is never
    disabled."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for f in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(f):
            return ssl.create_default_context(cafile=f)
    return ssl.create_default_context()

# Logical UI origin (as registered in Cognito) -> (namespace, service, port).
UIS = {
    "open-webui": ("localhost:8080", "svc/open-webui", 8080),
    "litellm": ("localhost:4000", "svc/litellm", 4000),
    "langfuse": ("localhost:3000", "svc/langfuse-web", 3000),
    "dashboard": ("localhost:9090", "svc/oauth2-proxy", 4180),  # oauth2-proxy gate in front of the dashboard
}

SEED_GROUPS = {
    "admin@example.com": "ai-platform-admins",
    "developer@example.com": "ai-platform-developers",
    "user@example.com": "ai-platform-users",
}
# Cognito custom:role value (what Terraform writes)...
LITELLM_ROLES = {"ai-platform-admins": "proxy_admin",
                 "ai-platform-developers": "internal_user",
                 "ai-platform-users": "internal_user_view_only"}
# ...and the role LiteLLM reports in its UI session. LiteLLM's enum member
# INTERNAL_USER_VIEW_ONLY has the value "internal_user_viewer".
LITELLM_SESSION_ROLES = {**LITELLM_ROLES, "ai-platform-users": "internal_user_viewer"}
OPENWEBUI_ROLES = {"ai-platform-admins": "admin",
                   "ai-platform-developers": "user",
                   "ai-platform-users": "user"}
DASHBOARD_ALLOWED = {"ai-platform-admins", "ai-platform-developers"}
CHAT_USER = "admin@example.com"


# --------------------------------------------------------------------------- #
# HTTP client: cookie jar + localhost:<port> -> port-forward rewrite
# --------------------------------------------------------------------------- #

def _safe_url(url: str) -> str:
    """Percent-encode characters browsers tolerate but urllib rejects (e.g. the
    literal spaces in `scope=openid email profile` that NextAuth and Cognito put
    in their redirect URLs)."""
    return urllib.parse.quote(url, safe=":/?#[]@!$&'()*+,;=%~")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _safe_url(newurl))


class _Rewrite(urllib.request.HTTPHandler):
    def __init__(self, mapping: dict[str, int]):
        super().__init__()
        self.mapping = mapping

    def http_open(self, req):
        local = self.mapping.get(req.host)
        if local is None:
            return super().http_open(req)

        def factory(host, **kw):
            return http.client.HTTPConnection("127.0.0.1", local, **kw)
        return self.do_open(factory, req)


class Browser:
    """Just enough browser: cookies, redirects, forms. One per (user, flow)."""

    def __init__(self, mapping: dict[str, int], jar: http.cookiejar.CookieJar | None = None):
        self.jar = jar if jar is not None else http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            _Rewrite(mapping), _Redirect(), urllib.request.HTTPSHandler(context=_tls_context()),
            urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("User-Agent", "ai-platform-e2e/1.0 (sso check)"),
                                  ("Accept", "text/html,application/json;q=0.9,*/*;q=0.8")]

    def request(self, method: str, url: str, data: dict | None = None, headers: dict | None = None,
                json_body: dict | None = None) -> tuple[int, str, str]:
        body = None
        hdrs = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(_safe_url(url), data=body, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=60) as r:
                return r.status, r.geturl(), r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.geturl(), e.read().decode(errors="replace")

    def cookie(self, name: str) -> str | None:
        return next((c.value for c in self.jar if c.name == name), None)


class _LoginForm(HTMLParser):
    """Find the Cognito hosted-UI sign-in form (action + hidden fields)."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []
        self._cur: dict | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._cur = {"action": a.get("action", ""), "fields": {}, "names": set()}
            self.forms.append(self._cur)
        elif tag == "input" and self._cur is not None and a.get("name"):
            self._cur["names"].add(a["name"])
            if a.get("type") == "hidden":
                self._cur["fields"][a["name"]] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def _cognito_login(b: Browser, page_url: str, page_html: str, email: str, password: str) -> tuple[int, str, str]:
    """Submit the hosted-UI form found on `page_html`; returns the final response
    after following the OIDC redirect back to the app."""
    p = _LoginForm()
    p.feed(page_html)
    form = next((f for f in p.forms if {"username", "password"} <= f["names"]), None)
    if form is None:
        raise AssertionError(f"no Cognito sign-in form at {urllib.parse.urlsplit(page_url).path}")
    action = urllib.parse.urljoin(page_url, form["action"])
    fields = dict(form["fields"])
    fields.update({"username": email, "password": password, "signInSubmitButton": "Sign in"})
    return b.request("POST", action, data=fields)


def _is_cognito(url: str) -> bool:
    return ".amazoncognito.com" in urllib.parse.urlsplit(url).netloc


def _login_error(html: str) -> str | None:
    m = re.search(r'id="loginErrorMessage"[^>]*>([^<]+)<', html)
    return m.group(1).strip() if m else None


def _jwt_claims(token: str) -> dict:
    seg = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))


# --------------------------------------------------------------------------- #
# Per-app login flows. Each returns (ok, detail). They never print secrets.
# --------------------------------------------------------------------------- #

def _start_on_cognito(b: Browser, url: str) -> tuple[str, str]:
    code, final, html = b.request("GET", url)
    if not _is_cognito(final):
        raise AssertionError(f"expected a redirect to the Cognito hosted UI, landed on {urllib.parse.urlsplit(final).netloc} (HTTP {code})")
    return final, html


def login_open_webui(b: Browser, email: str, password: str) -> dict:
    page, html = _start_on_cognito(b, "http://localhost:8080/oauth/oidc/login")
    code, final, body = _cognito_login(b, page, html, email, password)
    if _is_cognito(final):
        raise AssertionError(f"Cognito rejected the login: {_login_error(body) or 'HTTP ' + str(code)}")
    token = b.cookie("token")
    if not token:
        raise AssertionError(f"no Open WebUI session after callback (landed on {urllib.parse.urlsplit(final).path}, HTTP {code})")
    code, _, me = b.request("GET", "http://localhost:8080/api/v1/auths/", headers={"Authorization": f"Bearer {token}"})
    if code != 200:
        raise AssertionError(f"/api/v1/auths/ HTTP {code}")
    u = json.loads(me)
    return {"email": u.get("email"), "role": u.get("role"), "token": token}


def login_litellm(b: Browser, email: str, password: str) -> dict:
    page, html = _start_on_cognito(b, "http://localhost:4000/sso/key/generate")
    code, final, body = _cognito_login(b, page, html, email, password)
    if _is_cognito(final):
        raise AssertionError(f"Cognito rejected the login: {_login_error(body) or 'HTTP ' + str(code)}")
    token = b.cookie("token")
    if not token:
        raise AssertionError(f"no LiteLLM UI session cookie after callback (landed on {urllib.parse.urlsplit(final).path}, HTTP {code})")
    c = _jwt_claims(token)
    return {"email": c.get("user_email") or c.get("user_id"), "role": c.get("user_role"), "landing": urllib.parse.urlsplit(final).path}


def login_langfuse(b: Browser, email: str, password: str) -> dict:
    code, _, csrf = b.request("GET", "http://localhost:3000/api/auth/csrf")
    if code != 200:
        raise AssertionError(f"/api/auth/csrf HTTP {code}")
    token = json.loads(csrf)["csrfToken"]
    code, _, signin = b.request("POST", "http://localhost:3000/api/auth/signin/custom",
                                data={"csrfToken": token, "callbackUrl": "http://localhost:3000/", "json": "true"})
    url = json.loads(signin).get("url", "") if code == 200 else ""
    if not _is_cognito(url):
        raise AssertionError(f"Langfuse did not hand off to Cognito (HTTP {code}); NEXTAUTH_URL or the custom provider is misconfigured")
    page, html = _start_on_cognito(b, url)
    code, final, body = _cognito_login(b, page, html, email, password)
    if _is_cognito(final):
        raise AssertionError(f"Cognito rejected the login: {_login_error(body) or 'HTTP ' + str(code)}")
    code, _, sess = b.request("GET", "http://localhost:3000/api/auth/session")
    user = (json.loads(sess) or {}).get("user") if code == 200 else None
    if not user:
        raise AssertionError(f"no Langfuse session after callback (landed on {urllib.parse.urlsplit(final).path})")
    orgs = user.get("organizations") or []
    return {"email": user.get("email"), "orgs": [(o.get("name"), o.get("role")) for o in orgs]}


def login_dashboard(b: Browser, email: str, password: str) -> dict:
    page, html = _start_on_cognito(b, "http://localhost:9090/")
    code, final, body = _cognito_login(b, page, html, email, password)
    if _is_cognito(final):
        raise AssertionError(f"Cognito rejected the login: {_login_error(body) or 'HTTP ' + str(code)}")
    res = {"status": code, "path": urllib.parse.urlsplit(final).path}
    ucode, _, uinfo = b.request("GET", "http://localhost:9090/oauth2/userinfo")
    if ucode == 200:
        u = json.loads(uinfo)
        res.update({"email": u.get("email"), "groups": u.get("groups")})
    dcode, _, data = b.request("GET", "http://localhost:9090/data.json")
    res["data_json"] = dcode
    res["data_ok"] = dcode == 200 and data.lstrip().startswith("{")
    return res


# --------------------------------------------------------------------------- #
# Context: port-forwards, passwords, pool
# --------------------------------------------------------------------------- #

class SsoEnv:
    def __init__(self, repo_root: str, env: str, port_forward_cls, kubectl_json, secret_value):
        self.repo_root, self.env = repo_root, env
        self.PortForward, self.kubectl_json, self.secret_value = port_forward_cls, kubectl_json, secret_value
        self._pw: dict | None = None

    def passwords(self) -> dict:
        if self._pw is None:
            out = subprocess.run(
                ["terraform", f"-chdir={os.path.join(self.repo_root, 'terraform/30.eks/30.cluster')}",
                 "output", "-json", "sso_seed_user_passwords"],
                capture_output=True, text=True, check=True, timeout=120,
                env={**os.environ, "TF_WORKSPACE": self.env, "TF_IN_AUTOMATION": "1"})
            self._pw = json.loads(out.stdout)
        return self._pw

    def pool_id(self) -> str:
        issuer = self.secret_value(NS, "sso-secrets", "issuer")
        return issuer.rstrip("/").rsplit("/", 1)[-1]

    def forwards(self):
        """Start all four port-forwards; returns (mapping, stop())."""
        pfs = {name: self.PortForward(NS, svc, port) for name, (_, svc, port) in UIS.items()}
        for pf in pfs.values():
            pf.__enter__()
        mapping = {UIS[name][0]: pf.local for name, pf in pfs.items()}

        def stop():
            for pf in pfs.values():
                pf.__exit__(None, None, None)
        return mapping, stop


# --------------------------------------------------------------------------- #
# Checks (registered by e2e.py). Signature: fn(ctx) -> (ok, detail)
# --------------------------------------------------------------------------- #

def _users(s: SsoEnv):
    pw = s.passwords()
    for email, group in SEED_GROUPS.items():
        if not pw.get(email):
            raise AssertionError(f"no seed password for {email} in terraform output")
        yield email, group, pw[email]


def chk_seed_users(c, s: SsoEnv):
    pool = s.pool_id()
    problems, ok = [], []
    for email, group in SEED_GROUPS.items():
        u = json.loads(subprocess.run(["aws", "cognito-idp", "admin-get-user", "--user-pool-id", pool, "--username", email],
                                      capture_output=True, text=True, check=True).stdout)
        attrs = {a["Name"]: a["Value"] for a in u.get("UserAttributes", [])}
        groups = [g["GroupName"] for g in json.loads(subprocess.run(
            ["aws", "cognito-idp", "admin-list-groups-for-user", "--user-pool-id", pool, "--username", email],
            capture_output=True, text=True, check=True).stdout).get("Groups", [])]
        want_role = LITELLM_ROLES[group]
        bad = []
        if u.get("UserStatus") != "CONFIRMED":
            bad.append(f"status={u.get('UserStatus')}")
        if not u.get("Enabled"):
            bad.append("disabled")
        if attrs.get("email_verified") != "true":
            bad.append("email unverified")
        if groups != [group]:
            bad.append(f"groups={groups}")
        if attrs.get("custom:role") != want_role:
            bad.append(f"custom:role={attrs.get('custom:role')}")
        (problems if bad else ok).append(f"{email}: " + (", ".join(bad) if bad else f"CONFIRMED, {group}, role {want_role}"))
    return not problems, "; ".join(problems or ok)


def _per_user(s: SsoEnv, flow, judge):
    mapping, stop = s.forwards()
    try:
        lines, failed = [], False
        for email, group, pw in _users(s):
            try:
                res = flow(Browser(mapping), email, pw)
                good, msg = judge(email, group, res)
            except Exception as e:  # noqa: BLE001 — one user's failure must not hide the others'
                good, msg = False, f"{type(e).__name__}: {e}"
            failed |= not good
            lines.append(f"{email.split('@')[0]}: {'✓' if good else '✗'} {msg}")
        return not failed, "; ".join(lines)
    finally:
        stop()


def chk_open_webui_logins(c, s: SsoEnv):
    def judge(email, group, r):
        want = OPENWEBUI_ROLES[group]
        return r["email"] == email and r["role"] == want, f"signed in as {r['email']} role={r['role']} (want {want})"
    return _per_user(s, login_open_webui, judge)


def chk_litellm_ui_logins(c, s: SsoEnv):
    def judge(email, group, r):
        want = LITELLM_SESSION_ROLES[group]
        return r["role"] == want, f"role={r['role']} (want {want})"
    return _per_user(s, login_litellm, judge)


def chk_langfuse_logins(c, s: SsoEnv):
    def judge(email, group, r):
        return r["email"] == email and bool(r["orgs"]), f"session {r['email']}, orgs {r['orgs']}"
    return _per_user(s, login_langfuse, judge)


def chk_dashboard_logins(c, s: SsoEnv):
    def judge(email, group, r):
        if group in DASHBOARD_ALLOWED:
            good = r["data_ok"] and group in (r.get("groups") or [])
            return good, f"allowed: dashboard /data.json HTTP {r['data_json']}, groups {r.get('groups')}"
        # ai-platform-users is not in --allowed-group: oauth2-proxy must refuse.
        good = r["status"] == 403 and not r["data_ok"]
        return good, f"denied as expected (HTTP {r['status']}, /data.json HTTP {r['data_json']})"
    return _per_user(s, login_dashboard, judge)


def chk_wrong_password_rejected(c, s: SsoEnv):
    mapping, stop = s.forwards()
    try:
        b = Browser(mapping)
        page, html = _start_on_cognito(b, "http://localhost:8080/oauth/oidc/login")
        code, final, body = _cognito_login(b, page, html, "user@example.com", "Wrong-Password-1!")
        rejected = _is_cognito(final) and b.cookie("token") is None
        return rejected, f"Cognito {'rejected' if rejected else 'ACCEPTED'} a wrong password ({_login_error(body) or 'HTTP ' + str(code)})"
    finally:
        stop()


def chk_single_sign_on(c, s: SsoEnv):
    """Sign in once (Open WebUI), then open Langfuse and the dashboard with the
    same browser: the Cognito session must carry over without a password."""
    mapping, stop = s.forwards()
    try:
        pw = s.passwords()["admin@example.com"]
        b = Browser(mapping)
        login_open_webui(b, "admin@example.com", pw)
        out = []
        code, _, csrf = b.request("GET", "http://localhost:3000/api/auth/csrf")
        tok = json.loads(csrf)["csrfToken"]
        code, _, signin = b.request("POST", "http://localhost:3000/api/auth/signin/custom",
                                    data={"csrfToken": tok, "callbackUrl": "http://localhost:3000/", "json": "true"})
        code, final, _ = b.request("GET", json.loads(signin)["url"])
        code, _, sess = b.request("GET", "http://localhost:3000/api/auth/session")
        lf = ((json.loads(sess) or {}).get("user") or {}).get("email")
        out.append(f"Langfuse {'✓' if lf == 'admin@example.com' and not _is_cognito(final) else '✗'} {lf}")
        code, final, _ = b.request("GET", "http://localhost:9090/")
        dcode, _, _ = b.request("GET", "http://localhost:9090/data.json")
        out.append(f"dashboard {'✓' if dcode == 200 and not _is_cognito(final) else '✗'} /data.json HTTP {dcode}")
        ok = all(x.split()[1] == "✓" for x in out)
        return ok, "after one password entry: " + "; ".join(out)
    finally:
        stop()


def chk_openwebui_chat_attribution(c, s: SsoEnv, enroll, cleanup, models_fn, litellm_key, port_forward_cls):
    """admin@example.com chats through Open WebUI with its own session token;
    LiteLLM must attribute the spend to that user (end-user/customer), proving
    the identity flows UI -> gateway for per-user cost and budgets.

    Uses the admin because Open WebUI (v0.10 and v0.12 alike) shows a model with
    no workspace access record only to admins: developer@/user@ sign in but see
    an empty model list until an admin grants access (or
    BYPASS_MODEL_ACCESS_CONTROL is set). That is a platform policy decision,
    recorded in COMPATIBILITY.md, not something this check should paper over."""
    alias = enroll()
    try:
        mapping, stop = s.forwards()
        try:
            b = Browser(mapping)
            ow = login_open_webui(b, CHAT_USER, s.passwords()[CHAT_USER])
            hdr = {"Authorization": f"Bearer {ow['token']}"}
            # Open WebUI caches the model list; wait until it sees the new alias.
            deadline = time.time() + 300
            visible = False
            while time.time() < deadline and not visible:
                code, _, body = b.request("GET", "http://localhost:8080/api/models", headers=hdr)
                visible = code == 200 and any(m.get("id") == alias for m in json.loads(body).get("data", []))
                if not visible:
                    time.sleep(10)
            if not visible:
                return False, f"model {alias} not visible to {CHAT_USER} in Open WebUI after 300s"
            code, _, body = b.request("POST", "http://localhost:8080/api/chat/completions", headers=hdr,
                                      json_body={"model": alias, "stream": False,
                                                 "messages": [{"role": "user", "content": "Reply with the single word: pong"}]})
            if code != 200:
                return False, f"Open WebUI chat HTTP {code}: {body[:160]}"
        finally:
            stop()
        # LiteLLM writes spend logs asynchronously; poll the end-user record.
        key = litellm_key()
        with port_forward_cls(NS, "svc/litellm", 4000) as pf:
            deadline, spend = time.time() + 180, None
            while time.time() < deadline:
                req = urllib.request.Request(f"{pf.base}/customer/info?end_user_id={urllib.parse.quote(CHAT_USER)}",
                                             headers={"Authorization": f"Bearer {key}"})
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        info = json.loads(r.read())
                    spend = info.get("spend")
                    if spend is not None and float(spend) > 0:
                        break
                except urllib.error.HTTPError:
                    pass
                time.sleep(10)
        ok = spend is not None and float(spend) > 0
        return ok, f"{CHAT_USER} chatted via Open WebUI (200); LiteLLM end-user spend={spend}"
    finally:
        cleanup()


def chk_argocd_sso_redirect(c, s: SsoEnv, argocd_url: str | None):
    """Managed ArgoCD authenticates through IAM Identity Center. A real login
    needs an IdC user (and MFA), which cannot be automated safely, so this
    asserts the reachable part: the server answers and its login hands off to
    Identity Center."""
    if not argocd_url:
        return False, "no argocd_url terraform output"
    b = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_tls_context()),
                                    urllib.request.HTTPCookieProcessor())
    try:
        with b.open(urllib.request.Request(argocd_url.rstrip("/") + "/auth/login", method="GET"), timeout=60) as r:
            final, code = r.geturl(), r.status
    except urllib.error.HTTPError as e:
        final, code = e.geturl(), e.code
    host = urllib.parse.urlsplit(final).netloc
    idc = any(h in host for h in (".awsapps.com", "signin.aws", "portal.sso.", ".sso.", "identitycenter"))
    return idc, f"login redirected to {'Identity Center' if idc else host} (HTTP {code}); interactive IdC sign-in not automated"
