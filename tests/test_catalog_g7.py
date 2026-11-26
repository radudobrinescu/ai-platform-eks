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


def test_the_gpu_nodepools_allow_every_catalog_family_except_the_excluded_ones():
    """The catalog and the NodePools agree: a family the NodePools refuse is not
    offered as in-cluster for g7/g7e, and g7/g7e are no longer refused."""
    with open(os.path.join(ROOT, "terraform/30.eks/30.cluster/karpenter/gpu-inference.yaml"), encoding="utf-8") as fh:
        notin = re.search(r'instance-family\n\s+operator: NotIn\n\s+values: \[([^\]]*)\]', fh.read()).group(1)
    assert '"g7"' not in notin and '"g7e"' not in notin
    for name in ("g7.2xlarge", "g7e.2xlarge"):
        pool, _ = recommend._classify_nodepool(BY_NAME[name], 1, 10.0, 0.10)
        assert pool == "gpu-inference"


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
