#!/usr/bin/env python3
"""Local dev server that serves static files AND proxies /api/* to kubectl proxy.
Eliminates CORS issues by making everything same-origin.

Usage:
    kubectl proxy --port=8001 &
    python3 docs/serve.py
    # Open http://localhost:8080/cluster-topology.html
"""
import http.server
import urllib.request
import urllib.error
import os
import sys

PORT = 8080
K8S_PROXY = "http://localhost:8001"
DOCS_DIR = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DOCS_DIR, **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/") or self.path.startswith("/apis/"):
            self._proxy()
        else:
            super().do_GET()

    def _proxy(self):
        url = f"{K8S_PROXY}{self.path}"
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.send_response(resp.status)
                # Forward content-type, add streaming headers
                ct = resp.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", ct)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f'{{"error": "{str(e)}"}}'.encode())

    def log_message(self, format, *args):
        if "/api/" not in str(args[0]):
            super().log_message(format, *args)


if __name__ == "__main__":
    print(f"Serving docs from {DOCS_DIR}")
    print(f"Proxying /api/* → {K8S_PROXY}")
    print(f"Open http://localhost:{PORT}/cluster-topology.html")
    server = http.server.HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
