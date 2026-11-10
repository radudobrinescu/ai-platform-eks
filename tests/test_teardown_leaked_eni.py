"""A network interface the VPC CNI failed to delete outlives its node, keeps the
EKS cluster security group, and through it pins the VPC (E2E run 42770328926:
two 20-minute Terraform timeouts, then a leftover VPC while `down` exited 0).

`down` must delete such ENIs before the security groups (watchdog and sweep),
retry destroy after the final sweep, and fail when the destroy never completed.
"""

import os
import re
import subprocess

import sweep_env

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _functions(*names):
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        text = fh.read()
    out = []
    for name in names:
        m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
        assert m, name
        out.append(m.group(0))
    return "".join(out)


def _bin(tmp_path, name, body):
    f = tmp_path / name
    f.write_text("#!/bin/bash\n" + body)
    f.chmod(0o755)


# --------------------------------------------------------------------------- #
# sweep_env: available ENIs go before security groups                          #
# --------------------------------------------------------------------------- #
class OrderEc2:
    def __init__(self):
        self.calls = []
        self.eni_gone = False

    def describe_instances(self, Filters):
        return {"Reservations": []}

    def describe_nat_gateways(self, Filter):
        return {"NatGateways": []}

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": [{"GroupId": "sg-cluster", "GroupName": "eks-cluster-sg-x-1"},
                                   {"GroupId": "sg-default", "GroupName": "default"}]}

    def describe_network_interfaces(self, Filters):
        assert {"Name": "status", "Values": ["available"]} in Filters
        return {"NetworkInterfaces": [{"NetworkInterfaceId": "eni-leaked"}]}

    def delete_network_interface(self, NetworkInterfaceId):
        self.calls.append(("eni", NetworkInterfaceId))
        self.eni_gone = True

    def delete_security_group(self, GroupId):
        self.calls.append(("sg", GroupId))
        if not self.eni_gone:
            raise sweep_env.ClientError({"Error": {"Code": "DependencyViolation"}}, "DeleteSecurityGroup")


def test_sweep_deletes_available_enis_before_security_groups(monkeypatch):
    monkeypatch.setattr(sweep_env.boto3, "client", lambda *a, **k: object())
    s = sweep_env.Sweeper("e2e", "ai-platform-e2e", "eu-central-1")
    s.ec2 = OrderEc2()
    s.clear_vpc_blockers("vpc-1")
    assert s.ec2.calls == [("eni", "eni-leaked"), ("sg", "sg-cluster")]



# --------------------------------------------------------------------------- #
# watchdog: the security-group loop clears ENIs that hold the group            #
# --------------------------------------------------------------------------- #
FAKE_AWS = r"""
echo "aws $*" >> "$CALLS"
case "$*" in
  *"describe-security-groups"*"--filters"*) echo "sg-cluster" ;;
  *"describe-security-groups"*"IpPermissions"*) echo "[]" ;;
  *"describe-security-groups"*"--group-ids"*) grep -q "delete-security-group" "$CALLS" && grep -q "sg-ok" "$CALLS" && exit 255; exit 0 ;;
  *"describe-network-interfaces"*"group-id,Values=sg-cluster"*)
    grep -q "delete-network-interface" "$CALLS" && { echo None; exit 0; }; echo "eni-leaked" ;;
  *"delete-network-interface"*) : ;;
  *"delete-security-group"*)
    grep -q "delete-network-interface" "$CALLS" || exit 255; echo sg-ok >> "$CALLS" ;;
esac
"""


def test_watchdog_deletes_the_eni_then_the_security_group(tmp_path):
    _bin(tmp_path, "aws", FAKE_AWS)
    _bin(tmp_path, "sleep", ":")
    calls = tmp_path / "calls"
    calls.write_text("")
    script = ("set -euo pipefail\n" + _functions("_delete_available_enis", "_delete_cluster_security_groups")
              + '_delete_cluster_security_groups ai-platform-e2e vpc-1 eu-central-1\necho done\n')
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    log = calls.read_text()
    assert r.returncode == 0 and "done" in r.stdout, r.stderr
    assert "deleted leftover network interface eni-leaked (held sg-cluster)" in r.stdout
    assert "deleted leftover security group sg-cluster" in r.stdout
    assert log.index("delete-network-interface") < log.index("sg-ok")
    # scoped: only available ENIs in this VPC that carry this SG
    assert "Name=vpc-id,Values=vpc-1 Name=status,Values=available Name=group-id,Values=sg-cluster" in log


def test_eni_cleanup_needs_a_vpc_and_groups(tmp_path):
    _bin(tmp_path, "aws", 'echo "aws $*" >> "$CALLS"\n')
    calls = tmp_path / "calls"
    calls.write_text("")
    script = "set -euo pipefail\n" + _functions("_delete_available_enis") + \
        '_delete_available_enis "" eu-central-1 sg-1\n_delete_available_enis vpc-1 eu-central-1 ""\necho done\n'
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0 and "done" in r.stdout and calls.read_text() == ""


# --------------------------------------------------------------------------- #
# cmd_down: retry after the final sweep; fail when the destroy never completed  #
# --------------------------------------------------------------------------- #
STUBS = r"""
log(){ echo "LOG $*"; }
warn(){ echo "WARN $*"; }
die(){ echo "DIE $*"; exit 1; }
need_env(){ echo e2e; }
need(){ :; }
require_tfvars(){ :; }
pin_region(){ :; }
_edge_set_tfvar(){ :; }
_predown_drain(){ :; }
cluster_of(){ echo ai-platform-e2e; }
_cluster_vpc(){ echo vpc-1; }
_postdown_sweep(){ echo "SWEEP" >> "$CALLS"; }
purge_env_definition(){ echo "PURGE" >> "$CALLS"; }
TF_DIR=/nonexistent
"""
# make fails for the first FAILS calls.
FAKE_MAKE = r"""
echo "MAKE" >> "$CALLS"
n=0; while read -r l; do [ "$l" = MAKE ] && n=$((n + 1)); done < "$CALLS"
[ "$n" -gt "${FAILS:-0}" ]
"""


def _down(tmp_path, fails, *args):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _bin(bindir, "make", FAKE_MAKE)
    calls = tmp_path / "calls"
    calls.write_text("")
    script = "set -euo pipefail\n" + STUBS + _functions("cmd_down") + f"cmd_down {' '.join(args)} <<< e2e\necho done\n"
    # PATH holds only the fake make: no kubectl/aws, so the watchdog is not started.
    env = {"PATH": str(bindir), "CALLS": str(calls), "FAILS": str(fails), "HOME": str(tmp_path)}
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    return r, calls.read_text().split()


def test_clean_destroy_runs_once_and_sweeps(tmp_path):
    r, calls = _down(tmp_path, 0, "e2e")
    assert r.returncode == 0 and "done" in r.stdout, r.stdout + r.stderr
    assert calls == ["MAKE", "SWEEP"]


def test_destroy_freed_by_the_final_sweep_is_retried(tmp_path):
    r, calls = _down(tmp_path, 2, "e2e", "--purge")
    assert r.returncode == 0 and "done" in r.stdout, r.stdout + r.stderr
    assert calls == ["MAKE", "SWEEP", "MAKE", "SWEEP", "MAKE", "SWEEP", "PURGE"]
    assert "Retrying destroy once more after the final sweep" in r.stdout


def test_destroy_that_never_completes_fails_and_keeps_the_definition(tmp_path):
    r, calls = _down(tmp_path, 99, "e2e", "--purge")
    assert r.returncode != 0 and "done" not in r.stdout
    assert "DIE destroy did not complete for 'e2e'" in r.stdout
    assert calls == ["MAKE", "SWEEP", "MAKE", "SWEEP", "MAKE"] and "PURGE" not in calls
