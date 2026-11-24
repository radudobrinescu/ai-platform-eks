"""Viewer-key CronJob script (scripts/litellm_viewer_key.py) against an
in-process fake of the LiteLLM admin API and the Kubernetes Secret API.

The fake mirrors LiteLLM 1.108.10 behaviour the script relies on: unique key
aliases on /key/generate, /key/delete failing for unknown keys, /key/info
returning `info.status` and `info.user_id`, /user/info returning 404 for an
unknown user, and /health/liveliness answering with a non-object body.
/key/info without a `key` parameter describes the key in the Authorization
header (`key = key or user_api_key_dict.api_key` in LiteLLM); a deleted,
revoked or expired key does not get that far, it fails authentication (401).
"""
import base64
import json
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

import litellm_viewer_key as vk

MASTER, SA_TOKEN, NS, SECRET = "sk-master-test", "sa-token-test", "ai-platform", "cluster-dashboard-litellm-key"
DASH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Fake:
    def __init__(self):
        self.secrets: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.keys: dict[str, dict] = {}          # token -> {alias, user_id, status}
        self.calls: list[tuple[str, str]] = []
        self.requests: list[tuple[str, str, str]] = []   # (method, raw path with query, Authorization)
        self.key_info_status: int | None = None          # force this status on /key/info
        self.litellm_up = True
        self.master_ok = True
        self.fail_secret_write = 0
        self._n = 0

    def new_token(self):
        self._n += 1
        return f"sk-generated-{self._n}"


def _handler(fake: Fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=None, raw=None):
            data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n)) if n else {}

        def _route(self, method):
            u = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(u.query))
            fake.calls.append((method, u.path))
            auth = self.headers.get("Authorization", "")
            fake.requests.append((method, self.path, auth))
            if u.path.startswith("/api/v1/"):
                if auth != f"Bearer {SA_TOKEN}":
                    return self._send(401)
                return self._kube(method, u.path)
            if not fake.litellm_up:
                return self._send(503)
            if u.path == "/health/liveliness":
                return self._send(200, raw=b"\"I'm alive!\"")
            if method == "GET" and u.path == "/key/info" and fake.key_info_status:
                return self._send(fake.key_info_status, {"error": "forced"})
            if auth != f"Bearer {MASTER}":
                return self._as_virtual_key(method, u.path, q, auth[len("Bearer "):])
            if not fake.master_ok:
                return self._send(401, {"error": "auth"})
            return self._litellm(method, u.path, q)

        def _as_virtual_key(self, method, path, q, token):
            """A request authenticated with a generated key: only an active
            key passes, and all it may do here is read its own info."""
            k = fake.keys.get(token)
            if k is None or k["status"] != "active":
                return self._send(401, {"error": "Authentication Error"})
            if method == "GET" and path == "/key/info" and "key" not in q:
                return self._send(200, {"key": "<hash>", "info": {"status": k["status"], "user_id": k["user_id"],
                                                                  "key_alias": k["alias"]}})
            return self._send(403, {"error": "not allowed for this role"})

        def _kube(self, method, path):
            base = f"/api/v1/namespaces/{NS}/secrets"
            if method == "GET" and path == f"{base}/{SECRET}":
                return self._send(200, fake.secrets[SECRET]) if SECRET in fake.secrets else self._send(404)
            if method in ("POST", "PATCH") and fake.fail_secret_write:
                fake.fail_secret_write -= 1
                return self._send(500)
            if method == "POST" and path == base:
                obj = self._body()
                if obj["metadata"]["name"] in fake.secrets:
                    return self._send(409)
                fake.secrets[obj["metadata"]["name"]] = obj
                return self._send(201, obj)
            if method == "PATCH" and path == f"{base}/{SECRET}":
                assert self.headers.get("Content-Type") == "application/merge-patch+json"
                if SECRET not in fake.secrets:
                    return self._send(404)
                fake.secrets[SECRET]["data"].update(self._body()["data"])
                return self._send(200, fake.secrets[SECRET])
            return self._send(403)

        def _litellm(self, method, path, q):
            if method == "GET" and path == "/key/info":
                k = fake.keys.get(q.get("key", ""))
                if k is None:
                    return self._send(404, {"error": "Key not found in database"})
                return self._send(200, {"key": q["key"], "info": {"status": k["status"], "user_id": k["user_id"],
                                                                  "key_alias": k["alias"]}})
            if method == "GET" and path == "/user/info":
                u = fake.users.get(q.get("user_id", ""))
                return self._send(200, {"user_id": q["user_id"], "user_info": u}) if u else self._send(404)
            if method == "POST" and path == "/user/new":
                b = self._body()
                assert b.get("auto_create_key") is False
                fake.users[b["user_id"]] = {"user_role": b["user_role"]}
                return self._send(200, {"user_id": b["user_id"]})
            if method == "POST" and path == "/user/update":
                b = self._body()
                fake.users[b["user_id"]]["user_role"] = b["user_role"]
                return self._send(200, {})
            if method == "GET" and path == "/key/list":
                return self._send(200, {"keys": ["hash-" + t for t, k in fake.keys.items()
                                                 if k["alias"] == q.get("key_alias")]})
            if method == "POST" and path == "/key/delete":
                hashes = self._body()["keys"]
                tokens = [h[len("hash-"):] for h in hashes if h[len("hash-"):] in fake.keys]
                if len(tokens) != len(hashes):
                    return self._send(400, {"error": "Not all keys passed in were deleted"})
                for t in tokens:
                    del fake.keys[t]
                return self._send(200, {"deleted_keys": hashes})
            if method == "POST" and path == "/key/generate":
                b = self._body()
                if any(k["alias"] == b["key_alias"] for k in fake.keys.values()):
                    return self._send(400, {"error": "Unique key aliases across all keys are required"})
                t = fake.new_token()
                fake.keys[t] = {"alias": b["key_alias"], "user_id": b["user_id"], "status": "active"}
                return self._send(200, {"key": t})
            return self._send(404)

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PATCH(self):
            self._route("PATCH")

    return H


@pytest.fixture
def fake():
    f = Fake()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(f))
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    f.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield f
    srv.shutdown()
    srv.server_close()


def _run(fake, wait=0.0):
    kube = vk.Kube(vk.Http(fake.url, SA_TOKEN), NS, SECRET)
    lite = vk.LiteLLM(vk.Http(fake.url, MASTER), "cluster-dashboard", "cluster-dashboard")
    return vk.run(kube, lite, wait, sleep=lambda s: None)


def _stored_key(fake):
    return base64.b64decode(fake.secrets[SECRET]["data"]["api-key"]).decode()


def _writes(fake):
    return [c for c in fake.calls if c[0] in ("POST", "PATCH")]


def test_first_run_creates_user_key_and_secret(fake, capsys):
    assert _run(fake) == 0
    assert fake.users == {"cluster-dashboard": {"user_role": "proxy_admin_viewer"}}
    key = _stored_key(fake)
    assert fake.keys[key] == {"alias": "cluster-dashboard", "user_id": "cluster-dashboard", "status": "active"}
    assert fake.secrets[SECRET]["metadata"]["labels"] == {"app": "cluster-dashboard"}
    assert key not in capsys.readouterr().out


def test_valid_key_changes_nothing(fake):
    assert _run(fake) == 0
    key, fake.calls[:] = _stored_key(fake), []
    assert _run(fake) == 0
    assert _writes(fake) == []
    assert _stored_key(fake) == key


@pytest.mark.parametrize("status", ["revoked", "expired"])
def test_inactive_key_is_replaced(fake, status):
    assert _run(fake) == 0
    old = _stored_key(fake)
    fake.keys[old]["status"] = status
    assert _run(fake) == 0
    new = _stored_key(fake)
    assert new != old and old not in fake.keys and fake.keys[new]["status"] == "active"


def test_viewer_key_is_checked_as_bearer_token_and_never_sent_in_a_url(fake):
    """A query string can end up in LiteLLM's access log (and any proxy's);
    the Authorization header does not."""
    assert _run(fake) == 0
    key, fake.requests[:], fake.calls[:] = _stored_key(fake), [], []
    assert _run(fake) == 0
    assert all(key not in path for _method, path, _auth in fake.requests), fake.requests
    info = [(path, auth) for method, path, auth in fake.requests if path.startswith("/key/info")]
    assert info == [("/key/info", f"Bearer {key}")]
    assert _writes(fake) == [] and _stored_key(fake) == key


def test_key_of_another_user_is_replaced(fake):
    assert _run(fake) == 0
    old = _stored_key(fake)
    fake.keys[old]["user_id"] = "someone-else"
    fake.keys[old]["alias"] = "someone-elses-key"
    assert _run(fake) == 0
    new = _stored_key(fake)
    assert new != old and fake.keys[new]["user_id"] == "cluster-dashboard"


@pytest.mark.parametrize("status,kept", [(400, False), (403, False), (404, False), (500, True), (503, True)])
def test_key_info_errors(fake, status, kept):
    """4xx: LiteLLM does not accept the stored key, so it is replaced. 5xx:
    LiteLLM is in trouble, so the run stops without changing anything."""
    assert _run(fake) == 0
    old, fake.calls[:] = _stored_key(fake), []
    fake.key_info_status = status
    assert _run(fake) == (1 if kept else 0)
    assert (_stored_key(fake) == old) is kept and (_writes(fake) == []) is kept


def test_deleted_key_is_replaced(fake):
    assert _run(fake) == 0
    old = _stored_key(fake)
    del fake.keys[old]
    assert _run(fake) == 0
    assert _stored_key(fake) != old and len(fake.keys) == 1


def test_lost_secret_deletes_orphan_alias_and_recreates(fake):
    assert _run(fake) == 0
    del fake.secrets[SECRET]                         # key still in LiteLLM under the alias
    assert _run(fake) == 0
    assert len(fake.keys) == 1 and _stored_key(fake) in fake.keys


def test_failed_secret_write_heals_on_next_run(fake):
    fake.fail_secret_write = 1
    assert _run(fake) == 1
    assert SECRET not in fake.secrets and len(fake.keys) == 1     # orphan left behind
    assert _run(fake) == 0
    assert len(fake.keys) == 1 and _stored_key(fake) in fake.keys


def test_user_with_other_role_is_corrected(fake):
    fake.users["cluster-dashboard"] = {"user_role": "internal_user"}
    assert _run(fake) == 0
    assert fake.users["cluster-dashboard"]["user_role"] == "proxy_admin_viewer"


def test_master_key_rejected_changes_nothing(fake, capsys):
    fake.master_ok = False
    assert _run(fake) == 1
    assert _writes(fake) == [] and fake.secrets == {}
    out = capsys.readouterr().out
    assert "master key rejected" in out and MASTER not in out


def test_master_key_rejected_keeps_existing_secret(fake):
    assert _run(fake) == 0
    key, fake.master_ok, fake.calls[:] = _stored_key(fake), False, []
    assert _run(fake) == 1
    assert _writes(fake) == [] and _stored_key(fake) == key


def test_litellm_never_up(fake, capsys):
    fake.litellm_up = False
    assert _run(fake, wait=0.0) == 1
    assert "litellm not reachable" in capsys.readouterr().out
    assert fake.secrets == {}


def test_main_reads_env_and_token_file(fake, tmp_path, capsys):
    (tmp_path / "token").write_text(SA_TOKEN + "\n")
    env = {"LITELLM_MASTER_KEY": MASTER, "LITELLM_URL": fake.url, "KUBE_API": fake.url,
           "SA_DIR": str(tmp_path), "WAIT_SECONDS": "0"}
    assert vk.main(env) == 0
    out = capsys.readouterr().out
    key = _stored_key(fake)
    assert key not in out and MASTER not in out and SA_TOKEN not in out
    for line in out.strip().splitlines():
        assert json.loads(line)["component"] == "litellm-viewer-key"


def test_main_without_master_key():
    assert vk.main({}) == 1


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------

def _docs():
    with open(os.path.join(DASH, "manifests.yaml"), encoding="utf-8") as f:
        return [d for d in yaml.safe_load_all(f) if d]


def _get(kind, name):
    return next(d for d in _docs() if d["kind"] == kind and d["metadata"]["name"] == name)


def test_cronjob_spec():
    cj = _get("CronJob", "cluster-dashboard-litellm-key")["spec"]
    assert cj["schedule"] == "*/15 * * * *" and cj["concurrencyPolicy"] == "Forbid"
    assert cj["successfulJobsHistoryLimit"] == 1 and cj["failedJobsHistoryLimit"] == 1
    job = cj["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 2 and job["activeDeadlineSeconds"] == 300
    pod = job["template"]["spec"]
    assert pod["serviceAccountName"] == "cluster-dashboard-litellm-key"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    (c,) = pod["containers"]
    assert c["securityContext"] == {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}}
    env = {e["name"]: e for e in c["env"]}
    assert env["LITELLM_MASTER_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "litellm-secrets", "key": "master-key"}
    assert c["resources"]["limits"]


def test_key_job_rbac_is_least_privilege():
    rules = _get("Role", "cluster-dashboard-litellm-key")["rules"]
    assert all(r["resources"] == ["secrets"] for r in rules)
    named = [r for r in rules if r.get("resourceNames")]
    unnamed = [r for r in rules if not r.get("resourceNames")]
    assert [r["verbs"] for r in unnamed] == [["create"]]
    assert named == [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "patch"],
                      "resourceNames": [SECRET]}]
    sa = _get("ServiceAccount", "cluster-dashboard-litellm-key")
    assert sa.get("automountServiceAccountToken") is False


def test_dashboard_reads_only_its_own_secret():
    rules = _get("Role", "cluster-dashboard-config")["rules"]
    secret_rules = [r for r in rules if "secrets" in r["resources"]]
    assert secret_rules == [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"],
                             "resourceNames": [SECRET]}]
    cluster = _get("ClusterRole", "cluster-dashboard")["rules"]
    assert not any("secrets" in r.get("resources", []) for r in cluster)
