#!/usr/bin/env python3
"""E2E smoke runner for the component upgrade lifecycle (COMPATIBILITY.md).

Runs against an ALREADY PROVISIONED cluster (platformctl e2e handles up/down) and
answers one question per upgrade domain: does this set of component versions
work together on a real cluster? It is deliberately a *smoke* suite — one
representative, end-to-end path per domain — not a conformance suite.

Domains and checks (see COMPATIBILITY.md "Upgrade domains"):
  gitops     ArgoCD Applications Synced+Healthy; KRO ResourceGraphDefinitions Active
  infra      EKS cluster version == tfvars; managed addons ACTIVE; ALB provisioned;
             a Karpenter NodeClaim became Ready (observed via the GPU check)
  gateway    LiteLLM ready; a BedrockModel enrolls and answers on /v1; the call
             lands a Langfuse trace; deleting the CR deregisters the alias;
             cluster-dashboard /healthz
  inference  a VLLMEndpoint (small ungated model) becomes Ready on a GPU node and
             answers on /v1; DCGM exporter exposes GPU metrics
  inference+ (--with-llmd) an LLMDEndpoint routes a request (second GPU node)

Model CRs are produced by `recommend-instance.py --emit` (the same code path
operators use) and applied with kubectl into the `inference` namespace. ArgoCD's
prune only removes resources carrying its tracking label, so these are safe and
are deleted in a finally-block regardless of outcome.

Output: human log on stderr; --report-json / --report-md write the report. The
report REDACTS account ids, ARNs and *.amazonaws.com hostnames so it can be
posted on a public PR (NFR2); it names checks, outcomes, durations and the
component versions under test (read from the repo, not the cluster).

Dependencies: python3 stdlib + kubectl + aws CLI on PATH. No boto3 needed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

NS_PLATFORM = "ai-platform"
NS_INFERENCE = "inference"
NS_ARGOCD = "argocd"
E2E_LABEL = "ai-platform.e2e/run"

# Smallest ungated chat model that vLLM serves without special flags; fits any
# GPU NodePool instance. Keep the Bedrock pick cheap and widely enabled.
GPU_SMOKE_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
BEDROCK_SMOKE_MODEL = "nova-lite"


# --------------------------------------------------------------------------- #
# Infrastructure helpers
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"[e2e {datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def sh(args: list[str], *, check: bool = True, input_: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check, input=input_, timeout=timeout)


def kubectl_json(*args: str) -> dict:
    out = sh(["kubectl", *args, "-o", "json"]).stdout
    return json.loads(out) if out.strip() else {}


def wait_for(desc: str, fn, timeout: int, interval: int = 15):
    """Poll fn() until it returns a truthy value or timeout (seconds). Returns the value."""
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:  # noqa: BLE001 — transient kubectl/HTTP errors are expected while converging
            last_err = e
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {desc}" + (f" (last error: {last_err})" if last_err else ""))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PortForward:
    """kubectl port-forward as a context manager; waits for the local port."""

    def __init__(self, ns: str, target: str, remote_port: int):
        self.ns, self.target, self.remote = ns, target, remote_port
        self.local = free_port()
        self.proc: subprocess.Popen | None = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            ["kubectl", "port-forward", "-n", self.ns, self.target, f"{self.local}:{self.remote}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_for(f"port-forward {self.target}", lambda: self._open(), timeout=60, interval=1)
        return self

    def _open(self) -> bool:
        with socket.socket() as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", self.local)) == 0

    def __exit__(self, *exc) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.local}"


def http(method: str, url: str, *, headers: dict | None = None, body: dict | None = None, timeout: int = 60) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def secret_value(ns: str, name: str, key: str) -> str:
    raw = sh(["kubectl", "get", "secret", "-n", ns, name, "-o", f"jsonpath={{.data.{key}}}"]).stdout
    return base64.b64decode(raw).decode()


# --------------------------------------------------------------------------- #
# Report model
# --------------------------------------------------------------------------- #

@dataclass
class CheckResult:
    domain: str
    name: str
    ok: bool
    detail: str = ""
    seconds: float = 0.0
    skipped: bool = False


@dataclass
class Report:
    env: str
    started: str
    versions: dict = field(default_factory=dict)
    results: list[CheckResult] = field(default_factory=list)
    finished: str = ""

    @property
    def passed(self) -> bool:
        return all(r.ok or r.skipped for r in self.results)


_REDACTIONS = [
    (re.compile(r"arn:aws[a-z-]*:[^\s\"']+"), "arn:***"),
    (re.compile(r"\b\d{12}\b"), "************"),
    (re.compile(r"\b[a-zA-Z0-9.-]+\.amazonaws\.com\b"), "***.amazonaws.com"),
    (re.compile(r"https?://[^\s\"']+\.cloudfront\.net"), "https://***.cloudfront.net"),
]


def redact(text: str) -> str:
    for rx, repl in _REDACTIONS:
        text = rx.sub(repl, text)
    return text


def render_markdown(rep: Report) -> str:
    icon = "✅ PASSED" if rep.passed else "❌ FAILED"
    lines = [f"## E2E gate — `{rep.env}` — {icon}", "",
             f"Started {rep.started} · finished {rep.finished or '—'}", ""]
    if rep.versions:
        lines += ["### Component versions under test", "", "| Component | Version |", "|---|---|"]
        lines += [f"| {k} | `{v}` |" for k, v in sorted(rep.versions.items())]
        lines.append("")
    lines += ["### Checks", "", "| Domain | Check | Result | Time | Detail |", "|---|---|---|---|---|"]
    for r in rep.results:
        res = "⏭ skipped" if r.skipped else ("✅ pass" if r.ok else "❌ fail")
        lines.append(f"| {r.domain} | {r.name} | {res} | {r.seconds:.0f}s | {redact(r.detail)} |")
    lines.append("")
    lines.append("<sub>Identifiers (account, ARNs, AWS hostnames) are redacted; this report is safe to post publicly.</sub>")
    return "\n".join(lines) + "\n"


def to_json(rep: Report) -> str:
    d = {"env": rep.env, "started": rep.started, "finished": rep.finished, "passed": rep.passed,
         "versions": rep.versions,
         "results": [{**r.__dict__, "detail": redact(r.detail)} for r in rep.results]}
    return json.dumps(d, indent=2)


def versions_under_test(repo_root: str) -> dict:
    """Read the pinned component versions from the repo (the thing under test)."""
    v: dict[str, str] = {}

    def grab(rel: str, rx: str, key: str, group: int = 1):
        try:
            with open(os.path.join(repo_root, rel), encoding="utf-8") as fh:
                m = re.search(rx, fh.read())
            if m:
                v[key] = m.group(group)
        except OSError:
            pass

    grab("terraform/30.eks/30.cluster/locals.tf", r'vllm_image_tag\s*=\s*"([^"]+)"', "vllm")
    appset = os.path.join(repo_root, "argocd/bootstrap/templates/platform.yaml")
    try:
        with open(appset, encoding="utf-8") as fh:
            for m in re.finditer(r"chart:\s*(\S+)\s*\n\s*chartRepo:\s*\S+\s*\n\s*chartVersion:\s*\"?([^\"\s]+)", fh.read()):
                v[f"chart/{m.group(1)}"] = m.group(2)
    except OSError:
        pass
    grab("platform/services/litellm/litellm.yaml", r"image:\s*litellm/litellm:(\S+)", "litellm")
    grab("platform/services/open-webui/open-webui.yaml", r"image:\s*ghcr.io/open-webui/open-webui:(\S+)", "open-webui")
    grab("platform/services/inference-gateway/kustomization.yaml", r"gateway-api-inference-extension/[^\s?]*\?ref=(v?[0-9][^\s\"]*)", "gie-crds")
    grab("terraform/30.eks/30.cluster/karpenter.tf", r'version\s*=\s*"(\d[^"]*)"', "karpenter")
    grab("terraform/30.eks/35.addons/main.tf", r'chart_version\s*=\s*"([^"]+)"', "aws-load-balancer-controller")
    grab("terraform/30.eks/35.addons/keda.tf", r'version\s*=\s*"([^"]+)"', "keda")
    return v


# --------------------------------------------------------------------------- #
# Checks. Each returns (ok, detail). Registry at the bottom.
# --------------------------------------------------------------------------- #

class Ctx:
    def __init__(self, a: argparse.Namespace):
        self.env, self.cluster, self.region, self.repo_root = a.env, a.cluster, a.region, a.repo_root
        self.with_llmd = a.with_llmd
        self.t = {"converge": a.timeout_converge, "gpu": a.timeout_gpu, "bedrock": a.timeout_bedrock}
        self.run_start = datetime.now(timezone.utc)
        self.master_key: str | None = None
        self.gpu_node_seen = False

    def litellm_key(self) -> str:
        if self.master_key is None:
            self.master_key = secret_value(NS_PLATFORM, "litellm-secrets", "master-key")
        return self.master_key

    def tfvars_k8s_version(self) -> str | None:
        p = os.path.join(self.repo_root, "terraform/00.global/vars", f"{self.env}.tfvars")
        try:
            with open(p, encoding="utf-8") as fh:
                m = re.search(r'kubernetes_version\s*=\s*"([^"]+)"', fh.read())
            return m.group(1) if m else None
        except OSError:
            return None


def chk_argocd_converged(c: Ctx):
    def state():
        apps = kubectl_json("get", "applications", "-n", NS_ARGOCD).get("items", [])
        if not apps:
            return None
        bad = [a["metadata"]["name"] for a in apps
               if a.get("status", {}).get("sync", {}).get("status") != "Synced"
               or a.get("status", {}).get("health", {}).get("status") != "Healthy"]
        return (len(apps), bad) if not bad else None
    n, _ = wait_for("ArgoCD apps Synced+Healthy", state, c.t["converge"], 20)
    return True, f"{n} applications Synced/Healthy"


def chk_kro_rgds_active(c: Ctx):
    def state():
        items = kubectl_json("get", "resourcegraphdefinitions").get("items", [])
        if not items:
            return None
        inactive = [i["metadata"]["name"] for i in items if i.get("status", {}).get("state") != "Active"]
        return (len(items), inactive) if not inactive else None
    n, _ = wait_for("KRO RGDs Active", state, 600, 15)
    return True, f"{n} RGDs Active"


def chk_cluster_version(c: Ctx):
    want = c.tfvars_k8s_version()
    out = sh(["aws", "eks", "describe-cluster", "--name", c.cluster, "--query", "cluster.version", "--output", "text"]).stdout.strip()
    ok = want is None or out == want
    return ok, f"cluster {out}" + ("" if ok else f" != tfvars {want}")


def chk_addons_active(c: Ctx):
    names = json.loads(sh(["aws", "eks", "list-addons", "--cluster-name", c.cluster]).stdout)["addons"]
    bad = []
    for n in names:
        st = sh(["aws", "eks", "describe-addon", "--cluster-name", c.cluster, "--addon-name", n,
                 "--query", "addon.status", "--output", "text"]).stdout.strip()
        if st != "ACTIVE":
            bad.append(f"{n}={st}")
    return not bad, f"{len(names)} addons ACTIVE" if not bad else "; ".join(bad)


def chk_alb_provisioned(c: Ctx):
    def state():
        items = kubectl_json("get", "ingress", "-n", NS_PLATFORM).get("items", [])
        if not items:
            return None
        hosts = [((i.get("status") or {}).get("loadBalancer") or {}).get("ingress") or [] for i in items]
        return len(items) if all(h and h[0].get("hostname") for h in hosts) else None
    n = wait_for("ALB hostnames on ingresses", state, 900, 20)
    return True, f"{n} ingress(es) have a load balancer hostname"


def chk_litellm_ready(c: Ctx):
    wait_for("litellm pod Ready", lambda: sh(["kubectl", "wait", "-n", NS_PLATFORM, "--for=condition=Ready", "pod", "-l", "app=litellm", "--timeout=30s"], check=False).returncode == 0, 900, 10)
    with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
        code, body = http("GET", f"{pf.base}/health/readiness")
    return code == 200, f"readiness HTTP {code}" + ("" if code == 200 else f": {body[:120]}")


def _emit_cr(c: Ctx, path: str, *args: str) -> None:
    cmd = [sys.executable, os.path.join(c.repo_root, "ops/lib/recommend-instance.py"), *args, "--emit", path]
    if c.region:
        cmd += ["--region", c.region]
    sh(cmd, timeout=600)


def _apply_labeled(path: str) -> None:
    sh(["kubectl", "apply", "-n", NS_INFERENCE, "-f", path])
    # Label for cleanup/traceability (kubectl apply strips nothing; label after).
    kind_name = sh(["kubectl", "get", "-n", NS_INFERENCE, "-f", path, "-o", "name"]).stdout.strip()
    sh(["kubectl", "label", "-n", NS_INFERENCE, kind_name, f"{E2E_LABEL}=true", "--overwrite"])


def _delete_if_exists(path: str) -> None:
    sh(["kubectl", "delete", "-n", NS_INFERENCE, "-f", path, "--ignore-not-found", "--wait=false"], check=False)


def _models(pf: PortForward, key: str) -> list[str]:
    code, body = http("GET", f"{pf.base}/v1/models", headers={"Authorization": f"Bearer {key}"})
    if code != 200:
        return []
    return [m["id"] for m in json.loads(body).get("data", [])]


def _chat(pf: PortForward, key: str, model: str) -> tuple[int, str]:
    return http("POST", f"{pf.base}/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                body={"model": model, "messages": [{"role": "user", "content": "Reply with the single word: pong"}], "max_tokens": 8}, timeout=120)


def chk_bedrock_roundtrip(c: Ctx):
    """Enroll nova-lite via --emit + kubectl, chat through /v1, verify a Langfuse
    trace, delete the CR and verify deregistration. Covers the whole gateway
    domain in one path (LiteLLM + litellm-sync + Bedrock IRSA + Langfuse)."""
    path = f"/tmp/e2e-bedrock-{c.env}.yaml"
    _emit_cr(c, path, "--source", "bedrock", BEDROCK_SMOKE_MODEL)
    try:
        _apply_labeled(path)
        key = c.litellm_key()
        with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
            alias = wait_for("Bedrock alias in /v1/models",
                             lambda: next((m for m in _models(pf, key) if BEDROCK_SMOKE_MODEL in m), None),
                             c.t["bedrock"], 10)
            code, body = _chat(pf, key, alias)
            if code != 200:
                return False, f"chat via {alias} HTTP {code}: {body[:160]}"
        trace_ok, trace_detail = _langfuse_has_trace_since(c)
        _delete_if_exists(path)
        with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
            wait_for("Bedrock alias deregistered", lambda: alias not in _models(pf, key), 300, 10)
        return trace_ok, f"{alias} answered 200; deregistered on delete; {trace_detail}"
    finally:
        _delete_if_exists(path)


def _langfuse_has_trace_since(c: Ctx) -> tuple[bool, str]:
    pk = secret_value(NS_PLATFORM, "langfuse-litellm-keys", "LANGFUSE_PUBLIC_KEY")
    sk = secret_value(NS_PLATFORM, "langfuse-litellm-keys", "LANGFUSE_SECRET_KEY")
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    since = c.run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    with PortForward(NS_PLATFORM, "svc/langfuse-web", 3000) as pf:
        def traces():
            code, body = http("GET", f"{pf.base}/api/public/traces?fromTimestamp={since}&limit=5",
                              headers={"Authorization": f"Basic {auth}"})
            return len(json.loads(body).get("data", [])) if code == 200 else None
        try:
            n = wait_for("Langfuse trace", traces, 180, 10)
            return True, f"{n} Langfuse trace(s) since run start"
        except TimeoutError as e:
            return False, str(e)


def chk_gpu_model_roundtrip(c: Ctx):
    """VLLMEndpoint for a tiny ungated model: Karpenter node -> vLLM Ready ->
    LiteLLM answers. The inference domain and the Karpenter half of infra."""
    path = f"/tmp/e2e-vllm-{c.env}.yaml"
    _emit_cr(c, path, GPU_SMOKE_MODEL, "--tier", "vllm", "--users", "1", "--seq", "2048")
    try:
        _apply_labeled(path)
        name = sh(["kubectl", "get", "-n", NS_INFERENCE, "-f", path, "-o", "jsonpath={.metadata.name}"]).stdout.strip()

        def ready():
            ep = kubectl_json("get", "vllmendpoint", "-n", NS_INFERENCE, name)
            return (ep.get("status") or {}).get("ready") == "True"
        wait_for(f"VLLMEndpoint {name} Ready", ready, c.t["gpu"], 20)
        claims = kubectl_json("get", "nodeclaims").get("items", [])
        c.gpu_node_seen = any(
            any(cond.get("type") == "Ready" and cond.get("status") == "True" for cond in (cl.get("status") or {}).get("conditions", []))
            for cl in claims)
        key = c.litellm_key()
        with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
            alias = wait_for("GPU model in /v1/models", lambda: next((m for m in _models(pf, key) if m == name), None), 300, 10)
            code, body = _chat(pf, key, alias)
        return code == 200, f"{name} Ready on GPU; chat HTTP {code}" + ("" if code == 200 else f": {body[:160]}")
    finally:
        _delete_if_exists(path)


def chk_karpenter_nodeclaim(c: Ctx):
    # Evaluated after the GPU check populated the observation.
    return c.gpu_node_seen, "a Karpenter NodeClaim reached Ready during the GPU check" if c.gpu_node_seen else "no Ready NodeClaim observed"


def chk_dcgm_metrics(c: Ctx):
    pods = kubectl_json("get", "pods", "-n", "gpu-operator", "-l", "app=nvidia-dcgm-exporter").get("items", [])
    if not pods:
        return False, "no nvidia-dcgm-exporter pods (GPU node gone already, or operator not ready)"
    pod = pods[0]["metadata"]["name"]
    with PortForward("gpu-operator", f"pod/{pod}", 9400) as pf:
        code, body = http("GET", f"{pf.base}/metrics")
    return code == 200 and "DCGM_FI_DEV_GPU_UTIL" in body, f"dcgm-exporter HTTP {code}, GPU util metric {'present' if 'DCGM_FI_DEV_GPU_UTIL' in body else 'absent'}"


def chk_dashboard_health(c: Ctx):
    # The dashboard's own liveness/readiness probes hit /data.json (there is no
    # /healthz); it returns the live topology JSON, so a 200 proves the backend
    # is up AND can read the cluster.
    with PortForward(NS_PLATFORM, "svc/cluster-dashboard", 9090) as pf:
        code, body = http("GET", f"{pf.base}/data.json")
    ok = code == 200 and body.lstrip().startswith("{")
    return ok, f"/data.json HTTP {code}" + ("" if ok else f": {body[:80]}")


def chk_llmd_roundtrip(c: Ctx):
    path = f"/tmp/e2e-llmd-{c.env}.yaml"
    _emit_cr(c, path, GPU_SMOKE_MODEL, "--tier", "llm-d", "--users", "4", "--seq", "2048")
    try:
        _apply_labeled(path)
        name = sh(["kubectl", "get", "-n", NS_INFERENCE, "-f", path, "-o", "jsonpath={.metadata.name}"]).stdout.strip()
        wait_for(f"LLMDEndpoint {name} Ready",
                 lambda: (kubectl_json("get", "llmdendpoint", "-n", NS_INFERENCE, name).get("status") or {}).get("ready") == "True",
                 c.t["gpu"], 20)
        key = c.litellm_key()
        with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
            alias = wait_for("llm-d model in /v1/models", lambda: next((m for m in _models(pf, key) if m == name), None), 300, 10)
            code, _body = _chat(pf, key, alias)
        return code == 200, f"{name} routed via llm-d; chat HTTP {code}"
    finally:
        _delete_if_exists(path)


# (domain, name, fn, optional-flag). Order matters: GPU before the checks that
# depend on a GPU node having existed (NodeClaim, DCGM).
CHECKS: list[tuple[str, str, object, str | None]] = [
    ("gitops",    "argocd-applications-converged",  chk_argocd_converged,   None),
    ("gitops",    "kro-rgds-active",                chk_kro_rgds_active,    None),
    ("infra",     "eks-cluster-version",            chk_cluster_version,    None),
    ("infra",     "eks-managed-addons-active",      chk_addons_active,      None),
    ("infra",     "alb-provisioned",                chk_alb_provisioned,    None),
    ("gateway",   "litellm-ready",                  chk_litellm_ready,      None),
    ("gateway",   "bedrock-enroll-chat-trace-deregister", chk_bedrock_roundtrip, None),
    ("gateway",   "cluster-dashboard-healthz",      chk_dashboard_health,   None),
    ("inference", "vllmendpoint-gpu-chat",          chk_gpu_model_roundtrip, None),
    ("infra",     "karpenter-nodeclaim-ready",      chk_karpenter_nodeclaim, None),
    ("inference", "dcgm-gpu-metrics",               chk_dcgm_metrics,       None),
    ("inference", "llmdendpoint-routed-chat",       chk_llmd_roundtrip,     "with_llmd"),
]

DOMAINS = sorted({d for d, *_ in CHECKS})


def run(a: argparse.Namespace) -> Report:
    c = Ctx(a)
    rep = Report(env=a.env, started=c.run_start.strftime("%Y-%m-%dT%H:%M:%SZ"), versions=versions_under_test(a.repo_root))
    for domain, name, fn, flag in CHECKS:
        if flag and not getattr(a, flag):
            rep.results.append(CheckResult(domain, name, True, f"skipped (enable with --{flag.replace('_', '-')})", 0, skipped=True))
            continue
        log(f"[{domain}] {name} …")
        t0 = time.time()
        try:
            ok, detail = fn(c)
        except Exception as e:  # noqa: BLE001 — a check must never abort the suite; it fails instead
            ok, detail = False, f"{type(e).__name__}: {e}"
        r = CheckResult(domain, name, bool(ok), str(detail), time.time() - t0)
        rep.results.append(r)
        log(f"[{domain}] {name}: {'PASS' if ok else 'FAIL'} ({r.seconds:.0f}s) {redact(r.detail)}")
        if not ok and a.fail_fast:
            break
    rep.finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E2E smoke checks per upgrade domain (cluster must already be up).")
    ap.add_argument("--env", required=True)
    ap.add_argument("--cluster", required=True)
    ap.add_argument("--region", default="")
    ap.add_argument("--repo-root", default=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    ap.add_argument("--with-llmd", action="store_true", help="also exercise the llm-d scale tier (second GPU node)")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--timeout-converge", type=int, default=1800)
    ap.add_argument("--timeout-gpu", type=int, default=1500)
    ap.add_argument("--timeout-bedrock", type=int, default=300)
    ap.add_argument("--report-json", default=None)
    ap.add_argument("--report-md", default=None)
    a = ap.parse_args(argv)
    if a.region:
        os.environ["AWS_REGION"] = a.region

    rep = run(a)
    md = render_markdown(rep)
    if a.report_md:
        with open(a.report_md, "w", encoding="utf-8") as fh:
            fh.write(md)
    if a.report_json:
        with open(a.report_json, "w", encoding="utf-8") as fh:
            fh.write(to_json(rep))
    print(md)
    return 0 if rep.passed else 1


if __name__ == "__main__":
    sys.exit(main())
