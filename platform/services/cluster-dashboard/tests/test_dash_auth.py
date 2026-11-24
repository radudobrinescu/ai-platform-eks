"""Mutation authorization (R8): every branch of authorize_mutation and
Verifier.verify. PyJWT is stubbed; the signature check itself is PyJWT's."""
import types

import pytest

from dashboard import auth, config

OIDC = {"OIDC_ISSUER_URL": "https://cognito.example/pool", "OIDC_CLIENT_ID": "client"}


class _Verifier:
    def __init__(self, configured=True, result=("admin@example.com", "")):
        self.configured, self.result, self.seen = configured, result, []

    def verify(self, header):
        self.seen.append(header)
        return self.result


@pytest.mark.parametrize("env,headers,verifier,expected", [
    ({}, {}, _Verifier(), (None, 403, "missing X-Requested-With header (CSRF protection)")),
    ({"DASHBOARD_AUTH_REQUIRED": "false"}, {"X-Requested-With": "d", "X-Auth-Request-Email": "a@x"},
     _Verifier(), ("a@x", 0, "")),
    ({"DASHBOARD_AUTH_REQUIRED": "false"}, {"X-Requested-With": "d"}, _Verifier(), ("auth-disabled", 0, "")),
    ({}, {"X-Requested-With": "d"}, _Verifier(configured=False), None),
    ({}, {"X-Requested-With": "d", "Authorization": "Bearer t"}, _Verifier(result=(None, "expired")),
     (None, 401, "unauthorized: expired")),
    ({}, {"X-Requested-With": "d", "Authorization": "Bearer t"}, _Verifier(), ("admin@example.com", 0, "")),
])
def test_authorize_mutation(env, headers, verifier, expected):
    got = auth.authorize_mutation(headers, config.load(env), verifier)
    if expected is None:
        assert got[0] is None and got[1] == 503 and "not configured" in got[2]
    else:
        assert got == expected


@pytest.fixture
def fake_jwt(monkeypatch):
    claims = {"cognito:groups": ["ai-platform-admins"], "email": "admin@example.com"}
    state = {"claims": claims, "error": None, "calls": []}

    def decode(token, key, **kw):
        state["calls"].append(kw)
        if state["error"]:
            raise state["error"]
        return state["claims"]

    class JWKS:
        def __init__(self, url):
            state["url"] = url

        def get_signing_key_from_jwt(self, token):
            return types.SimpleNamespace(key="k")

    monkeypatch.setattr(auth, "jwt", types.SimpleNamespace(decode=decode))
    monkeypatch.setattr(auth, "PyJWKClient", JWKS)
    return state


def test_verify_accepts_an_admin_token(fake_jwt):
    v = auth.Verifier(config.load(OIDC))
    assert v.configured
    assert v.verify("Bearer abc") == ("admin@example.com", "")
    kw = fake_jwt["calls"][0]
    assert (kw["audience"], kw["issuer"], kw["algorithms"]) == ("client", "https://cognito.example/pool", ["RS256"])
    assert fake_jwt["url"] == "https://cognito.example/pool/.well-known/jwks.json"


@pytest.mark.parametrize("header,reason", [
    ("", "missing bearer token"), ("Basic x", "malformed Authorization header"), ("Bearer ", "malformed Authorization header"),
])
def test_verify_rejects_bad_headers(fake_jwt, header, reason):
    assert auth.Verifier(config.load(OIDC)).verify(header) == (None, reason)


def test_verify_rejects_invalid_tokens_and_wrong_groups(fake_jwt):
    v = auth.Verifier(config.load(OIDC))
    fake_jwt["error"] = ValueError("bad signature")
    assert v.verify("Bearer t") == (None, "token verification failed: ValueError")
    fake_jwt["error"] = None
    fake_jwt["claims"] = {"cognito:groups": ["ai-platform-developers"], "email": "dev@example.com"}
    assert v.verify("Bearer t") == (None, "caller not in an allowed admin group")


def test_not_configured_without_pyjwt_or_oidc(monkeypatch):
    monkeypatch.setattr(auth, "jwt", None)
    assert not auth.Verifier(config.load(OIDC)).configured
    assert not auth.Verifier(config.load({})).configured
