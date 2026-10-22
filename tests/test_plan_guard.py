"""Plan guard: delete/replace of long-lived or stateful resources must stop an upgrade.

`platformctl upgrade` runs the guard on every stage's saved plan before
anything is applied, so a false negative here can destroy a cluster or a data
bucket. Fixtures are small hand-written `terraform show -json` documents.
"""

import json
import os
import subprocess
import sys

import pytest

import plan_guard

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "plans")
SCRIPT = os.path.join(os.path.dirname(HERE), "ops", "lib", "plan_guard.py")

CLUSTER_STAGE = "30.eks/30.cluster"
MANIFEST = {
    "version": "v2026.11.0",
    "previous": "v2026.10.0",
    "kubernetes": {"supported": ["1.36", "1.37"], "recommended": "1.37"},
    "required_tfvars": [],
    "manual_steps": [],
    "acknowledged_replacements": [
        {"stage": "30.eks/30.cluster", "address": "aws_s3_bucket.model_cache[0]",
         "reason": "bucket name now includes the account id; regenerable cache"},
    ],
}


def plan(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def run_cli(*args):
    return subprocess.run([sys.executable, SCRIPT] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, universal_newlines=True)


class TestClassify:
    @pytest.mark.parametrize("actions, word", [
        (["create"], "create"),
        (["update"], "update"),
        (["delete"], "delete"),
        (["delete", "create"], "replace"),
        (["create", "delete"], "replace"),
        (["no-op"], "no-op"),
        ([], "no-op"),
        (["read"], "read"),
        (["forget"], "forget"),
    ])
    def test_actions(self, actions, word):
        assert plan_guard.classify(actions) == word


class TestEvaluate:
    def test_replace_of_eks_cluster_is_guarded(self):
        r = plan_guard.evaluate(plan("cluster-replace.json"), CLUSTER_STAGE, MANIFEST)
        assert r["blocked"] == 1
        (g,) = r["guarded"]
        assert g["address"] == "module.eks.aws_eks_cluster.this[0]"
        assert g["action"] == "replace" and not g["acknowledged"]
        assert "cluster and nodes" in g["reason"]

    def test_model_cache_replace_is_guarded_without_acknowledgement(self):
        r = plan_guard.evaluate(plan("model-cache-replace.json"), CLUSTER_STAGE, manifest=None)
        assert r["blocked"] == 1
        assert [g["address"] for g in r["guarded"]] == ["aws_s3_bucket.model_cache[0]"]

    def test_model_cache_replace_passes_when_manifest_acknowledges_it(self):
        r = plan_guard.evaluate(plan("model-cache-replace.json"), CLUSTER_STAGE, MANIFEST)
        assert r["blocked"] == 0
        (g,) = r["guarded"]
        assert g["acknowledged"]
        assert "v2026.11.0" in g["reason"] and "regenerable cache" in g["reason"]

    def test_acknowledgement_is_per_stage(self):
        # Same address in another stage is a different resource: still guarded.
        r = plan_guard.evaluate(plan("model-cache-replace.json"), "10.networking", MANIFEST)
        assert r["blocked"] == 1

    def test_stage_is_normalized(self):
        r = plan_guard.evaluate(plan("model-cache-replace.json"), "./30.eks/30.cluster/", MANIFEST)
        assert r["stage"] == CLUSTER_STAGE and r["blocked"] == 0

    def test_in_place_updates_pass(self):
        r = plan_guard.evaluate(plan("in-place-updates.json"), CLUSTER_STAGE)
        assert r["blocked"] == 0 and r["guarded"] == []
        # aws_iam_role is not a protected type: its delete is counted, not guarded.
        assert r["counts"] == {"create": 0, "update": 2, "delete": 1, "replace": 0}

    def test_delete_of_kubectl_manifest_is_guarded(self):
        r = plan_guard.evaluate(plan("bootstrap-delete.json"), CLUSTER_STAGE, MANIFEST)
        assert r["blocked"] == 1
        assert r["guarded"][0]["kind"] == "in-cluster"
        assert r["guarded"][0]["action"] == "delete"

    def test_allow_replace_any_stage(self):
        r = plan_guard.evaluate(plan("cluster-replace.json"), CLUSTER_STAGE,
                                allow_replace=["module.eks.aws_eks_cluster.this[0]"])
        assert r["blocked"] == 0
        assert r["guarded"][0]["reason"] == "allowed with --allow-replace"

    def test_allow_replace_scoped_to_stage(self):
        addr = "module.eks.aws_eks_cluster.this[0]"
        ok = plan_guard.evaluate(plan("cluster-replace.json"), CLUSTER_STAGE,
                                 allow_replace=["30.eks/30.cluster:" + addr])
        other = plan_guard.evaluate(plan("cluster-replace.json"), CLUSTER_STAGE,
                                    allow_replace=["10.networking:" + addr])
        assert ok["blocked"] == 0
        assert other["blocked"] == 1

    def test_summary_counts(self):
        r = plan_guard.evaluate(plan("model-cache-replace.json"), CLUSTER_STAGE, MANIFEST)
        assert r["counts"] == {"create": 1, "update": 1, "delete": 0, "replace": 2}
        assert plan_guard.format_summary(r).splitlines()[0] == (
            "stage 30.eks/30.cluster: 1 to create, 1 to update, 0 to delete, 2 to replace")

    def test_plan_without_changes(self):
        r = plan_guard.evaluate(plan("no-changes.json"), CLUSTER_STAGE)
        assert r["blocked"] == 0
        assert r["counts"] == {"create": 0, "update": 0, "delete": 0, "replace": 0}

    def test_every_listed_type_is_guarded(self):
        for rtype in plan_guard.GUARDED_TYPES:
            p = {"resource_changes": [{"address": rtype + ".x", "mode": "managed", "type": rtype,
                                       "change": {"actions": ["delete"]}}]}
            assert plan_guard.evaluate(p, CLUSTER_STAGE)["blocked"] == 1, rtype


class TestCli:
    def test_guarded_exits_3_and_prints_the_flag(self):
        p = run_cli(os.path.join(FIXTURES, "cluster-replace.json"), "--stage", "./30.eks/30.cluster")
        assert p.returncode == 3
        assert "GUARDED" in p.stdout
        assert "--allow-replace '30.eks/30.cluster:module.eks.aws_eks_cluster.this[0]'" in p.stdout

    def test_acknowledged_by_manifest_exits_0(self, tmp_path):
        m = tmp_path / "v2026.11.0.json"
        m.write_text(json.dumps(MANIFEST))
        p = run_cli(os.path.join(FIXTURES, "model-cache-replace.json"), "--stage", CLUSTER_STAGE,
                    "--manifest", str(m))
        assert p.returncode == 0, p.stdout + p.stderr
        assert "allowed" in p.stdout

    def test_allow_replace_flag_exits_0(self):
        p = run_cli(os.path.join(FIXTURES, "bootstrap-delete.json"), "--stage", CLUSTER_STAGE,
                    "--allow-replace", "kubectl_manifest.argocd_bootstrap[0]")
        assert p.returncode == 0, p.stdout + p.stderr

    def test_unreadable_plan_exits_2(self, tmp_path):
        bad = tmp_path / "plan.json"
        bad.write_text("not json")
        p = run_cli(str(bad), "--stage", CLUSTER_STAGE)
        assert p.returncode == 2
        assert "cannot read plan" in p.stderr
