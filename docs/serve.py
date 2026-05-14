#!/usr/bin/env python3
"""Local dev server that serves static files AND streams /api/* from kubectl proxy.
Supports Kubernetes Watch API (long-lived chunked responses).

Usage:
    kubectl proxy --port=8001 &
    python3 docs/serve.py
    # Open http://localhost:8080/cluster-topology.html
"""
import http.server
import http.client
import os
import sys
import threading
from urllib.parse import urlparse

PORT = 8080
K8S_PROXY_HOST = "localhost"
K8S_PROXY_PORT = 8001
DOCS_DIR = os.path.dirname(os.path.abspath(__file__))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DOCS_DIR, **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/") or self.path.startswith("/apis/"):
            self._proxy_stream()
        else:
            super().do_GET()

    def _proxy_stream(self):
        try:
            conn = http.client.HTTPConnection(K8S_PROXY_HOST, K8S_PROXY_PORT, timeout=600)
            conn.request("GET", self.path)
            resp = conn.getresponse()

            self.send_response(resp.status)
            self.send_header("Content-Type", resp.getheader("Content-Type", "application/json"))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):x}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            conn.close()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(f'{{"error": "{str(e)}"}}'.encode())
            except Exception:
                pass

    def log_message(self, format, *args):
        msg = str(args[0]) if args else ""
        if "watch=1" in msg:
            return
        if "/api/" in msg and "200" in str(args[-1] if len(args) > 1 else ""):
            return
        super().log_message(format, *args)


class ThreadedHTTPServer(http.server.HTTPServer):
    """Handle each request in a new thread for concurrent watch streams."""
    def process_request(self, request, client_address):
        t = threading.Thread(target=self.process_request_thread, args=(request, client_address))
        t.daemon = True
        t.start()

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    print(f"Serving docs from {DOCS_DIR}")
    print(f"Proxying /api/* → http://{K8S_PROXY_HOST}:{K8S_PROXY_PORT}")
    print(f"Open http://localhost:{PORT}/cluster-topology.html")
    print()
    server = ThreadedHTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
