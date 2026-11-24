"""HTTP API (R4), stream (R5) and mutation authorization (R8), against the
real server on a free local port."""
import http.client
import json
import threading
import uuid

import pytest

from dash_testlib import FakeConn, fixture_app
from dashboard import api

INV = str(uuid.UUID(int=0x1234abcd))


@pytest.fixture
def server(tmp_path, monkeypatch):
    (tmp_path / "cluster-topology.html").write_text("<html>dashboard</html>")
    app = fixture_app()
    app.derive_once()
    srv = api.Server(("127.0.0.1", 0), api.make_handler(app, str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield app, srv.server_address[1]
    app.stop.set()
    srv.shutdown()


def call(port, method, path, headers=None, timeout=5):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, dict(r.getheaders()), (json.loads(body) if body and r.getheader("Content-Type") == "application/json" else body)


def test_root_and_data_json(server):
    app, port = server
    status, _, body = call(port, "GET", "/")
    assert status == 200 and b"dashboard" in body
    status, headers, doc = call(port, "GET", "/data.json")
    assert status == 200 and headers["Cache-Control"] == "no-cache"
    assert {m["name"] for m in doc["models"]} >= {"qwen2-5-3b-instruct", "nova-lite"}
    assert doc["sourceStatus"][0]["name"] == "kubernetes"


def test_snapshot_etag_and_304(server):
    app, port = server
    status, headers, body = call(port, "GET", "/api/v1/snapshot")
    assert status == 200 and headers["ETag"] == f'"v{body["version"]}"' and "models" in body["sections"]
    status, _, _ = call(port, "GET", "/api/v1/snapshot", {"If-None-Match": headers["ETag"]})
    assert status == 304


def test_sources_health_and_unknown_paths(server):
    app, port = server
    status, _, srcs = call(port, "GET", "/api/v1/sources")
    assert status == 200 and srcs[0]["name"] == "kubernetes"
    assert call(port, "GET", "/api/v1/health/live")[0] == 200
    assert call(port, "GET", "/api/v1/health/ready")[0] == 200
    app.caches["nodes"].synced = False
    status, _, body = call(port, "GET", "/api/v1/health/ready")
    assert status == 503 and "waiting for the first Kubernetes list" in body["reasons"]
    status, _, body = call(port, "GET", "/api/v1/nope")
    assert (status, body) == (404, {"error": "not found"})


def test_investigation_reads_on_old_and_new_paths(server):
    app, port = server
    rows = [{"id": uuid.UUID(int=1), "created_at": None}]
    app.approvals.connect = lambda: FakeConn([("FROM investigations", rows)])
    for path in ("/investigations", "/api/v1/investigations", "/api/v1/investigations?status=pending"):
        status, _, body = call(port, "GET", path)
        assert status == 200 and body["items"][0]["id"] == str(uuid.UUID(int=1)), path
    assert call(port, "GET", "/api/v1/investigations?status=all&limit=0")[0] == 400
    assert call(port, "GET", "/api/v1/investigations?status=weird")[0] == 400
    assert call(port, "GET", "/api/v1/investigations/not-a-uuid")[0] == 400

    def down():
        raise OSError("connection refused")

    app.approvals.connect = down
    status, _, body = call(port, "GET", "/api/v1/investigations?status=all&limit=5")
    assert (status, body) == (503, {"error": "approvals db unavailable"})


def test_mutations_require_csrf_header_and_auth(server):
    app, port = server
    path = f"/api/v1/investigations/{INV}/approve"
    status, _, body = call(port, "POST", path)
    assert status == 403 and "X-Requested-With" in body["error"]
    status, _, body = call(port, "POST", path, {"X-Requested-With": "dashboard"})
    assert status == 503 and "not configured" in body["error"]          # default: auth required, no OIDC
    assert call(port, "POST", "/api/v1/investigations/bad/approve", {"X-Requested-With": "d"})[0] == 400
    assert call(port, "POST", f"/api/v1/investigations/{INV}/explode", {"X-Requested-With": "d"})[0] == 404


def test_mutation_reaches_approvals_when_auth_disabled(server, monkeypatch):
    app, port = server
    from dashboard import config
    monkeypatch.setattr(app, "s", config.load({"DASHBOARD_AUTH_REQUIRED": "false"}))
    seen = []
    monkeypatch.setattr(app.approvals, "dismiss", lambda i, who: seen.append((i, who)) or (200, {"ok": True}))
    hdr = {"X-Requested-With": "d", "X-Forwarded-Email": "ops@example.com"}
    status, _, _ = call(port, "POST", f"/investigations/{INV}/dismiss", hdr)
    assert status == 200 and seen == [(INV, "ops@example.com")]


# --- stream ------------------------------------------------------------------------

def _read_events(resp, n):
    out, block = [], []
    while len(out) < n:
        line = resp.fp.readline().decode()
        if not line:
            break
        if line == "\n":
            if block:
                out.append(block)
            block = []
        else:
            block.append(line.rstrip("\n"))
    return out


def test_stream_sends_snapshot_sections_and_heartbeats(server, monkeypatch):
    app, port = server
    monkeypatch.setattr(api, "HEARTBEAT_S", 0.2)
    monkeypatch.setattr(api, "MIN_SECTION_GAP_S", 0.0)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", "/api/v1/stream")
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == "text/event-stream"
    first = _read_events(r, 1)[0]
    assert first[0] == "retry: 5000" and first[1] == "event: snapshot"
    assert "models" in json.loads(first[2][len("data: "):])["sections"]
    assert _read_events(r, 1)[0] == [": ping"]
    app.store.put("teams", [{"name": "new-team"}])
    ev = _read_events(r, 1)[0]
    data = json.loads(ev[1][len("data: "):])
    assert ev[0] == "event: section" and data["name"] == "teams" and data["data"] == [{"name": "new-team"}]
    c.close()


def test_stream_cap(server, monkeypatch):
    app, port = server
    monkeypatch.setattr(api, "MAX_STREAMS", 0)
    status, _, body = call(port, "GET", "/api/v1/stream")
    assert status == 503 and "poll" in body["error"]


# ---------------------------------------------------------------------------
# Fonts and static files
# ---------------------------------------------------------------------------

@pytest.fixture
def font_server(tmp_path):
    html, fonts = tmp_path / "html", tmp_path / "fonts"
    html.mkdir()
    fonts.mkdir()
    (html / "cluster-topology.html").write_text("<html>dashboard</html>")
    (html / "sub").mkdir()
    (html / "sub" / "x.txt").write_text("x")
    (fonts / "a-latin.woff2").write_bytes(b"wOF2font")
    (fonts / "OFL-A.txt").write_text("licence")
    (fonts / "notes.md").write_text("not served")
    (fonts / ".hidden.woff2").write_bytes(b"x")
    (fonts / "sub.woff2").mkdir()
    (tmp_path / "secret.woff2").write_bytes(b"outside")
    app = fixture_app()
    srv = api.Server(("127.0.0.1", 0), api.make_handler(app, str(html), str(fonts)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    app.stop.set()
    srv.shutdown()


def test_fonts_served_from_the_fixed_list(font_server):
    status, headers, body = call(font_server, "GET", "/fonts/a-latin.woff2")
    assert (status, body) == (200, b"wOF2font")
    assert headers["Content-Type"] == "font/woff2" and headers["Cache-Control"] == "public, max-age=86400"
    status, headers, body = call(font_server, "GET", "/fonts/OFL-A.txt")
    assert status == 200 and headers["Content-Type"].startswith("text/plain") and body == b"licence"


@pytest.mark.parametrize("path", ["/fonts/notes.md", "/fonts/.hidden.woff2", "/fonts/sub.woff2", "/fonts/",
                                  "/fonts/../secret.woff2", "/fonts/..%2fsecret.woff2", "/fonts/%2e%2e/secret.woff2",
                                  "/fonts/a-latin.woff2/x", "/fonts//etc/passwd"])
def test_fonts_unknown_names_are_404(font_server, path):
    assert call(font_server, "GET", path)[0] == 404


def test_no_directory_listing(font_server):
    """The HTML directory is a ConfigMap mount (with ..data subdirectories);
    it is never listed."""
    status, _, body = call(font_server, "GET", "/sub/")
    assert status == 404 and body == {"error": "not found"}


def test_no_fonts_dir_serves_nothing(tmp_path):
    assert api.load_fonts(str(tmp_path / "missing")) == {} and api.load_fonts(None) == {}
