"""`down` clears KEDA's finalizer on ScaledObjects/ScaledJobs, like the other
controller finalizers that hang namespace deletes.

Seen in E2E: an llm-d model's ScaledObject was deleted during teardown, after
the Karpenter drain had removed the node running the KEDA operator. Nothing
could remove finalizer.keda.sh, the inference namespace stayed Terminating,
Terraform's namespace destroy timed out, and the cluster was left ACTIVE.
"""

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_KUBECTL = r"""#!/bin/sh
echo "$*" >> "$CALLS"
case "$*" in
  *"get scaledobjects.keda.sh"*) printf 'inference/qwen-llmd\n' ;;
  *"get scaledjobs.keda.sh"*) printf 'inference/batch-job\n' ;;
  *"get "*) : ;;
esac
"""


def _function(name):
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
    assert m, name
    return m.group(0)


def test_keda_finalizers_are_cleared_in_every_namespace(tmp_path):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    script = "set -euo pipefail\n" + _function("_strip_teardown_finalizers") + "_strip_teardown_finalizers e2e\necho done\n"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    log = calls.read_text()
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    assert "--context e2e get scaledobjects.keda.sh -A" in log
    assert 'patch scaledobjects.keda.sh qwen-llmd -n inference --type=merge -p {"metadata":{"finalizers":[]}}' in log
    assert 'patch scaledjobs.keda.sh batch-job -n inference --type=merge -p {"metadata":{"finalizers":[]}}' in log


def test_the_watchdog_keeps_clearing_them_until_the_cluster_is_gone():
    assert '_strip_teardown_finalizers "$env"' in _function("_teardown_watchdog")
