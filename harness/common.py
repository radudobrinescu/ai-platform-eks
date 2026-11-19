"""Shared helpers for the E2E harness: subprocess, kubectl, polling, port-forwards,
HTTP, and report redaction. Ported unchanged in behaviour from v1 ops/lib/e2e.py.

stdlib only; kubectl and the AWS CLI must be on PATH for live runs.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

NS_PLATFORM = "ai-platform"
NS_INFERENCE = "inference"
NS_ARGOCD = "argocd"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Redaction: reports and streamed logs never carry account ids, ARNs or AWS
# hostnames (BR5). Applied to every line the harness prints.
# --------------------------------------------------------------------------- #

_REDACTIONS = [
    (re.compile(r"arn:aws[a-z-]*:[^\s\"']+"), "arn:***"),
    (re.compile(r"\b\d{12}\b"), "************"),
    (re.compile(r"\b[a-zA-Z0-9.-]+\.amazonaws\.com\b"), "***.amazonaws.com"),
    (re.compile(r"https?://[^\s\"']+\.cloudfront\.net"), "https://***.cloudfront.net"),
    (re.compile(r"x-access-token:[^@\s]+@"), "x-access-token:***@"),
]


def redact(text: str) -> str:
    for rx, repl in _REDACTIONS:
        text = rx.sub(repl, text)
    return text


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[e2e {stamp}] {redact(msg)}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Subprocess helpers
# --------------------------------------------------------------------------- #

def sh(args: list[str], *, check: bool = True, input_: str | None = None, timeout: int | None = 300,
       cwd: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check, input=input_,
                          timeout=timeout, cwd=cwd, env=env)


def stream(args: list[str], *, cwd: str | None = None, env: dict | None = None,
           input_: str | None = None, prefix: str = "") -> int:
    """Run a long command (platformctl up/down/upgrade) and stream its combined
    output to stderr line by line, redacted. Returns the exit code."""
    proc = subprocess.Popen(args, cwd=cwd, env=env, text=True, bufsize=1,
                            stdin=subprocess.PIPE if input_ is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if input_ is not None and proc.stdin is not None:
        proc.stdin.write(input_)
        proc.stdin.close()
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stderr.write(prefix + redact(line))
    sys.stderr.flush()
    return proc.wait()


def kubectl_json(*args: str) -> dict:
    out = sh(["kubectl", *args, "-o", "json"]).stdout
    return json.loads(out) if out.strip() else {}


def secret_value(ns: str, name: str, key: str) -> str:
    raw = sh(["kubectl", "get", "secret", "-n", ns, name, "-o", f"jsonpath={{.data.{key}}}"]).stdout
    return base64.b64decode(raw).decode()


def wait_for(desc: str, fn, timeout: int, interval: int = 15):
    """Poll fn() until it returns a truthy value or timeout (seconds). Returns the value."""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:  # noqa: BLE001 - transient kubectl/HTTP errors are expected while converging
            last_err = e
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {desc}" + (f" (last error: {last_err})" if last_err else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortForward:
    """kubectl port-forward as a context manager; waits for the local port.

    kubectl exits at once when the service has no running pod at that moment (a
    rollout right after an upgrade), and then nothing will ever open the port.
    So while the port is not open, a kubectl that exited is started again."""

    OPEN_TIMEOUT = 120     # seconds until the local port must accept connections
    RESTART_PAUSE = 3      # seconds before a kubectl that gave up is started again

    def __init__(self, ns: str, target: str, remote_port: int):
        self.ns, self.target, self.remote = ns, target, remote_port
        self.local = free_port()
        self.proc: subprocess.Popen | None = None
        self._err = None

    def _start(self) -> None:
        self._err = tempfile.TemporaryFile(mode="w+")   # a file, not a pipe: nobody drains it while kubectl runs
        self.proc = subprocess.Popen(
            ["kubectl", "port-forward", "-n", self.ns, self.target, f"{self.local}:{self.remote}"],
            stdout=subprocess.DEVNULL, stderr=self._err,
        )

    def _said(self) -> str:
        if not self._err:
            return ""
        self._err.seek(0)
        text = " ".join(self._err.read().split())[-200:]
        self._err.close()
        self._err = None
        return text

    def __enter__(self):
        deadline = time.time() + self.OPEN_TIMEOUT
        said = ""
        while time.time() < deadline:
            self._start()
            while time.time() < deadline and self.proc.poll() is None:
                if self._open():
                    return self
                time.sleep(0.5)
            if self.proc.poll() is None:       # still running, and the port never opened
                break
            said = self._said() or said
            time.sleep(self.RESTART_PAUSE)
        self.__exit__(None, None, None)
        raise TimeoutError(f"timed out after {self.OPEN_TIMEOUT:g}s waiting for port-forward {self.target}"
                           + (f" (kubectl: {said})" if said else ""))

    def _open(self) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", self.local)) == 0

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._err:
            self._err.close()
            self._err = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.local}"


def http(method: str, url: str, *, headers: dict | None = None, body: dict | None = None,
         timeout: int = 60) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
