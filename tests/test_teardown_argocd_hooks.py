"""`down` clears Argo CD's hook finalizer on hook objects left behind.

Seen in E2E (upgrade/default, test of the next release): the litellm-enduser-budget
PostSync Job was still running when `down` removed Argo CD. Nothing could remove
argocd.argoproj.io/hook-finalizer, the ai-platform namespace stayed Terminating,
Terraform's namespace destroy timed out, and the cluster was left ACTIVE.
"""

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_KUBECTL = r"""#!/bin/sh
echo "$*" >> "$CALLS"
case "$*" in
  *"get jobs.batch"*) printf 'ai-platform/litellm-enduser-budget ["argocd.argoproj.io/hook-finalizer"]\n' ;;
  *"get pods"*) printf 'inference/vllm-x-123 ["batch.kubernetes.io/job-tracking"]\n' ;;
  *"get secrets"*) printf 'ai-platform/langfuse-s3-sftp-secret ["argocd.argoproj.io/hook-finalizer"]\n' ;;
  *"get configmaps"*) exit 1 ;;
  *"get "*) : ;;
esac
"""


def _function(name):
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
    assert m, name
    return m.group(0)


def test_only_objects_with_the_argocd_hook_finalizer_are_cleared(tmp_path):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    script = "set -euo pipefail\n" + _function("_strip_teardown_finalizers") + "_strip_teardown_finalizers e2e\necho done\n"
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    log = calls.read_text()
    assert r.returncode == 0 and "done" in r.stdout, r.stderr  # a failing list (configmaps) is not fatal
    patch = '--type=merge -p {"metadata":{"finalizers":[]}}'
    assert f"--context e2e patch jobs.batch litellm-enduser-budget -n ai-platform {patch}" in log
    assert f"--context e2e patch secrets langfuse-s3-sftp-secret -n ai-platform {patch}" in log
    assert "patch pods" not in log  # other finalizers (job tracking) are left alone
