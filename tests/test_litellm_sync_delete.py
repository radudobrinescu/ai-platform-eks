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
