"""Helpers shared by the dashboard and agent tests.

Kept out of conftest.py on purpose: the repo-root tests/ also has a conftest.py,
so `from conftest import ...` would resolve to whichever one Python imported
first. This module has a unique name.
"""
import json
import os

# Fixture files end in .json.in (recorded inputs) and .json.golden (expected
# output), not .json, so CI's kubeconform scan of platform/services skips them.
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def check_golden(name, actual):
    """Compare `actual` to fixtures/<name>. Set UPDATE_GOLDEN=1 to rewrite it
    (only when a behaviour change is intended and reviewed)."""
    path = os.path.join(FIXTURES, name)
    text = json.dumps(actual, indent=2, sort_keys=True, default=str) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    assert os.path.exists(path), f"missing golden file {name}; run with UPDATE_GOLDEN=1"
    with open(path, encoding="utf-8") as f:
        expected = json.load(f)
    assert json.loads(text) == expected


# ---------------------------------------------------------------------------
# Fake psycopg connection
# ---------------------------------------------------------------------------

class FakeCursor:
    """Records every statement; answers from scripted (sql-fragment, rows) pairs.

    The first script entry whose fragment occurs in the statement wins and is
    consumed, so the same query can be answered differently on repeat calls."""

    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        self.rows, self.rowcount = [], 0
        for i, (fragment, rows) in enumerate(self.conn.script):
            if fragment in sql:
                self.conn.script.pop(i)
                if isinstance(rows, Exception):
                    raise rows
                if isinstance(rows, int):
                    self.rowcount = rows
                else:
                    self.rows = list(rows)
                    self.rowcount = len(self.rows)
                break

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.executed = []
        self.closed = False

    def cursor(self, row_factory=None):
        return FakeCursor(self)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sql(self):
        return [s for s, _ in self.executed]


# ---------------------------------------------------------------------------
# Fake Kubernetes API for the watch caches
# ---------------------------------------------------------------------------

class FakeKube:
    """Answers list calls from `lists` ({path: {"items": [...]}}), keyed by
    path or by (path, fieldSelector); `watches` maps a path to a list of
    streams (each a list of events) consumed one per watch call."""

    def __init__(self, lists=None, watches=None, errors=None):
        self.lists = dict(lists or {})
        self.watches = {k: list(v) for k, v in (watches or {}).items()}
        self.errors = dict(errors or {})   # path -> exception raised on list
        self.calls = []
        self.posted = []

    def get(self, path, query=None):
        sel = (query or {}).get("fieldSelector")
        self.calls.append(("GET", path, sel))
        err = self.errors.get(path)
        if err is not None:
            raise err
        data = self.lists.get((path, sel), self.lists.get(path))
        if data is None:
            from dashboard.k8s import ApiError
            raise ApiError(404, "not found")
        return data

    def watch(self, path, query):
        self.calls.append(("WATCH", path, query.get("resourceVersion")))
        streams = self.watches.get(path) or [[]]
        stream = streams.pop(0) if len(streams) > 1 else streams[0]
        for ev in stream:
            if isinstance(ev, Exception):
                raise ev
            yield ev

    def get_text(self, path, query=None):
        self.calls.append(("TEXT", path, None))
        return ""

    def post(self, path, body):
        self.posted.append((path, body))
        return {}


def fixture_kube():
    """FakeKube serving the recorded cluster in fixtures/k8s.json.in at the
    paths and field selectors the dashboard's watch caches use."""
    k8s = load_fixture("k8s.json.in")
    ing = dict(k8s["/apis/networking.k8s.io/v1/namespaces/ai-platform/ingresses/ai-platform-litellm"],
               metadata={"name": "ai-platform-litellm", "namespace": "ai-platform"})
    cm = dict(k8s["/api/v1/namespaces/ai-platform/configmaps/litellm-config"],
              metadata={"name": "litellm-config", "namespace": "ai-platform"})
    def named(items, ns):
        # Recorded events carry no metadata; real ones always have a unique name.
        return [dict(e, metadata={"name": f"ev-{ns}-{i}", "namespace": ns}) for i, e in enumerate(items)]

    events_inf = named(k8s["/api/v1/namespaces/inference/events?fieldSelector=involvedObject.kind=Pod"]["items"]
                       + k8s["/api/v1/namespaces/inference/events"]["items"], "inference")
    events_nc = named(k8s["/api/v1/events?fieldSelector=involvedObject.kind=NodeClaim"]["items"], "default")
    lists = {p: v for p, v in k8s.items() if "?" not in p and "/ingresses/" not in p and "/configmaps/" not in p
             and p != "/api/v1/namespaces/inference/events"}
    lists.update({
        ("/apis/networking.k8s.io/v1/namespaces/ai-platform/ingresses", "metadata.name=ai-platform-litellm"): {"items": [ing]},
        ("/api/v1/namespaces/ai-platform/configmaps", "metadata.name=litellm-config"): {"items": [cm]},
        ("/api/v1/namespaces/ai-platform/configmaps", "metadata.name=cluster-dashboard-node-prices"): {"items": []},
        ("/api/v1/events", "involvedObject.kind=NodeClaim"): {"items": events_nc},
        "/api/v1/namespaces/inference/events": {"items": events_inf},
    })
    return FakeKube(lists)


# The recorded cluster's "now": events are kept for 60 minutes after this.
FIXTURE_NOW = 1795168800.0   # 2026-11-20T10:00:00Z


def fixture_app(env=None, deps_missing=(), clock=lambda: FIXTURE_NOW, kube=None):
    """An App with every watch cache listed from the recorded cluster and the
    polled sources' inputs set from the recorded metrics. Nothing runs in the
    background."""
    import yaml

    from dashboard import config, models
    from dashboard.app import App
    from dashboard.deps import Deps

    settings = config.load({"CLUSTER_NAME": "ai-platform-dev", "AWS_REGION": "eu-central-1",
                            "ARGOCD_URL": "https://argocd.example", "DB_USER": "u", "DB_PASSWORD": "p",
                            **(env or {})})
    kube = kube or fixture_kube()
    app = App(settings, Deps(frozenset(deps_missing)), kube=kube, yaml_load=yaml.safe_load, clock=clock,
              remediator_job=lambda i: {"metadata": {"name": f"remediator-{i[:8]}"}})
    for c in app.caches.values():
        c.list(kube)
    prom = load_fixture("prom.json.in")
    app.set_input("usage", kube.lists["/apis/metrics.k8s.io/v1beta1/nodes"]["items"])
    lite = load_fixture("litellm_inputs.json.in")
    app.set_input("litellm_api", lite["litellm_api"])
    app.set_input("litellm_traffic", lite["litellm_traffic"])
    app.set_input("dcgm", load_fixture("dcgm.json.in"))
    app.set_input("latency", (models.quantile_map(prom["vllm:time_to_first_token_seconds"]),
                              models.quantile_map(prom["vllm:inter_token_latency_seconds"])))
    for s in app.sources:
        if not s.tracker.disabled:
            s.tracker.ok()
    return app
