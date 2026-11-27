"""g7 GPU nodes are used only on Kubernetes 1.37 and later.

g7 (NVIDIA RTX PRO 4500 Blackwell) needs NVIDIA driver 595. The Bottlerocket
NVIDIA AMIs ship that driver for Kubernetes 1.37 and later only (EKS user guide,
"Use EKS-optimized accelerated Linux AMIs for GPU instances"); on 1.36 a g7 node
joins the cluster but its GPU never comes up, and the model waits for it for
ever. Seen in the release test of 2026-11-26: both upgrade jobs ran the new
release on 1.36 first and got g7.12xlarge / g7.24xlarge nodes that never became
ready, while g7.2xlarge served on a fresh 1.37 cluster. g7e uses driver 580 and
runs on every version.
"""
from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "terraform/30.eks/30.cluster"
sys.path.insert(0, str(ROOT / "ops/lib"))
catalog = importlib.import_module("recommend_instance.catalog")
BY_NAME = {i.name: i for i in catalog.INSTANCES}


class TestNodePools:
    def _locals(self) -> str:
        return (STAGE / "karpenter.tf").read_text(encoding="utf-8")

    def test_g7_is_the_family_that_needs_driver_595(self) -> None:
        assert re.search(r'gpu_needs_driver_595\s*=\s*\["g7"\]', self._locals())

    def test_it_is_refused_below_kubernetes_137(self) -> None:
        text = self._locals()
        assert re.search(r"gpu_driver_595_ok\s*=\s*tonumber\(split\(\"\.\", local\.cluster_version\)\[1\]\)\s*>=\s*37", text)
        assert re.search(r"gpu_refused_for_driver\s*=\s*local\.gpu_driver_595_ok \? \[\] : local\.gpu_needs_driver_595", text)

    @pytest.mark.parametrize("pool, placeholder", [
        ("gpu-inference", "gpu_inference_excluded_families"),
        ("gpu-shared", "gpu_shared_excluded_families"),
    ])
    def test_both_gpu_pools_use_the_rendered_list(self, pool: str, placeholder: str) -> None:
        yaml_text = (STAGE / "karpenter" / f"{pool}.yaml").read_text(encoding="utf-8")
        assert f"values: ${{{placeholder}}}" in yaml_text
        tf = self._locals()
        assert re.search(rf"{placeholder}\s*=\s*jsonencode\(concat\(local\.{placeholder}_base, local\.gpu_refused_for_driver\)\)", tf)
        # Both renderings (the real one and the count-stabilising one) pass it.
        assert len(re.findall(rf"^\s*{placeholder}\s*=\s*local\.{placeholder}\s*$", tf, flags=re.M)) == 2

    @pytest.mark.parametrize("pool", ["gpu-inference", "gpu-shared"])
    def test_the_base_lists_are_the_recommenders(self, pool: str) -> None:
        name = pool.replace("-", "_") + "_excluded_families_base"
        m = re.search(rf'{name}\s*=\s*\[([^\]]*)\]', self._locals())
        assert m
        assert set(re.findall(r'"([^"]+)"', m.group(1))) == catalog.CLUSTER_EXCLUDED_FAMILIES[pool]


class TestRecommender:
    @pytest.fixture(autouse=True)
    def _no_version(self, monkeypatch) -> None:
        monkeypatch.delenv(catalog.KUBERNETES_VERSION_ENV, raising=False)

    @pytest.mark.parametrize("pool", ["gpu-inference", "gpu-shared"])
    def test_g7_is_refused_on_136(self, monkeypatch, pool: str) -> None:
        monkeypatch.setenv(catalog.KUBERNETES_VERSION_ENV, "1.36")
        refusal = catalog.nodepool_refusal(BY_NAME["g7.2xlarge"], pool)
        assert refusal and "1.37" in refusal and "driver 595" in refusal

    def test_g7_is_allowed_on_137_and_when_the_version_is_unknown(self, monkeypatch) -> None:
        assert catalog.nodepool_refusal(BY_NAME["g7.2xlarge"], "gpu-inference") is None
        monkeypatch.setenv(catalog.KUBERNETES_VERSION_ENV, "1.37")
        assert catalog.nodepool_refusal(BY_NAME["g7.2xlarge"], "gpu-inference") is None

    def test_g7e_is_allowed_on_136(self, monkeypatch) -> None:
        monkeypatch.setenv(catalog.KUBERNETES_VERSION_ENV, "1.36")
        assert catalog.nodepool_refusal(BY_NAME["g7e.2xlarge"], "gpu-inference") is None

    def test_an_unreadable_version_is_treated_as_unknown(self, monkeypatch) -> None:
        monkeypatch.setenv(catalog.KUBERNETES_VERSION_ENV, "latest")
        assert catalog.nodepool_refusal(BY_NAME["g7.2xlarge"], "gpu-inference") is None


def _function(name: str) -> str:
    text = (ROOT / "platformctl").read_text(encoding="utf-8")
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
    assert m, name
    return m.group(0)


def test_new_model_is_told_the_clusters_kubernetes_version(tmp_path: Path) -> None:
    vars_dir = tmp_path / "00.global" / "vars"
    vars_dir.mkdir(parents=True)
    (vars_dir / "dev.tfvars").write_text('cluster_config = {\n  kubernetes_version = "1.36"\n}\n')
    fake = tmp_path / "recommend-instance.py"
    fake.write_text('import os\nprint("K8S=" + os.environ.get("PLATFORM_KUBERNETES_VERSION", ""))\n')
    text = (ROOT / "platformctl").read_text(encoding="utf-8")
    helpers = _function("resolve_env") + "\n".join(line for line in text.splitlines() if line.startswith("tfvar_kubernetes_version()")) + "\n"
    script = (f'set -euo pipefail\nTF_DIR="{tmp_path}"\nLIB_DIR="{tmp_path}"\nCLUSTER_STAGE=x\n'
              f'ACTIVE_ENV_FILE="{tmp_path}/none"\nneed(){{ :; }}\ndie(){{ echo "DIE $*"; exit 1; }}\n'
              + helpers + _function("cmd_new_model") + "cmd_new_model org/m\n")
    env = {**os.environ, "PLATFORM_ENV": "dev"}
    env.pop("PLATFORM_KUBERNETES_VERSION", None)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "K8S=1.36" in r.stdout
    assert "tfvar_kubernetes_version()" in text
