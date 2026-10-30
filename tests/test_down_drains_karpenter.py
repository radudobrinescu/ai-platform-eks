"""`down` deletes Karpenter's NodePools and waits for its NodeClaims to drain
while the controller still runs, before Terraform removes the controller."""

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fake kubectl. NODECLAIMS_AFTER: number of `get nodeclaims` calls that still
# return a claim; HAS_KARPENTER=0 hides the controller Deployment.
FAKE = r"""#!/bin/bash
echo "$*" >> "$CALLS"
case "$*" in
  *"get deployment karpenter"*) [ "${HAS_KARPENTER:-1}" = 1 ] ;;
  *"get nodepools"*) echo nodepool.karpenter.sh/default; echo nodepool.karpenter.sh/gpu-shared ;;
  *"delete nodepools"*) : ;;
  *"get nodeclaims"*)
    n=$(grep -c "get nodeclaims" "$CALLS")
    if [ "$n" -le "${NODECLAIMS_AFTER:-0}" ]; then echo nodeclaim.karpenter.sh/default-abc; fi ;;
esac
"""


def _function(name):
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
    assert m, name
    return m.group(0)


def _run(tmp_path, **env):
    k = tmp_path / "kubectl"
    k.write_text(FAKE)
    k.chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    script = ("set -euo pipefail\nlog(){ echo \"LOG $*\"; }\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
              + _function("_drain_karpenter_nodes") + "_drain_karpenter_nodes e2e\necho done\n")
    e = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls),
         "PLATFORMCTL_KARPENTER_DRAIN_INTERVAL": "0", **env}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=30)
    return r, calls.read_text()


def test_deletes_nodepools_and_waits_for_nodeclaims(tmp_path):
    r, calls = _run(tmp_path, NODECLAIMS_AFTER="3")
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    assert "delete nodepools.karpenter.sh --all --wait=false" in calls
    assert calls.count("get nodeclaims") == 4
    assert "OK Karpenter terminated all of its nodes" in r.stdout


def test_times_out_and_continues(tmp_path):
    r, _ = _run(tmp_path, NODECLAIMS_AFTER="999", PLATFORMCTL_KARPENTER_DRAIN_TIMEOUT="0")
    assert r.returncode == 0 and "done" in r.stdout
    assert "WARN Karpenter still has 1 NodeClaim(s)" in r.stdout and "fallback" in r.stdout


def test_skips_without_self_managed_karpenter(tmp_path):
    r, calls = _run(tmp_path, HAS_KARPENTER="0")
    assert r.returncode == 0 and "done" in r.stdout
    assert "delete nodepools" not in calls


def test_runs_inside_the_predown_drain_before_terraform():
    assert '_drain_karpenter_nodes "$env"' in _function("_predown_drain")
