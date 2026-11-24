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
            "spec": {"scaleTargetRef": {"name": target}, "minReplicas": mn, "maxReplicas": mx,
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
