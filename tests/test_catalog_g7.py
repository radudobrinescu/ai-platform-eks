"""g7 / g7e in the `platformctl new-model` catalog.

The GPU NodePools allow them again (E2E runs pinned to g7.2xlarge and
g7e.2xlarge passed on 2026-11-26), so the recommender has to know them: to
size models for them, and to accept --instance-type g7.../g7e....
"""

import json
import os
import re
import time

import pytest
import yaml

from recommend_instance import catalog, pricing, recommend
from recommend_instance.model import ModelSpec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BY_NAME = {i.name: i for i in catalog.INSTANCES}

# (type, GPUs, GiB per GPU, vCPU, host GiB) from EC2 DescribeInstanceTypes, eu-central-1.
EC2 = [
    ("g7.2xlarge", 1, 32, 8, 32), ("g7.4xlarge", 1, 32, 16, 64), ("g7.8xlarge", 1, 32, 32, 128),
    ("g7.12xlarge", 2, 32, 48, 192), ("g7.24xlarge", 4, 32, 96, 384), ("g7.48xlarge", 8, 32, 192, 768),
    ("g7e.2xlarge", 1, 96, 8, 64), ("g7e.4xlarge", 1, 96, 16, 128), ("g7e.8xlarge", 1, 96, 32, 256),
    ("g7e.12xlarge", 2, 96, 48, 512), ("g7e.24xlarge", 4, 96, 96, 1024), ("g7e.48xlarge", 8, 96, 192, 2048),
]


@pytest.mark.parametrize("name,gpus,vram,vcpu,mem", EC2)
def test_sizes_match_ec2(name, gpus, vram, vcpu, mem):
    i = BY_NAME[name]
    assert (i.num_gpus, i.vram_gb, i.vcpu, i.mem_gb) == (gpus, vram, vcpu, mem)
    assert (i.compute_capability, i.arch_family, i.has_nvlink) == ("12.0", "Blackwell", False)
    assert i.price_usd_h > 0 and i.hbm_bandwidth_tb_s > 0


KARPENTER = os.path.join(ROOT, "terraform/30.eks/30.cluster/karpenter")
# Requirements that do not depend on the instance type.
NOT_ABOUT_THE_TYPE = {"kubernetes.io/arch", "kubernetes.io/os", "karpenter.sh/capacity-type"}


def _nodepool_requirements(pool):
    """{key: (operator, values)} of the NodePool in karpenter/<pool>.yaml. The
    file is a Terraform template: its ${...} placeholders are filled in first."""
    with open(os.path.join(KARPENTER, f"{pool}.yaml"), encoding="utf-8") as fh:
        docs = re.split(r"^---$", fh.read(), flags=re.M)
    doc = next(d for d in docs if re.search(r"^kind: NodePool$", d, flags=re.M))
    # The excluded-family lists as Terraform renders them on Kubernetes 1.37
    # (karpenter.tf; on 1.36 it adds g7, see tests/test_g7_needs_kubernetes_137.py).
    with open(os.path.join(KARPENTER, "..", "karpenter.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    for name in ("gpu_inference_excluded_families", "gpu_shared_excluded_families"):
        base = re.search(rf"{name}_base\s*=\s*(\[[^\]]*\])", tf).group(1)
        doc = doc.replace("${" + name + "}", base)
    reqs = yaml.safe_load(re.sub(r"\$\{\w+\}", "x", doc))["spec"]["template"]["spec"]["requirements"]
    by_key = {r["key"]: (r["operator"], [str(v) for v in r["values"]]) for r in reqs}
    assert len(by_key) == len(reqs), f"{pool}: a requirement key is repeated"
    return by_key


def _nodepool_admits(reqs, inst):
    """Would Karpenter launch `inst` from a NodePool with these requirements?"""
    labels = {
        "karpenter.k8s.aws/instance-category": inst.category,
        "karpenter.k8s.aws/instance-gpu-manufacturer": inst.gpu_manufacturer,
        "karpenter.k8s.aws/instance-family": inst.family,
        # Karpenter reads the generation off the type name: g6e -> 6, p4de -> 4, p6-b300 -> 6.
        "karpenter.k8s.aws/instance-generation": re.match(r"[a-z]+(\d+)", inst.name).group(1),
        "karpenter.k8s.aws/instance-gpu-count": str(inst.num_gpus),
    }
    for key, (operator, values) in reqs.items():
        if key in NOT_ABOUT_THE_TYPE:
            continue
        assert key in labels, f"new NodePool requirement {key}: the recommender does not know it"
        value = labels[key]
        if operator == "In":
            ok = value in values
        elif operator == "NotIn":
            ok = value not in values
        elif operator == "Gte":
            ok = int(value) >= int(values[0])
        else:
            raise AssertionError(f"new NodePool operator {operator} on {key}: the recommender does not know it")
        if not ok:
            return False
    return True


def test_the_gpu_nodepools_allow_every_catalog_family_except_the_excluded_ones():
    """The catalog and the NodePools agree, for every catalog type: what
    gpu-inference.yaml / gpu-shared.yaml refuse (family list, generation floor,
    GPU count) is not offered as in-cluster, and what they admit is."""
    inference, shared = _nodepool_requirements("gpu-inference"), _nodepool_requirements("gpu-shared")
    refused = set()
    for inst in catalog.INSTANCES:
        # 1 GiB per GPU: small enough that four copies fit, so only the NodePool rules decide.
        pool, shared_eligible = recommend._classify_nodepool(inst, 1, 1.0, 0.10)
        assert (pool == "gpu-inference") == _nodepool_admits(inference, inst), inst.name
        assert shared_eligible == _nodepool_admits(shared, inst), inst.name
        if pool == "out-of-cluster":
            refused.add(inst.family)
    # g4dn (T4) is refused by family name, g7/g7e are no longer refused.
    assert "g4dn" in refused and not refused & {"g5", "g6", "g6e", "g7", "g7e"}


@pytest.mark.parametrize("pool", ["gpu-inference", "gpu-shared"])
def test_the_recommender_rules_are_the_nodepool_rules(pool):
    """The constants the recommender uses are the values in the YAML, also for
    families the catalog does not list (p2, p3, p3dn, g4ad)."""
    reqs = _nodepool_requirements(pool)
    assert reqs["karpenter.k8s.aws/instance-family"][0] == "NotIn"
    assert set(reqs["karpenter.k8s.aws/instance-family"][1]) == catalog.CLUSTER_EXCLUDED_FAMILIES[pool]
    assert reqs["karpenter.k8s.aws/instance-generation"] == ("Gte", [str(catalog.CLUSTER_MIN_INSTANCE_GENERATION[pool])])
    assert reqs["karpenter.k8s.aws/instance-gpu-manufacturer"] == ("In", [catalog.CLUSTER_GPU_MANUFACTURER])
    categories = catalog.CLUSTER_SHARED_CATEGORIES if pool == "gpu-shared" else catalog.CLUSTER_INFERENCE_CATEGORIES
    assert set(reqs["karpenter.k8s.aws/instance-category"][1]) == set(categories)


@pytest.mark.parametrize("name,rule", [
    ("g4dn.xlarge", "instance-family NotIn"),          # T4: refused by family name
    ("p4d.24xlarge", "instance-generation Gte 5"),     # A100: generation 4, below the floor
    ("p4de.24xlarge", "instance-generation Gte 5"),
])
def test_a_refused_type_names_the_nodepool_rule(name, rule):
    assert rule in catalog.nodepool_refusal(BY_NAME[name], "gpu-inference")


def test_an_allowed_type_is_not_refused():
    assert catalog.nodepool_refusal(BY_NAME["g6.2xlarge"], "gpu-inference") is None
    assert catalog.nodepool_refusal(BY_NAME["g6.2xlarge"], "gpu-shared") is None
    assert "instance-gpu-count" in catalog.nodepool_refusal(BY_NAME["g6.12xlarge"], "gpu-shared")
    assert "instance-category" in catalog.nodepool_refusal(BY_NAME["p5.48xlarge"], "gpu-shared")


def _options(need_gb, params_b, **kw):
    args = dict(total_need_gb=need_gb, num_heads=40, tp_pin=None, require_in_cluster=False, safety_margin=0.10,
                prices={}, max_price=100.0, model=_model(params_b), weight_q="fp16", users=1)
    args.update(kw)
    return recommend.find_options(**args)


def test_a_type_the_nodepools_refuse_is_listed_as_out_of_cluster():
    opts = {o.instance.name: o for o in _options(8.0, 1.5)}
    for name in ("g4dn.xlarge", "g4dn.12xlarge", "p4d.24xlarge", "p4de.24xlarge"):
        assert opts[name].nodepool == "out-of-cluster" and not opts[name].shared_eligible, name
        assert any("NodePool" in n for n in opts[name].notes), name
    assert opts["g5.xlarge"].nodepool == "gpu-inference"


def test_in_cluster_only_drops_the_refused_types():
    names = {o.instance.name for o in _options(8.0, 1.5, require_in_cluster=True)}
    assert names and not {n for n in names if n.split(".")[0] in ("g4dn", "p4d", "p4de")}


def test_a_refused_type_is_never_recommended_ahead_of_an_in_cluster_one():
    # fp16 runs natively on a T4 and g4dn.xlarge is the cheapest fit, but
    # Karpenter cannot launch it: the cheapest in-cluster type is the pick.
    opts = _options(8.0, 1.5)
    assert opts[0].instance.name == "g6.xlarge"
    seen_out = False
    for o in opts:
        seen_out = seen_out or not o.in_cluster
        assert not (seen_out and o.in_cluster), f"{o.instance.name} is listed after an out-of-cluster type"
    # ... also when every in-cluster fit is over the price ceiling
    opts = _options(8.0, 1.5, max_price=0.60)
    assert opts[0].in_cluster and opts[0].over_price_ceiling


def test_a_refused_type_is_never_the_fleet_pick_ahead_of_an_in_cluster_one():
    from recommend_instance.scaling import ScalingRecommendation
    opts = {o.instance.name: o for o in _options(8.0, 1.5)}

    def fleet(name, **kw):
        return ScalingRecommendation(option=opts[name], max_concurrency_per_inst=10, effective_capacity=7,
                                     replicas=1, fleet_cost_usd_h=opts[name].price_usd_h,
                                     utilization_factor=0.7, target_users=7, **kw)

    # cheaper and meets the SLO, but cannot launch: the in-cluster fleet comes first
    ranked = sorted([fleet("g4dn.xlarge"), fleet("g6.xlarge", slo_unmet=True)], key=lambda r: r.sort_key)
    assert [r.option.instance.name for r in ranked] == ["g6.xlarge", "g4dn.xlarge"]


def test_no_quant_warning_on_blackwell():
    # cc 12.0 is compared numerically: as text "12.0" < "8.9" would warn about fp8.
    for q in ("bf16", "fp8", "int4", "mxfp4"):
        assert catalog._quant_compat_warning(BY_NAME["g7.2xlarge"], q, "fp8") is None


def _model(params_b):
    return ModelSpec(model_id="org/m", params=int(params_b * 1e9), num_layers=64, hidden_size=5120, num_heads=40,
                     num_kv_heads=8, head_dim=128, vocab_size=152064, max_position=32768)


def test_a_32b_model_fits_one_g7e_gpu_but_not_one_g7_gpu():
    opts = recommend.find_options(total_need_gb=70.0, num_heads=40, tp_pin=None, require_in_cluster=True,
                                  safety_margin=0.10, prices={}, max_price=100.0, model=_model(32), users=1)
    single = {o.instance.name for o in opts if o.total_gpus == 1}
    # 96 GB of GPU memory holds it; g7e.2xlarge's 64 GiB host RAM is below the
    # ~68 GiB the worker needs to stage 60 GiB of weights, so 4xlarge is the first.
    assert "g7e.4xlarge" in single and "g7e.2xlarge" not in single
    assert not {n for n in single if n.startswith("g7.")}


def test_a_small_model_can_use_the_smallest_g7():
    opts = recommend.find_options(total_need_gb=8.0, num_heads=40, tp_pin=None, require_in_cluster=True,
                                  safety_margin=0.10, prices={}, max_price=100.0, model=_model(1.5), users=1)
    assert "g7.2xlarge" in {o.instance.name for o in opts}


class TestPriceCache:
    """A cache written before g7/g7e were in the catalog must not hand them the
    static us-east-1 price in every region for 30 days."""

    def _write(self, tmp_path, monkeypatch, payload):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        path = pricing._pricing_cache_path("eu-central-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"timestamp": time.time(), "region": "eu-central-1", **payload}))

    def test_cache_without_the_new_types_is_refetched(self, tmp_path, monkeypatch):
        self._write(tmp_path, monkeypatch, {"prices": {"g6.xlarge": 0.9}})   # written by the old code
        assert pricing._load_cached_prices("eu-central-1", [i.name for i in catalog.INSTANCES]) is None

    def test_cache_that_looked_up_every_type_is_used(self, tmp_path, monkeypatch):
        names = [i.name for i in catalog.INSTANCES]
        # p6-b300 is not offered in every region: looked up, no price, still complete
        self._write(tmp_path, monkeypatch, {"prices": {"g7.2xlarge": 3.1}, "queried": names})
        assert pricing._load_cached_prices("eu-central-1", names) == {"g7.2xlarge": 3.1}

    def test_save_records_what_was_looked_up(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        pricing._save_cached_prices("eu-central-1", {"g7.2xlarge": 3.1}, ["g7.2xlarge", "p6-b300.48xlarge"])
        saved = json.loads(pricing._pricing_cache_path("eu-central-1").read_text())
        assert saved["queried"] == ["g7.2xlarge", "p6-b300.48xlarge"]
