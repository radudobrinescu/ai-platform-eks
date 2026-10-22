"""tfvars rendering against the real example.tfvars on main, both profiles."""

import os
import re

import pytest

import tfvars

ARN = "arn:aws:sso:::instance/ssoins-0000000000000000"
IDC = {"instance_arn": ARN, "region": "eu-west-1", "group_id": "g-1234"}
KW = dict(region="eu-central-1", repo_url="https://github.com/o/r.git", revision="e2e/e2e-fd-7",
          operator_cidr="203.0.113.10/32")


@pytest.fixture(scope="module")
def example(subject_root):
    with open(os.path.join(subject_root, "terraform/00.global/vars/example.tfvars"), encoding="utf-8") as fh:
        return fh.read()


def _effective(text):
    return tfvars.strip_comments(text)


def test_default_profile_changes_only_what_a_forker_must(example):
    out = tfvars.render(example, **KW)
    eff = _effective(out)
    assert re.search(r'(?m)^region = "eu-central-1"$', eff)
    assert re.search(r'(?m)^cluster_endpoint_public_access_cidrs = \["203.0.113.10/32"\]$', eff)
    assert re.search(r'(?m)^gitops_repo_url = "https://github.com/o/r.git"$', eff)
    assert re.search(r'(?m)^gitops_revision = "e2e/e2e-fd-7"$', eff)
    assert re.search(r"(?m)^\s*eks_capabilities = false", eff)
    assert "capabilities_config" not in eff
    changed = [ln for ln in out.splitlines() if ln not in example.splitlines()]
    assert len(changed) == 4, changed


def test_managed_profile_switches_capabilities_on(example):
    out = tfvars.render(example, profile="managed", idc=IDC, **KW)
    eff = _effective(out)
    assert re.search(r"(?m)^\s*eks_capabilities = true", eff)
    assert f'argocd_idc_instance_arn = "{ARN}"' in eff
    assert 'argocd_idc_region       = "eu-west-1"' in eff
    assert '{ id = "g-1234", type = "SSO_GROUP" }' in eff
    start, close = tfvars._block_span(out, "cluster_config")
    assert "capabilities_config = {" in out[start:close], "must sit inside cluster_config"


def test_managed_profile_requires_identity_center_values(example):
    with pytest.raises(tfvars.TfvarsError):
        tfvars.render(example, profile="managed", idc={"instance_arn": ARN}, **KW)


def test_rendered_braces_balance(example):
    for profile in tfvars.PROFILES:
        eff = _effective(tfvars.render(example, profile=profile, idc=IDC, **KW))
        assert eff.count("{") == eff.count("}") and eff.count("[") == eff.count("]")


def test_no_placeholder_survives(example):
    out = tfvars.render(example, **KW)
    assert not tfvars.PLACEHOLDER_RX.search(_effective(out))


def test_placeholder_left_is_an_error():
    with pytest.raises(tfvars.TfvarsError, match="placeholders"):
        tfvars.render('region = "x"\nsome_id = "REPLACE-ME"\n', **KW)


@pytest.mark.parametrize("cidr", ["0.0.0.0/0", "203.0.113.10", "not-an-ip", '1.2.3.4/32"'])
def test_operator_cidr_is_validated(example, cidr):
    with pytest.raises(tfvars.TfvarsError):
        tfvars.render(example, **{**KW, "operator_cidr": cidr})


def test_injection_is_refused(example):
    with pytest.raises(tfvars.TfvarsError):
        tfvars.render(example, **{**KW, "revision": 'x"\nevil = "1'})


def test_add_required_copies_top_level_blocks():
    new_example = 'a = 1\n\nnew_block = {\n  x = "y" # c {\n  z = [1, 2]\n}\n\nsimple = "v"\n'
    env = 'a = 1\nsimple = "kept"\n'
    out, added = tfvars.add_required(env, new_example, ["new_block", "simple"])
    assert added == ["new_block"]
    assert 'new_block = {\n  x = "y" # c {\n  z = [1, 2]\n}' in out
    assert 'simple = "kept"' in out


def test_add_required_fails_when_example_lacks_it():
    with pytest.raises(tfvars.TfvarsError):
        tfvars.add_required("a = 1\n", "b = 2\n", ["c"])


def test_kubernetes_bump_is_one_minor(example):
    out = tfvars.render(example, **KW)
    cur = tfvars.kubernetes_version(out)
    bumped, new = tfvars.bump_kubernetes_minor(out)
    major, minor = cur.split(".")
    assert new == f"{major}.{int(minor) + 1}" and tfvars.kubernetes_version(bumped) == new
