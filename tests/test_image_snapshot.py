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
import shutil
import signal
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "ops/image/create-data-volume-snapshot.sh")
TF = os.path.join(ROOT, "terraform/30.eks/30.cluster/image-optimization.tf")

# `aws eks update-kubeconfig --kubeconfig FILE` writes FILE, as the real one does.
AWS_UPDATE_KUBECONFIG = r"""
case "$*" in *"eks update-kubeconfig"*)
  if [ -n "${KUBECONFIG_FAILS:-}" ]; then
    echo "An error occurred (ResourceNotFoundException) when calling the DescribeCluster operation: No cluster found for name: c1." >&2
    exit 254
  fi
  while [ $# -gt 0 ]; do
    if [ "$1" = "--kubeconfig" ]; then echo "kubeconfig written by the script" > "$2"; fi
    shift
  done
  exit 0 ;;
esac
"""

# Every kubectl call records the kubeconfig it ran with; UNREACHABLE makes the
# API server unreachable.
KUBECTL_PRELUDE = r"""
echo "${KUBECONFIG:-<ambient>}" >> "$CALLS.kubeconfig"
if [ -n "${UNREACHABLE:-}" ]; then echo "Unable to connect to the server: dial tcp: i/o timeout" >&2; exit 1; fi
"""

FAKE_AWS = r"""#!/bin/bash
echo "aws $*" >> "$CALLS"
""" + AWS_UPDATE_KUBECONFIG + r"""
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
""" + KUBECTL_PRELUDE + r"""
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

    def _block(self, header):
        tf = self._tf()
        block = tf[tf.index(header):]
        return block[:block.index("\n}\n")]

    def _code(self, block):
        """The block without its comments."""
        return "\n".join(line.split("#")[0] for line in block.splitlines())

    def test_a_failed_build_runs_again_on_the_next_apply(self):
        # With on_failure = continue a failed build was recorded as done: it ran
        # again only when the vLLM version changed, and nothing said so.
        block = self._code(self._block('resource "null_resource" "gpu_data_volume_snapshot"'))
        triggers = block[block.index("triggers = {"):]
        triggers = triggers[:triggers.index("}")]
        m = re.search(r'(\w+)\s*=\s*local\.resolved_snapshot_id\s*(==|!=)\s*""\s*\?\s*(.+?)\s*:\s*(.+)', triggers)
        assert m, triggers
        missing, found = (m.group(3), m.group(4)) if m.group(2) == "==" else (m.group(4), m.group(3))
        # No snapshot: a value that differs at every apply, so the builder runs again.
        assert missing.strip() == "timestamp()"
        # A snapshot exists: a constant, so there is no diff from then on.
        assert re.fullmatch(r'"[a-z-]+"', found.strip()), found
        # The two triggers that were there still are.
        assert "local.snapshot_image" in triggers and "local.cluster_name" in triggers

    def test_a_failed_build_still_never_fails_the_apply(self):
        block = self._code(self._block('resource "null_resource" "gpu_data_volume_snapshot"'))
        assert re.search(r"on_failure\s*=\s*continue", block)

    def test_snapshot_lookup_does_not_depend_on_the_builder(self):
        # The trigger reads the lookup; the lookup must not wait for the builder
        # (a cycle), nor be deferred to apply time by a depends_on (the trigger
        # would then be unknown at every plan: a diff for ever).
        block = self._code(self._block('data "aws_ebs_snapshot_ids" "gpu_data_volume"'))
        assert "null_resource" not in block and "depends_on" not in block
        tf = self._tf()
        m = re.search(r"^\s*resolved_snapshot_id\s*=\s*(.+)", tf, re.M)
        assert m and "null_resource" not in m.group(1)
        assert "data.aws_ebs_snapshot_ids.gpu_data_volume[0].ids[0]" in m.group(1)


BUILD_AWS = r"""#!/bin/bash
echo "aws $*" >> "$CALLS"
""" + AWS_UPDATE_KUBECONFIG + r"""
case "$*" in
  *"sts get-caller-identity"*) echo 111122223333 ;;
  *"describe-snapshots"*"--snapshot-ids"*) echo "${SNAPSHOT_STATE:-pending}" ;;
  # OTHER_BUILD_DONE_AFTER=N: from lookup N+1 on, another cluster's build of the
  # same image has completed.
  *"describe-snapshots"*)
    n=$(grep -c "describe-snapshots.*tag:Images" "$CALLS")
    if [ -n "${OTHER_BUILD_DONE_AFTER:-}" ] && [ "$n" -gt "$OTHER_BUILD_DONE_AFTER" ]; then echo snap-0c0de; else echo None; fi ;;
  *"describe-subnets"*) echo subnet-1 ;;
  *"describe-cluster"*"clusterSecurityGroupId"*) echo sg-1 ;;
  *"describe-cluster"*"cluster.version"*) echo 1.37 ;;
  *"describe-cluster"*"endpoint"*) echo https://api.example ;;
  *"describe-cluster"*"certificateAuthority"*) echo Q0E= ;;
  *"list-instance-profiles-for-role"*)
    n=$(grep -c "list-instance-profiles-for-role" "$CALLS")
    if [ "$n" -le "${PROFILE_MISSING:-0}" ]; then echo None; else echo c1_123; fi ;;
  *"ssm get-parameter"*) echo ami-1 ;;
  *"ecr describe-images"*) if [ -n "${ECR_MISSING:-}" ]; then echo "An error occurred (ImageNotFoundException)" >&2; exit 254; fi ;;
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
  *"stop-instances"*)
    if [ -n "${STOP_FAILS:-}" ]; then echo "An error occurred (IncorrectInstanceState)" >&2; exit 254; fi ;;
  *"create-snapshot"*) echo snap-0bead ;;
  # The real waiter gives up after 40 checks, 15 s apart (10 minutes).
  *"wait snapshot-completed"*)
    n=$(grep -c "wait snapshot-completed" "$CALLS")
    if [ "$n" -le "${SNAPSHOT_WAIT_FAILS:-0}" ]; then
      echo "Waiter SnapshotCompleted failed: Max attempts exceeded" >&2
      exit 255
    fi ;;
esac
"""

# The builder instance is i-0abc (BUILD_AWS), its node node-1.
# KUBECTL_BREAKS: every call fails from the node readiness wait on (the cluster
# became unreachable, or the context changed, in the middle of a build).
BUILD_KUBECTL = r"""#!/bin/bash
echo "kubectl $*" >> "$CALLS"
""" + KUBECTL_PRELUDE + r"""
if [ -n "${KUBECTL_BREAKS:-}" ]; then
  case "$*" in *"wait --for=condition=Ready"*) touch "$CALLS.kubectl-broken" ;; esac
  if [ -e "$CALLS.kubectl-broken" ]; then echo "Unable to connect to the server" >&2; exit 1; fi
fi
case "$*" in
  # name <tab> providerID, one builder node per line (NODES overrides).
  *"get nodes"*) printf '%b' "${NODES:-node-1\\taws:///eu-central-1a/i-0abc\\n}" ;;
  *"wait --for=condition=Ready"*)
    if [ -n "${NODE_NOT_READY:-}" ]; then echo "error: timed out waiting for the condition on nodes/node-1" >&2; exit 1; fi ;;
  *"apply -f -"*)
    cat >> "$CALLS"
    if [ -n "${APPLY_FAILS:-}" ]; then echo "Error from server (Forbidden): pods is forbidden" >&2; exit 1; fi ;;
  *"get ec2nodeclass"*"deviceName"*) printf '/dev/xvda\n/dev/xvdb\n' ;;
  *"wait --for=jsonpath"*)
    if [ -n "${PULL_HANGS:-}" ]; then sleep "$PULL_HANGS"; fi
    if [ -n "${PULL_FAILS:-}" ]; then echo "error: timed out waiting for the condition on pods/snapshot-puller-0" >&2; exit 1; fi ;;
  *"get pod "*"waiting"*) if [ -n "${PULL_FAILS:-}" ]; then echo "ImagePullBackOff"; fi ;;
  *"get pod "*"status.phase"*) echo "${POD_PHASE:-Pending}" ;;
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
    assert "Snapshot ID:  snap-0bead" in r.stdout
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
    assert "Snapshot ID:  snap-0bead" in r.stdout


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
    assert "Snapshot ID:  snap-0bead" in r.stdout
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


# --- kubectl only ever talks to the cluster named on the command line ---------

IMAGE = "docker.io/vllm/vllm-openai:v0.33.0"
PATCH = ("--reuse", "--patch-nodeclasses", "gpu-inference", IMAGE)


def _operator_kubeconfig(tmp_path):
    """A kubeconfig whose current context is some other cluster."""
    f = tmp_path / "operator-kubeconfig"
    f.write_text("current-context: another-cluster\n")
    return f


def _assert_own_kubeconfig(tmp_path, calls, operator):
    used = set((tmp_path / "calls.kubeconfig").read_text().split())
    assert len(used) == 1, used                      # one kubeconfig for every call
    own = used.pop()
    assert own not in ("<ambient>", str(operator))
    assert f"eks update-kubeconfig --name c1 --region eu-central-1 --kubeconfig {own}" in calls
    assert operator.read_text() == "current-context: another-cluster\n"   # untouched
    assert not os.path.exists(own)                   # removed on exit


def test_reuse_patches_the_named_cluster_not_the_current_context(tmp_path):
    # The GPU EC2NodeClasses have the same names in every environment: with the
    # operator's context on another cluster, that cluster's were patched.
    operator = _operator_kubeconfig(tmp_path)
    r, calls = _run(tmp_path, *PATCH, EXISTING_SNAPSHOT="snap-0abc123", KUBECONFIG=str(operator))
    assert r.returncode == 0, r.stderr
    assert "patch ec2nodeclass gpu-inference" in calls
    _assert_own_kubeconfig(tmp_path, calls, operator)


def test_build_drives_the_named_cluster_not_the_current_context(tmp_path):
    operator = _operator_kubeconfig(tmp_path)
    r, calls = _build(tmp_path, KUBECONFIG=str(operator))
    assert r.returncode == 0, r.stdout + r.stderr
    _assert_own_kubeconfig(tmp_path, calls, operator)


def test_build_stops_before_launching_when_the_cluster_is_unreachable(tmp_path):
    r, calls = _build(tmp_path, UNREACHABLE="1")
    assert r.returncode == 1
    assert "cannot reach cluster c1" in r.stderr and "i/o timeout" in r.stderr
    assert "run-instances" not in calls


def test_build_stops_before_launching_when_no_kubeconfig_can_be_written(tmp_path):
    r, calls = _build(tmp_path, KUBECONFIG_FAILS="1")
    assert r.returncode == 1
    assert "kubeconfig for cluster c1" in r.stderr and "No cluster found" in r.stderr
    assert "run-instances" not in calls and "kubectl" not in calls


def _path_without_kubectl(tmp_path):
    """A PATH with the fake aws and the system tools the script uses, no kubectl."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("bash", "mktemp", "sed", "awk", "cat", "rm", "tr", "grep", "seq", "sleep", "date"):
        found = shutil.which(tool)
        if found:
            (bin_dir / tool).symlink_to(found)
    (tmp_path / "kubectl").unlink()
    return f"{tmp_path}:{bin_dir}"


def test_build_stops_before_launching_when_kubectl_is_missing(tmp_path):
    e, calls = _build_env(tmp_path)
    e["PATH"] = _path_without_kubectl(tmp_path)
    r = subprocess.run(["bash", SCRIPT, "-r", "eu-central-1", "-n", "c1", *BUILD_ARGS],
                       capture_output=True, text=True, env=e, timeout=60)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "kubectl not found" in r.stderr
    assert "run-instances" not in calls.read_text()


def test_reuse_without_patching_needs_no_kubectl(tmp_path):
    r, calls = _run(tmp_path, "--reuse", IMAGE, EXISTING_SNAPSHOT="snap-0abc123")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines()[-1] == "snap-0abc123"
    assert "kubectl" not in calls and "update-kubeconfig" not in calls


# --- The node that is baked is the one this run launched ----------------------

STALE_NODE = r"stale-node\taws:///eu-central-1a/i-0abcdef\n"   # an earlier run's builder


def test_builder_node_is_the_one_of_the_launched_instance(tmp_path):
    # "First node with the builder label" could be a node left by an earlier
    # run: the image was pulled there, and the new instance's empty volume was
    # snapshotted.
    r, calls = _build(tmp_path, NODES=STALE_NODE + r"node-1\taws:///eu-central-1b/i-0abc\n")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "nodeName: node-1\n" in calls
    assert "wait --for=condition=Ready node/node-1 " in calls
    assert "stale-node" not in calls


def test_build_fails_when_only_another_runs_builder_node_is_there(tmp_path):
    r, calls = _build(tmp_path, NODES=STALE_NODE, NODE_JOIN_ATTEMPTS="2", NODE_JOIN_SECONDS="0")
    assert r.returncode == 1
    assert "i-0abc" in r.stderr and "did not join" in r.stderr
    assert "apply -f -" not in calls and "create-snapshot" not in calls
    assert TERMINATE in calls


# --- A snapshot is only made, and only tagged, for images that were baked -----

PRIVATE_IMAGE = "111122223333.dkr.ecr.eu-central-1.amazonaws.com/platform/trainer:v1"


def test_a_failed_image_pull_makes_no_snapshot(tmp_path):
    # It was a warning: the empty volume was snapshotted and tagged with the
    # image, and since reuse and Terraform find snapshots by that tag, every
    # later cluster in the account and region booted GPU nodes from it.
    r, calls = _build(tmp_path, args=("--reuse", "--patch-nodeclasses", "gpu-inference", IMAGE), PULL_FAILS="1")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "create-snapshot" not in calls
    assert "patch ec2nodeclass" not in calls
    assert TERMINATE in calls
    assert "vllm-openai:v0.33.0" in r.stderr and "ImagePullBackOff" in r.stderr
    assert "No snapshot was created" in r.stderr
    assert "snap-" not in r.stdout


def test_a_pod_that_started_counts_as_pulled(tmp_path):
    # The wait can time out on a pod that is already running: the image is there.
    r, calls = _build(tmp_path, PULL_FAILS="1", POD_PHASE="Running")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "create-snapshot" in calls


def test_images_tag_lists_only_what_was_baked(tmp_path):
    # An image skipped as "not built yet" was still in the tag: the snapshot was
    # then reused as if it held that image.
    r, calls = _build(tmp_path, args=("--reuse", IMAGE, PRIVATE_IMAGE), ECR_MISSING="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "skipping " + PRIVATE_IMAGE in r.stderr
    snapshot = calls[calls.index("create-snapshot"):]
    snapshot = snapshot[:snapshot.index("\naws ", 1)]
    assert "{Key=Images,Value=" + IMAGE + "}" in snapshot
    assert "platform/trainer" not in snapshot
    # ... and reuse looks for exactly that list.
    assert f"Name=tag:Images,Values={IMAGE} Name=volume-size" in calls
    assert "platform/trainer" not in r.stdout[r.stdout.index("Snapshot ID:"):]


def test_nothing_left_to_bake_fails_without_building(tmp_path):
    r, calls = _build(tmp_path, args=("--reuse", PRIVATE_IMAGE), ECR_MISSING="1")
    assert r.returncode == 1
    assert "nothing to bake" in r.stderr
    assert "run-instances" not in calls and "describe-snapshots" not in calls


# --- No silent exits; the snapshot wait matches what the script says ----------

def test_a_node_that_never_becomes_ready_is_reported(tmp_path):
    # `kubectl wait ... 2>/dev/null` under `set -e`: the script just stopped.
    r, calls = _build(tmp_path, NODE_NOT_READY="1")
    assert r.returncode == 1
    assert "node-1" in r.stderr and "did not become Ready within 120 seconds" in r.stderr
    assert "create-snapshot" not in calls and TERMINATE in calls


def test_a_puller_pod_that_cannot_be_created_is_reported(tmp_path):
    r, calls = _build(tmp_path, APPLY_FAILS="1")
    assert r.returncode == 1
    assert "could not create the puller pod" in r.stderr and IMAGE in r.stderr
    assert "create-snapshot" not in calls and TERMINATE in calls


def test_an_instance_that_does_not_stop_is_reported(tmp_path):
    r, calls = _build(tmp_path, STOP_FAILS="1")
    assert r.returncode == 1
    assert "could not stop builder instance i-0abc" in r.stderr
    assert "create-snapshot" not in calls and TERMINATE in calls


def test_no_cluster_name_is_reported(tmp_path):
    # Without -n the name comes from the kubectl context; with no usable
    # kubectl the script ended without a word.
    e, calls = _build_env(tmp_path, UNREACHABLE="1")
    r = subprocess.run(["bash", SCRIPT, "-r", "eu-central-1", IMAGE], capture_output=True, text=True, env=e, timeout=30)
    assert r.returncode == 1 and "Pass -n" in r.stderr
    assert "aws" not in calls.read_text()


def test_snapshot_wait_outlasts_one_waiter_timeout(tmp_path):
    # `aws ec2 wait snapshot-completed` gives up after 10 minutes; the script
    # said "2-5 min" and died there, leaving a snapshot that completed later.
    r, calls = _build(tmp_path, SNAPSHOT_WAIT_FAILS="2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert calls.count("wait snapshot-completed") == 3
    assert "Snapshot ID:  snap-0bead" in r.stdout
    assert "2-5 min" not in r.stdout
    assert "waiting up to 40 min" in r.stdout


def test_a_snapshot_still_pending_after_the_bounded_wait_is_left_to_complete(tmp_path):
    r, calls = _build(tmp_path, args=("--reuse", "--patch-nodeclasses", "gpu-inference", IMAGE),
                      SNAPSHOT_WAIT_FAILS="99", SNAPSHOT_WAIT_ROUNDS="3")
    assert r.returncode == 1
    assert calls.count("wait snapshot-completed") == 3   # bounded
    assert "snap-0bead" in r.stderr and "30 minutes" in r.stderr
    assert "next apply" in r.stderr
    assert "delete-snapshot" not in calls                # it will complete
    assert "patch ec2nodeclass" not in calls             # not usable yet
    assert TERMINATE in calls


def test_a_snapshot_that_failed_is_deleted(tmp_path):
    r, calls = _build(tmp_path, SNAPSHOT_WAIT_FAILS="99", SNAPSHOT_WAIT_ROUNDS="2", SNAPSHOT_STATE="error")
    assert r.returncode == 1
    assert calls.count("wait snapshot-completed") == 1   # no point waiting on a failed one
    assert "snap-0bead" in r.stderr and "failed" in r.stderr
    assert "delete-snapshot --snapshot-id snap-0bead" in calls
    assert TERMINATE in calls


# --- Two clusters building the same image at once -----------------------------

def test_a_snapshot_completed_by_another_build_is_used_instead_of_a_second_one(tmp_path):
    # Both found nothing, both built: two snapshots with the same lookup tags.
    r, calls = _build(tmp_path, args=("--reuse", "--patch-nodeclasses", "gpu-inference", IMAGE),
                      OTHER_BUILD_DONE_AFTER="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "create-snapshot" not in calls
    assert TERMINATE in calls
    assert r.stdout.strip().splitlines()[-1] == "snap-0c0de"
    patches = [line for line in calls.splitlines() if "patch ec2nodeclass" in line]
    assert len(patches) == 1 and '"value":"snap-0c0de"' in patches[0]
    # The second lookup is the last thing before the snapshot would be created.
    assert calls.rindex("describe-snapshots") > calls.index("wait instance-stopped")


def test_without_reuse_a_new_snapshot_is_always_made(tmp_path):
    r, calls = _build(tmp_path, args=(IMAGE,), OTHER_BUILD_DONE_AFTER="0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "create-snapshot" in calls and "tag:Images" not in calls.replace("Key=Images", "")
    assert "Snapshot ID:  snap-0bead" in r.stdout


# --- What the script prints at the end, and in its own comments ---------------

def _script():
    with open(SCRIPT, encoding="utf-8") as fh:
        return fh.read()


def test_a_run_from_terraform_does_not_tell_the_user_to_pin_the_snapshot(tmp_path):
    # "Next steps: 1. Add to tfvars: gpu_data_volume_snapshot_id = ..." was
    # printed in the apply output. Following it sets count = 0 on the automatic
    # path: a later vLLM version is never baked.
    r, _ = _build(tmp_path, args=("--reuse", "--patch-nodeclasses", "gpu-inference", IMAGE))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Snapshot ID:  snap-0bead" in r.stdout
    assert "Next steps" not in r.stdout and "gpu_data_volume_snapshot_id" not in r.stdout


def test_a_manual_run_says_pinning_is_optional_and_what_it_turns_off(tmp_path):
    r, _ = _build(tmp_path, args=(IMAGE,))
    assert r.returncode == 0, r.stdout + r.stderr
    steps = r.stdout[r.stdout.index("Next steps"):]
    assert 'gpu_data_volume_snapshot_id = "snap-0bead"' in steps
    assert "Optional" in steps and "Add to tfvars" not in steps
    assert "turns the automatic snapshot off" in steps and "vLLM version" in steps


def test_the_snapshot_id_is_the_last_line_of_a_build(tmp_path):
    # The header said so; the cleanup printed after it.
    for args in ((IMAGE,), ("--reuse", "--patch-nodeclasses", "gpu-inference", IMAGE)):
        r, _ = _build(tmp_path, args=args)
        assert r.stdout.strip().splitlines()[-1] == "snap-0bead", r.stdout
    assert "as the last line" in _script()


def test_retry_messages_state_the_real_delay(tmp_path):
    r, _ = _build(tmp_path, PROFILE_ERRORS="1", PROFILE_MISSING="1")
    assert r.stdout.count("retrying in 0s") == 2 and "retrying in 10s" not in r.stdout


def test_comments_and_generated_text_match_the_code():
    script = _script()
    assert '"snapshot-builder" = "true:NoExecute"' in script and "NoSchedule" not in script
    assert "Auto-generated by ops/image/create-data-volume-snapshot.sh" in script
    assert "ops/create-data-volume-snapshot.sh" not in script
    assert "v0.24.0" not in script


# --- Fast Snapshot Restore is billed until it is disabled ---------------------

def test_help_states_what_fast_snapshot_restore_costs(tmp_path):
    r, calls = _run(tmp_path, "--help")
    assert r.returncode == 0 and calls == ""
    text = " ".join(r.stdout.split())
    assert "billed per snapshot, per Availability Zone, per hour" in text
    assert "after the cluster is destroyed" in text
    assert "aws ec2 disable-fast-snapshot-restores" in text
    # Plain text: no comment markers, no shell code.
    assert not any(line.startswith("#") for line in r.stdout.splitlines()), r.stdout
    assert "set -euo pipefail" not in r.stdout


def test_enabling_fast_snapshot_restore_prints_how_to_disable_it(tmp_path):
    r, calls = _build(tmp_path, args=("--fsr", "eu-central-1a,eu-central-1b", IMAGE))
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("enable-fast-snapshot-restores --region eu-central-1 --source-snapshot-ids snap-0bead "
            "--availability-zones eu-central-1a eu-central-1b") in calls   # behaviour unchanged
    assert ("aws ec2 disable-fast-snapshot-restores --region eu-central-1 --source-snapshot-ids snap-0bead "
            "--availability-zones eu-central-1a eu-central-1b") in r.stdout
    assert "per hour" in r.stdout


def test_without_fsr_nothing_is_enabled(tmp_path):
    r, calls = _build(tmp_path)
    assert "fast-snapshot-restores" not in calls and "fast-snapshot-restores" not in r.stdout
