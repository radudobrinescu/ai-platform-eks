"""Demo: llm-d prefix-cache routing vs round robin (ops/demos/).

Offline checks of what the demo creates in the cluster and of the reports it
hands Prism. The live run is documented in docs/demos/prefix-cache-routing.md.
"""

import importlib
import re
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ops/demos"))

bench = importlib.import_module("prefix_cache_bench")
report = importlib.import_module("prefix_cache_report")


def _cfg(**kw):
    base = dict(served_model="Qwen/Qwen3-8B", url=bench.target_url("support-assistant", "router"),
                out_dir="/results/r/router", rates=[1.0, 2.0], stage_seconds=60, groups=8, users_per_group=6,
                prefix_len=6000, question_len=150, output_len=150, seed=7)
    base.update(kw)
    return bench.bench_config(**base)


class TestWorkload:
    def test_is_the_shared_prefix_multi_turn_chat_profile(self):
        cfg = _cfg()
        sp = cfg["data"]["shared_prefix"]
        assert cfg["data"]["type"] == "shared_prefix" and sp["enable_multi_turn_chat"] is True
        assert (sp["num_groups"], sp["num_prompts_per_group"], sp["system_prompt_len"]) == (8, 6, 6000)
        assert cfg["load"]["stages"] == [{"rate": 1.0, "duration": 60}, {"rate": 2.0, "duration": 60}]
        assert cfg["load"]["base_seed"] == 7 and cfg["api"] == {"type": "completion", "streaming": True}
        assert cfg["server"]["model_name"] == cfg["tokenizer"]["pretrained_model_name_or_path"] == "Qwen/Qwen3-8B"

    def test_longest_conversation_fits_the_documented_context(self):
        d = bench.DEFAULTS
        requests = sum(float(r) for r in d["rates"].split(",")) * d["stage_seconds"]
        turns = requests / (d["groups"] * d["users_per_group"])
        longest = d["prefix_len"] + turns * (d["question_len"] + d["output_len"])
        assert longest < 16384 * 0.8, longest   # maxModelLen in the runbook, with headroom

    def test_targets_reach_the_same_pods_two_ways(self):
        assert bench.target_url("m", "router") == "http://m-epp.inference.svc.cluster.local:80"
        assert bench.target_url("m", "direct") == "http://m-direct.inference.svc.cluster.local:8000"
        svc = bench.direct_service("m")
        assert svc["metadata"]["namespace"] == "inference"
        assert svc["spec"]["selector"] == {"llm-d.ai/pool": "m"}
        assert svc["spec"].get("type", "ClusterIP") == "ClusterIP"
        assert svc["spec"]["ports"][0]["port"] == 8000


class TestExposure:
    def test_prism_is_private(self):
        deploy, svc, netpol = bench.prism_objects()
        assert svc["spec"]["type"] == "ClusterIP"
        assert netpol["spec"] == {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "prism"}},
                                  "policyTypes": ["Ingress"], "ingress": []}
        pod = deploy["spec"]["template"]["spec"]
        assert pod["automountServiceAccountToken"] is False and pod["securityContext"]["runAsNonRoot"] is True
        assert pod["containers"][0]["volumeMounts"][0]["readOnly"] is True

    def test_nothing_is_exposed_outside_the_cluster(self):
        objs = [*bench.base_objects(), *bench.prism_objects(), bench.direct_service("m"),
                bench.bench_job(name="j", config_map="j", env={"A": 1})]
        assert not [o for o in objs if o["kind"] in ("Ingress", "Gateway", "HTTPRoute")]
        assert not [o for o in objs if o["kind"] == "Service" and o["spec"].get("type", "ClusterIP") != "ClusterIP"]

    def test_job_runs_once_and_reports(self):
        job = bench.bench_job(name="j", config_map="j", env={"TARGET": "router"})
        spec = job["spec"]
        assert spec["backoffLimit"] == 0
        c = spec["template"]["spec"]["containers"][0]
        assert c["image"] == bench.INFERENCE_PERF_IMAGE
        assert "inference-perf --config_file /cfg/config.yml" in c["command"][2]
        assert "prefix_cache_report.py" in c["command"][2]
        assert {"name": "TARGET", "value": "router"} in c["env"]

    @pytest.mark.parametrize("bad", ["Model", "a;b", "x" * 60, "-m"])
    def test_rejects_bad_model_names(self, bad):
        with pytest.raises(SystemExit):
            bench.main(["run", "--model", bad])


def _partial(ttft_ms=None):
    lat = {"units": "ms", "p50": 200.0, "p99": 900.0} if ttft_ms else {"units": "s", "p50": 2.0, "p99": 9.0}
    return {"version": "0.2.1", "run": {"uid": "u", "eid": "e", "time": {"start": "2026-11-25T14:30:00+00:00",
                                                                          "end": "2026-11-25T14:31:00+00:00"}},
            "results": {"request_performance": {"aggregate": {
                "latency": {"time_to_first_token": lat},
                "throughput": {"output_token_rate": {"units": "tokens/s", "mean": 950.0}}}}}}


class TestReport:
    def _overlay(self, target, hit=0.9, doc=None):
        return report.overlay(doc or _partial(), stage=1, target=target, run_id="r1", model="Qwen/Qwen3-8B",
                              replicas=4, accelerator="L4", vllm_version="v0.33.0", router_version="v1.0.0",
                              rate_qps=2.0, input_len=6150, output_len=150, hit_rate=hit)

    def test_names_the_run_for_the_chart(self):
        d = self._overlay("router")
        assert d["run"]["eid"] == "r1-router" and d["run"]["description"] == "llm-d router (prefix-cache aware)"
        assert self._overlay("direct")["run"]["description"] == "Kubernetes Service (round robin)"

    def test_scenario_is_br02_shaped(self):
        d = self._overlay("router")
        router, engine = d["scenario"]["stack"]
        assert router["standardized"] == {"kind": "generic", "tool": "llm-d-router", "tool_version": "v1.0.0"}
        e = engine["standardized"]
        assert (e["kind"], e["role"], e["replicas"], e["model"]["name"]) == ("inference_engine", "replica", 4,
                                                                              "Qwen/Qwen3-8B")
        assert e["accelerator"] == {"model": "L4", "count": 4, "parallelism": {"tp": 1}}
        assert all(c["metadata"]["label"] and c["metadata"]["cfg_id"] and "native" in c for c in (router, engine))
        load = d["scenario"]["load"]["standardized"]
        assert (load["rate_qps"], load["stage"], load["input_seq_len"]["value"]) == (2.0, 1, 6150)

    def test_measured_results_are_kept(self):
        d = self._overlay("direct")
        lat = d["results"]["request_performance"]["aggregate"]["latency"]["time_to_first_token"]
        assert lat == {"units": "s", "p50": 2.0, "p99": 9.0}
        assert d["results"]["observability"]["vllm_prefix_cache_hit_rate"]["aggregated"]["mean"] == 0.9

    def test_no_hit_rate_no_observability(self):
        assert "observability" not in self._overlay("router", hit=None)["results"]

    def test_summary_in_seconds(self):
        assert report.stage_summary(self._overlay("router", doc=_partial(ttft_ms=True)), 2.0) == {
            "rate": 2.0, "ttft_p50": 0.2, "ttft_p99": 0.9, "out_tok_s": 950.0, "hit": 0.9}

    def test_hit_rate_query_is_per_pool_over_the_stage(self):
        q = report.hit_rate_query("support-assistant", 60)
        assert 'vllm:prefix_cache_hits_total{pool="support-assistant"}[60s]' in q
        assert 'vllm:prefix_cache_queries_total{pool="support-assistant"}[60s]' in q

    def test_stage_number_from_file_name(self):
        assert report.stage_of("/r/x/inference-perf.partial.stage_3.yaml") == 3


def test_table_puts_the_two_runs_side_by_side():
    row = {"rate": 2.0, "ttft_p50": 0.25, "ttft_p99": 1.5, "out_tok_s": 900.0, "hit": 0.88}
    slow = {**row, "ttft_p50": 3.1, "hit": 0.2}
    out = bench.table({"direct": [slow], "router": [row]})
    assert "3.10s" in out and "0.25s" in out and "20%" in out and "88%" in out


class TestRunsOnTheDocumentedPrerequisites:
    """The README asks for python3 with boto3; the driver must not need PyYAML."""

    def test_the_driver_does_not_import_the_report_module(self):
        with open(os.path.join(ROOT, "ops/demos/prefix_cache_bench.py"), encoding="utf-8") as fh:
            source = fh.read()
        assert "import yaml" not in source
        assert "from prefix_cache_report import" not in source and "import prefix_cache_report" not in source


class TestCleanup:
    def test_removes_every_direct_service_also_without_a_model_name(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bench, "kubectl", lambda *args, **kw: calls.append(args))
        monkeypatch.setattr(bench, "log", lambda *args, **kw: None)
        assert bench.cleanup(bench.argparse.Namespace(model=None)) == 0
        services = [c for c in calls if c[:2] == ("delete", "service")]
        assert services, calls
        assert "-l" in services[0] and "app.kubernetes.io/part-of=prefix-cache-demo" in services[0]
        assert bench.MODEL_NS in services[0]


class TestPrismSeesTheRuns:
    """Prism lists its directory recursively without handling errors, and the
    root of a new EBS volume holds lost+found, which only root can read. With
    PRISM_LOCAL_DIR at the volume root the listing failed and Prism showed no
    runs (seen on dev, 2026-11-27). Runs go to a subdirectory, and Prism lists
    only that (a subPath mount would not do: the kubelet creates a missing
    subPath as root, which Prism could not read either)."""

    def test_runs_are_written_below_the_runs_directory(self):
        import inspect
        src = inspect.getsource(bench.run)
        assert 'out_dir = f"{RUNS_DIR}/{run_id}/{target}"' in src
        assert bench.RUNS_DIR == "/results/runs"

    def test_prism_lists_only_the_runs_directory(self):
        dep = next(o for o in bench.prism_objects() if o["kind"] == "Deployment")
        c = dep["spec"]["template"]["spec"]["containers"][0]
        mount = next(m for m in c["volumeMounts"] if m["name"] == "results")
        assert mount["mountPath"] == "/results" and mount.get("readOnly") is True
        env = {e["name"]: e["value"] for e in c["env"]}
        assert env["PRISM_LOCAL_DIR"] == bench.RUNS_DIR


class TestOneSELinuxLevel:
    """Bottlerocket enforces SELinux and gives each pod its own MCS categories.
    Files the benchmark pods wrote carried theirs, so Prism, another pod, got
    "Permission denied" on /results although the file modes allowed reading
    (seen on dev, 2026-11-27). Every pod of the demo that touches the results
    volume uses the same level."""

    def _pod_specs(self):
        job = bench.bench_job(name="b", config_map="c", env={})
        dep = next(o for o in bench.prism_objects() if o["kind"] == "Deployment")
        return [job["spec"]["template"]["spec"], dep["spec"]["template"]["spec"]]

    def test_every_pod_on_the_results_volume_shares_one_level(self):
        levels = {spec["securityContext"]["seLinuxOptions"]["level"] for spec in self._pod_specs()}
        assert levels == {bench.SELINUX_LEVEL}
        assert re.fullmatch(r"s0:c\d+,c\d+", bench.SELINUX_LEVEL)

    def test_prism_still_runs_as_a_non_root_user(self):
        sc = self._pod_specs()[1]["securityContext"]
        assert sc["runAsNonRoot"] is True and sc["runAsUser"] == 1000
