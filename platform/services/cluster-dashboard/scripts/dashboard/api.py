"""HTTP API and static files (standard library server, one thread per request
or open stream). Every read is served from the store; no request waits on an
upstream except the approval mutations and investigation reads (agent DB).

Routes (R4, R5 in the U2 functional design):
  GET  /api/v1/snapshot          versioned sections, ETag, 304
  GET  /api/v1/stream            server-sent events
  GET  /api/v1/sources           source status
  GET  /api/v1/health/live|ready probes
  GET  /api/v1/investigations[?status=pending|all&limit=N], /api/v1/investigations/<id>
  POST /api/v1/investigations/<id>/approve|dismiss
  DELETE /api/v1/investigations[/<id>]
  GET  /data.json                pre-rebuild document (compatibility)
  GET  /fonts/<file>             the UI fonts and their licences (fixed list, read at start-up)
  GET  /                         the UI (cluster-topology.html); no directory listings
  /investigations...             pre-rebuild paths of the same handlers (until the UI moves)
"""
from __future__ import annotations

import http.server
import json
import os
import logging
import re
import threading
import time
import uuid
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from dashboard.auth import Verifier, authorize_mutation

log = logging.getLogger("dashboard.api")

HEARTBEAT_S = 15
MIN_SECTION_GAP_S = 1.0
MAX_STREAMS = 8
_INV = re.compile(r"^/(?:api/v1/)?investigations(?:/([^/]+))?(?:/(approve|dismiss))?/?$")
_QUIET = ("/api/v1/health/", "/api/v1/stream")


def _json_default(o):
    if isinstance(o, Decimal):       # psycopg NUMERIC columns
        return float(o)
    return str(o)


class _Streams:
    def __init__(self):
        self.n, self.lock = 0, threading.Lock()

    def acquire(self) -> bool:
        with self.lock:
            if self.n >= MAX_STREAMS:
                return False
            self.n += 1
            return True

    def release(self) -> None:
        with self.lock:
            self.n -= 1


_FONT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(woff2|txt)$")
_FONT_TYPES = {"woff2": "font/woff2", "txt": "text/plain; charset=utf-8"}


def load_fonts(fonts_dir: str | None) -> dict[str, tuple[str, bytes]]:
    """{file name: (content type, bytes)} of the font files (and their
    licences) in `fonts_dir`, read once at start-up. Requests are answered
    only from this table, so no request path ever reaches the file system."""
    out: dict[str, tuple[str, bytes]] = {}
    if not fonts_dir or not os.path.isdir(fonts_dir):
        return out
    for name in sorted(os.listdir(fonts_dir)):
        m = _FONT_NAME.match(name)
        path = os.path.join(fonts_dir, name)
        if m and os.path.isfile(path):
            with open(path, "rb") as f:
                out[name] = (_FONT_TYPES[m.group(1)], f.read())
    return out


def make_handler(app, html_dir: str, fonts_dir: str | None = None):
    verifier = Verifier(app.s)
    streams = _Streams()
    fonts = load_fonts(fonts_dir)

    class Handler(http.server.SimpleHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __init__(self, *a, **kw):
            super().__init__(*a, directory=html_dir, **kw)

        # -- plumbing ---------------------------------------------------------------
        def log_message(self, *_a):            # replaced by _access_log
            pass

        def list_directory(self, path):        # never list the HTML directory
            self._json(404, {"error": "not found"})
            return None

        def send_response(self, code, message=None):
            self._status = code
            super().send_response(code, message)

        def handle_one_request(self):
            self._t0, self._status = time.monotonic(), 0
            super().handle_one_request()
            if getattr(self, "command", None):
                self._access_log()

        def _access_log(self):
            path = urlparse(self.path).path
            quiet = path.startswith(_QUIET) or not (path.startswith("/api/") or path in ("/data.json",)
                                                    or path.startswith("/investigations"))
            log.log(logging.DEBUG if quiet and self.command == "GET" else logging.INFO, "http.request",
                    extra={"method": self.command, "path": path, "status": self._status,
                           "ms": round((time.monotonic() - self._t0) * 1000)})

        def _json(self, status: int, body, headers: dict | None = None) -> None:
            data = json.dumps(body, default=_json_default).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, fn, *args):
            try:
                return fn(*args)
            except (BrokenPipeError, ConnectionResetError):
                return None
            except Exception:  # unexpected: log with the stack, answer without details
                log.exception("request failed", extra={"path": urlparse(self.path).path})
                if not getattr(self, "_headers_buffer", None):
                    try:
                        self._json(500, {"error": "internal error"})
                    except OSError:
                        pass
                return None

        # -- verbs --------------------------------------------------------------------
        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._mutate, "POST")

        def do_DELETE(self):
            self._guard(self._mutate, "DELETE")

        # -- GET ----------------------------------------------------------------------
        def _get(self):
            url = urlparse(self.path)
            path = url.path
            if path in ("", "/"):
                self.path = "/cluster-topology.html"
                return super().do_GET()
            if path == "/data.json":
                return self._json(200, app.store.data_json())
            if path.startswith("/fonts/"):
                return self._font(path[len("/fonts/"):])
            if path == "/api/v1/snapshot":
                version, sections = app.store.get()
                etag = f'"v{version}"'
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._json(200, {"version": version, "sections": sections}, {"ETag": etag})
            if path == "/api/v1/stream":
                return self._stream()
            if path == "/api/v1/sources":
                return self._json(200, [st.to_dict() for st in app.statuses()])
            if path in ("/api/v1/health/live", "/api/v1/health/ready"):
                live, ready, body = app.health()
                ok = live if path.endswith("live") else ready
                return self._json(200 if ok else 503, body)
            m = _INV.match(path)
            if m and not m.group(2):
                return self._investigations_get(m.group(1), parse_qs(url.query))
            if path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            return super().do_GET()

        def _font(self, name: str):
            font = fonts.get(name)
            if font is None:
                return self._json(404, {"error": "not found"})
            ctype, data = font
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None

        def _investigations_get(self, inv_id, query):
            try:
                return self._investigations_read(inv_id, query)
            except Exception as e:  # agent DB down or psycopg error
                log.warning("investigations read failed", extra={"error": f"{type(e).__name__}: {e}"})
                return self._json(503, {"error": "approvals db unavailable"})

        def _investigations_read(self, inv_id, query):
            if inv_id is None or inv_id == "all":
                status = "all" if inv_id == "all" else (query.get("status") or ["pending"])[0]
                if status not in ("pending", "all"):
                    return self._json(400, {"error": "status must be pending or all"})
                if status == "pending":
                    return self._json(200, {"items": app.approvals.pending()})
                try:
                    limit = int((query.get("limit") or ["20"])[0])
                except ValueError:
                    limit = 0
                if not 1 <= limit <= 100:
                    return self._json(400, {"error": "limit must be 1-100"})
                return self._json(200, {"items": app.approvals.recent(limit)})
            if not _is_uuid(inv_id):
                return self._json(400, {"error": "invalid investigation id"})
            inv = app.approvals.get(inv_id)
            return self._json(200, inv) if inv else self._json(404, {"error": "not found"})

        # -- mutations ------------------------------------------------------------------
        def _mutate(self, verb: str):
            m = _INV.match(urlparse(self.path).path)
            if not m or (verb == "POST" and not (m.group(1) and m.group(2))) or (verb == "DELETE" and m.group(2)):
                return self._json(404, {"error": "not found"})
            inv_id, action = m.group(1), m.group(2)
            if inv_id is not None and not _is_uuid(inv_id):
                return self._json(400, {"error": "invalid investigation id"})
            approver, status, error = authorize_mutation(self.headers, app.s, verifier)
            if approver is None:
                return self._json(status, {"error": error})
            try:
                if verb == "POST":
                    fn = app.approvals.approve if action == "approve" else app.approvals.dismiss
                    status, body = fn(inv_id, approver)
                else:
                    status, body = app.approvals.delete(inv_id)
            except Exception as e:  # agent DB down mid-request
                log.warning("approval failed", extra={"investigation": inv_id, "error": f"{type(e).__name__}: {e}"})
                status, body = 503, {"error": "approvals db unavailable"}
            return self._json(status, body)

        # -- stream ---------------------------------------------------------------------
        def _stream(self):
            if not streams.acquire():
                return self._json(503, {"error": "too many open streams; poll /api/v1/snapshot"})
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                version, sections = app.store.get()
                self._event("snapshot", {"version": version, "sections": sections}, retry=5000)
                sent_at: dict = {}
                pending: dict = {}
                while not app.stop.is_set():
                    new = app.store.wait(version, HEARTBEAT_S)
                    if new == version and not pending:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    version, changed = app.store.changed_since(version)
                    pending.update(changed)
                    now = time.monotonic()
                    for name in list(pending):
                        if now - sent_at.get(name, 0) >= MIN_SECTION_GAP_S:
                            self._event("section", {"name": name, "version": version, "data": pending.pop(name)})
                            sent_at[name] = now
                    if pending:
                        time.sleep(MIN_SECTION_GAP_S)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass                                  # client left
            finally:
                streams.release()
            return None

        def _event(self, kind: str, data, retry: int | None = None) -> None:
            head = f"retry: {retry}\n" if retry else ""
            payload = json.dumps(data, default=_json_default, separators=(",", ":"))
            self.wfile.write(f"{head}event: {kind}\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()

    return Handler


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(app, port: int, html_dir: str, fonts_dir: str | None = None) -> None:
    Server(("", port), make_handler(app, html_dir, fonts_dir)).serve_forever()
