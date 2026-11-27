"""Pure dashboard logic not already pinned by the /data.json golden test:
Prometheus parsing, DCGM aggregation, settings, autoscaling signal choice,
diagnostics event picking, links, quantities."""
import pytest

from dashboard import config, dcgm, models, prom
from dashboard.k8s import project_argo


def test_parse_text_handles_labels_comments_and_filters():
    text = "\n".join([
        "# HELP x y",
        'DCGM_FI_DEV_GPU_UTIL{gpu="0",Hostname="ip-1",UUID="a,b"} 37',
        'DCGM_FI_DEV_GPU_UTIL{gpu="1",Hostname="ip-1"} 5 1700000000',
        "plain_metric 2.5",
        "broken{",
        'other{gpu="0"} 9',
    ])
    got = prom.parse_text(text, {"DCGM_FI_DEV_GPU_UTIL", "plain_metric"})
    assert [(s.name, s.labels.get("gpu"), s.value) for s in got] == [
        ("DCGM_FI_DEV_GPU_UTIL", "0", 37.0), ("DCGM_FI_DEV_GPU_UTIL", "1", 5.0), ("plain_metric", None, 2.5)]
    assert got[0].labels["UUID"] == "a,b", "a comma inside a label value must not split labels"


def test_dcgm_aggregate_orders_gpus_and_computes_memory_percent():
    s = prom.Sample
    samples = [s("DCGM_FI_DEV_GPU_UTIL", {"Hostname": "n1", "gpu": "1"}, 10),
               s("DCGM_FI_DEV_GPU_UTIL", {"Hostname": "n1", "gpu": "0"}, 90),
               s("DCGM_FI_DEV_FB_USED", {"Hostname": "n1", "gpu": "0"}, 3),
               s("DCGM_FI_DEV_FB_FREE", {"Hostname": "n1", "gpu": "0"}, 1)]
    assert dcgm.aggregate(samples) == {"n1": {"util": [90, 10], "mem": [75, None]}}


def test_dcgm_scrape_reports_failing_exporters_and_falls_back():
    def fetch(url):
        if url == "bad":
            raise OSError("refused")
        return 'DCGM_FI_DEV_GPU_UTIL{Hostname="n",gpu="0"} 4'
    util, errors = dcgm.scrape(["bad"], "svc", fetch=fetch)
    assert util == {"n": {"util": [4], "mem": [None]}}
    assert errors == ["bad: refused"]


def test_settings_defaults_and_validation():
    s = config.load({})
    assert (s.port, s.approval_expiry_hours, s.auth_required, s.db_configured) == (8080, 24, True, False)
    s = config.load({"OIDC_ALLOWED_GROUPS": "a, b,", "DASHBOARD_AUTH_REQUIRED": "False",
                     "DB_USER": "u", "DB_PASSWORD": "s3cr3t-pw", "OIDC_ISSUER_URL": "https://x/"})
    assert s.oidc_allowed_groups == ("a", "b") and not s.auth_required and s.db_configured
    assert s.oidc_issuer_url == "https://x"
    assert "s3cr3t-pw" not in repr(s), "the DB password must not appear in repr/logs"
    with pytest.raises(ValueError, match="APPROVAL_EXPIRY_HOURS"):
        config.load({"APPROVAL_EXPIRY_HOURS": "soon"})


def _hpa(target, metrics, currents, mn=1, mx=4, cur=1, des=1):
    return {"metadata": {"name": f"keda-hpa-{target}"},
            "spec": {"scaleTargetRef": {"name": target}, **({} if mn is None else {"minReplicas": mn}),
                     "maxReplicas": mx,
                     "metrics": [{"external": {"target": {"averageValue": t}}} for t in metrics]},
            "status": {"currentReplicas": cur, "desiredReplicas": des,
                       "currentMetrics": [{"external": {"current": {"averageValue": v}}} for v in currents]}}


def test_autoscaling_picks_the_metric_closest_to_its_target_and_roles():
    hpas = [_hpa("m-prefill", ["10", "4"], ["2", "3900m"], des=4),
            _hpa("m-decode", ["80"], ["10"])]
    out = models.autoscaling_status([{"name": "m", "mode": "llm-d-disagg"}], hpas, [],
                                    {("m", "prefill"): 0.5}, {})
    pre, dec = [a for a in out if a["role"] == "prefill"][0], [a for a in out if a["role"] == "decode"][0]
    assert (pre["value"], pre["target"], pre["saturated"], pre["atMax"]) == (3.9, 4.0, True, True)
    assert pre["ttftMs"] == 500 and pre["mode"] == "llm-d-disagg"
    assert (dec["signal"], dec["unit"], dec["saturated"]) == ("KV-cache", "%", False)


def test_autoscaling_names_what_an_llmd_model_scales_on():
    """KEDA sizes an LLMDEndpoint on the requests in flight per replica
    (targetInflight); the vLLM queue depth is the prefill pool's signal."""
    hpas = [_hpa("m-llmd", ["16"], ["4"]), _hpa("d-prefill", ["20"], ["1"]), _hpa("d-decode", ["85"], ["10"])]
    out = {a["deployment"]: a for a in models.autoscaling_status([], hpas, [], {}, {})}
    assert (out["m-llmd"]["signal"], out["m-llmd"]["unit"]) == ("requests in flight", "req")
    assert (out["d-prefill"]["signal"], out["d-prefill"]["unit"]) == ("queue depth", "req")
    assert (out["d-decode"]["signal"], out["d-decode"]["unit"]) == ("KV-cache", "%")


def test_autoscaling_shows_the_real_minimum_including_zero():
    """KEDA gives the HPA of a scale-to-zero model minReplicas 1 and moves the
    Deployment between 0 and 1 itself, so the model's own minimum is the real one."""
    zero = models.normalize_endpoint(_llmd("z", {"minReplicas": 0, "maxReplicas": 2}), "llm-d")
    warm = models.normalize_endpoint(_llmd("w", {"minReplicas": 2, "maxReplicas": 4}), "llm-d")
    hpas = [_hpa("z-llmd", ["16"], ["0"], mn=1, mx=2, cur=0, des=0),
            _hpa("w-llmd", ["16"], ["1"], mn=2, mx=4, cur=2, des=2),
            _hpa("other-llmd", ["16"], ["0"], mn=0, mx=2, cur=0, des=0), _hpa("bare-llmd", ["16"], ["0"], mn=None)]
    out = {a["pool"]: a for a in models.autoscaling_status([zero, warm], hpas, [], {}, {})}
    assert (out["z"]["min"], out["z"]["max"], out["z"]["current"], out["z"]["atMax"]) == (0, 2, 0, False)
    assert out["w"]["min"] == 2
    assert out["other"]["min"] == 0, "an HPA minimum of 0 is 0, not 1"
    assert out["bare"]["min"] == 1, "no minimum reported: the Kubernetes default"


# ---------------------------------------------------------------------------
# Serving endpoints: replica counts, scaled to zero, waking from zero
# ---------------------------------------------------------------------------

NOW = 1795168800.0               # 2026-11-20T10:00:00Z


def _llmd(name, spec=None, status=None):
    return {"metadata": {"name": name, "creationTimestamp": "2026-11-20T08:00:00Z"},
            "spec": {"model": "org/m", **(spec or {})}, "status": status or {}}


def _model_pod(name, created, gpu=1):
    return {"name": name, "namespace": "inference", "node": "", "phase": "Pending", "gpu": gpu, "created": created}


def test_replica_counts_come_from_the_current_fields():
    """spec.replicas (LLMDEndpoint) and spec.prefillReplicas/decodeReplicas
    (LLMDDisaggEndpoint) are deprecated and ignored by the kro definitions, but
    still filled in with their defaults by the API server."""
    norm = models.normalize_endpoint
    ep = norm(_llmd("m", {"replicas": 1, "minReplicas": 2, "maxReplicas": 4},
                    {"replicas": 3, "availableReplicas": 2, "ready": "True"}), "llm-d")
    assert (ep["replicas"], ep["availableReplicas"], ep["minReplicas"]) == (3, 2, 2), "the Deployment's size"
    assert norm(_llmd("m", {"replicas": 1, "minReplicas": 2}), "llm-d")["replicas"] == 2, "no status yet: the floor"
    assert norm(_llmd("m", {"replicas": 1, "minReplicas": 0}), "llm-d")["replicas"] == 0
    assert norm(_llmd("m"), "llm-d")["replicas"] == 1, "schema default"

    disagg = norm(_llmd("d", {"prefillReplicas": 1, "decodeReplicas": 2, "prefillMinReplicas": 2,
                              "decodeMinReplicas": 3}, {"prefillReplicas": 1, "decodeReplicas": 3}), "llm-d-disagg")
    assert (disagg["prefillReplicas"], disagg["decodeReplicas"], disagg["replicas"]) == (2, 3, 5)
    assert disagg["availableReplicas"] == 4 and disagg["minReplicas"] is None
    bare = norm(_llmd("d", {"prefillReplicas": 1, "decodeReplicas": 2}), "llm-d-disagg")
    assert (bare["prefillReplicas"], bare["decodeReplicas"]) == (1, 1), "the floors' defaults, not the old fields"

    vllm = norm(_llmd("v", {"replicas": 2}, {"availableReplicas": 1}), "vllm")
    assert (vllm["replicas"], vllm["availableReplicas"], vllm["minReplicas"]) == (2, 1, None)


ZERO = {"ready": "True", "scaledToZero": True, "replicas": 0, "availableReplicas": 0, "routerHealth": "Healthy",
        "state": "ACTIVE"}
STARTING = {"ready": "False", "scaledToZero": False, "replicas": 1, "availableReplicas": 0,
            "routerHealth": "Healthy", "state": "IN_PROGRESS"}


def test_a_model_scaled_to_zero_is_its_own_state():
    """kro reports an idle model at zero replicas as ready; it serves nothing
    and costs nothing until a request wakes it."""
    ep = models.normalize_endpoint(_llmd("z", {"minReplicas": 0, "maxReplicas": 2}, ZERO), "llm-d")
    assert (ep["scaledToZero"], ep["replicas"], ep["availableReplicas"], ep["ready"]) == (True, 0, 0, "True")
    assert models.model_status(ep) == "scaled-to-zero"
    mdl = models.build_models([ep], [], models.EMPTY_LITELLM, [], [], NOW)[0]
    assert (mdl["status"], mdl["replicas"], mdl["availableReplicas"], mdl["costHr"]) == ("scaled-to-zero", 0, 0, 0.0)
    models.deploy_diagnostics([mdl], [], [], [], lambda n: None, lambda n, c: "")
    assert "deploy" not in mdl, "nothing is deploying"
    assert models.log_tail_targets([ep], [], lambda n: None) == []

    for status in ({**ZERO, "scaledToZero": "true"}, {**ZERO, "scaledToZero": "True"}):
        assert models.model_status(models.normalize_endpoint(_llmd("z", {"minReplicas": 0}, status), "llm-d")) \
            == "scaled-to-zero"
    stopping = models.normalize_endpoint(_llmd("z", {"minReplicas": 0}, {**ZERO, "availableReplicas": 1}), "llm-d")
    assert models.model_status(stopping) == "running", "its last replica still answers"
    broken = models.normalize_endpoint(_llmd("z", {"minReplicas": 0}, {**ZERO, "state": "ERROR"}), "llm-d")
    assert models.model_status(broken) == "error"
    assert models.model_status(models.normalize_endpoint(_llmd("v", {}, ZERO), "vllm")) == "running", \
        "only an LLMDEndpoint scales to zero"


@pytest.mark.parametrize("spec,status,pods,now,expected", [
    ({"minReplicas": 0}, STARTING, [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:57:00Z")], NOW, "waking"),
    # 600 s hold + 120 s: the pod is 11 min 59 s old, then 12 min.
    ({"minReplicas": 0}, STARTING, [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:48:01Z")], NOW, "waking"),
    ({"minReplicas": 0}, STARTING, [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:48:00Z")], NOW, "deploying"),
    ({"minReplicas": 0, "coldStartTimeoutSeconds": 120}, STARTING,
     [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:55:00Z")], NOW, "deploying"),
    ({"minReplicas": 0, "coldStartTimeoutSeconds": 900}, STARTING,
     [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:45:00Z")], NOW, "waking"),
    ({"minReplicas": 1}, STARTING, [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:57:00Z")], NOW, "deploying"),
    ({"minReplicas": 0}, STARTING, [], NOW, "deploying"),                       # no pod to time the start by
    ({"minReplicas": 0}, STARTING, [_model_pod("m-llmd-epp-7f6d-abcde", "2026-11-20T09:57:00Z", gpu=0),
                                    _model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:40:00Z")], NOW, "deploying"),
    ({"minReplicas": 0}, STARTING, [_model_pod("mm-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:57:00Z")], NOW, "deploying"),
    ({"minReplicas": 0}, STARTING, [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "not a time")], NOW, "deploying"),
    ({"minReplicas": 0}, STARTING, [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:57:00Z")], None, "deploying"),
    ({"minReplicas": 0}, {**STARTING, "state": "ERROR"},
     [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:57:00Z")], NOW, "error"),
    ({"minReplicas": 0}, {**STARTING, "ready": "True", "availableReplicas": 1},
     [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:57:00Z")], NOW, "running"),
], ids=["within-the-hold", "just-inside", "hold-passed", "short-hold-passed", "long-hold", "always-warm", "no-pod",
        "old-gpu-pod", "another-models-pod", "bad-time", "no-clock", "kro-error", "ready"])
def test_a_model_starting_from_zero_is_waking_until_the_hold_has_passed(spec, status, pods, now, expected):
    ep = models.normalize_endpoint(_llmd("m", spec, status), "llm-d")
    assert models.model_status(ep, pods, now) == expected
    assert models.build_models([ep], [], models.EMPTY_LITELLM, pods, [], now)[0]["status"] == expected


def test_a_waking_model_keeps_its_diagnostics():
    """What it waits for (node, image, model load) is worth showing; it is the alert that is not."""
    pods = [_model_pod("m-llmd-5b7c9d6f4-k8x2p", "2026-11-20T09:57:00Z")]
    mdl = models.build_models([models.normalize_endpoint(_llmd("m", {"minReplicas": 0}, STARTING), "llm-d")], [],
                              models.EMPTY_LITELLM, pods, [], NOW)
    models.deploy_diagnostics(mdl, pods, [], [], lambda n: None, lambda n, c: "")
    assert mdl[0]["status"] == "waking" and mdl[0]["deploy"]["pod"] == "m-llmd-5b7c9d6f4-k8x2p"


def test_dashboard_and_agent_allow_a_cold_start_the_same_time():
    from pha import triggers
    assert models.WAKE_MARGIN_SEC == triggers.WAKE_MARGIN_SEC


def test_compute_takes_the_time_from_its_caller():
    from dashboard import config, derive
    settings = config.load({"CLUSTER_NAME": "c", "AWS_REGION": "eu-central-1"})
    view = {"llm-d": [_llmd("m", {"minReplicas": 0}, STARTING)],
            "pods": [{"metadata": {"name": "m-llmd-5b7c9d6f4-k8x2p", "namespace": "inference",
                                   "creationTimestamp": "2026-11-20T09:57:00Z"},
                      "spec": {"containers": [{"resources": {"requests": {"nvidia.com/gpu": "1"}}}]},
                      "status": {"phase": "Pending"}}]}
    status = lambda now: derive.compute(view, {}, settings, now=now)[0]["models"][0]["status"]   # noqa: E731
    assert (status(NOW), status(NOW + 900), status(None)) == ("waking", "deploying", "deploying")


def test_diagnostics_picks_warnings_first_one_per_reason():
    ev = [{"type": "Normal", "reason": "Pulled", "ts": "3", "obj": "m-x"},
          {"type": "Warning", "reason": "BackOff", "ts": "1", "obj": "m-x"},
          {"type": "Warning", "reason": "BackOff", "ts": "2", "obj": "m-x"}]
    mdl = [{"name": "m", "status": "deploying", "tier": "vllm"}]
    pods = [{"name": "m-x", "namespace": "inference", "phase": "Running", "gpu": 1, "created": "t"}]
    logs = []
    status = {"phase": "Running", "containerStatuses": [
        {"name": "vllm", "ready": False, "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "a  b"}}}]}
    models.deploy_diagnostics(mdl, pods, ev, [], lambda n: status, lambda n, c: logs.append((n, c)) or "l1\n\nl2")
    dep = mdl[0]["deploy"]
    assert [(e["reason"], e["ts"]) for e in dep["events"]] == [("BackOff", "2"), ("Pulled", "3")]
    assert dep["waiting"] == {"reason": "CrashLoopBackOff", "message": "a b"}
    assert dep["log"] == ["l1", "l2"] and logs == [("m-x", "vllm")]


def test_diagnostics_skip_running_and_bedrock_models():
    mdl = [{"name": "a", "status": "running", "tier": "vllm"}, {"name": "b", "status": "deploying", "tier": "bedrock"}]
    models.deploy_diagnostics(mdl, [], [], [], lambda n: None, lambda n, c: "")
    assert all("deploy" not in m for m in mdl)


def test_links_prefer_public_urls_and_skip_missing():
    links = models.build_links("alb", "https://chat.example", "", "", "")
    assert [(lk["label"], lk["url"]) for lk in links] == [
        ("Open WebUI", "https://chat.example"), ("LiteLLM Admin", "http://alb:4000/ui/"), ("Langfuse", "http://alb:3000")]
    assert models.build_links("", "", "", "", "") == []


@pytest.mark.parametrize("fn,raw,expected", [
    (models.cpu_m, "250m", 250), (models.cpu_m, "2", 2000), (models.cpu_m, "120000000n", 120), (models.cpu_m, "x", 0),
    (models.mem_mi, "1Gi", 1024), (models.mem_mi, "2048Ki", 2), (models.mem_mi, "bad", 0),
    (models.quantity, "3800m", 3.8), (models.quantity, None, 0.0), (models.quantity, "?", 0.0),
])
def test_quantities(fn, raw, expected):
    assert fn(raw) == expected


# ---------------------------------------------------------------------------
# Karpenter node pools and node claims (Infrastructure view)
# ---------------------------------------------------------------------------

def _pool(name, *, limit=None, gpu=True, capacity=("spot", "on-demand"), not_in=(), workload="", gpu_in_use=None, nodes=None):
    reqs = [{"key": "karpenter.sh/capacity-type", "operator": "In", "values": list(capacity)}]
    if not_in:
        reqs.append({"key": "karpenter.k8s.aws/instance-family", "operator": "NotIn", "values": list(not_in)})
    spec = {"template": {"metadata": {"labels": {"workload-type": workload} if workload else {}},
                         "spec": {"requirements": reqs,
                                  "taints": [{"key": "nvidia.com/gpu", "effect": "NoSchedule"}] if gpu else []}}}
    if limit is not None:
        spec["limits"] = {"nvidia.com/gpu": str(limit)}
    resources = {}
    if gpu_in_use is not None:
        resources["nvidia.com/gpu"] = str(gpu_in_use)
    if nodes is not None:
        resources["nodes"] = str(nodes)
    return {"metadata": {"name": name}, "spec": spec, "status": {"resources": resources}}


def test_nodepool_records_carry_limit_gpu_flag_and_what_the_pool_may_launch():
    got = models.nodepool_records([
        _pool("gpu-shared", limit=32, not_in=("g4dn", "g7", "g7e"), workload="gpu-shared", gpu_in_use=4, nodes=1),
        _pool("default", gpu=False, capacity=("spot",)),
        {"metadata": {"name": "bare"}},                       # no spec, no status: defaults, not a crash
    ])
    assert [p["name"] for p in got] == ["bare", "default", "gpu-shared"], "sorted by name"
    shared = got[2]
    assert shared == {"name": "gpu-shared", "isGpu": True, "gpuLimit": 32, "gpuStarted": 4, "nodes": 1,
                      "capacityTypes": ["on-demand", "spot"], "excludedFamilies": ["g4dn", "g7", "g7e"],
                      "workload": "gpu-shared"}
    assert got[1]["isGpu"] is False and got[1]["gpuLimit"] is None and got[1]["capacityTypes"] == ["spot"]
    assert got[0] == {"name": "bare", "isGpu": False, "gpuLimit": None, "gpuStarted": 0, "nodes": 0,
                      "capacityTypes": [], "excludedFamilies": [], "workload": ""}


def _claim(name, conditions, *, pool="gpu-shared", instance="g6.4xlarge", node="", deleting=False):
    meta = {"name": name, "creationTimestamp": "2026-11-20T09:48:00Z",
            "labels": {"karpenter.sh/nodepool": pool, "node.kubernetes.io/instance-type": instance,
                       "topology.kubernetes.io/zone": "eu-central-1c", "karpenter.sh/capacity-type": "spot"}}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-20T09:59:00Z"
    return {"metadata": meta, "status": {"nodeName": node, "conditions": [
        {"type": t, "status": s, **({"message": msg} if msg else {})} for t, s, msg in conditions]}}


def test_nodeclaim_records_say_how_far_a_node_got_and_what_it_waits_for():
    ready = _claim("ok", [("Launched", "True", ""), ("Registered", "True", ""), ("Initialized", "True", ""),
                          ("Ready", "True", "")], node="ip-1")
    stuck = _claim("stuck", [("Launched", "True", ""), ("Registered", "True", ""),
                             ("Initialized", "Unknown", 'Resource "nvidia.com/gpu" was requested but not registered'),
                             ("Drifted", "True", ""), ("Ready", "Unknown", "Initialized=Unknown")],
                   instance="g7.2xlarge", node="ip-2")
    refused = _claim("refused", [("Launched", "False", "creating instance, insufficient capacity")], instance="")
    got = {c["name"]: c for c in models.nodeclaim_records([stuck, refused, ready, _claim("gone", [], deleting=True)])}
    assert list(got) == ["gone", "ok", "refused", "stuck"]
    assert got["ok"]["ready"] and got["ok"]["waitingOn"] == "" and got["ok"]["node"] == "ip-1"
    s = got["stuck"]
    assert (s["launched"], s["registered"], s["initialized"], s["ready"], s["drifted"]) == (True, True, False, False, True)
    assert s["waitingOn"] == "Initialized" and "not registered" in s["message"]
    assert (s["pool"], s["instance"], s["zone"], s["capacity"]) == ("gpu-shared", "g7.2xlarge", "eu-central-1c", "spot")
    assert s["created"] == "2026-11-20T09:48:00Z"
    r = got["refused"]
    assert not r["launched"] and r["waitingOn"] == "Launched" and "insufficient capacity" in r["message"]
    assert got["gone"]["deleting"] is True


def test_the_karpenter_projection_keeps_what_the_records_need():
    """The generic projection drops labels and deletionTimestamp; a node claim's
    pool, instance type, zone and capacity type are labels."""
    from dashboard import k8s
    raw = _claim("stuck", [("Launched", "True", ""), ("Registered", "True", ""), ("Initialized", "Unknown", "no gpu")],
                 instance="g7.2xlarge", deleting=True)
    raw["metadata"]["managedFields"] = [{"manager": "karpenter"}]
    projected = k8s.project_karpenter(raw)
    assert "managedFields" not in projected["metadata"]
    rec = models.nodeclaim_records([projected])[0]
    assert (rec["pool"], rec["instance"], rec["deleting"], rec["waitingOn"]) == ("gpu-shared", "g7.2xlarge", True, "Initialized")
    pool = models.nodepool_records([k8s.project_karpenter(_pool("gpu-shared", limit=32, workload="gpu-shared"))])[0]
    assert (pool["gpuLimit"], pool["isGpu"], pool["workload"]) == (32, True, "gpu-shared")


def test_compute_publishes_node_pools_and_claims_and_copes_without_karpenter():
    from dashboard import config, derive
    settings = config.load({"CLUSTER_NAME": "c", "AWS_REGION": "eu-central-1"})
    view = {"nodepools": [_pool("gpu-shared", limit=32)],
            "nodeclaims": [_claim("a", [("Ready", "True", "")])]}
    sections, _ = derive.compute(view, {}, settings)
    assert [p["name"] for p in sections["nodePools"]] == ["gpu-shared"]
    assert [c["name"] for c in sections["nodeClaims"]] == ["a"]
    sections, _ = derive.compute({}, {}, settings)            # Karpenter not installed: the caches stay empty
    assert sections["nodePools"] == [] and sections["nodeClaims"] == []


@pytest.mark.parametrize("app", [
    {"metadata": {"name": "new-app"}},                                        # just created: no status yet
    {"metadata": {"name": "new-app"}, "status": {}},
    {"metadata": {"name": "new-app"}, "status": {"sync": None, "health": {}}},
    {"metadata": {"name": "new-app"}, "status": {"sync": {"status": None}, "health": {"status": ""}}},
])
def test_argocd_app_without_status_shows_a_question_mark(app):
    """Never None: the UI prints the values as they are ("null · null")."""
    assert models.argocd_records([project_argo(app)]) == [{"name": "new-app", "sync": "?", "health": "?"}]
    assert models.argocd_records([app]) == [{"name": "new-app", "sync": "?", "health": "?"}]


def test_argocd_records_keep_reported_status():
    app = {"metadata": {"name": "litellm"}, "status": {"sync": {"status": "OutOfSync"}, "health": {"status": "Degraded"}}}
    assert models.argocd_records([project_argo(app)]) == [{"name": "litellm", "sync": "OutOfSync", "health": "Degraded"}]
