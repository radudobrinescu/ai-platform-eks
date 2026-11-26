"""The ECR registry host is built for the partition the platform runs in.

A private registry is `<account>.dkr.ecr.<region>.<dns suffix>`, and the suffix
is not the same everywhere: `amazonaws.com` in the commercial and GovCloud
partitions, `amazonaws.com.cn` in China, `amazonaws.eu` in the AWS European
Sovereign Cloud. The pull-through cache path (Docker Hub credentials set) wrote
`amazonaws.com` into the image reference, so in another partition the model
pods and the image snapshot pointed at a registry host that does not exist.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLUSTER_STAGE = ROOT / "terraform/30.eks/30.cluster"
SNAPSHOT = ROOT / "ops/image/create-data-volume-snapshot.sh"
SOCI = ROOT / "ops/image/create-soci-index.sh"
ESC_IMAGE = "111122223333.dkr.ecr.eusc-de-east-1.amazonaws.eu/docker-hub/vllm/vllm-openai:v1"


def _code(path: Path) -> str:
    """The file without its comment lines."""
    return "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#"))


class TestTerraform:
    def test_no_stage_file_writes_the_commercial_suffix_into_a_registry_host(self) -> None:
        for tf in sorted(CLUSTER_STAGE.glob("*.tf")):
            assert not re.search(r"dkr\.ecr\.[^\"\n]*amazonaws\.com", _code(tf)), tf.name

    def test_the_registry_host_is_defined_once_from_the_partition(self) -> None:
        locals_tf = _code(CLUSTER_STAGE / "locals.tf")
        m = re.search(r"ecr_registry\s*=\s*\"(.+)\"", locals_tf)
        assert m, "local.ecr_registry is not defined"
        assert "data.aws_partition.current.dns_suffix" in m.group(1)
        assert ".dkr.ecr.${local.region}." in m.group(1)

    @pytest.mark.parametrize("name, key", [("capabilities.tf", "vllmImage"), ("image-optimization.tf", "vllm_ecr_image")])
    def test_both_image_references_use_it(self, name: str, key: str) -> None:
        line = next(ln for ln in _code(CLUSTER_STAGE / name).splitlines() if re.match(rf"\s*{key}\s*=", ln))
        assert "${local.ecr_registry}/docker-hub/" in line, line


class TestSnapshotScript:
    @pytest.mark.parametrize("region, suffix", [
        ("eu-central-1", "amazonaws.com"),
        ("us-gov-west-1", "amazonaws.com"),
        ("cn-north-1", "amazonaws.com.cn"),
        ("eusc-de-east-1", "amazonaws.eu"),
    ])
    def test_the_suffix_follows_the_region(self, region: str, suffix: str) -> None:
        text = SNAPSHOT.read_text(encoding="utf-8")
        m = re.search(r"^ecr_dns_suffix\(\) \{\n.*?^\}\n", text, flags=re.S | re.M)
        assert m, "ecr_dns_suffix is not defined"
        out = subprocess.run(["bash", "-c", m.group(0) + f'ecr_dns_suffix "{region}"\n'],
                             capture_output=True, text=True, timeout=30)
        assert out.stdout.strip() == suffix

    def test_a_short_reference_is_expanded_with_that_suffix(self) -> None:
        code = _code(SNAPSHOT)
        assert ".amazonaws.com/docker-hub/" not in code
        assert re.search(r'\$\{ACCOUNT\}\.dkr\.ecr\.\$\{REGION\}\.\$\(ecr_dns_suffix "\$REGION"\)/docker-hub/', code)


class TestSociScript:
    def _run(self, tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, str]:
        calls = tmp_path / "calls"
        aws = tmp_path / "aws"
        # describe-images fails: the image is not in ECR yet, which the script
        # treats as "nothing to index" and leaves with exit 0.
        aws.write_text(f'#!/bin/sh\necho "$*" >> "{calls}"\ncase "$*" in *"describe-images"*) exit 254 ;; esac\nexit 0\n')
        aws.chmod(0o755)
        env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
        env.pop("AWS_REGION", None)
        result = subprocess.run(["bash", str(SOCI), *args], capture_output=True, text=True, timeout=60,
                                env=env, stdin=subprocess.DEVNULL)
        return result, calls.read_text() if calls.exists() else ""

    def test_a_sovereign_cloud_registry_is_an_ecr_image(self, tmp_path: Path) -> None:
        result, calls = self._run(tmp_path, ESC_IMAGE)
        assert "is not an ECR image URI" not in result.stderr, result.stderr
        assert result.returncode == 0, result.stdout + result.stderr
        assert "eusc-de-east-1" in calls, "the region was not read from the registry host"

    def test_a_host_that_is_not_ecr_is_still_refused(self, tmp_path: Path) -> None:
        result, _calls = self._run(tmp_path, "-r", "eu-central-1", "docker.io/vllm/vllm-openai:v1")
        assert result.returncode == 1 and "is not an ECR image URI" in result.stderr

    def test_the_login_uses_the_host_of_the_image(self) -> None:
        code = _code(SOCI)
        assert "--password-stdin $IMAGE_HOST" in code
        assert ".amazonaws.com\\\"" not in code and "$REGION.amazonaws.com" not in code


class TestSovereignCloudExample:
    def test_the_image_snapshot_is_off_there(self) -> None:
        """The snapshot build has not been run in that partition. With it off,
        the example behaves as it did before the build became automatic."""
        text = (ROOT / "terraform/00.global/vars/example-esc.tfvars").read_text(encoding="utf-8")
        assert re.search(r"^gpu_image_snapshot\s*=\s*false\b", text, flags=re.M)
