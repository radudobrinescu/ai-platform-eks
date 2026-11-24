"""Pure transformations from Kubernetes objects and metrics to the records
the UI shows: nodes, alerts, pods, serving endpoints, models, GPU attribution,
autoscaling state and deploy diagnostics.

Inputs are Kubernetes objects in their API shape (the watch caches keep that
shape, minus fields the dashboard never reads), so these functions behave the
same on a live list and on a recorded fixture.
"""
from __future__ import annotations

from collections.abc import Callable

from dashboard.cost import model_cost, tok_per_dollar

# Serving kinds: kro.run/v1alpha1 in the `inference` namespace.
SERVING_KINDS = (
    ("vllmendpoints", "vllm"),
    ("llmdendpoints", "llm-d"),
    ("llmddisaggendpoints", "llm-d-disagg"),
)
# Karpenter warning reasons that explain a GPU deploy stuck on capacity.
PROVISION_HINTS = ("capacity", "fulfill", "launch", "zonal", "quota")


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------

def cpu_m(s) -> int:
    """Kubernetes CPU quantity -> millicores (0 if unparseable)."""
    s = str(s or "0").strip()
    try:
        if s.endswith("m"):
            return int(float(s[:-1]))
        if s.endswith("n"):
            return int(float(s[:-1]) / 1e6)
        if s.endswith("u"):
            return int(float(s[:-1]) / 1e3)
        return int(float(s) * 1000)
    except ValueError:
        return 0


def mem_mi(s) -> int:
    """Kubernetes memory quantity -> MiB (0 if unparseable)."""
    s = str(s or "0").strip()
    for u, f in (("Ki", 1 / 1024), ("Mi", 1.0), ("Gi", 1024.0), ("Ti", 1048576.0),
                 ("K", 0.000953674), ("M", 0.953674), ("G", 953.674)):
        if s.endswith(u):
            try:
                return int(float(s[:-len(u)]) * f)
            except ValueError:
                return 0
    try:
        return int(float(s) / 1048576)
    except ValueError:
        return 0


def quantity(q) -> float:
    """Kubernetes quantity such as '144', '900m' -> float (0.0 if unparseable)."""
    s = str(q if q is not None else "0").strip()
    try:
        if s.endswith("m"):
            return float(s[:-1]) / 1000.0
        return float(s)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Nodes and pods
# ---------------------------------------------------------------------------

def node_records(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(Ready nodes as dashboard records, node alerts).

    NotReady and resource-pressure conditions become alerts, except on nodes
    Karpenter is removing (deletionTimestamp or the `karpenter.sh/disrupted`
    taint): those go NotReady by design during scale-down. NotReady nodes are
    not listed as nodes."""
    nodes, alerts = [], []
    for n in items:
        meta = n.get("metadata", {}) or {}
        labels = meta.get("labels", {}) or {}
        name = meta["name"]
        terminating = bool(meta.get("deletionTimestamp")) or any(
            t.get("key") == "karpenter.sh/disrupted"
            for t in ((n.get("spec", {}) or {}).get("taints") or []))
        conditions = (n.get("status", {}) or {}).get("conditions", []) or []
        ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
        if not terminating:
            for c in conditions:
                if c.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure") and c.get("status") == "True":
                    alerts.append({"node": name, "issue": c["type"], "sev": "warn"})
        if not ready:
            if not terminating:
                alerts.append({"node": name, "issue": "NotReady", "sev": "warn"})
            continue
        alloc = (n.get("status", {}) or {}).get("allocatable", {}) or {}
        zone = labels.get("topology.kubernetes.io/zone", "")
        nodes.append({
            "name": name,
            "instance": labels.get("node.kubernetes.io/instance-type", "unknown"),
            "pool": labels.get("karpenter.sh/nodepool", "__mng__"),
            "zone": zone,
            "region": labels.get("topology.kubernetes.io/region", zone[:-1] if zone else ""),
            "capacity": labels.get("karpenter.sh/capacity-type",
                                   labels.get("eks.amazonaws.com/capacityType", "on-demand")),
            "gpu": int(alloc.get("nvidia.com/gpu", "0")),
            "gpuProduct": labels.get("nvidia.com/gpu.product", "").replace("-", " "),
            "cpuAllocM": cpu_m(alloc.get("cpu", "0")),
            "memAllocMi": mem_mi(alloc.get("memory", "0")),
            "created": meta.get("creationTimestamp", ""),
        })
    return nodes, alerts


def apply_usage(nodes: list[dict], usage_items: list[dict] | None) -> None:
    """Attach metrics.k8s.io usage; None when metrics-server has no data."""
    usage = {}
    for it in usage_items or []:
        u = it.get("usage", {}) or {}
        usage[(it.get("metadata", {}) or {}).get("name", "")] = {
            "cpuUsedM": cpu_m(u.get("cpu", "0")), "memUsedMi": mem_mi(u.get("memory", "0"))}
    for n in nodes:
        n["cpuUsedM"] = usage.get(n["name"], {}).get("cpuUsedM")
        n["memUsedMi"] = usage.get(n["name"], {}).get("memUsedMi")


def pod_records(items: list[dict]) -> list[dict]:
    """Running and Pending pods with their summed requests."""
    out = []
    for p in items:
        phase = (p.get("status", {}) or {}).get("phase", "")
        if phase not in ("Running", "Pending"):
            continue
        reqs = [(c.get("resources", {}) or {}).get("requests", {}) or {}
                for c in (p.get("spec", {}) or {}).get("containers", []) or []]
        out.append({
            "name": p["metadata"]["name"],
            "namespace": p["metadata"]["namespace"],
            "node": (p.get("spec", {}) or {}).get("nodeName", ""),
            "phase": phase,
            "gpu": sum(int(r.get("nvidia.com/gpu", "0") or 0) for r in reqs),
            "cpuReqM": sum(cpu_m(r.get("cpu", "0")) for r in reqs),
            "memReqMi": sum(mem_mi(r.get("memory", "0")) for r in reqs),
            "created": p["metadata"].get("creationTimestamp", ""),
        })
    return out


# ---------------------------------------------------------------------------
# Serving endpoints and Bedrock models
# ---------------------------------------------------------------------------

def normalize_endpoint(ep: dict, mode: str) -> dict:
    """Flatten a serving CR (any of the three kinds) into one record.

    `status` is Running / Pending / Error. A kro CR in ERROR (for example a name
    collision with another endpoint's resources) can still report the colliding
    resource's replicas, so its real state and the failing condition's message
    are surfaced instead of a false "ready"."""
    spec = ep.get("spec", {}) or {}
    status = ep.get("status", {}) or {}
    gpu_count = int(spec.get("gpuCount", 1) or 1)
    tp = int(spec.get("tensorParallelSize", 0) or 0)
    pp = int(spec.get("pipelineParallelSize", 1) or 1)
    if tp == 0:
        tp = gpu_count if pp == 1 else 1
    disagg = mode == "llm-d-disagg"
    if disagg:
        prefill_n = int(spec.get("prefillReplicas", 1) or 1)
        decode_n = int(spec.get("decodeReplicas", 1) or 1)
        replicas = prefill_n + decode_n
        avail = int(status.get("prefillReplicas", 0) or 0) + int(status.get("decodeReplicas", 0) or 0)
    else:
        prefill_n = decode_n = None
        replicas = int(spec.get("replicas", spec.get("minReplicas", 1)) or 1)
        avail = int(status.get("availableReplicas", 0) or 0)
    ready = str(status.get("ready", "False"))
    norm = "Running" if ready == "True" else "Pending"
    kro_state = str(status.get("state", "") or "")
    recon_err = ""
    if kro_state == "ERROR":
        norm = "Error"
        for c in status.get("conditions", []) or []:
            if c.get("type") in ("Ready", "ResourcesReady") and str(c.get("status")) == "False":
                recon_err = " ".join((c.get("message", "") or "").split())[:300]
                break
    return {
        "name": ep["metadata"]["name"], "model": spec.get("model", ""), "mode": mode,
        "shared": bool(spec.get("shared", False)), "gpuCount": gpu_count, "tp": tp, "pp": pp,
        "replicas": replicas, "availableReplicas": avail, "ready": ready,
        # `modelStatus` is kept for the current UI; `status` is the same value.
        "modelStatus": norm, "status": norm,
        "routerHealth": str(status.get("routerHealth", "")) if mode in ("llm-d", "llm-d-disagg") else "",
        "message": "", "kroState": kro_state, "reconcileError": recon_err,
        "prefillReplicas": prefill_n, "decodeReplicas": decode_n,
        "created": ep["metadata"].get("creationTimestamp", ""),
    }


def endpoint_records(items_by_mode: dict[str, list[dict]]) -> list[dict]:
    """Normalized endpoints of all kinds, sorted by (mode, name). A CR that
    cannot be normalized (malformed) is skipped."""
    out = []
    for _plural, mode in SERVING_KINDS:
        for ep in items_by_mode.get(mode, []):
            try:
                out.append(normalize_endpoint(ep, mode))
            except (KeyError, TypeError, ValueError):
                continue
    out.sort(key=lambda e: (e["mode"], e["name"]))
    return out


def bedrock_from_config(config: dict | None) -> list[dict]:
    """Static Bedrock models in a parsed litellm-config `config.yaml`."""
    out = []
    for m in (config or {}).get("model_list") or []:
        mdl = str((m.get("litellm_params") or {}).get("model", ""))
        if mdl.startswith("bedrock/"):
            out.append({"name": m.get("model_name", ""), "model": mdl})
    return out


def bedrock_from_crs(items: list[dict]) -> list[dict]:
    """Bedrock models enrolled as BedrockModel CRs (alias = spec.modelName or the name)."""
    out = []
    for item in items:
        spec = item.get("spec") or {}
        name = spec.get("modelName") or (item.get("metadata") or {}).get("name", "")
        if name:
            out.append({"name": name, "model": str(spec.get("model", ""))})
    return out


def merge_bedrock(static: list[dict], crs: list[dict]) -> list[dict]:
    """Static entries plus CRs, de-duplicated by name; a CR wins."""
    merged = {m["name"]: m for m in static if m.get("name")}
    for m in crs:
        merged[m["name"]] = m
    return list(merged.values())


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

EMPTY_LITELLM = {"perModel": {}, "teams": [], "trend": [], "health": {}, "byTeam": [], "byUser": []}


def model_status(ep: dict) -> str:
    """error / running / deploying, from a normalized endpoint."""
    if ep.get("kroState") == "ERROR":
        return "error"
    return "running" if ep["ready"] == "True" else "deploying"


def build_models(endpoints: list[dict], bedrock: list[dict], litellm: dict,
                 pods: list[dict], nodes: list[dict]) -> list[dict]:
    """One record per served model (self-hosted and Bedrock) with traffic,
    cost and tokens per dollar, busiest first."""
    by_node = {n["name"]: n for n in nodes}
    models = []
    for ep in endpoints:
        models.append({
            "name": ep["name"], "model": ep["model"], "tier": ep["mode"],
            "status": model_status(ep),
            "gpu": "shared" if ep.get("shared") else f'{ep["gpuCount"]}×GPU',
            "replicas": ep.get("replicas"), "availableReplicas": ep.get("availableReplicas"),
            "router": ep.get("routerHealth", ""), "costHr": model_cost(ep["name"], pods, by_node),
            "reconcileError": ep.get("reconcileError", ""),
            "prefillReplicas": ep.get("prefillReplicas"), "decodeReplicas": ep.get("decodeReplicas"),
            "created": ep.get("created")})
    for b in bedrock:
        models.append({"name": b["name"], "model": b["model"], "tier": "bedrock",
                       "status": "running", "gpu": "—", "replicas": None, "availableReplicas": None,
                       "router": "", "costHr": 0.0, "created": None})
    per_model = litellm.get("perModel", {})
    health = litellm.get("health", {})
    for m in models:
        pm = per_model.get(m["name"])
        m["rpm"] = pm["rpm"] if pm else 0
        m["p50"] = pm["p50"] if pm else None
        m["p95"] = pm["p95"] if pm else None
        m["tps"] = pm["tps"] if pm else 0
        m["errPct"] = pm["errPct"] if pm else 0
        if pm and m["tier"] == "bedrock":
            m["costHr"] = round(pm["spendHr"], 2)
        h = health.get(m["name"])
        m["health"] = h["status"] if h else ""
        m["tokPerDollar"] = tok_per_dollar(m.get("tps"), m.get("costHr"))
    models.sort(key=lambda x: (-(x.get("rpm") or 0), x["name"]))
    return models


def gpu_records(nodes: list[dict], pods: list[dict], endpoints: list[dict], util: dict) -> list[dict]:
    """Per GPU node: utilization and memory per GPU, and the model it serves."""
    out = []
    for n in nodes:
        if n.get("gpu", 0) <= 0:
            continue
        host = n["name"].split(".")[0]
        e = util.get(host) or util.get(n["name"]) or {}
        model = next((ep["name"] for ep in endpoints for p in pods
                      if p.get("node") == n["name"] and p.get("gpu", 0) > 0
                      and p["name"].startswith(ep["name"] + "-")), None)
        out.append({"node": host, "instance": n["instance"],
                    "util": e.get("util") or [0] * n["gpu"],
                    "mem": e.get("mem") or [None] * n["gpu"], "model": model})
    return out


def argocd_records(items: list[dict]) -> list[dict]:
    return [{"name": a["metadata"]["name"],
             "sync": (a.get("status", {}) or {}).get("sync", {}).get("status", "?"),
             "health": (a.get("status", {}) or {}).get("health", {}).get("status", "?")}
            for a in items]


def cost_ext(models: list[dict], litellm: dict) -> dict:
    """Cost views: by model, team, user, 14-day trend, tokens-per-dollar ranking.
    Models with a cost basis but no throughput rank last (idle but billing)."""
    by_model = sorted([[m["name"], m["costHr"], m["tier"]] for m in models if m["costHr"] > 0],
                      key=lambda x: -x[1])
    by_eff = sorted([[m["name"], m["tokPerDollar"], m["tier"]] for m in models
                     if m.get("tokPerDollar") is not None], key=lambda x: -x[1])
    return {"byModel": by_model, "byTeam": litellm.get("byTeam", []), "trend": litellm.get("trend", []),
            "byUser": litellm.get("byUser", []), "byEfficiency": by_eff}


# ---------------------------------------------------------------------------
# Events, autoscaling, diagnostics
# ---------------------------------------------------------------------------

def event_record(e: dict) -> dict:
    io = e.get("involvedObject", {}) or {}
    return {
        "type": e.get("type", ""), "reason": e.get("reason", ""),
        "message": " ".join((e.get("message", "") or "").split())[:220],
        "count": int(e.get("count", 1) or 1), "obj": io.get("name", ""),
        "ts": e.get("lastTimestamp") or e.get("eventTime") or "",
        "by": (e.get("source", {}) or {}).get("component", "") or e.get("reportingComponent", ""),
    }


def events_of_kind(items: list[dict], kind: str) -> list[dict]:
    return [event_record(e) for e in items if (e.get("involvedObject", {}) or {}).get("kind") == kind]


def quantile_map(results: list[dict]) -> dict:
    """PromQL vector grouped by (pool, role) -> value; NaN (no samples) dropped."""
    out = {}
    for s in results:
        m = s.get("metric", {})
        try:
            v = float(s["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if v == v:
            out[(m.get("pool", ""), m.get("role", ""))] = v
    return out


def _pick(b: dict, pool: str, role: str):
    if (pool, role) in b:
        return b[(pool, role)]
    # llm-d single pools label their pods role=decode while the HPA target is
    # <name>-llmd (role ""): fall back to any role of the same pool.
    for (p, _r), v in b.items():
        if p == pool:
            return v
    return None


def autoscaling_status(endpoints: list[dict], hpas: list[dict], hpa_events: list[dict],
                       ttft: dict, itl: dict) -> list[dict]:
    """Per-pool KEDA/HPA state: current and desired within min..max, the
    driving signal against its target, capped-at-max, p95 latency, recent
    scale events. Pool and role come from the scale target's suffix:
    <pool>-prefill, <pool>-decode, <pool>-llmd."""
    mode_by_pool = {ep["name"]: ep.get("mode", "") for ep in endpoints}
    rescales: dict = {}
    for e in hpa_events:
        if e.get("reason") == "SuccessfulRescale":
            rescales.setdefault(e.get("obj", ""), []).append({"ts": e.get("ts", ""), "msg": e.get("message", "")})
    out = []
    for h in hpas:
        meta, spec, st = h.get("metadata", {}) or {}, h.get("spec", {}) or {}, h.get("status", {}) or {}
        dep = (spec.get("scaleTargetRef", {}) or {}).get("name", "")
        if dep.endswith("-prefill"):
            pool, role, sig, unit = dep[:-len("-prefill")], "prefill", "queue depth", "req"
        elif dep.endswith("-decode"):
            pool, role, sig, unit = dep[:-len("-decode")], "decode", "KV-cache", "%"
        elif dep.endswith("-llmd"):
            pool, role, sig, unit = dep[:-len("-llmd")], "", "queue depth", "req"
        else:
            pool, role, sig, unit = dep, "", "queue depth", "req"
        # Driving signal: the external metric with the highest value/target.
        best = None
        curs = st.get("currentMetrics", []) or []
        for i, ms in enumerate(spec.get("metrics", []) or []):
            tgt = quantity(((ms.get("external", {}) or {}).get("target", {}) or {}).get("averageValue"))
            val = 0.0
            if i < len(curs):
                val = quantity((((curs[i] or {}).get("external", {}) or {}).get("current", {}) or {}).get("averageValue"))
            ratio = (val / tgt) if tgt > 0 else 0.0
            if best is None or ratio > best[2]:
                best = (val, tgt, ratio)
        val, tgt, _ = best or (0.0, 0.0, 0.0)
        tt, it = _pick(ttft, pool, role), _pick(itl, pool, role)
        mn = int(spec.get("minReplicas", 1) or 1)
        mx = int(spec.get("maxReplicas", 1) or 1)
        des = int(st.get("desiredReplicas", 0) or 0)
        out.append({
            "pool": pool, "role": role, "mode": mode_by_pool.get(pool, ""),
            "deployment": dep, "signal": sig, "unit": unit,
            "value": round(val, 2), "target": round(tgt, 2),
            "ttftMs": round(tt * 1000) if tt else None,
            "itlMs": round(it * 1000, 1) if it else None,
            "min": mn, "max": mx, "current": int(st.get("currentReplicas", 0) or 0), "desired": des,
            "atMax": des >= mx and mx > mn,            # pinned at the ceiling and wanting more
            "saturated": tgt > 0 and val >= 0.9 * tgt,  # within 10 % of the target
            "lastScale": st.get("lastScaleTime", ""),
            "events": sorted(rescales.get(meta.get("name", ""), []), key=lambda x: x["ts"], reverse=True)[:6],
        })
    out.sort(key=lambda x: (x["pool"], x["role"]))
    return out


def attach_scale(models: list[dict], autoscaling: list[dict]) -> None:
    """Replace spec replica counts with the live HPA view for scaled models."""
    by_pool: dict = {}
    for a in autoscaling:
        by_pool.setdefault(a["pool"], []).append(a)
    for m in models:
        sc = by_pool.get(m["name"])
        if sc:
            m["scale"] = sc
            m["availableReplicas"] = sum(x["current"] for x in sc)
            m["replicas"] = sum(x["desired"] for x in sc)


def provisioning_warnings(nodeclaim_events: list[dict]) -> list[dict]:
    """Up to 5 distinct Karpenter capacity warnings (GPU provisioning)."""
    out, seen = [], set()
    for e in nodeclaim_events:
        if e["type"] != "Warning" or not any(h in e["reason"].lower() for h in PROVISION_HINTS):
            continue
        key = (e["reason"], e["message"][:60])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out[:5]


def deploy_pod(name: str, pods: list[dict]) -> dict | None:
    """The pod the diagnostics show for self-hosted model `name`: its newest
    GPU pod, else its newest pod. The transient LiteLLM register Job pod is
    never it."""
    cand = [p for p in pods if p.get("namespace") == "inference"
            and p["name"].startswith(name + "-") and "-register" not in p["name"]]
    return max(cand, key=lambda p: ((p.get("gpu", 0) or 0) > 0, p.get("created", "")), default=None)


def log_container(pod_status: dict | None) -> str:
    """The container whose log tail the diagnostics show: the pod's first
    one, while the pod is Running with no container ready (the model is
    loading). "" when no log is shown."""
    st = pod_status or {}
    cstats = st.get("containerStatuses", []) or []
    if st.get("phase") != "Running" or not cstats or any(c.get("ready") for c in cstats):
        return ""
    return cstats[0].get("name") or ""


def log_tail_targets(endpoints: list[dict], pods: list[dict],
                     pod_status: Callable[[str], dict | None]) -> list[tuple[str, str]]:
    """(pod, container) of every log tail deploy_diagnostics shows: for each
    endpoint that is not running, its displayed pod while the model loads."""
    out = []
    for ep in endpoints:
        pod = deploy_pod(ep["name"], pods) if model_status(ep) != "running" else None
        container = log_container(pod_status(pod["name"])) if pod else ""
        if container:
            out.append((pod["name"], container))
    return out


def deploy_diagnostics(models: list[dict], pods: list[dict], pod_events: list[dict],
                       provisioning: list[dict], pod_status: Callable[[str], dict | None],
                       log_tail: Callable[[str, str], str]) -> None:
    """Attach a `deploy` block to every self-hosted model that is not running:
    its pod's phase and waiting reason, the most useful recent events, GPU
    capacity warnings when the pod is unschedulable, and a log tail while the
    model loads. `pod_status(name)` returns the pod's `status` object;
    `log_tail(pod, container)` returns recent log text."""
    ev_index: dict = {}
    for e in pod_events:
        ev_index.setdefault(e["obj"], []).append(e)
    for m in models:
        if m.get("status") == "running" or m.get("tier") == "bedrock":
            continue
        pod = deploy_pod(m["name"], pods)
        dep = {"phase": "NoPod", "pod": "", "waiting": None, "events": [], "provisioning": [], "log": []}
        if pod:
            dep["pod"], dep["phase"] = pod["name"], pod.get("phase", "")
            evs = ev_index.get(pod["name"], [])
            unschedulable = any(e["reason"] == "FailedScheduling" for e in evs)
            # Most useful few: warnings first, newest first, one per reason.
            # (The pre-rebuild sort put Normal events first, contrary to its
            # own comment; fixed here.)
            picked, reasons = [], set()
            for e in sorted(evs, key=lambda e: (e["type"] == "Warning", e["ts"]), reverse=True):
                if e["reason"] in reasons:
                    continue
                reasons.add(e["reason"])
                picked.append(e)
                if len(picked) >= 4:
                    break
            dep["events"] = picked
            if pod.get("phase") == "Pending" and unschedulable and (pod.get("gpu", 0) or 0) > 0 and provisioning:
                dep["provisioning"] = provisioning
            st = pod_status(pod["name"])
            if st:
                for c in st.get("containerStatuses", []) or []:
                    w = (c.get("state", {}) or {}).get("waiting")
                    if w and not dep["waiting"]:
                        dep["waiting"] = {"reason": w.get("reason", ""),
                                          "message": " ".join((w.get("message", "") or "").split())[:200]}
                cname = log_container(st)
                if cname:
                    txt = log_tail(pod["name"], cname)
                    if txt:
                        dep["log"] = [ln for ln in txt.splitlines() if ln.strip()][-14:]
        m["deploy"] = dep


def build_links(alb_host: str, webui: str, litellm: str, langfuse: str, argocd: str) -> list[dict]:
    """Quick links: public edge URLs when set, else the internal ALB host and
    port (in-VPC and tunnel access); a link with no URL is omitted."""
    def alb(port: int) -> str:
        return f"http://{alb_host}:{port}" if alb_host else ""

    webui, litellm, langfuse = webui or alb(8080), litellm or alb(4000), langfuse or alb(3000)
    links = []
    if webui:
        links.append({"label": "Open WebUI", "url": webui, "desc": "Chat with models", "icon": "💬"})
    if litellm:
        links.append({"label": "LiteLLM Admin", "url": litellm.rstrip("/") + "/ui/",
                      "desc": "API gateway + keys + usage", "icon": "🔑"})
    if langfuse:
        links.append({"label": "Langfuse", "url": langfuse, "desc": "Traces, evals, cost", "icon": "📊"})
    if argocd:
        links.append({"label": "ArgoCD", "url": argocd, "desc": "GitOps sync status", "icon": "🚢"})
    return links
