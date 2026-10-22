"""Smoke suite: one representative end-to-end path per upgrade domain.

Ported from v1 ops/lib/e2e.py with one change (design section 3.5): models are
deployed and removed through the subject's own `./platformctl new-model ...
--deploy --yes` / `--undeploy NAME --yes`, which commit and push to the run's
throwaway branch e2e/<run-id>, instead of `--emit` + kubectl.

Domains and checks (COMPATIBILITY.md "Upgrade domains"):
  gitops     Argo CD Applications Synced+Healthy; kro RGDs Active; (managed) Argo
             CD login hands off to IAM Identity Center
  infra      EKS version == tfvars; managed addons ACTIVE; ALB provisioned; a
             Karpenter NodeClaim became Ready (observed during the GPU check)
  gateway    LiteLLM ready; a BedrockModel enrolls, answers, lands a Langfuse
             trace, deregisters on undeploy; dashboard; 8 SSO checks
  inference  a VLLMEndpoint becomes Ready on a GPU node and answers; DCGM
             metrics; (--with-llmd) an LLMDEndpoint routes a request

A check never aborts the suite: an exception is recorded as a failure.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sso
from common import (
    NS_ARGOCD,
    NS_INFERENCE,
    NS_PLATFORM,
    PortForward,
    http,
    kubectl_json,
    log,
    redact,
    secret_value,
    sh,
    wait_for,
)
from subject import Subject

# Smallest ungated chat model vLLM serves without special flags. The Bedrock
# model is cheap and widely enabled. Smoke enrollments use their own LiteLLM
# aliases so they never collide with the models seeded in upgrade mode.
GPU_SMOKE_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
BEDROCK_SMOKE_MODEL = "nova-lite"
BEDROCK_SMOKE_ALIAS = "e2e-smoke-nova-lite"
BEDROCK_ATTR_ALIAS = "e2e-attr-nova-lite"


def model_name_from_id(model_id: str) -> str:
    """Same derivation as the product's new-model (render.model_name_from_id):
    the YAML file name, metadata.name and LiteLLM model name."""
    return model_id.split("/")[-1].lower().replace(".", "-").replace("_", "-")


# --------------------------------------------------------------------------- #
# Result model
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
class Ctx:
    subject: Subject
    cluster: str
    profile: str = "default"
    with_llmd: bool = False
    timeouts: dict = field(default_factory=lambda: {"converge": 1800, "gpu": 1500, "bedrock": 300})
    run_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    master_key: str | None = None
    gpu_node_seen: bool = False
    _sso: object = None

    @property
    def env(self) -> str:
        return self.subject.env

    @property
    def region(self) -> str:
        return self.subject.region

    def sso(self):
        if self._sso is None:
            self._sso = sso.SsoEnv(self.subject.root, self.env, PortForward, kubectl_json, secret_value)
        return self._sso

    def tf_output(self, name: str) -> str | None:
        out = sh(["terraform", f"-chdir={self.subject.path('terraform/30.eks/30.cluster')}",
                  "output", "-raw", name], check=False, timeout=120,
                 env={**os.environ, "TF_WORKSPACE": self.env, "TF_IN_AUTOMATION": "1"})
        return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None

    def litellm_key(self) -> str:
        if self.master_key is None:
            self.master_key = secret_value(NS_PLATFORM, "litellm-secrets", "master-key")
        return self.master_key

    def tfvars_k8s_version(self) -> str | None:
        try:
            m = re.search(r'kubernetes_version\s*=\s*"([^"]+)"', self.subject.read(self.subject.tfvars_rel))
            return m.group(1) if m else None
        except OSError:
            return None


# --------------------------------------------------------------------------- #
# Model deployment through the product's new-model (GitOps on e2e/<run-id>)
# --------------------------------------------------------------------------- #

def deploy_model(c: Ctx, name: str, *args: str, under: str = "workloads") -> bool:
    """Deploy unless a model file with this name already exists (seeded in upgrade
    mode). Returns True if this call created it (the caller then removes it)."""
    if c.subject.find_model_file(name, under):
        return False
    rc = c.subject.platformctl("new-model", *args, "--deploy", "--yes", "--region", c.region)
    if rc != 0 or not c.subject.find_model_file(name, under):
        raise RuntimeError(f"platformctl new-model --deploy for {name} failed (exit {rc})")
    return True


def undeploy_model(c: Ctx, name: str) -> None:
    if c.subject.find_model_file(name):
        rc = c.subject.platformctl("new-model", "--undeploy", name, "--yes")
        if rc != 0:
            log(f"undeploy {name} exited {rc}")


def deploy_bedrock(c: Ctx, alias: str | None = None) -> tuple[str, bool]:
    args = ["--source", "bedrock", BEDROCK_SMOKE_MODEL]
    if alias:
        args += ["--model-name", alias]
    name = alias or BEDROCK_SMOKE_MODEL
    return name, deploy_model(c, name, *args)


def models(pf: PortForward, key: str) -> list[str]:
    code, body = http("GET", f"{pf.base}/v1/models", headers={"Authorization": f"Bearer {key}"})
    if code != 200:
        return []
    return [m["id"] for m in json.loads(body).get("data", [])]


def chat(pf: PortForward, key: str, model: str) -> tuple[int, str]:
    return http("POST", f"{pf.base}/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                body={"model": model, "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
                      "max_tokens": 8}, timeout=120)


def wait_alias(c: Ctx, alias: str, timeout: int) -> str:
    key = c.litellm_key()
    with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
        return wait_for(f"{alias} in /v1/models", lambda: alias if alias in models(pf, key) else None, timeout, 10)


def wait_endpoint_ready(c: Ctx, kind: str, name: str) -> None:
    def ready():
        ep = kubectl_json("get", kind, "-n", NS_INFERENCE, name)
        return (ep.get("status") or {}).get("ready") == "True"
    wait_for(f"{kind} {name} Ready", ready, c.timeouts["gpu"], 20)


# --------------------------------------------------------------------------- #
# Checks. Each returns (ok, detail).
# --------------------------------------------------------------------------- #

def chk_argocd_converged(c: Ctx):
    def state():
        apps = kubectl_json("get", "applications", "-n", NS_ARGOCD).get("items", [])
        if not apps:
            return None
        bad = [a["metadata"]["name"] for a in apps
               if a.get("status", {}).get("sync", {}).get("status") != "Synced"
               or a.get("status", {}).get("health", {}).get("status") != "Healthy"]
        return (len(apps), bad) if not bad else None
    n, _ = wait_for("Argo CD apps Synced+Healthy", state, c.timeouts["converge"], 20)
    return True, f"{n} applications Synced/Healthy"


def chk_kro_rgds_active(c: Ctx):
    def state():
        items = kubectl_json("get", "resourcegraphdefinitions").get("items", [])
        if not items:
            return None
        inactive = [i["metadata"]["name"] for i in items if i.get("status", {}).get("state") != "Active"]
        return (len(items), inactive) if not inactive else None
    n, _ = wait_for("kro RGDs Active", state, 600, 15)
    return True, f"{n} RGDs Active"


def chk_cluster_version(c: Ctx):
    want = c.tfvars_k8s_version()
    out = sh(["aws", "eks", "describe-cluster", "--name", c.cluster, "--query", "cluster.version",
              "--output", "text"]).stdout.strip()
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
    # Judge the Deployment, not every pod matching the label: an evicted pod stays
    # in phase Failed until garbage-collected (seen on the v1 k8s 1.37 run).
    def deploy_ready():
        d = kubectl_json("get", "deployment", "litellm", "-n", NS_PLATFORM)
        want, ready = (d.get("spec") or {}).get("replicas", 1), (d.get("status") or {}).get("readyReplicas", 0)
        return (ready, want) if ready and ready >= want else None
    ready, want = wait_for("litellm Deployment ready", deploy_ready, 900, 10)
    failed = [p["metadata"]["name"] + ":" + str((p.get("status") or {}).get("reason"))
              for p in kubectl_json("get", "pods", "-n", NS_PLATFORM, "-l", "app=litellm").get("items", [])
              if (p.get("status") or {}).get("phase") == "Failed"]
    with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
        code, body = http("GET", f"{pf.base}/health/readiness")
    detail = f"{ready}/{want} replicas ready; readiness HTTP {code}" + ("" if code == 200 else f": {body[:120]}")
    if failed:
        detail += f"; note: failed pod(s) {', '.join(failed)}"
    return code == 200, detail


def langfuse_trace_count(since: str | None = None) -> int | None:
    pk = secret_value(NS_PLATFORM, "langfuse-litellm-keys", "LANGFUSE_PUBLIC_KEY")
    sk = secret_value(NS_PLATFORM, "langfuse-litellm-keys", "LANGFUSE_SECRET_KEY")
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    q = f"fromTimestamp={since}&limit=1" if since else "limit=1"
    with PortForward(NS_PLATFORM, "svc/langfuse-web", 3000) as pf:
        code, body = http("GET", f"{pf.base}/api/public/traces?{q}", headers={"Authorization": f"Basic {auth}"})
    if code != 200:
        return None
    d = json.loads(body)
    return int((d.get("meta") or {}).get("totalItems", len(d.get("data", []))))


def _langfuse_has_trace_since(c: Ctx) -> tuple[bool, str]:
    since = c.run_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        n = wait_for("Langfuse trace", lambda: langfuse_trace_count(since), 180, 10)
        return True, f"{n} Langfuse trace(s) since run start"
    except TimeoutError as e:
        return False, str(e)


def chk_bedrock_roundtrip(c: Ctx):
    """Enroll nova-lite under the smoke alias with new-model --deploy, chat through
    /v1, verify a Langfuse trace, undeploy, verify deregistration. Covers the
    gateway domain in one path (LiteLLM + litellm-sync + Bedrock IRSA + Langfuse)."""
    alias, created = deploy_bedrock(c, BEDROCK_SMOKE_ALIAS)
    try:
        key = c.litellm_key()
        wait_alias(c, alias, c.timeouts["bedrock"])
        with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
            code, body = chat(pf, key, alias)
        if code != 200:
            return False, f"chat via {alias} HTTP {code}: {body[:160]}"
        trace_ok, trace_detail = _langfuse_has_trace_since(c)
        if not created:
            return trace_ok, f"{alias} answered 200 (pre-existing, not undeployed); {trace_detail}"
        undeploy_model(c, alias)
        created = False
        with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
            wait_for("Bedrock alias deregistered", lambda: alias not in models(pf, key), 300, 10)
        return trace_ok, f"{alias} answered 200; deregistered on undeploy; {trace_detail}"
    finally:
        if created:
            undeploy_model(c, alias)


def chk_gpu_model_roundtrip(c: Ctx):
    """VLLMEndpoint for a tiny ungated model: Karpenter node -> vLLM Ready ->
    LiteLLM answers. In upgrade mode the seeded copy is reused and kept."""
    name = model_name_from_id(GPU_SMOKE_MODEL)
    created = deploy_model(c, name, GPU_SMOKE_MODEL, "--tier", "vllm", "--users", "1", "--seq", "2048")
    try:
        wait_endpoint_ready(c, "vllmendpoint", name)
        claims = kubectl_json("get", "nodeclaims").get("items", [])
        c.gpu_node_seen = any(
            any(cond.get("type") == "Ready" and cond.get("status") == "True"
                for cond in (cl.get("status") or {}).get("conditions", []))
            for cl in claims)
        wait_alias(c, name, 300)
        with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
            code, body = chat(pf, c.litellm_key(), name)
        return code == 200, f"{name} Ready on GPU; chat HTTP {code}" + ("" if code == 200 else f": {body[:160]}")
    finally:
        if created:
            undeploy_model(c, name)


def chk_karpenter_nodeclaim(c: Ctx):
    # Evaluated after the GPU check populated the observation.
    return c.gpu_node_seen, ("a Karpenter NodeClaim reached Ready during the GPU check"
                             if c.gpu_node_seen else "no Ready NodeClaim observed")


def chk_dcgm_metrics(c: Ctx):
    # Wait for a READY exporter pod: gpu-operator v26 added a 45 s readiness delay.
    def ready_pod():
        for p in kubectl_json("get", "pods", "-n", "gpu-operator", "-l", "app=nvidia-dcgm-exporter").get("items", []):
            conds = (p.get("status") or {}).get("conditions", [])
            if any(cd.get("type") == "Ready" and cd.get("status") == "True" for cd in conds):
                return p
        return None
    try:
        pod = wait_for("Ready nvidia-dcgm-exporter pod", ready_pod, 300, 10)
    except TimeoutError as e:
        return False, str(e)
    port = next((pt["containerPort"] for ct in pod["spec"]["containers"] for pt in ct.get("ports", [])
                 if pt.get("name") == "metrics" or pt.get("containerPort") == 9400), 9400)
    name = pod["metadata"]["name"]

    def scrape():
        with PortForward("gpu-operator", f"pod/{name}", port) as pf:
            code, body = http("GET", f"{pf.base}/metrics")
        return (code, body) if code == 200 else None
    try:
        code, body = wait_for("dcgm-exporter /metrics 200", scrape, 120, 10)
    except TimeoutError as e:
        return False, str(e)
    has = "DCGM_FI_DEV_GPU_UTIL" in body
    return has, f"dcgm-exporter HTTP {code}, GPU util metric {'present' if has else 'absent'}"


def chk_dashboard_health(c: Ctx):
    # The dashboard's probes hit /data.json (there is no /healthz); a 200 with JSON
    # proves the backend is up AND can read the cluster.
    with PortForward(NS_PLATFORM, "svc/cluster-dashboard", 9090) as pf:
        code, body = http("GET", f"{pf.base}/data.json")
    ok = code == 200 and body.lstrip().startswith("{")
    return ok, f"/data.json HTTP {code}" + ("" if ok else f": {body[:80]}")


def chk_llmd_roundtrip(c: Ctx):
    name = model_name_from_id(GPU_SMOKE_MODEL)
    if c.subject.find_model_file(name):
        return False, f"a model named {name} is already deployed; cannot add the llm-d copy under the same name"
    created = deploy_model(c, name, GPU_SMOKE_MODEL, "--tier", "llm-d", "--users", "4", "--seq", "2048",
                           under="workloads/scale-models")
    try:
        wait_endpoint_ready(c, "llmdendpoint", name)
        wait_alias(c, name, 300)
        with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
            code, _body = chat(pf, c.litellm_key(), name)
        return code == 200, f"{name} routed via llm-d; chat HTTP {code}"
    finally:
        if created:
            undeploy_model(c, name)


# --- SSO / UI logins (gateway domain: the UIs and oauth2-proxy live there) --- #

def _sso_check(name):
    def fn(c: Ctx):
        return getattr(sso, name)(c, c.sso())
    fn.__name__ = name
    return fn


def chk_sso_cost_attribution(c: Ctx):
    state = {"created": False}

    def enroll():
        alias, state["created"] = deploy_bedrock(c, BEDROCK_ATTR_ALIAS)
        return wait_alias(c, alias, c.timeouts["bedrock"])

    def cleanup():
        if state["created"]:
            undeploy_model(c, BEDROCK_ATTR_ALIAS)
    return sso.chk_openwebui_chat_attribution(c, c.sso(), enroll, cleanup, models, c.litellm_key, PortForward)


def chk_argocd_sso(c: Ctx):
    return sso.chk_argocd_sso_redirect(c, c.sso(), c.tf_output("argocd_url"))


def _only_managed(c: Ctx) -> str | None:
    return None if c.profile == "managed" else "profile default runs self-managed Argo CD (no Identity Center)"


def _only_llmd(c: Ctx) -> str | None:
    return None if c.with_llmd else "llm-d tier not requested (enable with --with-llmd)"


# (domain, name, fn, skip-reason function or None). Order matters: GPU before
# the checks that depend on a GPU node having existed (NodeClaim, DCGM).
CHECKS: list[tuple[str, str, object, object]] = [
    ("gitops",    "argocd-applications-converged",  chk_argocd_converged,   None),
    ("gitops",    "kro-rgds-active",                chk_kro_rgds_active,    None),
    ("infra",     "eks-cluster-version",            chk_cluster_version,    None),
    ("infra",     "eks-managed-addons-active",      chk_addons_active,      None),
    ("infra",     "alb-provisioned",                chk_alb_provisioned,    None),
    ("gateway",   "litellm-ready",                  chk_litellm_ready,      None),
    ("gateway",   "bedrock-enroll-chat-trace-deregister", chk_bedrock_roundtrip, None),
    ("gateway",   "cluster-dashboard-healthz",      chk_dashboard_health,   None),
    ("gateway",   "sso-seed-users-provisioned",     _sso_check("chk_seed_users"),              None),
    ("gateway",   "sso-login-open-webui",           _sso_check("chk_open_webui_logins"),       None),
    ("gateway",   "sso-login-litellm-ui",           _sso_check("chk_litellm_ui_logins"),       None),
    ("gateway",   "sso-login-langfuse",             _sso_check("chk_langfuse_logins"),         None),
    ("gateway",   "sso-login-dashboard-oauth2-proxy", _sso_check("chk_dashboard_logins"),      None),
    ("gateway",   "sso-wrong-password-rejected",    _sso_check("chk_wrong_password_rejected"), None),
    ("gateway",   "sso-single-sign-on-across-uis",  _sso_check("chk_single_sign_on"),          None),
    ("gateway",   "sso-openwebui-chat-cost-attributed", chk_sso_cost_attribution,              None),
    ("gitops",    "argocd-ui-identity-center-redirect", chk_argocd_sso,                        _only_managed),
    ("inference", "vllmendpoint-gpu-chat",          chk_gpu_model_roundtrip, None),
    ("infra",     "karpenter-nodeclaim-ready",      chk_karpenter_nodeclaim, None),
    ("inference", "dcgm-gpu-metrics",               chk_dcgm_metrics,       None),
    ("inference", "llmdendpoint-routed-chat",       chk_llmd_roundtrip,     _only_llmd),
]

DOMAINS = sorted({d for d, *_ in CHECKS})


def run_check(domain: str, name: str, fn, c: Ctx, prefix: str = "") -> CheckResult:
    log(f"[{domain}] {prefix}{name} ...")
    t0 = time.time()
    try:
        ok, detail = fn(c)
    except (Exception, subprocess.SubprocessError) as e:  # noqa: BLE001 - a check fails, never aborts the suite
        ok, detail = False, f"{type(e).__name__}: {e}"
    r = CheckResult(domain, prefix + name, bool(ok), str(detail), time.time() - t0)
    log(f"[{domain}] {r.name}: {'PASS' if ok else 'FAIL'} ({r.seconds:.0f}s) {redact(r.detail)}")
    return r


def run_suite(c: Ctx, prefix: str = "") -> list[CheckResult]:
    results = []
    for domain, name, fn, when in CHECKS:
        reason = when(c) if when else None
        if reason:
            results.append(CheckResult(domain, prefix + name, True, f"skipped: {reason}", 0, skipped=True))
            continue
        results.append(run_check(domain, name, fn, c, prefix))
    return results
