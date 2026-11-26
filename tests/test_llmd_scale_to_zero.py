"""LLMDEndpoint scale to zero (minReplicas 0).

What has to hold, without a cluster:
  * the schema keeps minReplicas default 1 and allows 0 (opt-in per model);
  * a model with minReplicas >= 1 gets byte-for-byte the router config it had,
    so its router (EPP) does not restart on upgrade;
  * a model with minReplicas 0 gets llm-d flow control, holding requests for
    coldStartTimeoutSeconds while no replica runs;
  * KEDA sizes the pool on the router's in-flight + held requests (not vLLM's
    queue), wakes it on the same signal or LiteLLM's request rate, and stops
    it after scaleToZeroAfterSeconds;
  * the autoscaling Prometheus scrapes the router with a bearer token and
    has only `get /metrics` cluster-wide for that;
  * litellm-sync gives a scale-to-zero model a timeout that covers the hold.
"""

import os
import re

import pytest
import yaml

from test_litellm_sync_delete import sync  # noqa: F401 - module fixture (stubs kubernetes)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RGD = os.path.join(REPO_ROOT, "platform/config/kro/llmd-endpoint.yaml")
PROM = os.path.join(REPO_ROOT, "platform/config/autoscaling-prometheus.yaml")

# The router config text of v2026.12.0 (weights left as kro expressions). A
# minReplicas >= 1 model must render exactly this, or every router restarts.
BASELINE_ROUTER_CONFIG = """apiVersion: llm-d.ai/v1alpha1
kind: EndpointPickerConfig
plugins:
- type: queue-scorer
- type: kv-cache-utilization-scorer
- type: prefix-cache-scorer
- type: no-hit-lru-scorer
schedulingProfiles:
- name: default
  plugins:
  - pluginRef: queue-scorer
    weight: ${string(schema.spec.routingProfile == 'throughput' ? 3 : (schema.spec.routingProfile == 'latency' ? 4 : (schema.spec.routingProfile == 'prefix' ? 1 : 2)))}
  - pluginRef: kv-cache-utilization-scorer
    weight: ${string(schema.spec.routingProfile == 'latency' ? 1 : (schema.spec.routingProfile == 'balanced' ? 2 : 3))}
  - pluginRef: prefix-cache-scorer
    weight: ${string(schema.spec.routingProfile == 'prefix' ? 4 : (schema.spec.routingProfile == 'throughput' ? 1 : (schema.spec.routingProfile == 'latency' ? 2 : 3)))}
  - pluginRef: no-hit-lru-scorer
    weight: ${string((schema.spec.routingProfile == 'prefix' || schema.spec.routingProfile == 'latency') ? 1 : 2)}
"""

FLOW_CONTROL_RX = re.compile(
    r'^\$\{schema\.spec\.minReplicas == 0 \? (".*") \+ string\(schema\.spec\.coldStartTimeoutSeconds\) \+ (".*") : ""\}$')


@pytest.fixture(scope="module")
def rgd():
    with open(RGD, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _resource(rgd, rid):
    return next(r for r in rgd["spec"]["resources"] if r["id"] == rid)


def _router_config(rgd) -> str:
    epp = _resource(rgd, "routerapp")["template"]["spec"]["source"]["helm"]["valuesObject"]["router"]["epp"]
    return epp["pluginsCustomConfig"][epp["pluginsConfigFile"]]


def _render_config(text: str, min_replicas: int, cold_start: int) -> str:
    """What kro makes of the config text: the last line is the flow-control
    expression; everything before it is kept as is."""
    head, last = text.rsplit("\n", 1)
    m = FLOW_CONTROL_RX.match(last)
    assert m, f"last line of the router config is not the flow-control expression: {last!r}"
    tail = (yaml.safe_load(m.group(1)) + str(cold_start) + yaml.safe_load(m.group(2))) if min_replicas == 0 else ""
    return head + "\n" + tail


def _parse_config(rendered: str) -> dict:
    return yaml.safe_load(re.sub(r"\$\{string\(.*\)\}", "1", rendered))


class TestSchema:
    def test_min_replicas_defaults_to_one_and_allows_zero(self, rgd):
        assert rgd["spec"]["schema"]["spec"]["minReplicas"].replace(" ", "") == "integer|default=1minimum=0"

    @pytest.mark.parametrize("field,default", [
        ("targetInflight", 16), ("scaleToZeroAfterSeconds", 600), ("coldStartTimeoutSeconds", 600)])
    def test_new_fields_have_defaults(self, rgd, field, default):
        assert f"default={default}" in rgd["spec"]["schema"]["spec"][field].replace(" ", "")

    def test_cold_start_stays_under_the_router_proxy_timeout(self, rgd):
        # The chart's Envoy gives up on the EPP after message_timeout 1000s.
        m = re.search(r"maximum=(\d+)", rgd["spec"]["schema"]["spec"]["coldStartTimeoutSeconds"])
        assert m and int(m.group(1)) < 1000

    def test_deprecated_fields_are_kept(self, rgd):
        # kro refuses to drop a schema field in place.
        for field in ("replicas", "targetQueueDepth"):
            assert field in rgd["spec"]["schema"]["spec"]

    def test_idle_model_reads_as_ready(self, rgd):
        status = rgd["spec"]["schema"]["status"]
        assert "spec.?replicas.orValue(1) == 0" in status["ready"]
        assert "scaledToZero" in status
        ready_when = _resource(rgd, "vllmdeployment")["readyWhen"][0]
        assert "spec.?replicas.orValue(1) == 0" in ready_when


class TestRouterConfig:
    def test_unchanged_for_models_that_do_not_scale_to_zero(self, rgd):
        assert _render_config(_router_config(rgd), 1, 600) == BASELINE_ROUTER_CONFIG

    def test_scale_to_zero_turns_on_flow_control(self, rgd):
        cfg = _parse_config(_render_config(_router_config(rgd), 0, 420))
        assert cfg["featureGates"] == ["flowControl"]
        assert cfg["flowControl"]["noEndpointRequestTTL"] == "420s"
        assert cfg["flowControl"]["defaultRequestTTL"] == "0s"
        assert cfg["flowControl"]["maxBytes"] == "1Gi"
        # the scorers are still there
        assert [p["type"] for p in cfg["plugins"]] == [
            "queue-scorer", "kv-cache-utilization-scorer", "prefix-cache-scorer", "no-hit-lru-scorer"]

    def test_chart_renders_the_scale_to_zero_config(self, rgd, tmp_path):
        """The llm-d chart accepts the values. Pulls the chart, so it runs only
        with HELM_CHART_TESTS=1 (`make check` stays offline)."""
        import shutil
        import subprocess
        if os.environ.get("HELM_CHART_TESTS") != "1" or not shutil.which("helm"):
            pytest.skip("set HELM_CHART_TESTS=1 (needs helm and network)")
        app = _resource(rgd, "routerapp")["template"]["spec"]["source"]
        values = app["helm"]["valuesObject"]
        epp = values["router"]["epp"]
        epp["pluginsCustomConfig"] = {
            epp["pluginsConfigFile"]: re.sub(r"\$\{string\(.*\)\}", "1",
                                             _render_config(_router_config(rgd), 0, 600))}
        values["router"]["modelServers"]["matchLabels"] = {"llm-d.ai/pool": "demo"}
        vf = tmp_path / "values.yaml"
        vf.write_text(yaml.safe_dump(values))
        version = rgd["spec"]["schema"]["spec"]["routerChartVersion"].split("default=")[1].split()[0]
        r = subprocess.run(["helm", "template", "demo", f"oci://{app['repoURL']}/{app['chart']}",
                            "--version", version, "-f", str(vf)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and ("dial tcp" in r.stderr or "no such host" in r.stderr):
            pytest.skip("chart registry unreachable")
        assert r.returncode == 0, r.stderr
        cms = [d for d in yaml.safe_load_all(r.stdout) if d and d.get("kind") == "ConfigMap"
               and epp["pluginsConfigFile"] in (d.get("data") or {})]
        assert cms, "no ConfigMap carries the plugins config"
        assert "noEndpointRequestTTL: 600s" in cms[0]["data"][epp["pluginsConfigFile"]]


class TestScaledObject:
    @pytest.fixture(scope="class")
    def so(self, rgd):
        return _resource(rgd, "scaledobject")["template"]["spec"]

    def test_scale_to_zero_timing_comes_from_the_schema(self, so):
        assert so["minReplicaCount"] == "${schema.spec.minReplicas}"
        assert so["cooldownPeriod"] == "${schema.spec.scaleToZeroAfterSeconds}"
        assert so["initialCooldownPeriod"] == "${schema.spec.scaleToZeroAfterSeconds}"

    def test_scales_on_router_demand_not_vllm_queue(self, so):
        queries = [t["metadata"]["query"] for t in so["triggers"]]
        assert not any("vllm:num_requests_waiting" in q for q in queries)
        demand = next(t for t in so["triggers"] if "llm_d_epp_request_running" in t["metadata"]["query"])
        q = demand["metadata"]["query"]
        assert 'llm_d_epp_flow_control_queue_size{pool="${schema.metadata.name}"}' in q
        assert 'llm_d_epp_request_running{pool="${schema.metadata.name}"}' in q
        assert q.count("or vector(0)") == 2   # either metric may be absent
        assert demand["metadata"]["threshold"] == "${string(schema.spec.targetInflight)}"
        assert demand["metadata"]["activationThreshold"] == "0"

    def test_litellm_request_rate_still_wakes_the_pool(self, so):
        wake = next(t for t in so["triggers"] if "litellm_proxy_total_requests_metric_total" in t["metadata"]["query"])
        assert wake["metadata"]["activationThreshold"] == "0"


class TestPrometheus:
    @pytest.fixture(scope="class")
    def docs(self):
        with open(PROM, encoding="utf-8") as fh:
            return list(yaml.safe_load_all(fh))

    def test_router_job_sends_its_service_account_token(self, docs):
        cm = next(d for d in docs if d["kind"] == "ConfigMap")
        job = next(j for j in yaml.safe_load(cm["data"]["prometheus.yml"])["scrape_configs"]
                   if j["job_name"] == "llmd-epp")
        assert job["authorization"] == {"type": "Bearer",
                                        "credentials_file": "/var/run/secrets/kubernetes.io/serviceaccount/token"}
        pool = next(r for r in job["relabel_configs"] if r.get("target_label") == "pool")
        assert re.fullmatch(pool["regex"], "qwen2-0-5b-instruct-epp").group(1) == "qwen2-0-5b-instruct"

    def test_cluster_wide_grant_is_only_get_metrics(self, docs):
        role = next(d for d in docs if d["kind"] == "ClusterRole")
        assert role["rules"] == [{"nonResourceURLs": ["/metrics"], "verbs": ["get"]}]
        binding = next(d for d in docs if d["kind"] == "ClusterRoleBinding")
        assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "autoscaling-prometheus",
                                        "namespace": "inference"}]
        assert binding["roleRef"]["name"] == role["metadata"]["name"]


def _llmd(spec):
    return {"metadata": {"name": "m", "namespace": "inference"}, "spec": {"model": "org/m", **spec}}


class TestLiteLLMSync:
    def test_scale_to_zero_model_gets_a_cold_start_timeout(self, sync):  # noqa: F811
        p = sync._litellm_params_for("llmdendpoints", _llmd({"minReplicas": 0, "coldStartTimeoutSeconds": 300}))
        assert p["timeout"] == 300 + sync.COLD_START_ANSWER_SEC
        assert p["num_retries"] == 1

    @pytest.mark.parametrize("plural,spec", [
        ("llmdendpoints", {"minReplicas": 1}),
        ("llmdendpoints", {}),
        ("vllmendpoints", {"minReplicas": 0}),
        ("llmddisaggendpoints", {"minReplicas": 0}),
    ])
    def test_other_models_keep_the_global_timeout(self, sync, plural, spec):  # noqa: F811
        p = sync._litellm_params_for(plural, _llmd(spec))
        assert "timeout" not in p and "num_retries" not in p

    @pytest.mark.parametrize("current,desired,drift", [
        ({}, {}, False),
        ({"timeout": 720.0, "num_retries": 1}, {"timeout": 720, "num_retries": 1}, False),
        ({"timeout": "720", "num_retries": "1"}, {"timeout": 720, "num_retries": 1}, False),
        ({}, {"timeout": 720, "num_retries": 1}, True),      # turned to scale to zero
        ({"timeout": 720.0, "num_retries": 1}, {}, True),    # turned back
        ({"timeout": 720.0, "num_retries": 1}, {"timeout": 420, "num_retries": 1}, True),
    ])
    def test_timeout_changes_count_as_drift(self, sync, current, desired, drift):  # noqa: F811
        base = {"model": "openai/org/m", "api_base": "http://m-epp.inference.svc.cluster.local:80/v1"}
        assert sync._params_drifted({**base, **current}, {**base, **desired}) is drift


# --------------------------------------------------------------------------- #
# platformctl new-model --min-replicas
# --------------------------------------------------------------------------- #

class _Model:
    model_id = "Qwen/Qwen2-0.5B-Instruct"
    params = 0.5e9
    architecture = "Qwen2ForCausalLM"


class _Vram:
    weights_gb = 1.0


class _Best:
    total_gpus = 1
    tp_degree = 1
    pp_degree = 1
    per_gpu_need_gb = 4.0
    shared_eligible = False


def _args(**kw):
    import argparse
    base = dict(seq=2048, instance_type=None, worker_memory=None, extra_arg=None, tool_call_parser=None,
                workload=None, tier="llm-d", min_replicas=None, users=4)
    base.update(kw)
    return argparse.Namespace(**base)


def _llmd_spec(args):
    from recommend_instance import render
    kind = render.resolve_kind(render.pick_tier(args, _Best(), None), args, None)
    _name, _path, body, _msg = render.build_endpoint_yaml(kind, _Model(), _Vram(), _Best(), args, None)
    doc = yaml.safe_load(body)
    return doc["kind"], doc["spec"]


class TestNewModelMinReplicas:
    def test_default_keeps_one_replica(self):
        kind, spec = _llmd_spec(_args())
        assert kind == "LLMDEndpoint"
        assert spec["minReplicas"] == 1 and spec["targetInflight"] == 16
        assert "targetQueueDepth" not in spec and "scaleToZeroAfterSeconds" not in spec

    def test_zero_writes_the_scale_to_zero_settings(self):
        _kind, spec = _llmd_spec(_args(min_replicas=0))
        assert spec["minReplicas"] == 0
        assert spec["scaleToZeroAfterSeconds"] == 600 and spec["coldStartTimeoutSeconds"] == 600
        assert spec["maxReplicas"] >= 1

    def test_min_replicas_picks_the_llm_d_tier(self):
        kind, spec = _llmd_spec(_args(tier="auto", min_replicas=0))
        assert kind == "LLMDEndpoint" and spec["minReplicas"] == 0

    def test_max_is_never_below_min(self):
        _kind, spec = _llmd_spec(_args(min_replicas=5))
        assert spec["minReplicas"] == 5 and spec["maxReplicas"] >= 5

    @pytest.mark.parametrize("argv", [
        ["org/m", "--min-replicas", "-1"],
        ["org/m", "--min-replicas", "0", "--tier", "vllm"],
        ["org/m", "--min-replicas", "0", "--tier", "llm-d-disagg"],
    ])
    def test_cli_rejects_min_replicas_where_it_cannot_apply(self, argv):
        from recommend_instance import cli
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2
