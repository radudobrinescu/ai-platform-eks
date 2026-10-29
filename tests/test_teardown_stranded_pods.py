"""`down` force-deletes only pods that are terminating on an unreachable node.

Seen in E2E (managed profile): destroy removed Karpenter's NodePool and its
controller together, the nodes' instances went away, their Node objects stayed,
and two terminating Langfuse pods held the ai-platform namespace until
Terraform's namespace destroy timed out.
"""

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_KUBECTL = r"""#!/bin/sh
case "$*" in
  *"get nodes"*) printf 'node-ok True\nnode-dead Unknown\nnode-nr False\n' ;;
  *"get pods"*)  printf 'ai-platform stuck-a node-dead\nai-platform stuck-b node-nr\nai-platform draining node-ok\nkube-system pending \n' ;;
  *"delete pod"*) echo "$*" >> "$DELETED" ;;
esac
"""


def _function(name):
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        text = fh.read()
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
    assert m, name
    return m.group(0)


def test_only_terminating_pods_on_unready_nodes_are_forced(tmp_path):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(0o755)
    deleted = tmp_path / "deleted"
    script = _function("_force_delete_stranded_pods") + '_force_delete_stranded_pods e2e\n'
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "DELETED": str(deleted)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    lines = deleted.read_text().splitlines()
    assert len(lines) == 2
    assert any("delete pod stuck-a -n ai-platform --grace-period=0 --force" in ln for ln in lines)
    assert any("delete pod stuck-b -n ai-platform --grace-period=0 --force" in ln for ln in lines)


def test_watchdog_calls_it():
    assert "_force_delete_stranded_pods \"$env\"" in _function("_teardown_watchdog")


def test_failing_kubectl_does_not_kill_a_strict_shell(tmp_path):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text("#!/bin/sh\necho 'Unable to connect to the server' >&2\nexit 1\n")
    kubectl.chmod(0o755)
    script = ("set -euo pipefail\n" + _function("_force_delete_stranded_pods")
              + "_force_delete_stranded_pods e2e\necho survived\n")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and "survived" in r.stdout, r.stderr


def test_watchdog_turns_off_errexit():
    body = _function("_teardown_watchdog")
    assert "set +e +o pipefail" in body.split("local env", 1)[0]
