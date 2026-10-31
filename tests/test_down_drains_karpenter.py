"""`down` deletes Karpenter's NodePools and waits for its NodeClaims to drain
while the controller still runs, before Terraform removes the controller. A
drain that cannot finish (PDB with nowhere to reschedule) is forced by
terminating the instances behind the remaining NodeClaims."""

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Fake kubectl. NODECLAIMS_AFTER: number of `get nodeclaims -o name` calls that
# still return a claim; GONE_AFTER_TERMINATE=1 makes claims vanish once aws
# terminate-instances was called. HAS_KARPENTER=0 hides the controller.
FAKE_KUBECTL = r"""#!/bin/bash
echo "kubectl $*" >> "$CALLS"
case "$*" in
  *"get deployment karpenter"*) [ "${HAS_KARPENTER:-1}" = 1 ] ;;
  *"get nodepools"*) echo nodepool.karpenter.sh/default ;;
  *"delete nodepools"*) : ;;
  *"get nodeclaims"*"providerID"*) echo "aws:///eu-central-1a/i-0abc123" ;;
  *"get nodeclaims"*"nodeName"*) echo ip-10-0-0-1 ;;
  *"get pods"*) echo "kube-system/coredns-1 " ;;
  *"get nodeclaims"*)
    if [ "${GONE_AFTER_TERMINATE:-0}" = 1 ] && grep -q terminate-instances "$CALLS"; then exit 0; fi
    n=$(grep -c "get nodeclaims.karpenter.sh -o name" "$CALLS")
    if [ "$n" -le "${NODECLAIMS_AFTER:-0}" ]; then echo nodeclaim.karpenter.sh/default-abc; fi ;;
esac
"""
FAKE_AWS = r"""#!/bin/bash
echo "aws $*" >> "$CALLS"
"""


def _functions(*names):
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        text = fh.read()
    out = []
    for name in names:
        m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
        assert m, name
        out.append(m.group(0))
    return "".join(out)


def _run(tmp_path, **env):
    for name, body in (("kubectl", FAKE_KUBECTL), ("aws", FAKE_AWS)):
        f = tmp_path / name
        f.write_text(body)
        f.chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    script = ("set -euo pipefail\nlog(){ echo \"LOG $*\"; }\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
              + _functions("_drain_karpenter_nodes", "_karpenter_nodeclaims_gone", "_karpenter_drain_blockers")
              + "_drain_karpenter_nodes e2e\necho done\n")
    e = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls), "AWS_REGION": "eu-central-1",
         "PLATFORMCTL_KARPENTER_DRAIN_INTERVAL": "0", **env}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=30)
    return r, calls.read_text()


def test_deletes_nodepools_and_waits_for_nodeclaims(tmp_path):
    r, calls = _run(tmp_path, NODECLAIMS_AFTER="3")
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    assert "delete nodepools.karpenter.sh --all --wait=false" in calls
    assert "terminate-instances" not in calls
    assert "OK Karpenter terminated all of its nodes" in r.stdout


def test_stuck_drain_terminates_only_the_nodeclaim_instances(tmp_path):
    r, calls = _run(tmp_path, NODECLAIMS_AFTER="999", GONE_AFTER_TERMINATE="1",
                    PLATFORMCTL_KARPENTER_DRAIN_TIMEOUT="0", PLATFORMCTL_KARPENTER_FORCE_TIMEOUT="0")
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    assert "aws ec2 terminate-instances --region eu-central-1 --instance-ids i-0abc123" in calls
    assert "[drain] node ip-10-0-0-1 still holds: kube-system/coredns-1" in r.stdout
    assert "OK Karpenter removed its remaining nodes" in r.stdout


def test_still_stuck_after_force_continues_with_fallback(tmp_path):
    r, _ = _run(tmp_path, NODECLAIMS_AFTER="999",
                PLATFORMCTL_KARPENTER_DRAIN_TIMEOUT="0", PLATFORMCTL_KARPENTER_FORCE_TIMEOUT="0")
    assert r.returncode == 0 and "done" in r.stdout
    assert "fallback" in r.stdout


def test_skips_without_self_managed_karpenter(tmp_path):
    r, calls = _run(tmp_path, HAS_KARPENTER="0")
    assert r.returncode == 0 and "done" in r.stdout
    assert "delete nodepools" not in calls


def test_runs_inside_the_predown_drain_before_terraform():
    assert '_drain_karpenter_nodes "$env"' in _functions("_predown_drain")


def test_gpu_pools_exclude_families_the_node_image_cannot_drive():
    for f in ("gpu-shared.yaml", "gpu-inference.yaml"):
        with open(os.path.join(ROOT, "terraform/30.eks/30.cluster/karpenter", f), encoding="utf-8") as fh:
            text = fh.read()
        notin = re.search(r'instance-family\n\s+operator: NotIn\n\s+values: \[([^\]]*)\]', text).group(1)
        assert '"g7"' in notin and '"g7e"' in notin, f


def _run_fn(tmp_path, fn, call, kubectl_body, **env):
    k = tmp_path / "kubectl"
    k.write_text(kubectl_body)
    k.chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    script = ("set -euo pipefail\nlog(){ echo \"LOG $*\"; }\nok(){ echo \"OK $*\"; }\nwarn(){ echo \"WARN $*\"; }\n"
              + _functions(fn) + call + "\necho done\n")
    e = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls),
         "PLATFORMCTL_KARPENTER_DRAIN_INTERVAL": "0", **env}
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e, timeout=30), calls.read_text()


APPS_FAKE = r"""#!/bin/bash
echo "kubectl $*" >> "$CALLS"
n=$(grep -c "get applications" "$CALLS")
if [ "$n" -le "${APPS_AFTER:-0}" ]; then echo application.argoproj.io/litellm; fi
"""


def test_waits_for_argocd_apps_to_be_deleted(tmp_path):
    r, calls = _run_fn(tmp_path, "_wait_argocd_apps_gone", "_wait_argocd_apps_gone e2e", APPS_FAKE, APPS_AFTER="2")
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    assert calls.count("get applications") == 3 and "OK Argo CD applications" in r.stdout


def test_apps_wait_times_out_and_continues(tmp_path):
    r, _ = _run_fn(tmp_path, "_wait_argocd_apps_gone", "_wait_argocd_apps_gone e2e", APPS_FAKE,
                   APPS_AFTER="999", PLATFORMCTL_APPS_DELETE_TIMEOUT="0")
    assert r.returncode == 0 and "WARN Argo CD applications still deleting" in r.stdout


def test_apps_wait_runs_before_the_node_drain():
    body = _functions("_predown_drain")
    assert body.index('_wait_argocd_apps_gone "$env"') < body.index('_drain_karpenter_nodes "$env"')
