#!/usr/bin/env python3
"""Demo: llm-d prefix-cache-aware routing against plain round robin.

The same vLLM replicas of one LLMDEndpoint are benchmarked twice with the same
workload, many support chats that share long system prompts:

  direct   through a Kubernetes Service in front of the vLLM pods (kube-proxy
           spreads requests at random; no replica knows what another cached)
  router   through the llm-d router, which sends a request to the replica that
           already holds its prompt prefix in KV cache

The load is inference-perf (kubernetes-sigs, the harness llm-d-benchmark
uses) with the shared-prefix multi-turn chat workload from llm-d-benchmark's
profiles. Results are Benchmark Report v0.2 files on a volume that Prism
(llm-d's benchmark viewer) reads.

  ./ops/demos/prefix_cache_bench.py run --model <LLMDEndpoint name>
  ./ops/demos/prefix_cache_bench.py prism      # (re)start Prism on the results
  ./ops/demos/prefix_cache_bench.py cleanup    # remove everything it created

Prism has no login: it gets a ClusterIP Service only and a NetworkPolicy that
admits no other pod; reach it with `kubectl port-forward` (binds 127.0.0.1).
Needs kubectl pointed at the cluster. See docs/demos/prefix-cache-routing.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

NS = "demo-bench"
MODEL_NS = "inference"
PVC = "prefix-cache-results"
PROM_URL = "http://autoscaling-prometheus.inference.svc.cluster.local:9090"
INFERENCE_PERF_IMAGE = "quay.io/inference-perf/inference-perf:v0.7.0"
PRISM_IMAGE = "ghcr.io/llm-d/llm-d-prism:v1.0.0"
LABELS = {"app.kubernetes.io/part-of": "prefix-cache-demo"}
# Runs live below the volume root: the root of a new EBS volume holds lost+found,
# which only root can read, and Prism's directory listing fails on it.
RUNS_DIR = "/results/runs"
# Bottlerocket enforces SELinux and gives every pod its own MCS categories, so a
# file one pod wrote is unreadable to another. The pods on the results volume
# (benchmark Jobs and Prism) share this one level.
SELINUX_LEVEL = "s0:c317,c829"
HERE = os.path.dirname(os.path.abspath(__file__))
NAME_RX = re.compile(r"^[a-z0-9]([-a-z0-9]{0,50}[a-z0-9])?$")

# Workload defaults: 8 products, each with a ~6k-token manual as system prompt,
# 6 customers per product in multi-turn chats (48 conversations, each turn adds
# 150 + 150 tokens of history). On an L4 with an 8B model a replica's KV cache
# holds roughly 25k tokens, a few manuals: round robin sends every manual to
# every replica and keeps evicting them; the router keeps each manual (and each
# conversation) on the replica that already has it. 4 stages x 60 s keeps the
# longest conversation near 10k tokens, inside maxModelLen 16384.
DEFAULTS = {"groups": 8, "users_per_group": 6, "prefix_len": 6000, "question_len": 150, "output_len": 150,
            "rates": "1,2,3,4", "stage_seconds": 60, "seed": 20261125}


# --------------------------------------------------------------------------- #
# kubectl
# --------------------------------------------------------------------------- #

def kubectl(*args: str, input_: str | None = None, check: bool = True, timeout: int = 120) -> str:
    r = subprocess.run(["kubectl", *args], input=input_, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def kjson(*args: str) -> dict:
    out = kubectl(*args, "-o", "json")
    return json.loads(out) if out.strip() else {}


def apply(*objs: dict) -> None:
    kubectl("apply", "-f", "-", input_=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objs)}))


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Manifests (pure functions, unit-tested)
# --------------------------------------------------------------------------- #

def _meta(name: str, ns: str | None = NS, **labels: str) -> dict:
    m = {"name": name, "labels": {**LABELS, **labels}}
    if ns:
        m["namespace"] = ns
    return m


def direct_service(model: str) -> dict:
    """The baseline: a plain Service on the model's vLLM pods (port 8000)."""
    return {"apiVersion": "v1", "kind": "Service", "metadata": _meta(f"{model}-direct", MODEL_NS),
            "spec": {"selector": {"llm-d.ai/pool": model},
                     "ports": [{"name": "http", "port": 8000, "targetPort": 8000}]}}


def target_url(model: str, target: str) -> str:
    if target == "router":
        return f"http://{model}-epp.{MODEL_NS}.svc.cluster.local:80"
    return f"http://{model}-direct.{MODEL_NS}.svc.cluster.local:8000"


def bench_config(*, served_model: str, url: str, out_dir: str, rates: list[float], stage_seconds: int,
                 groups: int, users_per_group: int, prefix_len: int, question_len: int, output_len: int,
                 seed: int) -> dict:
    """inference-perf config: llm-d-benchmark's shared_prefix_multi_turn_chat
    profile, sized for the demo."""
    return {
        "load": {"type": "constant", "num_workers": 4, "worker_max_concurrency": 32, "base_seed": seed,
                 "stages": [{"rate": r, "duration": stage_seconds} for r in rates]},
        "api": {"type": "completion", "streaming": True},
        "server": {"type": "vllm", "model_name": served_model, "base_url": url, "ignore_eos": True},
        "tokenizer": {"pretrained_model_name_or_path": served_model},
        "data": {"type": "shared_prefix",
                 "shared_prefix": {"num_groups": groups, "num_prompts_per_group": users_per_group,
                                   "system_prompt_len": prefix_len, "question_len": question_len,
                                   "output_len": output_len, "enable_multi_turn_chat": True}},
        "report": {"request_lifecycle": {"summary": True, "per_stage": True, "per_request": False}},
        "storage": {"local_storage": {"path": out_dir}},
    }


def bench_job(*, name: str, config_map: str, env: dict) -> dict:
    script = ("inference-perf --config_file /cfg/config.yml && "
              "(command -v python3 >/dev/null && P=python3 || P=python; $P /cfg/prefix_cache_report.py)")
    return {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": _meta(name),
        "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 86400,
            "template": {
                "metadata": {"labels": {**LABELS, "app.kubernetes.io/name": "prefix-cache-bench"}},
                "spec": {
                    "restartPolicy": "Never",
                    "securityContext": {"seLinuxOptions": {"level": SELINUX_LEVEL}},
                    "containers": [{
                        "name": "bench", "image": INFERENCE_PERF_IMAGE, "command": ["sh", "-c", script],
                        "env": [{"name": k, "value": str(v)} for k, v in env.items()]
                               + [{"name": "HF_HOME", "value": "/tmp/hf"}],
                        "resources": {"requests": {"cpu": "4", "memory": "8Gi"}, "limits": {"memory": "16Gi"}},
                        "securityContext": {"allowPrivilegeEscalation": False},
                        "volumeMounts": [{"name": "cfg", "mountPath": "/cfg"},
                                         {"name": "results", "mountPath": "/results"}],
                    }],
                    "volumes": [{"name": "cfg", "configMap": {"name": config_map}},
                                {"name": "results", "persistentVolumeClaim": {"claimName": PVC}}],
                },
            },
        },
    }


def prism_objects(replicas: int = 1) -> list[dict]:
    sel = {"app.kubernetes.io/name": "prism"}
    deploy = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": _meta("prism", **sel),
        "spec": {
            "replicas": replicas, "selector": {"matchLabels": sel},
            # one pod on the ReadWriteOnce results volume at a time
            "strategy": {"type": "Recreate"},
            "template": {
                "metadata": {"labels": {**LABELS, **sel}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                        "seLinuxOptions": {"level": SELINUX_LEVEL}},
                    "containers": [{
                        "name": "prism", "image": PRISM_IMAGE,
                        "env": [{"name": "PORT", "value": "8080"}, {"name": "PRISM_LOCAL_DIR", "value": RUNS_DIR}],
                        "ports": [{"name": "http", "containerPort": 8080}],
                        "readinessProbe": {"httpGet": {"path": "/", "port": "http"}, "periodSeconds": 10},
                        "resources": {"requests": {"cpu": "200m", "memory": "512Mi"}, "limits": {"memory": "1Gi"}},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [{"name": "results", "mountPath": "/results", "readOnly": True}],
                    }],
                    "volumes": [{"name": "results", "persistentVolumeClaim": {"claimName": PVC, "readOnly": True}}],
                },
            },
        },
    }
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": _meta("prism"),
           "spec": {"type": "ClusterIP", "selector": sel, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}}
    # No login in Prism: no other pod may reach it. kubectl port-forward still
    # works (it enters the pod's own network namespace).
    netpol = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": _meta("prism-deny-ingress"),
              "spec": {"podSelector": {"matchLabels": sel}, "policyTypes": ["Ingress"], "ingress": []}}
    return [deploy, svc, netpol]


def base_objects() -> list[dict]:
    ns = {"apiVersion": "v1", "kind": "Namespace", "metadata": _meta(NS, None)}
    pvc = {"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": _meta(PVC),
           "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "5Gi"}}}}
    return [ns, pvc]


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

def table(rows: dict[str, list[dict]]) -> str:
    def f(v, kind):
        if v is None:
            return "-"
        return {"s": f"{v:.2f}s", "n": f"{v:.0f}", "pct": f"{v:.0%}", "q": f"{v:g}"}[kind]
    lines = [f"{'QPS':>4} | {'TTFT p50':>17} | {'TTFT p99':>17} | {'output tok/s':>17} | {'cache hit':>17}",
             f"{'':>4} | {'direct':>8} {'router':>8} | {'direct':>8} {'router':>8} | {'direct':>8} {'router':>8}"
             f" | {'direct':>8} {'router':>8}"]
    for d, r in zip(rows.get("direct", []), rows.get("router", [])):
        lines.append(f"{f(d['rate'], 'q'):>4} | {f(d['ttft_p50'], 's'):>8} {f(r['ttft_p50'], 's'):>8} | "
                     f"{f(d['ttft_p99'], 's'):>8} {f(r['ttft_p99'], 's'):>8} | "
                     f"{f(d['out_tok_s'], 'n'):>8} {f(r['out_tok_s'], 'n'):>8} | "
                     f"{f(d['hit'], 'pct'):>8} {f(r['hit'], 'pct'):>8}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def endpoint_facts(model: str) -> dict:
    ep = kjson("get", "llmdendpoint", "-n", MODEL_NS, model)
    spec, st = ep.get("spec") or {}, ep.get("status") or {}
    if st.get("ready") != "True":
        raise SystemExit(f"LLMDEndpoint {model} is not Ready (status {st})")
    pods = [p for p in kjson("get", "pods", "-n", MODEL_NS, "-l", f"llm-d.ai/pool={model}").get("items", [])
            if any(c.get("type") == "Ready" and c.get("status") == "True"
                   for c in (p.get("status") or {}).get("conditions") or [])]
    if not pods:
        raise SystemExit(f"no Ready vLLM pod for {model}")
    image = pods[0]["spec"]["containers"][0]["image"]
    node = kjson("get", "node", pods[0]["spec"]["nodeName"])
    labels = (node.get("metadata") or {}).get("labels") or {}
    gpu = labels.get("karpenter.k8s.aws/instance-gpu-name", "").upper()
    return {"served_model": spec.get("model", ""), "replicas": len(pods),
            "router_version": spec.get("routerChartVersion", ""),
            "vllm_version": image.rsplit(":", 1)[-1] if ":" in image else "",
            "accelerator": gpu, "instance_type": labels.get("node.kubernetes.io/instance-type", "")}


def wait_job(name: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = kjson("get", "job", "-n", NS, name).get("status") or {}
        if st.get("succeeded"):
            return True
        if st.get("failed"):
            return False
        time.sleep(15)
    raise TimeoutError(f"job {name} did not finish in {timeout}s")


def run(a: argparse.Namespace) -> int:
    if not NAME_RX.match(a.model):
        raise SystemExit(f"not a model name: {a.model!r}")
    rates = [float(r) for r in a.rates.split(",")]
    facts = endpoint_facts(a.model)
    log(f"{a.model}: {facts['replicas']} Ready replicas of {facts['served_model']} on "
        f"{facts['accelerator'] or '?'} ({facts['instance_type']}), vLLM {facts['vllm_version']}")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    apply(*base_objects(), direct_service(a.model))
    kubectl("scale", "deployment/prism", "-n", NS, "--replicas=0", check=False)   # frees the volume
    with open(os.path.join(HERE, "prefix_cache_report.py"), encoding="utf-8") as fh:
        report_py = fh.read()
    rows: dict[str, list[dict]] = {}
    for i, target in enumerate(("direct", "router")):
        if i and a.restart:
            log("restarting the vLLM pods so the router run starts with empty caches")
            kubectl("delete", "pods", "-n", MODEL_NS, "-l", f"llm-d.ai/pool={a.model}", "--wait=false")
            kubectl("wait", "pods", "-n", MODEL_NS, "-l", f"llm-d.ai/pool={a.model}", "--for=condition=Ready",
                    "--timeout=1500s", timeout=1560)
        out_dir = f"{RUNS_DIR}/{run_id}/{target}"
        cfg = bench_config(served_model=facts["served_model"], url=target_url(a.model, target), out_dir=out_dir,
                           rates=rates, stage_seconds=a.stage_seconds, groups=a.groups,
                           users_per_group=a.users_per_group, prefix_len=a.prefix_len, question_len=a.question_len,
                           output_len=a.output_len, seed=a.seed + i)   # same shape, fresh prompts per run
        name = f"bench-{target}-{run_id}"
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": _meta(name),
              "data": {"config.yml": json.dumps(cfg, indent=2), "prefix_cache_report.py": report_py}}
        env = {"OUT_DIR": out_dir, "TARGET": target, "RUN_ID": run_id, "MODEL": facts["served_model"],
               "POOL": a.model, "REPLICAS": facts["replicas"], "ACCELERATOR": facts["accelerator"],
               "VLLM_VERSION": facts["vllm_version"], "ROUTER_VERSION": facts["router_version"], "RATES": ",".join(f"{r:g}" for r in rates),
               "INPUT_LEN": a.prefix_len + a.question_len, "OUTPUT_LEN": a.output_len, "PROM_URL": PROM_URL}
        apply(cm, bench_job(name=name, config_map=name, env=env))
        log(f"{target}: inference-perf running against {target_url(a.model, target)} "
            f"({len(rates)} stages x {a.stage_seconds}s at {a.rates} QPS)")
        ok = wait_job(name, len(rates) * a.stage_seconds + 1800)
        logs = kubectl("logs", "-n", NS, f"job/{name}", "--tail=-1", check=False, timeout=60)
        if not ok:
            print(logs[-4000:])
            raise SystemExit(f"{target} run failed (job {name})")
        rows[target] = [json.loads(line.split("RESULT ", 1)[1]) for line in logs.splitlines() if "RESULT " in line]
    print("\n" + table(rows) + "\n")
    start_prism()
    return 0


def start_prism() -> None:
    apply(*base_objects(), *prism_objects())
    kubectl("rollout", "status", "deployment/prism", "-n", NS, "--timeout=300s", timeout=330)
    log("Prism is up. Open it with:\n\n"
        f"  kubectl port-forward -n {NS} svc/prism 8080:8080\n  open http://localhost:8080\n\n"
        "and pick the two runs (llm-d router, Kubernetes Service) in the dashboard.")


def cleanup(a: argparse.Namespace) -> int:
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false")
    # Every <model>-direct Service this script created, whichever model it was for.
    selector = ",".join(f"{k}={v}" for k, v in LABELS.items())
    kubectl("delete", "service", "-n", MODEL_NS, "-l", selector, "--ignore-not-found")
    log(f"removed namespace {NS} (results volume included) and the direct Service(s) in {MODEL_NS}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="benchmark direct vs router, then start Prism")
    r.add_argument("--model", required=True, help="LLMDEndpoint name (namespace inference)")
    r.add_argument("--groups", type=int, default=DEFAULTS["groups"], help="distinct system prompts (products)")
    r.add_argument("--users-per-group", type=int, default=DEFAULTS["users_per_group"],
                   help="conversations per system prompt")
    r.add_argument("--prefix-len", type=int, default=DEFAULTS["prefix_len"], help="system prompt tokens")
    r.add_argument("--question-len", type=int, default=DEFAULTS["question_len"], help="question tokens per turn")
    r.add_argument("--output-len", type=int, default=DEFAULTS["output_len"], help="answer tokens per turn")
    r.add_argument("--rates", default=DEFAULTS["rates"], help="requests per second, one stage each")
    r.add_argument("--stage-seconds", type=int, default=DEFAULTS["stage_seconds"])
    r.add_argument("--seed", type=int, default=DEFAULTS["seed"])
    r.add_argument("--restart", action="store_true",
                   help="restart the vLLM pods between the runs (empty caches; reloads the model)")
    sub.add_parser("prism", help="(re)start Prism on the results volume")
    c = sub.add_parser("cleanup", help="delete the demo namespace and the direct Service")
    c.add_argument("--model", default="", help="LLMDEndpoint name whose <name>-direct Service to delete")
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return run(a)
    if a.cmd == "prism":
        start_prism()
        return 0
    return cleanup(a)


if __name__ == "__main__":
    sys.exit(main())
