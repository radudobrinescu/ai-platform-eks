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
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
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


def test_investigation_reads(server):
    app, port = server
    rows = [{"id": uuid.UUID(int=1), "created_at": None}]
    app.approvals.connect = lambda: FakeConn([("FROM investigations", rows)])
    for path in ("/api/v1/investigations", "/api/v1/investigations?status=pending"):
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
    status, _, _ = call(port, "POST", f"/api/v1/investigations/{INV}/dismiss", hdr)
    assert status == 200 and seen == [(INV, "ops@example.com")]


def test_pre_rebuild_investigation_paths_are_gone(server):
    app, port = server
    hdr = {"X-Requested-With": "d"}
    assert call(port, "GET", "/investigations")[0] == 404
    assert call(port, "GET", f"/investigations/{INV}")[0] == 404
    assert call(port, "POST", f"/investigations/{INV}/approve", hdr)[0] == 404
    assert call(port, "DELETE", "/investigations", hdr)[0] == 404


# --- request bodies on keep-alive connections ---------------------------------------

def _exchange(conn, method, path, body=None, headers=None):
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    raw = r.read()
    return r, (json.loads(raw) if raw and r.getheader("Content-Type") == "application/json" else raw)


@pytest.mark.parametrize("method,path,body,expected", [
    ("POST", f"/api/v1/investigations/{INV}/approve", b'{"a":1}', 403),      # refused: no X-Requested-With
    ("POST", f"/api/v1/investigations/{INV}/approve", b'{"reason": "looks right"}', 403),
    ("DELETE", "/api/v1/investigations", b"x" * 4096, 403),
    ("GET", "/api/v1/sources", b"ignored", 200),
    ("PUT", "/api/v1/sources", b"data", 501),
], ids=["post-json", "post-json-with-spaces", "delete-4k", "get", "unsupported-method"])
def test_request_body_is_consumed_before_the_next_request(server, method, path, body, expected):
    """oauth2-proxy reuses its connections to the dashboard. Body bytes left
    unread would be parsed as the start of the next request on the connection,
    which may be another user's."""
    _app, port = server
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    r, _ = _exchange(c, method, path, body)
    assert r.status == expected
    if expected == 501:               # the standard library closes after an unsupported method
        c.close()
    r, sources = _exchange(c, "GET", "/api/v1/sources")
    assert r.status == 200 and sources[0]["name"] == "kubernetes"
    c.close()


def test_two_mutations_with_bodies_on_one_connection(server, monkeypatch):
    app, port = server
    from dashboard import config
    monkeypatch.setattr(app, "s", config.load({"DASHBOARD_AUTH_REQUIRED": "false"}))
    seen = []
    monkeypatch.setattr(app.approvals, "dismiss", lambda i, who: seen.append(i) or (200, {"ok": True}))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    for _ in range(2):
        r, body = _exchange(c, "POST", f"/api/v1/investigations/{INV}/dismiss", b'{"note":"x"}',
                            {"X-Requested-With": "d", "Content-Type": "application/json"})
        assert (r.status, body) == (200, {"ok": True})
    c.close()
    assert seen == [INV, INV]


@pytest.mark.parametrize("headers,status", [
    ({"Content-Length": str(api.MAX_BODY_BYTES + 1)}, 413),
    ({"Content-Length": "10000000000"}, 413),
    ({"Content-Length": "-5"}, 400),
    ({"Content-Length": "many"}, 400),
    ({"Transfer-Encoding": "chunked"}, 411),
], ids=["over-limit", "absurd", "negative", "not-a-number", "chunked"])
def test_unreadable_body_is_refused_and_the_connection_closed(server, monkeypatch, headers, status):
    """A body that will not be read (too large, no usable length) must not
    stay on a reusable connection: the request is refused, the mutation does
    not run and the server closes the connection."""
    app, port = server
    monkeypatch.setattr(app.approvals, "dismiss", lambda *a: pytest.fail("must not run"))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("POST", f"/api/v1/investigations/{INV}/dismiss")
    for k, v in {"X-Requested-With": "d", **headers}.items():
        c.putheader(k, v)
    c.endheaders()                    # the body is never sent: the server must answer without waiting for it
    r = c.getresponse()
    assert r.status == status and r.getheader("Connection") == "close" and "error" in json.loads(r.read())
    assert c.sock is None, "http.client drops the socket when the server says close"


def test_body_cut_short_closes_the_connection(server):
    import socket
    _app, port = server
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"POST /api/v1/investigations/x/approve HTTP/1.1\r\nHost: x\r\nContent-Length: 50\r\n\r\nshort")
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(4096) == b"", "no response to a request that never completed"


def test_body_length_rules():
    from email.message import Message

    def length(**headers):
        m = Message()
        for k, v in headers.items():
            for one in v if isinstance(v, list) else [v]:
                m[k.replace("_", "-")] = one
        return api.body_length(m)

    assert length() == (0, None)
    assert length(Content_Length="0") == (0, None)
    assert length(Content_Length=" 12 ") == (12, None)
    assert length(Content_Length=str(api.MAX_BODY_BYTES)) == (api.MAX_BODY_BYTES, None)
    assert length(Content_Length=["5", "6"])[1][0] == 400, "two lengths: which one is it?"
    assert length(Content_Length="\u00b2")[1][0] == 400, "a digit, but not a number"
    assert length(Content_Length="5", Transfer_Encoding="chunked")[1][0] == 411


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
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
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
