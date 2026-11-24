"""Authorization of mutating requests (approve, dismiss, delete).

These requests start a cluster-mutating Remediator Job or destroy audit
history, so they need a verified admin identity, never network position. The
SSM tunnel reaches the backend through oauth2-proxy, but a pod in the cluster
can reach it directly, so forwarded identity headers are not trusted: the
backend verifies the Cognito ID token oauth2-proxy injects
(`Authorization: Bearer`, --set-authorization-header) against the Cognito
JWKS. No header can forge that signature.
"""
from __future__ import annotations

import logging
import threading

from dashboard.config import Settings

log = logging.getLogger("dashboard.auth")

try:
    import jwt                      # type: ignore[import-not-found]
    from jwt import PyJWKClient     # type: ignore[import-not-found]
except ImportError:                 # degraded mode: mutations fail closed
    jwt = None
    PyJWKClient = None


class Verifier:
    def __init__(self, settings: Settings):
        self.s = settings
        self._jwks = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.s.oidc_issuer_url and self.s.oidc_client_id and jwt is not None)

    def _jwks_client(self):
        with self._lock:
            if self._jwks is None:
                self._jwks = PyJWKClient(f"{self.s.oidc_issuer_url}/.well-known/jwks.json")
            return self._jwks

    def verify(self, auth_header: str) -> tuple[str | None, str]:
        """(identity, "") for a valid token in an allowed group, else (None, reason)."""
        if not auth_header:
            return None, "missing bearer token"
        parts = auth_header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            return None, "malformed Authorization header"
        token = parts[1].strip()
        try:
            key = self._jwks_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=self.s.oidc_client_id,
                                issuer=self.s.oidc_issuer_url, options={"require": ["exp", "iss", "aud"]})
        except Exception as e:  # signature, expiry, issuer, audience or JWKS fetch failure: deny
            return None, f"token verification failed: {type(e).__name__}"
        if not any(g in self.s.oidc_allowed_groups for g in claims.get("cognito:groups") or []):
            return None, "caller not in an allowed admin group"
        return claims.get("email") or claims.get("cognito:username") or claims.get("sub") or "unknown", ""


def authorize_mutation(headers, settings: Settings, verifier: Verifier) -> tuple[str | None, int, str]:
    """(approver, 0, "") if allowed, else (None, http_status, error).

    1. CSRF: the custom X-Requested-With header. A cross-origin page cannot set
       it without a CORS preflight this server never approves, so a browser
       request riding a valid session cookie is refused.
    2. A verified token in OIDC_ALLOWED_GROUPS, unless DASHBOARD_AUTH_REQUIRED is
       explicitly false (trusted network, SSO disabled)."""
    if not headers.get("X-Requested-With"):
        return None, 403, "missing X-Requested-With header (CSRF protection)"
    if not settings.auth_required:
        approver = headers.get("X-Forwarded-Email") or headers.get("X-Auth-Request-Email") or "auth-disabled"
        log.warning("mutation allowed without authentication (DASHBOARD_AUTH_REQUIRED=false)",
                    extra={"approver": approver})
        return approver, 0, ""
    if not verifier.configured:
        return None, 503, ("dashboard auth is required but not configured "
                           "(need OIDC_ISSUER_URL, OIDC_CLIENT_ID, and PyJWT); "
                           "set DASHBOARD_AUTH_REQUIRED=false to override on a trusted network")
    identity, reason = verifier.verify(headers.get("Authorization", ""))
    if identity is None:
        return None, 401, f"unauthorized: {reason}"
    return identity, 0, ""
