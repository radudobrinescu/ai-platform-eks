"""litellm-sync: deleting a LiteLLM model row is idempotent.

LiteLLM runs two replicas; /model/info from one can still list a row that was
just deleted, and LiteLLM then answers HTTP 400 "Model with id=... not found in
db". That must count as deleted, or the CR's finalizer is held and
deregistration stalls (found by the E2E gate). Real failures must still return
False so the reconcile loop retries.

The kubernetes client is stubbed: the suite runs with only pytest/pyyaml/boto3.
"""

import importlib.util
import io
import os
import sys
import types
import urllib.error

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def sync():
    k8s = types.ModuleType("kubernetes")
    for sub in ("client", "config", "watch"):
        setattr(k8s, sub, types.ModuleType(f"kubernetes.{sub}"))
    exc = types.ModuleType("kubernetes.client.exceptions")
    exc.ApiException = type("ApiException", (Exception,), {})
    saved = {k: sys.modules.get(k) for k in ("kubernetes", "kubernetes.client", "kubernetes.client.exceptions")}
    sys.modules.update({"kubernetes": k8s, "kubernetes.client": k8s.client, "kubernetes.client.exceptions": exc})
    try:
        path = os.path.join(REPO_ROOT, "platform/services/litellm-sync/scripts/litellm_sync.py")
        spec = importlib.util.spec_from_file_location("litellm_sync_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError("http://litellm/model/delete", code, "err", {}, io.BytesIO(body.encode()))


@pytest.mark.parametrize("outcome,expected", [
    (_Resp(b"{}"), True),
    (_http_error(400, "{'error': 'Model with id=abc not found in db'}"), True),
    (_http_error(404, "Model not found"), True),
    (_http_error(400, "{'error': 'invalid request'}"), False),
    (_http_error(500, "internal"), False),
    (urllib.error.URLError("connection refused"), False),
])
def test_delete_model_row(sync, monkeypatch, outcome, expected):
    def fake_urlopen(req, timeout=None):
        assert req.get_method() == "POST" and req.full_url.endswith("/model/delete")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    assert sync._delete_model_row("abc") is expected


def test_no_call_site_uses_the_non_idempotent_delete(sync):
    with open(os.path.join(REPO_ROOT, "platform/services/litellm-sync/scripts/litellm_sync.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert '_litellm_request("POST", "/model/delete"' not in src


class TestRestartAfterLastModel:
    """LiteLLM replicas skip evictions when the DB holds no models, so removing
    the LAST DB model must be followed by a LiteLLM rolling restart."""

    def _setup(self, sync, monkeypatch, db_models):
        restarts = []
        monkeypatch.setattr(sync, "list_db_model_ids", lambda: db_models)
        monkeypatch.setattr(sync, "_delete_model_row", lambda _id: True)
        monkeypatch.setattr(sync, "_restart_litellm", lambda reason: restarts.append(reason) or True)
        return restarts

    def test_last_model_triggers_restart(self, sync, monkeypatch):
        restarts = self._setup(sync, monkeypatch, {"only": [("id-1", {})]})
        assert sync.deregister_model("only") is True
        assert len(restarts) == 1 and "only" in restarts[0]

    def test_other_models_left_no_restart(self, sync, monkeypatch):
        restarts = self._setup(sync, monkeypatch, {"a": [("id-1", {})], "b": [("id-2", {})]})
        assert sync.deregister_model("a") is True
        assert restarts == []

    def test_already_gone_no_restart(self, sync, monkeypatch):
        restarts = self._setup(sync, monkeypatch, {"b": [("id-2", {})]})
        assert sync.deregister_model("a") is True
        assert restarts == []

    def test_restart_patches_only_the_template_annotation(self, sync, monkeypatch):
        calls = []

        class Apps:
            def patch_namespaced_deployment(self, name, ns, body):
                calls.append((name, ns, body))

        monkeypatch.setattr(sync.client, "AppsV1Api", Apps, raising=False)
        assert sync._restart_litellm("test") is True
        name, ns, body = calls[0]
        assert (name, ns) == ("litellm", "ai-platform")
        assert list(body) == ["spec"] and list(body["spec"]["template"]["metadata"]["annotations"]) == [sync.RESTART_ANNOTATION]


def test_restart_rbac_is_scoped_to_the_litellm_deployment():
    import yaml
    with open(os.path.join(REPO_ROOT, "platform/services/litellm/litellm.yaml"), encoding="utf-8") as fh:
        docs = [d for d in yaml.safe_load_all(fh) if d]
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "litellm-sync-restart")
    assert role["rules"] == [{"apiGroups": ["apps"], "resources": ["deployments"],
                              "resourceNames": ["litellm"], "verbs": ["get", "patch"]}]
    rb = next(d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == "litellm-sync-restart")
    assert rb["subjects"] == [{"kind": "ServiceAccount", "name": "litellm-sync", "namespace": "inference"}]
