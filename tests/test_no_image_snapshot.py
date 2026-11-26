"""GPU nodes pull the vLLM image; they do not boot from an image snapshot.

The image used to be baked into an EBS snapshot that every GPU node booted from.
A volume created from a snapshot loads its blocks on first read, and reading the
image that way was slower than pulling it: in the release test of 2026-11-26 a
model woke from zero in 376 s and 573 s with the snapshot, against 198 s without
it. The automation, the script and both settings were removed. This keeps them
from coming back in parts.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLUSTER_STAGE = ROOT / "terraform/30.eks/30.cluster"


def test_no_node_class_boots_from_a_snapshot() -> None:
    templates = sorted((CLUSTER_STAGE / "karpenter").glob("*.yaml"))
    assert templates
    for template in templates:
        assert not re.search(r"snapshot_?id", template.read_text(encoding="utf-8"), flags=re.I), template.name


def test_terraform_neither_builds_nor_looks_up_a_snapshot() -> None:
    for tf in sorted(CLUSTER_STAGE.glob("*.tf")):
        text = tf.read_text(encoding="utf-8")
        assert "aws_ebs_snapshot" not in text, tf.name
        assert "gpu_data_volume_snapshot" not in text and "gpu_image_snapshot" not in text, tf.name
    assert not (ROOT / "ops/image/create-data-volume-snapshot.sh").exists()


def test_the_example_tfvars_offer_no_snapshot_setting() -> None:
    for name in ("example.tfvars", "example-esc.tfvars"):
        text = (ROOT / "terraform/00.global/vars" / name).read_text(encoding="utf-8")
        assert "snapshot" not in text.lower(), name


def test_the_documents_do_not_promise_one() -> None:
    for rel in ("README.md", "docs/demos/scale-to-zero.md", "terraform/30.eks/30.cluster/README.md"):
        text = (ROOT / rel).read_text(encoding="utf-8").lower()
        assert "snapshot" not in text.replace("snapshotter", ""), rel


def test_the_default_gpu_volume_size_is_unchanged() -> None:
    """The data volume keeps its size: existing node classes must not change."""
    text = (CLUSTER_STAGE / "karpenter.tf").read_text(encoding="utf-8")
    assert re.search(r"gpu_node_volume_default_gib\s*=\s*200\b", text)
