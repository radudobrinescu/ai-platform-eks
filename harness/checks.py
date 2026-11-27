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
             metrics; (--with-llmd) an LLMDEndpoint routes a request and KEDA scales it
             1 -> 2 under load (Karpenter adds the GPU capacity)

A check never aborts the suite: an exception is recorded as a failure.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import urllib.parse
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
# The llm-d checks need their own model: upgrade mode seeds GPU_SMOKE_MODEL before
# the upgrade and keeps it deployed (survival), so deploying it again on the llm-d
# tier refused with "already deployed" in both upgrade jobs of v2026.11.4's E2E.
# Same architecture family (Qwen2ForCausalLM), similar size, own chat template.
LLMD_SMOKE_MODEL = "Qwen/Qwen2-0.5B-Instruct"
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
    # bedrock: enrol or remove a model end to end (git push -> Argo CD -> litellm-sync
    # -> every LiteLLM replica). Removing a namespace's last model also rolls LiteLLM.
    timeouts: dict = field(default_factory=lambda: {"converge": 1800, "gpu": 1500, "bedrock": 600})
    run_start: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    master_key: str | None = None
    gpu_node_seen: bool = False
    # Set when the llm-d model is started (start_gpu_models or the llm-d check),
    # used and cleaned up by the KEDA check.
    llmd_model: str | None = None
    llmd_created: bool = False
    llmd_start_error: str | None = None
    # Set by start_gpu_models: the vLLM smoke model was deployed up front.
    gpu_started: bool = False
    gpu_created: bool = False
    gpu_start_error: str | None = None
    # --gpu-instance-type: pin the vLLM smoke model to this EC2 type (a family
    # the GPU NodePools newly allow, e.g. g7.2xlarge). Empty: Karpenter picks.
    gpu_instance_type: str = ""
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


def litellm_pods() -> list[str]:
    """Running, Ready, not-terminating LiteLLM pods. Each replica keeps its own
    in-memory model list (synced from the DB every 30 s), so registration is only
    complete when EVERY replica agrees; the Service may route to any of them."""
    names = []
    for p in kubectl_json("get", "pods", "-n", NS_PLATFORM, "-l", "app=litellm").get("items", []):
        meta, st = p.get("metadata") or {}, p.get("status") or {}
        ready = any(cd.get("type") == "Ready" and cd.get("status") == "True" for cd in st.get("conditions", []))
        if st.get("phase") == "Running" and ready and not meta.get("deletionTimestamp"):
            names.append(meta["name"])
    return names


def models_per_replica(key: str) -> dict[str, list[str] | None]:
    """{pod: model ids, or None if that pod could not be read}. A fresh
    port-forward per pod per call: a long-lived one dies with its pod (rollouts,
    restarts) and then refuses every later poll."""
    out: dict[str, list[str] | None] = {}
    for pod in litellm_pods():
        try:
            with PortForward(NS_PLATFORM, f"pod/{pod}", 4000) as pf:
                code, body = http("GET", f"{pf.base}/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
            out[pod] = [m["id"] for m in json.loads(body).get("data", [])] if code == 200 else None
        except Exception:  # noqa: BLE001 - unreadable replica: not converged yet
            out[pod] = None
    return out


def _all_replicas(key: str, alias: str, present: bool):
    views = models_per_replica(key)
    if len(views) < 1 or any(v is None for v in views.values()):
        return None
    return views if all((alias in v) == present for v in views.values()) else None


def wait_alias(c: Ctx, alias: str, timeout: int) -> str:
    key = c.litellm_key()
    wait_for(f"{alias} in /v1/models on every LiteLLM replica", lambda: _all_replicas(key, alias, True), timeout, 10)
    return alias


def litellm_settled() -> bool:
    """The litellm Deployment has finished any rollout: every replica updated,
    ready and available, and no old pod still terminating."""
    d = kubectl_json("get", "deployment", "litellm", "-n", NS_PLATFORM)
    spec, st, meta = d.get("spec") or {}, d.get("status") or {}, d.get("metadata") or {}
    want = spec.get("replicas", 1)
    if st.get("observedGeneration", 0) < meta.get("generation", 0):
        return False
    if not (st.get("updatedReplicas") == st.get("readyReplicas") == st.get("availableReplicas") == want):
        return False
    if st.get("replicas", want) != want:
        return False
    pods = kubectl_json("get", "pods", "-n", NS_PLATFORM, "-l", "app=litellm").get("items", [])
    return not any((p.get("metadata") or {}).get("deletionTimestamp") for p in pods)


def wait_alias_gone(c: Ctx, alias: str, timeout: int) -> None:
    key = c.litellm_key()
    wait_for(f"{alias} gone from /v1/models on every LiteLLM replica", lambda: _all_replicas(key, alias, False), timeout, 10)
    # Removing a namespace's last model rolls LiteLLM (litellm-sync works around
    # a LiteLLM eviction bug). Later checks must not land on a terminating pod.
    wait_for("litellm rollout settled", litellm_settled, timeout, 10)


def chat_via_service(key: str, model: str, attempts: int = 3) -> tuple[int, str]:
    """chat() through svc/litellm with a fresh port-forward per attempt; only a
    transport failure is retried, never an HTTP answer."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
                return chat(pf, key, model)
        except Exception as e:  # noqa: BLE001 - port-forward or socket failure
            last = e
            time.sleep(5)
    raise RuntimeError(f"chat via svc/litellm failed {attempts} times: {last}")


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


LANGFUSE_EPOCH = "2020-01-01T00:00:00Z"


def _count_traces_v2(get, since: str | None) -> int | None:
    """Langfuse 4: distinct trace ids among observations (Observations API v2,
    cursor-paginated, newest first). None when the API is not there (Langfuse 3)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    traces: set[str] = set()
    cursor = ""
    for _page in range(50):
        q = urllib.parse.urlencode({"fromStartTime": since or LANGFUSE_EPOCH, "toStartTime": now, "limit": 1000,
                                    **({"cursor": cursor} if cursor else {})})
        code, body = get(f"/api/public/v2/observations?{q}")
        if code != 200:
            return None
        d = json.loads(body)
        traces.update(o["traceId"] for o in d.get("data", []) if o.get("traceId"))
        cursor = (d.get("meta") or {}).get("cursor") or ""
        if not cursor:
            break
    return len(traces)


def _count_traces_v1(get, since: str | None) -> int | None:
    """Langfuse 3: GET /api/public/traces (removed in Langfuse 4)."""
    q = f"fromTimestamp={since}&limit=1" if since else "limit=1"
    code, body = get(f"/api/public/traces?{q}")
    if code != 200:
        return None
    d = json.loads(body)
    return int((d.get("meta") or {}).get("totalItems", len(d.get("data", []))))


def langfuse_trace_count(since: str | None = None) -> int | None:
    """Traces in the platform project (since a time, if given), on Langfuse 3 or 4:
    upgrade jobs read the count on the base release and again after the upgrade."""
    pk = secret_value(NS_PLATFORM, "langfuse-litellm-keys", "LANGFUSE_PUBLIC_KEY")
    sk = secret_value(NS_PLATFORM, "langfuse-litellm-keys", "LANGFUSE_SECRET_KEY")
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    with PortForward(NS_PLATFORM, "svc/langfuse-web", 3000) as pf:
        def get(path):
            return http("GET", f"{pf.base}{path}", headers={"Authorization": f"Basic {auth}"})
        n = _count_traces_v2(get, since)
        return n if n is not None else _count_traces_v1(get, since)


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
        code, body = chat_via_service(key, alias)
        if code != 200:
            return False, f"chat via {alias} HTTP {code}: {body[:160]}"
        trace_ok, trace_detail = _langfuse_has_trace_since(c)
        if not created:
            return trace_ok, f"{alias} answered 200 (pre-existing, not undeployed); {trace_detail}"
        undeploy_model(c, alias)
        created = False
        wait_alias_gone(c, alias, c.timeouts["bedrock"])
        return trace_ok, f"{alias} answered 200; deregistered on undeploy; {trace_detail}"
    finally:
        if created:
            undeploy_model(c, alias)


def chk_gpu_model_roundtrip(c: Ctx):
    """VLLMEndpoint for a tiny ungated model: Karpenter node -> vLLM Ready ->
    LiteLLM answers. In upgrade mode the seeded copy is reused and kept."""
    name = model_name_from_id(GPU_SMOKE_MODEL)
    if c.gpu_start_error:
        c.gpu_start_error, err = None, c.gpu_start_error
        return False, f"starting {name} at the beginning of the suite failed: {err}"
    if c.gpu_started:
        created, c.gpu_started = c.gpu_created, False
    else:
        created = deploy_model(c, name, GPU_SMOKE_MODEL, "--tier", "vllm", "--users", "1", "--seq", "2048")
        if c.gpu_instance_type and created:
            pin_gpu_model(c, name)
    try:
        wait_endpoint_ready(c, "vllmendpoint", name)
        on = model_instance_types(name)
        if c.gpu_instance_type and on != [c.gpu_instance_type]:
            return False, f"{name} Ready on {', '.join(on) or 'no node'}, expected {c.gpu_instance_type}"
        claims = kubectl_json("get", "nodeclaims").get("items", [])
        c.gpu_node_seen = any(
            any(cond.get("type") == "Ready" and cond.get("status") == "True"
                for cond in (cl.get("status") or {}).get("conditions", []))
            for cl in claims)
        wait_alias(c, name, c.timeouts["bedrock"])
        code, body = chat_via_service(c.litellm_key(), name)
        return code == 200, (f"{name} Ready on GPU ({', '.join(on) or '?'}); chat HTTP {code}"
                             + ("" if code == 200 else f": {body[:160]}"))
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


DASHBOARD_HEALTH_ATTEMPTS = 3
DASHBOARD_HEALTH_PAUSE = 10


def chk_dashboard_health(c: Ctx):
    # The dashboard's probes hit /data.json (there is no /healthz); a 200 with JSON
    # proves the backend is up AND can read the cluster. A fresh port-forward per
    # attempt: right after an upgrade the pod behind the first one can be replaced
    # (seen: "Remote end closed connection without response", then 200).
    last = ""
    for attempt in range(DASHBOARD_HEALTH_ATTEMPTS):
        if attempt:
            time.sleep(DASHBOARD_HEALTH_PAUSE)
        try:
            with PortForward(NS_PLATFORM, "svc/cluster-dashboard", 9090) as pf:
                code, body = http("GET", f"{pf.base}/data.json")
        except (ConnectionError, OSError, TimeoutError) as e:
            last = f"{type(e).__name__}: {e}"
            continue
        if code == 200 and body.lstrip().startswith("{"):
            return True, f"/data.json HTTP {code}" + (f" (attempt {attempt + 1})" if attempt else "")
        last = f"/data.json HTTP {code}: {body[:80]}"
    return False, last


# After a Kubernetes bump Karpenter replaces its nodes (they drift to the new
# AMI) and the platform pods move with them; checks that start before that is
# over hit pods that are moving.
ROLLOUT_SETTLE_TIMEOUT = 1800


def _condition(obj: dict, kind: str) -> str | None:
    for cond in (obj.get("status") or {}).get("conditions") or []:
        if cond.get("type") == kind:
            return cond.get("status")
    return None


def rollout_pending(claims: dict, nodes: dict, deployments: dict) -> list[str]:
    """What still moves: NodeClaims that are drifted or being deleted, nodes
    that are not Ready, Deployments without all replicas updated and ready."""
    out = []
    for cl in claims.get("items") or []:
        name = (cl.get("metadata") or {}).get("name", "?")
        if (cl.get("metadata") or {}).get("deletionTimestamp"):
            out.append(f"nodeclaim {name} being deleted")
        elif _condition(cl, "Drifted") == "True":
            out.append(f"nodeclaim {name} drifted")
    for n in nodes.get("items") or []:
        if _condition(n, "Ready") != "True":
            out.append(f"node {(n.get('metadata') or {}).get('name', '?')} not Ready")
    for d in deployments.get("items") or []:
        want = (d.get("spec") or {}).get("replicas", 1) or 0
        st = d.get("status") or {}
        if want and (st.get("readyReplicas", 0) < want or st.get("updatedReplicas", 0) < want):
            meta = d.get("metadata") or {}
            out.append(f"deployment {meta.get('namespace')}/{meta.get('name')} "
                       f"{st.get('readyReplicas', 0)}/{want} ready, {st.get('updatedReplicas', 0)} updated")
    return out


def wait_rollout_settled(timeout: int = ROLLOUT_SETTLE_TIMEOUT) -> tuple[bool, str]:
    seen: dict = {"pending": ["never read"]}
    t0 = time.time()

    def settled():
        seen["pending"] = rollout_pending(kubectl_json("get", "nodeclaims.karpenter.sh"),
                                          kubectl_json("get", "nodes"),
                                          kubectl_json("get", "deployments", "-n", NS_PLATFORM))
        return not seen["pending"]
    try:
        wait_for("the node rollout to settle", settled, timeout, 20)
    except TimeoutError:
        return False, f"still moving after {timeout}s: " + "; ".join(seen["pending"][:8])
    return True, f"nodes and platform pods settled after {time.time() - t0:.0f}s"


# The dashboard's LiteLLM key is made by a CronJob that runs every 15 minutes;
# on a new cluster the LiteLLM sources report an error until its first run.
DASHBOARD_SOURCES_TIMEOUT = 1200
SCRAPE_TARGETS_TIMEOUT = 300
WAKE_FROM_ZERO_JOB = "litellm"


def dashboard_problems(ready_code: int, sources: list[dict], sections: dict) -> list[str]:
    """What is wrong with the dashboard, as its own API reports it. /data.json
    answering 200 (cluster-dashboard-healthz) says the backend is up; this says
    every data source behind it works and the snapshot carries the cluster."""
    problems = [] if ready_code == 200 else [f"/api/v1/health/ready answered HTTP {ready_code}"]
    for s in sources:
        if s.get("state") != "ok":
            why = (s.get("lastError") or s.get("detail") or "")[:120]
            problems.append(f"source {s.get('name')} is {s.get('state')}" + (f" ({why})" if why else ""))
    for key, what in (("nodes", "nodes"), ("nodePools", "node pools")):
        if not sections.get(key):
            problems.append(f"the snapshot has no {what}")
    return problems


def chk_dashboard_sources(c: Ctx):
    seen: dict = {"problems": ["never read"], "sources": 0, "nodes": 0, "pools": 0}

    def healthy():
        with PortForward(NS_PLATFORM, "svc/cluster-dashboard", 9090) as pf:
            answers = {path: http("GET", f"{pf.base}{path}")
                       for path in ("/api/v1/health/ready", "/api/v1/sources", "/api/v1/snapshot")}
        failed = [f"{path} answered HTTP {code}" for path, (code, _b) in answers.items()
                  if path != "/api/v1/health/ready" and code != 200]
        if failed:
            seen["problems"] = failed
            return None
        sources = json.loads(answers["/api/v1/sources"][1])
        sections = json.loads(answers["/api/v1/snapshot"][1]).get("sections") or {}
        seen.update(problems=dashboard_problems(answers["/api/v1/health/ready"][0], sources, sections),
                    sources=len(sources), nodes=len(sections.get("nodes") or []), pools=len(sections.get("nodePools") or []))
        return not seen["problems"] or None
    try:
        wait_for("every dashboard data source to report ok", healthy, DASHBOARD_SOURCES_TIMEOUT, 30)
    except TimeoutError:
        return False, "; ".join(seen["problems"])
    return True, f"{seen['sources']} data sources ok; snapshot has {seen['nodes']} nodes and {seen['pools']} node pools"


def scrape_problems(targets: list[dict]) -> list[str]:
    """Scrape targets of the autoscaling Prometheus that are down. A target that
    is down fails nothing by itself: LiteLLM's answered 401 for a month, and with
    it the request rate that wakes an llm-d pool from zero never arrived."""
    problems = [f"{(t.get('labels') or {}).get('job')} {t.get('scrapeUrl')} is {t.get('health')}"
                + (f" ({(t.get('lastError') or '')[:120]})" if t.get("lastError") else "")
                for t in targets if t.get("health") != "up"]
    if not any((t.get("labels") or {}).get("job") == WAKE_FROM_ZERO_JOB for t in targets):
        problems.append(f"no target for the job {WAKE_FROM_ZERO_JOB} (the request rate that wakes a pool from zero)")
    return problems


def chk_scrape_targets(c: Ctx):
    seen: dict = {"problems": ["never read"], "jobs": []}

    def all_up():
        with PortForward(NS_INFERENCE, "deploy/autoscaling-prometheus", 9090) as pf:
            code, body = http("GET", f"{pf.base}/api/v1/targets")
        if code != 200:
            seen["problems"] = [f"/api/v1/targets answered HTTP {code}"]
            return None
        targets = (json.loads(body).get("data") or {}).get("activeTargets") or []
        seen.update(problems=scrape_problems(targets),
                    jobs=[(t.get("labels") or {}).get("job") for t in targets])
        return not seen["problems"] or None
    try:
        wait_for("every Prometheus scrape target to be up", all_up, SCRAPE_TARGETS_TIMEOUT, 15)
    except TimeoutError:
        return False, "; ".join(seen["problems"])
    jobs = sorted(set(seen["jobs"]))
    return True, f"{len(seen['jobs'])} scrape target{'' if len(seen['jobs']) == 1 else 's'} up ({', '.join(jobs)})"


# KEDA autoscaling (see platform/config/kro/llmd-endpoint.yaml, ScaledObject).
# Since scale to zero the pool scales on the requests in flight that the llm-d
# router reports, per replica targetInflight; before, on
# sum(vllm:num_requests_waiting) / targetQueueDepth. A 0.5B model on a GPU never
# queues under a light load, so the test edits the model file the way a user
# would: maxReplicas 2, the per-replica target 2, and vLLM limited to 2
# concurrent sequences, so LOAD_STREAMS parallel requests keep ~10 waiting.
# The generated file names the target the subject uses (targetInflight or
# targetQueueDepth); that one is set.
KEDA_MAX_REPLICAS = 2
KEDA_TARGET_PER_REPLICA = 2
KEDA_MAX_NUM_SEQS = 2
LOAD_STREAMS = 12
SCALE_DECISION_TIMEOUT = 600   # KEDA polls every 30 s, HPA scale-up window 30 s, metric over 1 m


def _set_spec_lines(text: str, values: dict, note: str, refuse: tuple[str, ...] = ()) -> str:
    """Set top-level spec keys of a model file line by line (append missing
    ones), so the file still reads like the generated one."""
    out, seen = [], set()
    for line in text.splitlines():
        m = re.match(r"^  ([A-Za-z]+):", line)
        if m and m.group(1) in refuse:
            raise ValueError(f"model file already sets {m.group(1)}; refusing to overwrite it")
        if m and m.group(1) in values:
            seen.add(m.group(1))
            line = f"  {m.group(1)}: {values[m.group(1)]}   # E2E: {note}"
        out.append(line)
    out += [f"  {k}: {v}   # E2E: {note}" for k, v in values.items() if k not in seen]
    return "\n".join(out) + "\n"


def tune_llmd_for_autoscale(text: str) -> str:
    """Return the LLMDEndpoint YAML with the KEDA test settings."""
    target = "targetInflight" if re.search(r"^  targetInflight:", text, re.M) else "targetQueueDepth"
    out = _set_spec_lines(text, {"maxReplicas": KEDA_MAX_REPLICAS, target: KEDA_TARGET_PER_REPLICA},
                          "KEDA autoscaling check", refuse=("extraArgs",))
    return out + f'  extraArgs: ["--max-num-seqs", "{KEDA_MAX_NUM_SEQS}"]   # E2E: make a small load queue\n'


# Scale to zero (LLMDEndpoint minReplicas 0). The smoke model is deployed with
# --min-replicas 0 when the subject has it; the check then shortens the idle
# time so the model stops within minutes, waits for its GPU node to go, and
# sends one chat that has to wake it.
SCALE_TO_ZERO_IDLE_SEC = 60          # the schema's minimum
SCALE_TO_ZERO_TIMEOUT = 600          # last request -> 0 replicas (1 m metric window + idle + polling)
GPU_NODE_GONE_TIMEOUT = 900          # Karpenter removes an empty node after 5 min
COLD_REQUEST_TIMEOUT = 900           # router hold (10 min) + answer; LiteLLM gives it 12 min


def subject_scales_to_zero(c: Ctx) -> bool:
    try:
        return "scaleToZeroAfterSeconds" in c.subject.read("platform/config/kro/llmd-endpoint.yaml")
    except OSError:
        return False


def tune_llmd_for_scale_to_zero(text: str) -> str:
    return _set_spec_lines(text, {"scaleToZeroAfterSeconds": SCALE_TO_ZERO_IDLE_SEC}, "scale-to-zero check")


def push_model_edit(c: Ctx, rel: str, message: str, app: str = "scale-models") -> None:
    """Commit one model file and push it to the env branch, then ask Argo CD to
    sync its app at that commit (what `platformctl new-model` does)."""
    c.subject.git("add", "--", rel)
    c.subject.git("commit", "--quiet", "-m", message, "--", rel)
    c.subject.git("push", "--quiet")
    sha = c.subject.head()
    op = {"operation": {"initiatedBy": {"username": "e2e"}, "sync": {"prune": True, "revision": sha}}}
    sh(["kubectl", "patch", "application", app, "-n", NS_ARGOCD, "--type", "merge",
        "-p", json.dumps(op)], check=False)


INSTANCE_TYPE_RX = re.compile(r"^[a-z][a-z0-9-]*\.[a-z0-9]+$")


def pin_gpu_model(c: Ctx, name: str) -> None:
    """Pin the vLLM smoke model to c.gpu_instance_type (VLLMEndpoint
    instanceType -> a node.kubernetes.io/instance-type nodeSelector)."""
    if not INSTANCE_TYPE_RX.match(c.gpu_instance_type):
        raise ValueError(f"not an EC2 instance type: {c.gpu_instance_type!r}")
    rel = c.subject.find_model_file(name)
    if not rel:
        raise RuntimeError(f"no model file for {name}")
    with open(c.subject.path(rel), encoding="utf-8") as fh:
        pinned = _set_spec_lines(fh.read(), {"instanceType": c.gpu_instance_type}, "GPU instance type under test")
    with open(c.subject.path(rel), "w", encoding="utf-8") as fh:
        fh.write(pinned)
    ns = os.path.relpath(rel, "workloads/models").split(os.sep)[0]
    push_model_edit(c, rel, f"e2e: pin {name} to {c.gpu_instance_type}", app=f"models-{ns}")


def model_instance_types(name: str) -> list[str]:
    """Instance types of the nodes the model's pods run on."""
    nodes = {(p.get("spec") or {}).get("nodeName") for p in kubectl_json(
        "get", "pods", "-n", NS_INFERENCE, "-l", f"app.kubernetes.io/name={name}").get("items", [])} - {None}
    return sorted({((kubectl_json("get", "node", n).get("metadata") or {}).get("labels") or {})
                   .get("node.kubernetes.io/instance-type", "?") for n in nodes})


def llmd_deployment(name: str) -> dict:
    return kubectl_json("get", "deployment", "-n", NS_INFERENCE, f"{name}-llmd")


def start_llmd_model(c: Ctx) -> None:
    """Deploy the llm-d smoke model and tune it for the KEDA check (sets
    c.llmd_model / c.llmd_created). Raises if it cannot."""
    name = model_name_from_id(LLMD_SMOKE_MODEL)
    if c.subject.find_model_file(name):
        raise RuntimeError(f"a model named {name} is already deployed; cannot add the llm-d copy under the same name")
    zero = ["--min-replicas", "0"] if subject_scales_to_zero(c) else []
    c.llmd_created = deploy_model(c, name, LLMD_SMOKE_MODEL, "--tier", "llm-d", "--users", "4", "--seq", "2048",
                                  *zero, under="workloads/scale-models")
    c.llmd_model = name
    rel = c.subject.find_model_file(name, "workloads/scale-models")
    with open(c.subject.path(rel), encoding="utf-8") as fh:
        tuned = tune_llmd_for_autoscale(fh.read())
    with open(c.subject.path(rel), "w", encoding="utf-8") as fh:
        fh.write(tuned)
    push_model_edit(c, rel, f"e2e: tune {name} for the KEDA autoscaling check")


def start_gpu_models(c: Ctx) -> None:
    """Deploy both GPU smoke models before the first check, so their GPU nodes
    launch (about 4-5 minutes each) while the gateway and SSO checks run instead
    of one after the other later. Errors are kept on c and reported by the
    check that needs the model; they never abort the suite."""
    t0 = time.time()
    try:
        name = model_name_from_id(GPU_SMOKE_MODEL)
        c.gpu_created = deploy_model(c, name, GPU_SMOKE_MODEL, "--tier", "vllm", "--users", "1", "--seq", "2048")
        c.gpu_started = True
        if c.gpu_instance_type and c.gpu_created:
            pin_gpu_model(c, name)
    except (Exception, subprocess.SubprocessError) as e:  # noqa: BLE001 - reported by the GPU check
        c.gpu_start_error = f"{type(e).__name__}: {e}"
    if c.with_llmd and not c.llmd_model:
        try:
            start_llmd_model(c)
        except (Exception, subprocess.SubprocessError) as e:  # noqa: BLE001 - reported by the llm-d check
            c.llmd_start_error = f"{type(e).__name__}: {e}"
    log(f"[inference] GPU smoke models started up front ({time.time() - t0:.0f}s); their nodes launch "
        "while the other checks run")


def chk_llmd_roundtrip(c: Ctx):
    if c.llmd_start_error:
        c.llmd_start_error, err = None, c.llmd_start_error
        return False, f"starting the llm-d model at the beginning of the suite failed: {err}"
    if not c.llmd_model:
        start_llmd_model(c)
    name = c.llmd_model
    wait_endpoint_ready(c, "llmdendpoint", name)
    wait_alias(c, name, c.timeouts["bedrock"])
    code, _body = chat_via_service(c.litellm_key(), name)
    return code == 200, f"{name} routed via llm-d; chat HTTP {code}"


def llmd_pods(name: str) -> list[dict]:
    """The model's vLLM pods that are not being deleted."""
    return [p for p in kubectl_json("get", "pods", "-n", NS_INFERENCE, "-l", f"llm-d.ai/pool={name}").get("items", [])
            if not (p.get("metadata") or {}).get("deletionTimestamp")]


def _gpu_pods_on(node: str) -> list[str]:
    pods = kubectl_json("get", "pods", "-A", "--field-selector", f"spec.nodeName={node}").get("items", [])
    return [f"{p['metadata']['namespace']}/{p['metadata']['name']}" for p in pods
            if any("nvidia.com/gpu" in ((ct.get("resources") or {}).get("requests") or {})
                   for ct in (p.get("spec") or {}).get("containers") or [])
            and (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]


COLD_REQUEST_PY = """
import json, os, time, urllib.error, urllib.request
body = json.dumps({"model": MODEL, "max_tokens": 8,
                   "messages": [{"role": "user", "content": "Reply with the single word: pong"}]}).encode()
req = urllib.request.Request("http://litellm.ai-platform.svc.cluster.local:4000/v1/chat/completions", data=body,
                             method="POST", headers={"Authorization": "Bearer " + os.environ["LITELLM_MASTER_KEY"],
                                                     "Content-Type": "application/json"})
print("sending", flush=True)
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        code, text = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    code, text = e.code, e.read().decode(errors="replace")
except Exception as e:
    code, text = 0, type(e).__name__ + ": " + str(e)
print("RESULT " + json.dumps({"code": code, "seconds": round(time.time() - t0, 1), "body": text[:300]}), flush=True)
"""
COLD_POD = "e2e-cold-start"


def cold_request_pod(name: str, image: str) -> dict:
    """The short-lived pod that sends the cold-start chat. It runs the LiteLLM
    image (Python, already in the cluster) and gets the master key from its
    Secret, so the key never leaves the cluster; do-not-disrupt keeps Karpenter
    from moving it while it waits."""
    code = COLD_REQUEST_PY.replace("MODEL", json.dumps(name)).replace("TIMEOUT", str(COLD_REQUEST_TIMEOUT))
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": COLD_POD, "namespace": NS_PLATFORM, "labels": {"app.kubernetes.io/part-of": "e2e"},
                     "annotations": {"karpenter.sh/do-not-disrupt": "true"}},
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": COLD_REQUEST_TIMEOUT + 120,
            "containers": [{
                "name": "chat", "image": image, "command": ["python", "-c", code],
                "env": [{"name": "LITELLM_MASTER_KEY",
                         "valueFrom": {"secretKeyRef": {"name": "litellm-secrets", "key": "master-key"}}}],
                "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"memory": "256Mi"}},
            }],
        },
    }


def cold_request(name: str) -> dict:
    """One chat for `name` that may wait minutes while the model starts, sent by
    a pod in the cluster; the result is read from its log once it ends.
    Not through a port-forward: one that carries no bytes for minutes can be cut
    on the way to the EKS API. Not as a background process exec'd into a
    LiteLLM pod either: in both fresh jobs of run 45679060534 that request never
    reached LiteLLM (no GPU node was asked for in 16 min) and left nothing to read."""
    image = kubectl_json("get", "deployment", "litellm", "-n", NS_PLATFORM)["spec"]["template"]["spec"][
        "containers"][0]["image"]
    sh(["kubectl", "delete", "pod", "-n", NS_PLATFORM, COLD_POD, "--ignore-not-found", "--wait=true"], check=False)
    sh(["kubectl", "apply", "-f", "-"], input_=json.dumps(cold_request_pod(name, image)))

    def ended():
        phase = (kubectl_json("get", "pod", "-n", NS_PLATFORM, COLD_POD).get("status") or {}).get("phase")
        return phase if phase in ("Succeeded", "Failed") else None

    def output() -> str:
        return sh(["kubectl", "logs", "-n", NS_PLATFORM, COLD_POD], check=False).stdout.strip()
    try:
        # + time for the image on a node that does not have it yet
        phase = wait_for(f"the cold-start chat to {name}", ended, COLD_REQUEST_TIMEOUT + 300, 15)
        out = output()
        line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("RESULT ")), "")
        if not line:
            return {"code": None, "seconds": None, "body": f"pod {phase}, no result; log: {out[-300:]}"}
        return json.loads(line[len("RESULT "):])
    except TimeoutError as e:
        raise TimeoutError(f"{e}; pod log: {output()[-300:] or 'empty'}") from None
    finally:
        sh(["kubectl", "delete", "pod", "-n", NS_PLATFORM, COLD_POD, "--ignore-not-found", "--wait=false"],
           check=False)


def wake_state(name: str) -> str:
    """What the autoscaler saw, for a failed wake: replicas, the ScaledObject's
    Active condition, and the two wake signals in the autoscaling Prometheus."""
    parts = []
    try:
        parts.append(f"replicas {(llmd_deployment(name).get('spec') or {}).get('replicas')}")
        so = kubectl_json("get", "scaledobject", "-n", NS_INFERENCE, f"{name}-llmd")
        conds = {cd.get("type"): cd.get("status") for cd in (so.get("status") or {}).get("conditions", [])}
        parts.append(f"ScaledObject Active={conds.get('Active')} Ready={conds.get('Ready')}")
        with PortForward(NS_INFERENCE, "deploy/autoscaling-prometheus", 9090) as pf:
            for label, q in (("router held", f'sum(llm_d_epp_flow_control_queue_size{{pool="{name}"}})'),
                             ("router running", f'sum(llm_d_epp_request_running{{pool="{name}"}})'),
                             ("LiteLLM req/s", f'sum(rate(litellm_proxy_total_requests_metric_total'
                                               f'{{requested_model="{name}"}}[5m]))')):
                code, body = http("GET", f"{pf.base}/api/v1/query?" + urllib.parse.urlencode({"query": q}))
                res = (json.loads(body).get("data") or {}).get("result") or [] if code == 200 else []
                parts.append(f"{label} {res[0]['value'][1] if res else 'none'}")
    except Exception as e:  # noqa: BLE001 - diagnostics only
        parts.append(f"state unreadable: {type(e).__name__}: {e}")
    return "; ".join(parts)


def _scale_to_zero_and_back(c: Ctx, name: str) -> tuple[bool, str]:
    rel = c.subject.find_model_file(name, "workloads/scale-models")
    if not rel:
        return False, f"no model file for {name}"
    spec = (kubectl_json("get", "llmdendpoint", "-n", NS_INFERENCE, name).get("spec") or {})
    if spec.get("minReplicas") != 0:
        return False, f"{name} has minReplicas {spec.get('minReplicas')}, expected 0 (deployed with --min-replicas 0)"
    nodes = sorted({(p.get("spec") or {}).get("nodeName") for p in llmd_pods(name)} - {None})
    with open(c.subject.path(rel), encoding="utf-8") as fh:
        tuned = tune_llmd_for_scale_to_zero(fh.read())
    with open(c.subject.path(rel), "w", encoding="utf-8") as fh:
        fh.write(tuned)
    push_model_edit(c, rel, f"e2e: {name} idles to zero after {SCALE_TO_ZERO_IDLE_SEC}s")

    t0 = time.time()
    def at_zero():
        d = llmd_deployment(name)
        return ((d.get("spec") or {}).get("replicas") == 0 and not llmd_pods(name)) or None
    wait_for(f"{name} scaled to zero", at_zero, SCALE_TO_ZERO_TIMEOUT, 15)
    t_zero = time.time() - t0
    st = kubectl_json("get", "llmdendpoint", "-n", NS_INFERENCE, name).get("status") or {}
    if st.get("ready") != "True" or st.get("scaledToZero") is not True:
        return False, f"at zero replicas the endpoint reports ready={st.get('ready')} scaledToZero={st.get('scaledToZero')}"

    gone, kept = [], []
    for node in nodes:
        others = _gpu_pods_on(node)
        if others:
            kept.append(f"{node} (also runs {', '.join(others[:2])})")
            continue
        wait_for(f"GPU node {node} removed", lambda n=node: not kubectl_json(
            "get", "node", "--ignore-not-found", n) or None, GPU_NODE_GONE_TIMEOUT, 20)
        gone.append(node)
    t_nodes = time.time() - t0

    try:
        res = cold_request(name)
    except TimeoutError as e:
        raise TimeoutError(f"{e} || wake state: {wake_state(name)}") from None
    if res.get("code") != 200:
        return False, (f"scaled to zero after {t_zero:.0f}s, GPU node(s) gone after {t_nodes:.0f}s; the chat that "
                       f"should wake it answered HTTP {res.get('code')} after {res.get('seconds')}s: "
                       f"{res.get('body')} || wake state: {wake_state(name)}")
    replicas = (llmd_deployment(name).get("spec") or {}).get("replicas")
    return True, (f"0 replicas {t_zero:.0f}s after the idle time was set to {SCALE_TO_ZERO_IDLE_SEC}s; "
                  f"GPU node removed: {', '.join(gone) or 'none'}" + (f"; kept: {'; '.join(kept)}" if kept else "")
                  + f"; cold start: one chat answered 200 after {res.get('seconds')}s; replicas now {replicas}")


def chk_llmd_scale_to_zero(c: Ctx):
    name = c.llmd_model
    if not name:
        return False, "no llm-d endpoint was deployed (see llmdendpoint-routed-chat)"
    try:
        return _scale_to_zero_and_back(c, name)
    except TimeoutError as e:
        short = capacity_shortage(f"{name}-llmd", time.time() - CAPACITY_LOOKBACK)
        return False, f"{e}" + (f"; the cloud had no GPU capacity: {short}" if short else "")


def _only_scale_to_zero(c: Ctx) -> str | None:
    return _only_llmd(c) or (None if subject_scales_to_zero(c) else "the subject has no llm-d scale to zero")


class Load:
    """LOAD_STREAMS threads, each sending long completions back to back through
    its own port-forward to svc/litellm, until stop()."""

    def __init__(self, key: str, model: str):
        import threading
        self.key, self.model, self.stop_ev = key, model, threading.Event()
        self.sent = self.ok = 0
        self.failed: dict[str, int] = {}   # non-200 answers and transport errors, by kind
        self.threads = [threading.Thread(target=self._run, daemon=True) for _ in range(LOAD_STREAMS)]

    def _run(self):
        body = {"model": self.model, "max_tokens": 512, "temperature": 0.8,
                "messages": [{"role": "user", "content": "Write a long story about a lighthouse keeper."}]}
        while not self.stop_ev.is_set():
            try:
                with PortForward(NS_PLATFORM, "svc/litellm", 4000) as pf:
                    while not self.stop_ev.is_set():
                        self.sent += 1
                        code, _ = http("POST", f"{pf.base}/v1/chat/completions", body=body, timeout=300,
                                       headers={"Authorization": f"Bearer {self.key}"})
                        if code == 200:
                            self.ok += 1
                        else:
                            self.failed[f"HTTP {code}"] = self.failed.get(f"HTTP {code}", 0) + 1
            except Exception as e:  # noqa: BLE001 - port-forward dropped: reopen and keep loading
                self.failed[type(e).__name__] = self.failed.get(type(e).__name__, 0) + 1
                time.sleep(2)

    def summary(self) -> str:
        """Answered, failed, and still in flight when the check reads it (the
        streams keep sending until stop(), so up to LOAD_STREAMS are in flight)."""
        failed = sum(self.failed.values())
        in_flight = max(self.sent - self.ok - failed, 0)
        why = f" ({', '.join(f'{k} x{v}' for k, v in sorted(self.failed.items()))})" if failed else ""
        return f"{self.ok} requests answered 200, {failed} failed{why}, {in_flight} in flight at the end"

    def __enter__(self):
        for t in self.threads:
            t.start()
        return self

    def __exit__(self, *exc):
        self.stop_ev.set()


CAPACITY_LOOKBACK = 900   # seconds before the check: the loaded replica may have asked for its node earlier


def capacity_shortage(pod_prefix: str, since: float) -> str:
    """The newest InsufficientCapacityError that Karpenter recorded since `since`
    (epoch seconds) for a pod named `pod_prefix`*: the cloud had no GPU instance to
    give at that moment. Empty when there was none, or when events cannot be read."""
    try:
        events = kubectl_json("get", "events", "-n", NS_INFERENCE, "--field-selector",
                              "reason=InsufficientCapacityError").get("items", [])
    except Exception:  # noqa: BLE001 - no evidence of a shortage is not a shortage
        return ""
    start = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hits = sorted((e.get("lastTimestamp") or e.get("eventTime") or "", (e.get("message") or "")[:160])
                  for e in events
                  if ((e.get("involvedObject") or {}).get("name") or "").startswith(pod_prefix)
                  and (e.get("lastTimestamp") or e.get("eventTime") or "") >= start)
    return hits[-1][1] if hits else ""


def _keda_scale(c: Ctx, name: str, so_name: str) -> tuple[bool, str]:
    def so_ready():
        so = kubectl_json("get", "scaledobject", "-n", NS_INFERENCE, so_name)
        conds = {cd.get("type"): cd.get("status") for cd in (so.get("status") or {}).get("conditions", [])}
        return so if conds.get("Ready") == "True" else None
    so = wait_for(f"ScaledObject {so_name} Ready", so_ready, 300, 15)
    if (so.get("spec") or {}).get("maxReplicaCount") != KEDA_MAX_REPLICAS:
        return False, f"ScaledObject maxReplicaCount is {so['spec'].get('maxReplicaCount')}, expected {KEDA_MAX_REPLICAS}"
    hpa = (so.get("status") or {}).get("hpaName") or f"keda-hpa-{so_name}"
    kubectl_json("get", "hpa", "-n", NS_INFERENCE, hpa)  # raises if KEDA did not create it
    before = (llmd_deployment(name).get("spec") or {}).get("replicas")
    t0 = time.time()
    with Load(c.litellm_key(), name) as load:
        def scaled():
            d = llmd_deployment(name)
            return d if ((d.get("spec") or {}).get("replicas") or 0) >= KEDA_MAX_REPLICAS else None
        what = f"KEDA scales {so_name} to {KEDA_MAX_REPLICAS}"
        short = ""
        try:
            wait_for(what, scaled, SCALE_DECISION_TIMEOUT, 15)
        except TimeoutError:
            # KEDA can only decide once the loaded replica serves. When that replica
            # waits for a GPU node the cloud could not give, the candidate is not the
            # cause: wait as long as a GPU node may take, then judge as usual.
            short = capacity_shortage(so_name, t0 - CAPACITY_LOOKBACK)
            if not short:
                raise
            log(f"[inference] no GPU capacity in the cloud ({short}); waiting up to {c.timeouts['gpu']}s more")
            try:
                wait_for(f"{what} (after a cloud capacity error)", scaled, c.timeouts["gpu"], 15)
            except TimeoutError as e:
                raise TimeoutError(f"{e}; the cloud had no GPU capacity: {short}") from None
        t_scale = time.time() - t0
        metric = ""
        for m in (kubectl_json("get", "hpa", "-n", NS_INFERENCE, hpa).get("status") or {}).get("currentMetrics") or []:
            cur = ((m.get("external") or {}).get("current") or {})
            metric = metric or cur.get("averageValue") or cur.get("value") or ""

        def all_ready():
            d = llmd_deployment(name)
            return d if ((d.get("status") or {}).get("readyReplicas") or 0) >= KEDA_MAX_REPLICAS else None
        wait_for(f"{KEDA_MAX_REPLICAS} ready replicas of {so_name}", all_ready, c.timeouts["gpu"], 20)
        t_ready = time.time() - t0
    return True, (f"{so_name}: replicas {before} -> {KEDA_MAX_REPLICAS} after {t_scale:.0f}s of load "
                  f"(scale metric per pod {metric or 'n/a'}); {KEDA_MAX_REPLICAS}/{KEDA_MAX_REPLICAS} Ready after "
                  f"{t_ready:.0f}s (Karpenter added GPU capacity); {load.summary()}"
                  + (f"; waited for GPU capacity ({short})" if short else ""))


def chk_keda_autoscaling(c: Ctx):
    name = c.llmd_model
    if not name:
        return False, "no llm-d endpoint was deployed (see llmdendpoint-routed-chat)"
    so_name = f"{name}-llmd"
    try:
        ok, detail = _keda_scale(c, name, so_name)
    except (Exception, subprocess.SubprocessError) as e:  # noqa: BLE001 - still clean up below
        ok, detail = False, f"{type(e).__name__}: {e}"
        if "timed out" in detail:
            detail += f" || stuck: {diagnose()}"
    created, c.llmd_model, c.llmd_created = c.llmd_created, None, False
    if not created:
        return ok, detail
    # The endpoint, its ScaledObject and its pods must be gone before the next step,
    # pass or fail. Seen in E2E: teardown started while they still existed, the drain
    # removed the node running KEDA, and finalizer.keda.sh then held the namespace.
    undeploy_model(c, name)

    def gone():
        left = [k for k, n in (("llmdendpoint", name), ("scaledobject", so_name), ("deployment", so_name))
                if kubectl_json("get", k, "-n", NS_INFERENCE, "--ignore-not-found", n)]
        return not left or None
    try:
        wait_for(f"{name} llm-d endpoint removed", gone, 600, 15)
    except TimeoutError as e:
        return False, f"{detail}; cleanup failed: {e}"
    return ok, detail + "; endpoint removed"


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
    ("inference", "autoscaling-scrape-targets-up",  chk_scrape_targets,     None),
    ("inference", "llmdendpoint-routed-chat",       chk_llmd_roundtrip,     _only_llmd),
    ("inference", "llmd-scales-to-zero-and-back",   chk_llmd_scale_to_zero, _only_scale_to_zero),
    ("inference", "keda-autoscales-llmd-on-queue-depth", chk_keda_autoscaling, _only_llmd),
    # Last: on a new cluster its LiteLLM sources wait for the key job (see above).
    ("gateway",   "cluster-dashboard-sources-ok",   chk_dashboard_sources,  None),
]

DOMAINS = sorted({d for d, *_ in CHECKS})


DIAG_NAMESPACES = ("ai-platform", "inference", "argocd", "keda", "gpu-operator", "kro-system", "kube-system")


CRASH_LOG_PODS = 3
CRASH_LOG_LINES = 20
CRASH_LOG_CHARS = 600


def crash_looping(pods: list[dict]) -> list[tuple[str, str, str]]:
    """(namespace, pod, container) of containers in CrashLoopBackOff in the
    namespaces diagnose() watches, at most CRASH_LOG_PODS."""
    out = []
    for p in pods:
        meta, st = p.get("metadata") or {}, p.get("status") or {}
        if meta.get("namespace") not in DIAG_NAMESPACES:
            continue
        for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
            if ((cs.get("state") or {}).get("waiting") or {}).get("reason") == "CrashLoopBackOff":
                out.append((meta["namespace"], meta["name"], cs["name"]))
    return out[:CRASH_LOG_PODS]


# Container logs can carry credentials (database URLs, API keys, tokens); the
# E2E log is readable by everyone with access to the repository's Actions.
_LOG_SECRETS = [
    (re.compile(r"(\w+://)[^/\s:@]+:[^@\s]+@"), r"\1***:***@"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
    (re.compile(r"(?i)\b([a-z_]*(password|passwd|secret|token|key)[a-z_]*)(\s*[=:]\s*)[^\s,;\"']+"), r"\1\3***"),
]


def scrub_log(text: str) -> str:
    for rx, repl in _LOG_SECRETS:
        text = rx.sub(repl, text)
    return redact(text)


def previous_log(ns: str, pod: str, container: str) -> str:
    """The last lines the crashed container wrote, on one line, credentials scrubbed."""
    r = sh(["kubectl", "logs", "-n", ns, pod, "-c", container, "--previous", f"--tail={CRASH_LOG_LINES}"],
           check=False, timeout=30)
    text = " / ".join(ln.strip() for ln in (r.stdout or r.stderr).splitlines() if ln.strip())
    return scrub_log(text)[-CRASH_LOG_CHARS:] or "no log"


def diagnose(limit: int = 2400) -> str:
    """What is stuck right now: Argo CD apps that are not Synced+Healthy (with their
    unhealthy resources), pods that are not Ready (with the waiting/termination
    reason and the scheduling message), and the latest Warning events. Added
    because a converge timeout left nothing to go on once the cluster was gone."""
    parts = []
    pods: list[dict] = []
    try:
        for a in kubectl_json("get", "applications", "-n", NS_ARGOCD).get("items", []):
            st = a.get("status") or {}
            sync, health = (st.get("sync") or {}).get("status"), (st.get("health") or {}).get("status")
            if sync == "Synced" and health == "Healthy":
                continue
            bad = [f"{r.get('kind')}/{r.get('name')}={(r.get('health') or {}).get('status')}"
                   for r in st.get("resources") or [] if (r.get("health") or {}).get("status") not in (None, "Healthy")]
            msg = ((st.get("operationState") or {}).get("message") or "")[:80]
            parts.append(f"app {a['metadata']['name']}: {sync}/{health}" + (f" [{', '.join(bad[:4])}]" if bad else "")
                         + (f" ({msg})" if msg and "successfully" not in msg else ""))
    except Exception as e:  # noqa: BLE001 - diagnostics must never fail the check further
        parts.append(f"apps unreadable: {e}")
    try:
        pods = kubectl_json("get", "pods", "-A").get("items", [])
        for p in pods:
            meta, st = p.get("metadata") or {}, p.get("status") or {}
            if meta.get("namespace") not in DIAG_NAMESPACES or st.get("phase") == "Succeeded":
                continue
            conds = {cd.get("type"): cd for cd in st.get("conditions") or []}
            if (conds.get("Ready") or {}).get("status") == "True":
                continue
            why = []
            for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
                w, t = (cs.get("state") or {}).get("waiting") or {}, (cs.get("lastState") or {}).get("terminated") or {}
                if w.get("reason"):
                    why.append(f"{cs['name']}:{w['reason']}" + (f" {w.get('message', '')[:90]}" if w.get("message") else ""))
                elif t.get("reason"):
                    why.append(f"{cs['name']}:last {t['reason']} exit {t.get('exitCode')}")
            sched = conds.get("PodScheduled") or {}
            if sched.get("status") == "False":
                why.append(f"unscheduled: {sched.get('message', '')[:90]}")
            parts.append(f"pod {meta['namespace']}/{meta['name']} {st.get('phase')}: {'; '.join(why) or 'not Ready'}")
    except Exception as e:  # noqa: BLE001
        parts.append(f"pods unreadable: {e}")
    # A model that does not come up: what kro reports for it, and the GPU nodes
    # Karpenter picked (an instance family the node image cannot drive never joins).
    for kind in ("vllmendpoints", "llmdendpoints"):
        try:
            for ep in kubectl_json("get", kind, "-n", NS_INFERENCE).get("items", []):
                st = ep.get("status") or {}
                if st.get("ready") == "True":
                    continue
                why = [f"{cd.get('type')}: {(cd.get('message') or cd.get('reason') or '')[:110]}"
                       for cd in st.get("conditions") or [] if cd.get("status") != "True"]
                parts.append(f"{kind[:-1]} {ep['metadata']['name']}: {st.get('state')} ready={st.get('ready')}"
                             + (f" ({'; '.join(why[:2])})" if why else ""))
        except Exception:  # noqa: BLE001 - the kind may not exist on this release
            pass
    try:
        for nc in kubectl_json("get", "nodeclaims").get("items", []):
            labels = (nc.get("metadata") or {}).get("labels") or {}
            ready = next((cd for cd in (nc.get("status") or {}).get("conditions") or [] if cd.get("type") == "Ready"), {})
            if ready.get("status") == "True":
                continue
            parts.append(f"nodeclaim {nc['metadata']['name']} ({labels.get('karpenter.sh/nodepool')}, "
                         f"{labels.get('node.kubernetes.io/instance-type')}) Ready={ready.get('status')}: "
                         f"{(ready.get('message') or ready.get('reason') or '')[:110]}")
    except Exception as e:  # noqa: BLE001
        parts.append(f"nodeclaims unreadable: {e}")
    try:
        evs = [e for e in kubectl_json("get", "events", "-A", "--field-selector", "type=Warning").get("items", [])
               if (e.get("involvedObject") or {}).get("namespace") in DIAG_NAMESPACES]
        evs.sort(key=lambda e: e.get("lastTimestamp") or e.get("eventTime") or "")
        seen = set()
        for e in reversed(evs):
            key = (e.get("reason"), (e.get("involvedObject") or {}).get("name"))
            if key in seen:
                continue
            seen.add(key)
            parts.append(f"event {key[1]}: {key[0]} {(e.get('message') or '')[:110]}")
            if len(seen) >= 8:
                break
    except Exception as e:  # noqa: BLE001
        parts.append(f"events unreadable: {e}")
    # Last: what a crash-looping container said before it died. In run
    # 45703427807 langfuse-web crash-looped in both jobs and nothing showed why
    # once the clusters were gone. The full text goes to the log; the check's
    # detail keeps only its first `limit` characters.
    for ns, pod, container in crash_looping(pods):
        try:
            parts.append(f"crash log {ns}/{pod} {container}: {previous_log(ns, pod, container)}")
        except Exception as e:  # noqa: BLE001
            parts.append(f"crash log {ns}/{pod} {container} unreadable: {e}")
    text = " | ".join(parts) or "nothing unhealthy found"
    log(f"[diagnose] {text}")
    return text if len(text) <= limit else text[:limit] + " ..."


def run_check(domain: str, name: str, fn, c: Ctx, prefix: str = "") -> CheckResult:
    log(f"[{domain}] {prefix}{name} ...")
    t0 = time.time()
    try:
        ok, detail = fn(c)
    except (Exception, subprocess.SubprocessError) as e:  # noqa: BLE001 - a check fails, never aborts the suite
        ok, detail = False, f"{type(e).__name__}: {e}"
    if not ok and "timed out" in str(detail):
        detail = f"{detail} || stuck: {diagnose()}"
    r = CheckResult(domain, prefix + name, bool(ok), str(detail), time.time() - t0)
    log(f"[{domain}] {r.name}: {'PASS' if ok else 'FAIL'} ({r.seconds:.0f}s) {redact(r.detail)}")
    return r


def run_suite(c: Ctx, prefix: str = "", only: set[str] | None = None) -> list[CheckResult]:
    """Run every check in order (or just the names in `only`, for `e2e.py check`)."""
    results = []
    selected = [x for x in CHECKS if not only or x[1] in only]
    gpu_checks = {"vllmendpoint-gpu-chat", "llmdendpoint-routed-chat"}
    wants_gpu = any(n in gpu_checks and not (w and w(c)) for _d, n, _f, w in selected)
    started = False
    for domain, name, fn, when in selected:
        # Right after the GitOps checks: starting earlier would make the "all Argo CD
        # apps healthy" check wait for the new GPU nodes too.
        if wants_gpu and not started and domain != "gitops":
            start_gpu_models(c)
            started = True
        reason = when(c) if when else None
        if reason:
            results.append(CheckResult(domain, prefix + name, True, f"skipped: {reason}", 0, skipped=True))
            continue
        results.append(run_check(domain, name, fn, c, prefix))
    return results
