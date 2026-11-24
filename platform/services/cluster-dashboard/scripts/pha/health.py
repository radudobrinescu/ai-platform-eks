"""Health endpoint of the event watcher (liveness and readiness probes):
200 "ok" when the check passes, 503 with the error otherwise."""
from __future__ import annotations

import http.server
from collections.abc import Callable


def make_server(check: Callable[[], None], port: int = 8080,
                address: str = "") -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                check()
            except Exception as e:  # the probe reports any failure as 503
                self._reply(503, str(e).encode())
            else:
                self._reply(200, b"ok")

        def _reply(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return http.server.HTTPServer((address, port), Handler)


def serve(check: Callable[[], None], port: int = 8080) -> None:
    make_server(check, port).serve_forever()
