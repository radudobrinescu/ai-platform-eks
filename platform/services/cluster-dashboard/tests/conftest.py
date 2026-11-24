"""Shared test setup for the cluster dashboard and the Platform Health Agent.

The code under test runs in-cluster from ConfigMaps, not as an installed
package, so `scripts/` is put on sys.path. CI installs only pytest, pyyaml and
boto3: `kubernetes`, `psycopg` and `jwt` are replaced by small stubs where a
test needs them (same approach as tests/test_litellm_sync_delete.py at the repo
root).
"""
import importlib
import os
import sys
import types

import pytest

from dash_testlib import FakeConn

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)


@pytest.fixture
def fake_db():
    return FakeConn


# ---------------------------------------------------------------------------
# Module stubs
# ---------------------------------------------------------------------------

def _stub_kubernetes():
    k8s = types.ModuleType("kubernetes")
    for sub in ("client", "config", "watch"):
        setattr(k8s, sub, types.ModuleType(f"kubernetes.{sub}"))
    exc = types.ModuleType("kubernetes.client.exceptions")

    class ApiException(Exception):
        def __init__(self, status=0, reason=""):
            super().__init__(reason)
            self.status = status
            self.reason = reason

    exc.ApiException = ApiException
    k8s.client.exceptions = exc
    k8s.client.BatchV1Api = lambda *a, **k: None
    k8s.client.CoreV1Api = lambda *a, **k: None
    k8s.client.CustomObjectsApi = lambda *a, **k: None
    k8s.client.RbacAuthorizationV1Api = lambda *a, **k: None
    k8s.config.load_incluster_config = lambda: None
    k8s.watch.Watch = object
    return {
        "kubernetes": k8s,
        "kubernetes.client": k8s.client,
        "kubernetes.config": k8s.config,
        "kubernetes.watch": k8s.watch,
        "kubernetes.client.exceptions": exc,
    }


def _stub_psycopg():
    pg = types.ModuleType("psycopg")
    rows = types.ModuleType("psycopg.rows")
    rows.dict_row = object()
    pg.rows = rows
    pg.Connection = object

    def connect(**_kwargs):
        raise RuntimeError("psycopg stub: tests must inject a FakeConn")

    pg.connect = connect
    return {"psycopg": pg, "psycopg.rows": rows}


@pytest.fixture
def stub_modules(monkeypatch):
    """Install kubernetes and psycopg stubs for the duration of one test."""
    for name, mod in {**_stub_kubernetes(), **_stub_psycopg()}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return sys.modules


@pytest.fixture
def event_watcher(stub_modules, monkeypatch):
    """A freshly imported event_watcher module (agent enabled, stubs in place)."""
    monkeypatch.setenv("KIRO_API_KEY", "test-key")
    for name in [m for m in sys.modules if m in ("event_watcher", "pha") or m.startswith("pha.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    mod = importlib.import_module("event_watcher")
    yield mod
    for name in [m for m in sys.modules if m in ("event_watcher", "pha") or m.startswith("pha.")]:
        sys.modules.pop(name, None)


@pytest.fixture
def pha_watch(stub_modules, monkeypatch):
    """Freshly imported pha.watch and pha.rbac on the kubernetes stubs."""
    for name in [m for m in sys.modules if m in ("pha.watch", "pha.rbac")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    watch = importlib.import_module("pha.watch")
    rbac = importlib.import_module("pha.rbac")
    yield watch, rbac
    for name in ("pha.watch", "pha.rbac"):
        sys.modules.pop(name, None)


@pytest.fixture
def backend():
    import backend as mod
    return mod
