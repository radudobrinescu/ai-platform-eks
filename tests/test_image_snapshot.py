"""GPU image snapshot without Docker Hub credentials.

The vLLM image is baked into an EBS snapshot that GPU nodes boot from. It used
to need the Docker Hub pull-through cache (credentials), was found by cluster
name, and only took effect from the second apply. Now:
  * a docker.io reference is pulled as given (anonymously), a short one still
    goes through the pull-through cache;
  * --reuse finds an existing snapshot of the same image in the account, so a
    new cluster does not build it again;
  * --patch-nodeclasses points the GPU EC2NodeClasses at it in the same apply;
  * Terraform bakes it on every cluster with Karpenter and finds it by image.
"""

import os
import re
import signal
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "ops/image/create-data-volume-snapshot.sh")
TF = os.path.join(ROOT, "terraform/30.eks/30.cluster/image-optimization.tf")

FAKE_AWS = r"""#!/bin/bash
echo "aws $*" >> "$CALLS"
case "$*" in
  *"sts get-caller-identity"*) echo 111122223333 ;;
  *"describe-snapshots"*) echo "${EXISTING_SNAPSHOT:-None}" ;;
  *"describe-subnets"*) echo None ;;
  *"describe-instances"*"instance-state-name"*) printf '%b\n' "${LEFTOVER_BUILDERS:-}" ;;
esac
"""

# gpu-inference: /dev/xvdb second, no snapshot yet; gpu-shared: already set;
# gpu-decode: missing.
FAKE_KUBECTL = r"""#!/bin/bash
echo "kubectl $*" >> "$CALLS"
case "$*" in
  *"get ec2nodeclass gpu-decode"*) exit 1 ;;
  *"get ec2nodeclass gpu-inference"*"deviceName"*) printf '/dev/xvda\n/dev/xvdb\n' ;;
  *"get ec2nodeclass gpu-shared"*"deviceName"*) printf '/dev/xvdb\n' ;;
  *"get ec2nodeclass gpu-shared"*"snapshotID"*) echo "$EXISTING_SNAPSHOT" ;;
  *"get ec2nodeclass"*"snapshotID"*) echo "" ;;
  *"patch ec2nodeclass"*) : ;;
esac
"""


def _run(tmp_path, *args, cluster="c1", **env):
    for name, body in (("aws", FAKE_AWS), ("kubectl", FAKE_KUBECTL)):
        f = tmp_path / name
        f.write_text(body)
        f.chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    e = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls), **env}
    r = subprocess.run(["bash", SCRIPT, "-r", "eu-central-1", "-n", cluster, *args],
                       capture_output=True, text=True, env=e, timeout=30)
    return r, calls.read_text()


def test_reuses_a_snapshot_of_the_same_image_and_patches_the_gpu_nodeclasses(tmp_path):
    r, calls = _run(tmp_path, "--reuse", "--patch-nodeclasses", "gpu-inference,gpu-shared,gpu-decode",
                    "docker.io/vllm/vllm-openai:v0.33.0", EXISTING_SNAPSHOT="snap-0abc123")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "snap-0abc123"
    assert "Name=tag:Images,Values=docker.io/vllm/vllm-openai:v0.33.0" in calls
    assert "Name=volume-size,Values=200" in calls
    patches = [line for line in calls.splitlines() if "patch ec2nodeclass" in line]
    assert len(patches) == 1 and "gpu-inference" in patches[0]
    assert '"path":"/spec/blockDeviceMappings/1/ebs/snapshotID","value":"snap-0abc123"' in patches[0]
    assert "run-instances" not in calls   # nothing built


def test_without_a_match_it_builds(tmp_path):
    # The fake has no subnet, so the build stops at networking: far enough to
    # show it did not reuse and did not rewrite the docker.io reference.
    r, calls = _run(tmp_path, "--reuse", "docker.io/vllm/vllm-openai:v0.33.0")
    assert r.returncode != 0
    assert "No snapshot of docker.io/vllm/vllm-openai:v0.33.0 yet" in r.stdout
    assert "docker.io/vllm/vllm-openai:v0.33.0" in r.stdout and "docker-hub/" not in r.stdout


def test_short_reference_still_uses_the_pull_through_cache(tmp_path):
    r, _ = _run(tmp_path, "vllm/vllm-openai:v0.33.0")
    assert "111122223333.dkr.ecr.eu-central-1.amazonaws.com/docker-hub/vllm/vllm-openai:v0.33.0" in r.stdout


def test_rejects_bad_nodeclass_names(tmp_path):
    r, _ = _run(tmp_path, "--patch-nodeclasses", "gpu;rm -rf /", "docker.io/vllm/vllm-openai:v0.33.0")
    assert r.returncode == 1 and "comma-separated Kubernetes names" in r.stderr


class TestTerraform:
    def _tf(self):
        with open(TF, encoding="utf-8") as fh:
            return fh.read()

    def test_snapshot_no_longer_needs_docker_hub_credentials(self):
        tf = self._tf()
        m = re.search(r"run_image_snapshot\s*=\s*(.+)", tf)
        assert m and "docker_hub_username" not in m.group(1)
        assert re.search(r'snapshot_image\s*=.*"docker\.io/\$\{local\.vllm_image\}"', tf)

    def test_snapshot_is_found_by_image_not_by_cluster(self):
        tf = self._tf()
        block = tf[tf.index('data "aws_ebs_snapshot_ids" "gpu_data_volume"'):]
        block = block[:block.index("\n}\n")]
        assert "tag:Images" in block and "tag:Cluster" not in block
        assert "volume-size" in block

    def test_builder_reuses_and_patches(self):
        tf = self._tf()
        block = tf[tf.index('resource "null_resource" "gpu_data_volume_snapshot"'):]
        block = block[:block.index("\n}\n")]
        assert "--reuse" in block and "--patch-nodeclasses" in block
        assert "kubectl_manifest.karpenter_manifests" in block   # NodeClasses exist first


BUILD_AWS = r"""#!/bin/bash
echo "aws $*" >> "$CALLS"
case "$*" in
  *"sts get-caller-identity"*) echo 111122223333 ;;
  *"describe-snapshots"*) echo None ;;
  *"describe-subnets"*) echo subnet-1 ;;
  *"describe-cluster"*"clusterSecurityGroupId"*) echo sg-1 ;;
  *"describe-cluster"*"cluster.version"*) echo 1.37 ;;
  *"describe-cluster"*"endpoint"*) echo https://api.example ;;
  *"describe-cluster"*"certificateAuthority"*) echo Q0E= ;;
  *"list-instance-profiles-for-role"*)
    n=$(grep -c "list-instance-profiles-for-role" "$CALLS")
    if [ "$n" -le "${PROFILE_MISSING:-0}" ]; then echo None; else echo c1_123; fi ;;
  *"ssm get-parameter"*) echo ami-1 ;;
  *"run-instances"*)
    n=$(grep -c "run-instances" "$CALLS")
    if [ "$n" -le "${PROFILE_ERRORS:-0}" ]; then
      echo "An error occurred (InvalidParameterValue) when calling the RunInstances operation: Value (c1_123) for parameter iamInstanceProfile.name is invalid. Invalid IAM Instance Profile name" >&2
      exit 254
    fi
    if [ -n "${OTHER_ERROR:-}" ]; then echo "$OTHER_ERROR" >&2; exit 254; fi
    if [ -n "${LAUNCH_WARNING:-}" ]; then echo "$LAUNCH_WARNING" >&2; fi
    echo i-0abc ;;
  # Builders an interrupted earlier run left behind (looked up by tags + state).
  *"describe-instances"*"instance-state-name"*) printf '%b\n' "${LEFTOVER_BUILDERS:-}" ;;
  *"describe-instances"*) echo vol-1 ;;
  *"terminate-instances"*)
    if [ -n "${TERMINATE_FAILS:-}" ]; then
      echo "An error occurred (UnauthorizedOperation) when calling the TerminateInstances operation" >&2
      exit 254
    fi ;;
  *"create-snapshot"*) echo snap-0new ;;
esac
"""

# KUBECTL_BREAKS: every call fails from the node readiness wait on (the cluster
# became unreachable, or the context changed, in the middle of a build).
BUILD_KUBECTL = r"""#!/bin/bash
echo "kubectl $*" >> "$CALLS"
if [ -n "${KUBECTL_BREAKS:-}" ]; then
  case "$*" in *"wait --for=condition=Ready"*) touch "$CALLS.kubectl-broken" ;; esac
  if [ -e "$CALLS.kubectl-broken" ]; then echo "Unable to connect to the server" >&2; exit 1; fi
fi
case "$*" in
  *"get nodes"*) echo node-1 ;;
  *"wait --for=jsonpath"*) if [ -n "${PULL_HANGS:-}" ]; then sleep "$PULL_HANGS"; fi ;;
esac
"""


BUILD_ARGS = ("--reuse", "docker.io/vllm/vllm-openai:v0.33.0")


def _build_env(tmp_path, **env):
    """Put the fake aws and kubectl first on PATH; returns (environment, calls file)."""
    for name, body in (("aws", BUILD_AWS), ("kubectl", BUILD_KUBECTL)):
        f = tmp_path / name
        f.write_text(body)
        f.chmod(0o755)
    calls = tmp_path / "calls"
    calls.write_text("")
    e = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "CALLS": str(calls),
         "LAUNCH_RETRY_SECONDS": "0", **env}
    return e, calls


def _build(tmp_path, args=BUILD_ARGS, **env):
    e, calls = _build_env(tmp_path, **env)
    r = subprocess.run(["bash", SCRIPT, "-r", "eu-central-1", "-n", "c1", *args],
                       capture_output=True, text=True, env=e, timeout=60)
    return r, calls.read_text()


def test_builder_launch_waits_for_a_new_instance_profile(tmp_path):
    # E2E: Karpenter created the node role's instance profiles 6 s before the
    # launch and EC2 rejected the name; the build was skipped.
    r, calls = _build(tmp_path, PROFILE_ERRORS="2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls.count("run-instances") == 3
    assert "Snapshot ID:  snap-0new" in r.stdout
    assert "terminate-instances --instance-ids i-0abc" in calls   # the builder never outlives the script
    assert "Key=Images,Value=docker.io/vllm/vllm-openai:v0.33.0" in calls


def test_builder_launch_does_not_retry_other_errors(tmp_path):
    r, calls = _build(tmp_path, OTHER_ERROR="An error occurred (UnauthorizedOperation)")
    assert r.returncode == 1 and calls.count("run-instances") == 1
    assert "UnauthorizedOperation" in r.stderr


def test_builder_waits_for_karpenter_to_create_the_instance_profile(tmp_path):
    # E2E: the step ran before Karpenter had created the node role's instance
    # profile ("cannot find instance profile") and the snapshot was skipped.
    r, calls = _build(tmp_path, PROFILE_MISSING="2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls.count("list-instance-profiles-for-role") == 3
    assert "Snapshot ID:  snap-0new" in r.stdout


# --- The builder never outlives the script -----------------------------------

TERMINATE = "terminate-instances --instance-ids i-0abc"


def test_builder_is_terminated_when_kubectl_fails_during_cleanup(tmp_path):
    # The cleanup looked the builder node up with kubectl before terminating the
    # instance; with the cluster unreachable it stopped there and the c5.xlarge
    # with its 200 GiB volume kept running, with nothing printed about it.
    r, calls = _build(tmp_path, KUBECTL_BREAKS="1")
    assert r.returncode != 0
    assert TERMINATE in calls, r.stdout + r.stderr


def test_builder_is_terminated_when_the_script_is_killed(tmp_path):
    # Terraform cancelled, CI job timed out: SIGTERM in the middle of the pull.
    e, calls = _build_env(tmp_path, PULL_HANGS="3")
    with open(tmp_path / "out", "w") as out:
        p = subprocess.Popen(["bash", SCRIPT, "-r", "eu-central-1", "-n", "c1", *BUILD_ARGS],
                             stdout=out, stderr=subprocess.STDOUT, env=e)
        deadline = time.time() + 20
        while "wait --for=jsonpath" not in calls.read_text() and time.time() < deadline:
            time.sleep(0.05)
        p.send_signal(signal.SIGTERM)
        rc = p.wait(timeout=20)
    assert TERMINATE in calls.read_text(), (tmp_path / "out").read_text()
    assert "create-snapshot" not in calls.read_text()
    assert rc in (-signal.SIGTERM, 128 + signal.SIGTERM)


def test_a_warning_on_a_successful_launch_does_not_lose_the_instance(tmp_path):
    # stderr was captured with the instance id: any warning line made the id
    # check fail, and the script left before its cleanup was installed.
    r, calls = _build(tmp_path, LAUNCH_WARNING="Warning: the CLI will drop this option soon")
    assert TERMINATE in calls, r.stdout + r.stderr
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Snapshot ID:  snap-0new" in r.stdout
    assert "the CLI will drop this option soon" in r.stderr   # still shown


def test_a_failed_termination_is_reported_with_the_command_to_run(tmp_path):
    r, calls = _build(tmp_path, TERMINATE_FAILS="1")
    assert TERMINATE in calls
    assert "terminated." not in r.stdout and "terminated." not in r.stderr
    assert "i-0abc" in r.stderr
    assert "aws ec2 terminate-instances --instance-ids i-0abc --region eu-central-1" in r.stderr
    assert r.returncode != 0   # an instance is still billed: not a success


def test_a_successful_termination_names_the_instance(tmp_path):
    r, _ = _build(tmp_path)
    assert "Builder instance i-0abc terminated." in r.stdout


def test_builder_volumes_are_deleted_with_the_instance(tmp_path):
    _, calls = _build(tmp_path)
    launch = calls[calls.index("run-instances"):calls.index("--tag-specifications")]
    assert launch.count('"DeleteOnTermination":true') == 2, launch


def test_leftover_builders_of_this_cluster_are_terminated_before_launching(tmp_path):
    # A run killed between launch and cleanup (closed terminal, CI timeout)
    # leaves its builder running; the next run removes it.
    r, calls = _build(tmp_path, LEFTOVER_BUILDERS=r"i-0dead1\ti-0dead2")
    assert r.returncode == 0, r.stdout + r.stderr
    lookup = calls[calls.index("describe-instances"):]
    lookup = lookup[:lookup.index("\naws ", 1)]
    # Exact tag values: this cluster's builders only, and only live states.
    assert "Name=tag:Name,Values=c1-snapshot-builder " in lookup
    assert "Name=tag:Purpose,Values=data-volume-snapshot " in lookup
    assert "Name=instance-state-name,Values=pending,running,stopping,stopped " in lookup
    assert "*" not in lookup and "?" not in lookup
    sweep = calls.index("terminate-instances --instance-ids i-0dead1 i-0dead2")
    assert sweep < calls.index("run-instances")
    assert "i-0dead1" in r.stdout   # said, not silent


def test_leftover_builders_are_also_removed_when_a_snapshot_is_reused(tmp_path):
    r, calls = _run(tmp_path, "--reuse", "docker.io/vllm/vllm-openai:v0.33.0",
                    EXISTING_SNAPSHOT="snap-0abc123", LEFTOVER_BUILDERS="i-0dead1")
    assert r.returncode == 0, r.stderr
    assert "terminate-instances --instance-ids i-0dead1" in calls


def test_a_cluster_name_that_could_match_other_clusters_is_rejected(tmp_path):
    # EC2 tag filters treat * and ? as wildcards.
    for name in ("c*", "c?", "c1 x"):
        r, calls = _run(tmp_path, "docker.io/vllm/vllm-openai:v0.33.0", cluster=name)
        assert r.returncode == 1 and "cluster name" in r.stderr, (name, r.stderr)
        assert calls == ""   # nothing was called
