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
            import socket
            # Use raw socket + minimal HTTP to avoid http.client buffering.
            # This ensures watch events are forwarded byte-by-byte as they arrive.
            sock = socket.create_connection((K8S_PROXY_HOST, K8S_PROXY_PORT), timeout=600)
            sock.sendall(f"GET {self.path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode())

            # Read the HTTP response header
            header_buf = b""
            while b"\r\n\r\n" not in header_buf:
                header_buf += sock.recv(1)

            headers_str = header_buf.decode()
            status_line = headers_str.split("\r\n")[0]
            status_code = int(status_line.split(" ")[1])

            content_type = "application/json"
            for line in headers_str.split("\r\n"):
                if line.lower().startswith("content-type:"):
                    content_type = line.split(":", 1)[1].strip()

            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            # Stream body with immediate flush on every recv
            sock.setblocking(False)
            import select
            while True:
                ready, _, _ = select.select([sock], [], [], 30)
                if not ready:
                    # Send empty chunk as keepalive
                    continue
                try:
                    data = sock.recv(65536)
                except (BlockingIOError, socket.error):
                    continue
                if not data:
                    break
                self.wfile.write(f"{len(data):x}\r\n".encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            sock.close()
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
