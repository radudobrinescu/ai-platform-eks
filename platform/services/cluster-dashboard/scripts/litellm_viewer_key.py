#!/usr/bin/env python3
"""Mint or reuse the cluster dashboard's read-only LiteLLM key.

Runs as CronJob `cluster-dashboard-litellm-key` every 15 minutes:

1. Wait for LiteLLM (`/health/liveliness`, up to WAIT_SECONDS).
2. If Secret `cluster-dashboard-litellm-key` holds a key that LiteLLM reports
   as active and owned by user `cluster-dashboard`, stop (the normal case).
   LiteLLM is asked with that key as the bearer token (`/key/info` then
   describes the calling key), so the key is never part of a URL.
3. Otherwise make sure user `cluster-dashboard` exists with role
   `proxy_admin_viewer`, delete any key with alias `cluster-dashboard`
   (LiteLLM requires unique aliases, so an orphan would block the next step),
   generate a new key and create or patch the Secret.

A 401 or 403 with the master key, or any LiteLLM 5xx, stops the run without
changing anything. Standard library only; logs JSON lines and never a key.
"""
from __future__ import annotations

import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 5
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
VIEWER_ROLE = "proxy_admin_viewer"


class HttpError(Exception):
    def __init__(self, status: int, where: str):
        super().__init__(f"HTTP {status} from {where}")
        self.status = status


class MasterKeyRejected(Exception):
    pass


def log(event: str, level: str = "info", **fields) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level,
                      "component": "litellm-viewer-key", "event": event, **fields}), flush=True)


class Http:
    """JSON over HTTP with a bearer token. Errors name the method and path,
    never the query string (it can carry a key) or the token."""

    def __init__(self, base: str, token: str, context: ssl.SSLContext | None = None):
        self.base, self._token, self._ctx = base.rstrip("/"), token, context

    def with_token(self, token: str) -> Http:
        """The same server and TLS settings, authenticating with `token`."""
        return Http(self.base, token, self._ctx)

    def call(self, method: str, path: str, body: dict | None = None, query: dict | None = None,
             content_type: str = "application/json", parse: bool = True) -> dict:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/json",
            **({"Content-Type": content_type} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=self._ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            e.close()
            raise HttpError(e.code, f"{method} {path}") from None
        return json.loads(raw) if raw and parse else {}


class Kube:
    def __init__(self, http: Http, namespace: str, secret: str):
        self.http, self.ns, self.secret = http, namespace, secret

    def read_key(self) -> tuple[bool, str | None]:
        """(secret exists, key or None)."""
        try:
            obj = self.http.call("GET", f"/api/v1/namespaces/{self.ns}/secrets/{self.secret}")
        except HttpError as e:
            if e.status == 404:
                return False, None
            raise
        raw = (obj.get("data") or {}).get("api-key")
        return True, base64.b64decode(raw).decode() if raw else None

    def write_key(self, key: str, exists: bool) -> None:
        data = {"api-key": base64.b64encode(key.encode()).decode()}
        if not exists:
            try:
                self.http.call("POST", f"/api/v1/namespaces/{self.ns}/secrets", {
                    "apiVersion": "v1", "kind": "Secret", "type": "Opaque", "data": data,
                    "metadata": {"name": self.secret, "labels": {"app": "cluster-dashboard"}}})
                return
            except HttpError as e:
                if e.status != 409:  # created meanwhile: patch it below
                    raise
        self.http.call("PATCH", f"/api/v1/namespaces/{self.ns}/secrets/{self.secret}", {"data": data},
                       content_type="application/merge-patch+json")


class LiteLLM:
    """LiteLLM admin calls with the master key."""

    def __init__(self, http: Http, user_id: str, alias: str):
        self.http, self.user_id, self.alias = http, user_id, alias

    def call(self, method: str, path: str, body: dict | None = None, query: dict | None = None) -> dict:
        try:
            return self.http.call(method, path, body, query)
        except HttpError as e:
            if e.status in (401, 403):
                raise MasterKeyRejected(str(e)) from None
            raise

    def wait_ready(self, seconds: float, sleep=time.sleep, clock=time.monotonic) -> bool:
        deadline = clock() + seconds
        while True:
            try:
                self.http.call("GET", "/health/liveliness", parse=False)
                return True
            except (HttpError, OSError):
                if clock() >= deadline:
                    return False
                sleep(5)

    def key_valid(self, key: str) -> bool:
        """True when `key` is an active key of the viewer user. The key is
        the bearer token of the request and /key/info, without a `key`
        parameter, describes the calling key: a key in the query string
        could end up in access logs."""
        try:
            info = self.http.with_token(key).call("GET", "/key/info").get("info") or {}
        except HttpError as e:
            # LiteLLM does not accept the key: unknown or malformed (400, 404),
            # or deleted, revoked or expired, which fail authentication (401, 403).
            if e.status in (400, 401, 403, 404):
                return False
            raise
        return info.get("status") == "active" and info.get("user_id") == self.user_id

    def check_master_key(self) -> None:
        """Raise MasterKeyRejected unless LiteLLM accepts the master key.
        key_valid() no longer uses it, and a run with a rejected master key
        must keep failing even when the stored key is still valid."""
        try:
            self.call("GET", "/user/info", query={"user_id": self.user_id})
        except HttpError as e:
            if e.status != 404:  # 404: the master key was accepted, the user does not exist
                raise

    def ensure_user(self) -> None:
        try:
            user = self.call("GET", "/user/info", query={"user_id": self.user_id}).get("user_info") or {}
        except HttpError as e:
            if e.status != 404:
                raise
            self.call("POST", "/user/new", {"user_id": self.user_id, "user_role": VIEWER_ROLE,
                                            "auto_create_key": False})
            log("user created", user=self.user_id, role=VIEWER_ROLE)
            return
        if user.get("user_role") != VIEWER_ROLE:
            self.call("POST", "/user/update", {"user_id": self.user_id, "user_role": VIEWER_ROLE})
            log("user role set", user=self.user_id, role=VIEWER_ROLE)

    def delete_alias_keys(self) -> int:
        hashes = self.call("GET", "/key/list", query={"key_alias": self.alias, "size": 100}).get("keys") or []
        hashes = [h for h in hashes if isinstance(h, str)]
        if hashes:
            self.call("POST", "/key/delete", {"keys": hashes})
        return len(hashes)

    def generate(self) -> str:
        key = self.call("POST", "/key/generate", {
            "user_id": self.user_id, "key_alias": self.alias,
            "metadata": {"managed_by": "cluster-dashboard"}}).get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("key generation returned no key")
        return key


def run(kube: Kube, litellm: LiteLLM, wait_seconds: float, sleep=time.sleep) -> int:
    if not litellm.wait_ready(wait_seconds, sleep=sleep):
        log("litellm not reachable", level="error", waitedSeconds=wait_seconds)
        return 1
    try:
        exists, key = kube.read_key()
        if key and litellm.key_valid(key):
            litellm.check_master_key()
            log("viewer key valid, nothing to do")
            return 0
        log("viewer key missing or invalid, creating one", secretExists=exists)
        litellm.ensure_user()
        deleted = litellm.delete_alias_keys()
        if deleted:
            log("old keys deleted", count=deleted, alias=litellm.alias)
        kube.write_key(litellm.generate(), exists)
        log("viewer key stored", secret=kube.secret)
        return 0
    except MasterKeyRejected as e:
        log("master key rejected", level="error", error=str(e))
    except (HttpError, OSError, ValueError) as e:
        log("run failed", level="error", error=f"{type(e).__name__}: {e}")
    return 1


def main(env=os.environ) -> int:
    master = env.get("LITELLM_MASTER_KEY", "")
    if not master:
        log("LITELLM_MASTER_KEY not set", level="error")
        return 1
    kube_api = env.get("KUBE_API", "https://kubernetes.default.svc")
    sa_dir = env.get("SA_DIR", SA_DIR)
    with open(os.path.join(sa_dir, "token"), encoding="utf-8") as f:
        sa_token = f.read().strip()
    ctx = ssl.create_default_context(cafile=os.path.join(sa_dir, "ca.crt")) if kube_api.startswith("https") else None
    kube = Kube(Http(kube_api, sa_token, ctx), env.get("NAMESPACE", "ai-platform"),
                env.get("SECRET_NAME", "cluster-dashboard-litellm-key"))
    litellm = LiteLLM(Http(env.get("LITELLM_URL", "http://litellm.ai-platform.svc.cluster.local:4000"), master),
                      env.get("VIEWER_USER", "cluster-dashboard"), env.get("KEY_ALIAS", "cluster-dashboard"))
    return run(kube, litellm, float(env.get("WAIT_SECONDS", "60")))


if __name__ == "__main__":
    sys.exit(main())
