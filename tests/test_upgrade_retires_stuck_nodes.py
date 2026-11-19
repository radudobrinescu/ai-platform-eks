"""An upgrade removes nodes that the new release no longer allows and that never
became ready.

Seen in the upgrade test from the public repository's state (2026-11-19): its
GPU pools allowed g7 instances, whose GPU the node image cannot drive. The node
joins, never reports a GPU, and the model waits. The new release excludes g7,
which marks the node as drifted, but Karpenter replaces a drifted node only
after it initialized, and it keeps counting the node's capacity for the waiting
pod. So the model stayed pending after the upgrade until the NodeClaim was
deleted by hand.
"""
import importlib.util
import json
import os
import re
import subprocess
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("stuck_nodes", os.path.join(ROOT, "ops", "lib", "stuck_nodes.py"))
stuck_nodes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stuck_nodes)

NOW = datetime(2026, 11, 19, 5, 0, tzinfo=timezone.utc)


def claim(name, created, drifted="True", initialized="Unknown", deleting=False, family="g7.2xlarge", pool="gpu-shared"):
    conds = [{"type": "Initialized", "status": initialized}]
    if drifted is not None:
        conds.append({"type": "Drifted", "status": drifted})
    meta = {"name": name, "creationTimestamp": created,
            "labels": {"karpenter.sh/nodepool": pool, "node.kubernetes.io/instance-type": family}}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-19T04:59:00Z"
    return {"metadata": meta, "status": {"conditions": conds}}


def test_only_drifted_claims_that_never_initialized_and_are_old_enough():
    items = [
        claim("stuck", "2026-11-19T04:31:54Z"),
        claim("booting", "2026-11-19T04:57:00Z"),                           # younger than the minimum age: may still come up
        claim("healthy-drifted", "2026-11-19T03:00:00Z", initialized="True"),  # Karpenter replaces it itself
        claim("not-drifted", "2026-11-19T03:00:00Z", drifted=None),         # still allowed: not ours to judge
        claim("going", "2026-11-19T03:00:00Z", deleting=True),
    ]
    found = stuck_nodes.stuck({"items": items}, NOW)
    assert found == [("stuck", "g7.2xlarge", "gpu-shared")]
    assert stuck_nodes.stuck({"items": []}, NOW, 600) == [] and stuck_nodes.stuck({}, NOW, 600) == []


def test_cli_reads_kubectl_json_and_prints_one_claim_per_line():
    doc = json.dumps({"items": [claim("stuck", "2020-01-01T00:00:00Z"), claim("ok", "2020-01-01T00:00:00Z", initialized="True")]})
    r = subprocess.run(["python3", os.path.join(ROOT, "ops", "lib", "stuck_nodes.py")], input=doc, capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == "stuck g7.2xlarge gpu-shared\n"
    r = subprocess.run(["python3", os.path.join(ROOT, "ops", "lib", "stuck_nodes.py")], input="not json", capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout == ""                              # unreadable is "none", never an error


def _function(name):
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", fh.read(), flags=re.S | re.M)
    assert m, name
    return m.group(0)


def test_the_wait_for_argo_cd_removes_them(tmp_path):
    calls = tmp_path / "calls"
    calls.write_text("")
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(f'''#!/bin/bash
echo "kubectl $*" >> "{calls}"
case "$*" in
  *"get nodeclaims.karpenter.sh -o json"*) printf '%s' "$CLAIMS" ;;
esac
''')
    kubectl.chmod(0o755)
    script = ('set -euo pipefail\nwarn(){ echo "WARN $*"; }\n' + f'LIB_DIR="{ROOT}/ops/lib"\n'
              + _function("_upgrade_retire_stuck_nodes") + "_upgrade_retire_stuck_nodes e2e\necho done\n")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
           "CLAIMS": json.dumps({"items": [claim("gpu-shared-abc", "2020-01-01T00:00:00Z")]})}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    assert "WARN" in r.stdout and "gpu-shared-abc (g7.2xlarge, pool gpu-shared)" in r.stdout
    assert "kubectl --context e2e delete nodeclaims.karpenter.sh gpu-shared-abc --wait=false" in calls.read_text()
    calls.write_text("")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**env, "CLAIMS": ""}, timeout=30)
    assert r.returncode == 0 and "WARN" not in r.stdout and "delete" not in calls.read_text()   # no Karpenter, or nothing stuck
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        wait = re.search(r"^_upgrade_wait_converged\(\) \{\n.*?^\}\n", fh.read(), flags=re.S | re.M).group(0)
    assert wait.index('_upgrade_retire_stuck_nodes "$env"') < wait.index("sleep 15")
