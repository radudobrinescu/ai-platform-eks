"""Helm installs a chart's crds/ only on the first install and never upgrades
them. Karpenter and the AWS Load Balancer Controller therefore apply their CRDs
themselves, rendered from the SAME pinned chart version, before the chart, and
never delete them (deleting a CRD deletes every object of that kind)."""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _block(text, header):
    i = text.index(header)
    depth, j = 0, text.index("{", i)
    for k in range(j, len(text)):
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            return text[i:k + 1]
    raise AssertionError(header)


CASES = [
    # file, version local, CRD data source, CRD resource, chart consumer header
    ("terraform/30.eks/30.cluster/karpenter.tf", "karpenter_chart_version",
     'data "helm_template" "karpenter_crds"', 'resource "kubectl_manifest" "karpenter_crds"',
     'resource "helm_release" "karpenter"'),
    ("terraform/30.eks/35.addons/main.tf", "lbc_chart_version",
     'data "helm_template" "lbc_crds"', 'resource "kubectl_manifest" "lbc_crds"',
     'module "eks_blueprints_addons"'),
    ("terraform/30.eks/30.cluster/capabilities-selfmanaged.tf", "kro_chart_version",
     'data "helm_template" "kro_crds"', 'resource "kubectl_manifest" "kro_crds"',
     'resource "helm_release" "kro_selfmanaged"'),
]


def test_one_pin_feeds_both_the_chart_and_its_crds():
    for rel, local, data_hdr, _res, consumer in CASES:
        text = _read(rel)
        assert re.search(rf'^\s*{local}\s*=\s*"\d+\.\d+\.\d+"', text, re.M), f"{rel}: {local} literal"
        data = _block(text, data_hdr)
        assert f"version      = local.{local}" in data and "include_crds = true" in data, rel
        assert f"local.{local}" in _block(text, consumer), f"{rel}: chart must use local.{local}"


def test_crds_are_applied_server_side_before_the_chart_and_never_deleted():
    for rel, _local, _data, res_hdr, consumer in CASES:
        text = _read(rel)
        res = _block(text, res_hdr)
        for want in ("server_side_apply = true", "force_conflicts   = true", "apply_only        = true"):
            assert want in res, f"{rel}: {res_hdr} needs {want}"
        name = res_hdr.split('"')[3]
        assert re.search(rf"depends_on\s*=\s*\[[^\]]*kubectl_manifest\.{name}", _block(text, consumer)), \
            f"{rel}: the chart must depend on kubectl_manifest.{name}"


def test_no_manual_crd_upgrade_notes_left():
    for rel, *_ in CASES:
        assert "kubectl apply -k" not in _read(rel) and "karpenter-crd chart" not in _read(rel), rel


def test_every_stage_points_kubectl_at_its_own_cluster():
    """A kubectl provider without an explicit block (or with load_config_file
    left on) uses the operator's kubeconfig current-context, i.e. possibly
    another cluster. Seen in 45.aws-oss-observability."""
    import glob
    for prov in glob.glob(os.path.join(ROOT, "terraform", "**", "providers.tf"), recursive=True):
        if "/.terraform/" in prov:
            continue
        text = open(prov, encoding="utf-8").read()
        if 'source  = "alekc/kubectl"' not in text and 'source = "alekc/kubectl"' not in text:
            continue
        block = _block(text, 'provider "kubectl"')
        assert "host " in block and re.search(r"load_config_file\s*=\s*false", block), prov
