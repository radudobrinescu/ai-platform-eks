#!/usr/bin/env python3
"""Env-scoped orphan sweep — run by `platformctl down` after `terraform destroy`.

Terraform only destroys what it manages. It leaves behind resources created by
in-cluster controllers or by EKS/Karpenter/AWS itself:

  * the controller security groups (`k8s-traffic-*`, `eks-cluster-sg-*`) and any
    orphaned ENIs — these PIN THE VPC and make `destroy` fail with a
    DependencyViolation;
  * Karpenter EC2 instances (pin subnets), EC2 fleets, launch templates and
    instance-profiles;
  * the EKS-created KMS key and CloudWatch log groups;
  * orphaned NAT gateways, EBS volumes and Elastic IPs.

Left alone these block the teardown or quietly cost money. This sweep removes
them so `down` is truly hands-off with no lingering billable resources.

SAFETY: everything is scoped to the env's cluster name (`<resources_prefix>-<env>`)
and its VPC. It never acts on a resource that isn't clearly part of this cluster,
so it cannot touch another environment or unrelated project (even if that project
happens to share an `Environment` tag value). "Clearly part of this cluster" means
an exact match, never a substring: `ai-platform-dev` is contained in
`ai-platform-dev2`, `ai-platform-dev-eu` and `my-ai-platform-dev`, which are other
environments. A resource is this cluster's when

  * a cluster tag has exactly this cluster's name (see `_has_cluster_tag`), or
  * its name is exactly one this project gives it (the patterns below, each with
    the place the name comes from), or
  * it sits in this cluster's VPC, itself found by an exact tag.

A leftover that fits none of these is not touched and stays for the user to
remove by hand (for example a KMS key with a customised description, or an
instance profile named differently from the ones listed in `Sweeper.__init__`).
That costs a little; a neighbour's deleted resource cannot be restored.

Uses boto3 (already a platform dependency). Best-effort + idempotent: every
action is independent, errors are reported not fatal, and re-running is safe.
"""
import argparse
import re
import sys

import boto3
from botocore.exceptions import ClientError


class Sweeper:
    def __init__(self, env, cluster, region):
        self.env = env
        self.cluster = cluster
        self.region = region
        c = re.escape(cluster)
        # Names this project gives the cluster's resources. Each is matched in
        # full (re.fullmatch), so the name of another environment that merely
        # starts with or contains this cluster's name never fits.
        #  - launch template of the system node group: the EKS module names it
        #    after the node group, "<cluster>-criticaladdons" (30.cluster/main.tf),
        #    plus Terraform's unique name_prefix suffix (digits and hex).
        #    Karpenter's templates are matched by their karpenter.sh/discovery tag.
        self._launch_template_name = re.compile(rf"{c}-criticaladdons-[0-9a-f]+")
        #  - instance profiles: "<cluster>-soci-builder" (image-optimization.tf)
        #    and the ones Karpenter creates for the EC2NodeClass role,
        #    "<cluster>_<numeric hash>". "<cluster>-..." in general is NOT ours:
        #    "ai-platform-dev-" is also how "ai-platform-dev-eu-soci-builder" starts.
        self._instance_profile_name = re.compile(rf"{c}-soci-builder|{c}_[0-9]+")
        #  - address of a NAT gateway: the VPC module tags it "<vpc name>-<az id>"
        #    (10.networking names the VPC after the cluster and passes AZ ids such
        #    as "usw2-az1"), next to the provider's Environment default tag.
        self._nat_eip_name = re.compile(rf"{c}-[a-z0-9]+-az[0-9]+")
        #  - KMS key for the cluster's secrets: the EKS module's default
        #    description (30.cluster does not set kms_key_description).
        self._kms_description = f"{cluster} cluster encryption key"
        self.ec2 = boto3.client("ec2", region_name=region)
        self.elbv2 = boto3.client("elbv2", region_name=region)
        self.kms = boto3.client("kms", region_name=region)
        self.logs = boto3.client("logs", region_name=region)
        self.eks = boto3.client("eks", region_name=region)
        self.iam = boto3.client("iam")  # IAM is global
        self.actions = []

    def log(self, msg):
        print(f"  [sweep] {msg}", flush=True)
        self.actions.append(msg)

    def _err(self, ctx, e):
        code = e.response["Error"]["Code"] if isinstance(e, ClientError) else str(e)
        print(f"  [sweep] {ctx}: {code}", flush=True)

    # ---- helpers -----------------------------------------------------------
    def _has_cluster_tag(self, tags):
        """True if a tag list clearly ties the resource to THIS cluster. Every
        comparison is an exact one: a Name that only contains the cluster name
        (a sibling environment, "<cluster>-eu") does not count."""
        for t in tags or []:
            k, v = t.get("Key", ""), t.get("Value", "")
            if k == f"kubernetes.io/cluster/{self.cluster}":
                return True
            if k in ("eks:cluster-name", "aws:eks:cluster-name", "karpenter.sh/discovery", "cluster-name", "elbv2.k8s.aws/cluster") and v == self.cluster:
                return True
            if k == "Name" and v == self.cluster:
                return True
        return False

    def _is_nat_eip(self, tags: list) -> bool:
        """True for an address Terraform allocated for one of this VPC's NAT
        gateways: its exact Name AND this env's Environment tag. Neither alone
        is enough; an Environment value is shared by unrelated projects."""
        by_key = {t.get("Key", ""): t.get("Value", "") for t in tags or []}
        return (by_key.get("Environment") == self.env
                and self._nat_eip_name.fullmatch(by_key.get("Name", "")) is not None)

    def _is_cluster_instance_profile(self, name: str, path: str) -> bool:
        """True for an instance profile this cluster created: one of its exact
        names, or an IAM path with the cluster name as a whole segment
        (Karpenter: /karpenter/<region>/<cluster>/<nodeclass uid>/)."""
        return (self._instance_profile_name.fullmatch(name) is not None
                or self.cluster in path.strip("/").split("/"))

    def find_vpc(self):
        # Only a VPC tied to THIS cluster: its ownership tag, or the exact Name
        # Terraform gives it (10.networking: the VPC is named after the cluster).
        # Never by the Environment tag: this also runs after a destroy that
        # removed the env's VPC, and the next VPC in the region tagged
        # Environment=<env> belongs to someone else. clear_vpc_blockers
        # terminates and deletes everything inside the VPC returned here.
        for filt in (
            [{"Name": f"tag:kubernetes.io/cluster/{self.cluster}", "Values": ["owned", "shared"]}],
            [{"Name": "tag:Name", "Values": [self.cluster]}],
        ):
            try:
                vpcs = self.ec2.describe_vpcs(Filters=filt).get("Vpcs", [])
            except ClientError as e:
                self._err("find_vpc", e)
                continue
            if len(vpcs) > 1:
                # A tag is not unique. Do not guess which one is ours.
                ids = ", ".join(v["VpcId"] for v in vpcs)
                print(f"  [sweep] {len(vpcs)} VPCs match cluster {self.cluster} ({ids}); "
                      "not clearing any of them, remove the leftover one by hand", flush=True)
                return None
            if vpcs:
                return vpcs[0]["VpcId"]
        return None

    # ---- VPC blockers (what makes destroy fail) ----------------------------
    def clear_vpc_blockers(self, vpc):
        # 1. Terminate any instances (Karpenter orphans pin subnets)
        try:
            ids = [i["InstanceId"]
                   for r in self.ec2.describe_instances(Filters=[
                       {"Name": "vpc-id", "Values": [vpc]},
                       {"Name": "instance-state-name", "Values": ["running", "pending", "stopping", "stopped"]}]).get("Reservations", [])
                   for i in r["Instances"]]
            if ids:
                self.ec2.terminate_instances(InstanceIds=ids)
                self.log(f"terminated {len(ids)} instance(s): {', '.join(ids)}")
                self.ec2.get_waiter("instance_terminated").wait(
                    InstanceIds=ids, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        except ClientError as e:
            self._err("terminate instances", e)

        # 2. NAT gateways (billable) — delete then release their EIPs
        try:
            for n in self.ec2.describe_nat_gateways(Filter=[{"Name": "vpc-id", "Values": [vpc]}]).get("NatGateways", []):
                if n["State"] not in ("deleted", "deleting"):
                    self.ec2.delete_nat_gateway(NatGatewayId=n["NatGatewayId"])
                    self.log(f"deleted NAT gateway {n['NatGatewayId']}")
        except ClientError as e:
            self._err("delete NAT", e)

        # 3. Leftover available ENIs, BEFORE the security groups: an available
        #    ENI still references its SGs, so the SG delete fails while it exists.
        #    Seen in testing: the VPC CNI could not delete a node's secondary ENI
        #    during the drain ("currently in use"), the ENI kept the EKS cluster
        #    SG, and the SG (deleted first) then held the VPC delete for 20 min.
        try:
            for e in self.ec2.describe_network_interfaces(Filters=[
                    {"Name": "vpc-id", "Values": [vpc]},
                    {"Name": "status", "Values": ["available"]}]).get("NetworkInterfaces", []):
                try:
                    self.ec2.delete_network_interface(NetworkInterfaceId=e["NetworkInterfaceId"])
                    self.log(f"deleted ENI {e['NetworkInterfaceId']}")
                except ClientError as ex:
                    self._err(f"delete ENI {e['NetworkInterfaceId']}", ex)
        except ClientError as e:
            self._err("ENIs", e)

        # 4. Non-default security groups (controller-created; pin the VPC).
        #    Strip all rules first so inter-SG references don't block deletion.
        try:
            sgs = [s for s in self.ec2.describe_security_groups(
                Filters=[{"Name": "vpc-id", "Values": [vpc]}]).get("SecurityGroups", [])
                if s["GroupName"] != "default"]
            for s in sgs:
                if s.get("IpPermissions"):
                    try:
                        self.ec2.revoke_security_group_ingress(GroupId=s["GroupId"], IpPermissions=s["IpPermissions"])
                    except ClientError:
                        pass
                if s.get("IpPermissionsEgress"):
                    try:
                        self.ec2.revoke_security_group_egress(GroupId=s["GroupId"], IpPermissions=s["IpPermissionsEgress"])
                    except ClientError:
                        pass
            for s in sgs:
                try:
                    self.ec2.delete_security_group(GroupId=s["GroupId"])
                    self.log(f"deleted security group {s['GroupId']} ({s['GroupName']})")
                except ClientError as e:
                    self._err(f"delete SG {s['GroupId']}", e)
        except ClientError as e:
            self._err("security groups", e)

    # ---- cluster-scoped orphans (money + hygiene) --------------------------
    def clear_load_balancers(self, vpc):
        try:
            for lb in self.elbv2.describe_load_balancers().get("LoadBalancers", []):
                # Scope to THIS env's VPC in addition to the cluster tag. The
                # elbv2.k8s.aws/cluster tag value (the LB Controller's clusterName)
                # is only unique for EKS within an account+region, so pairing it
                # with the env's unique VPC id ensures we never delete an ALB that
                # merely shares the cluster name elsewhere in the account.
                if lb.get("VpcId") != vpc:
                    continue
                tags = self.elbv2.describe_tags(ResourceArns=[lb["LoadBalancerArn"]]).get("TagDescriptions", [])
                tl = tags[0].get("Tags", []) if tags else []
                if self._has_cluster_tag(tl):
                    self.elbv2.delete_load_balancer(LoadBalancerArn=lb["LoadBalancerArn"])
                    self.log(f"deleted load balancer {lb['LoadBalancerName']}")
        except ClientError as e:
            self._err("load balancers", e)

    def clear_fleets(self):
        try:
            fleets = self.ec2.describe_fleets().get("Fleets", [])
            ids = [f["FleetId"] for f in fleets
                   if f.get("FleetState") not in ("deleted", "deleted-terminating")
                   and self._has_cluster_tag(f.get("Tags", []))]
            if ids:
                self.ec2.delete_fleets(FleetIds=ids, TerminateInstances=True)
                self.log(f"deleted {len(ids)} EC2 fleet(s)")
        except ClientError as e:
            self._err("fleets", e)

    def clear_launch_templates(self):
        try:
            for lt in self.ec2.describe_launch_templates().get("LaunchTemplates", []):
                if self._launch_template_name.fullmatch(lt.get("LaunchTemplateName", "")):
                    self.ec2.delete_launch_template(LaunchTemplateId=lt["LaunchTemplateId"])
                    self.log(f"deleted launch template {lt['LaunchTemplateName']}")
                    continue
                tags = self.ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [lt["LaunchTemplateId"]]}]).get("Tags", [])
                if self._has_cluster_tag([{"Key": t["Key"], "Value": t["Value"]} for t in tags]):
                    self.ec2.delete_launch_template(LaunchTemplateId=lt["LaunchTemplateId"])
                    self.log(f"deleted launch template {lt['LaunchTemplateName']}")
        except ClientError as e:
            self._err("launch templates", e)

    def clear_volumes(self):
        try:
            for v in self.ec2.describe_volumes(Filters=[
                    {"Name": "status", "Values": ["available"]},
                    {"Name": f"tag:kubernetes.io/cluster/{self.cluster}", "Values": ["owned", "shared"]}]).get("Volumes", []):
                self.ec2.delete_volume(VolumeId=v["VolumeId"])
                self.log(f"deleted available EBS volume {v['VolumeId']}")
        except ClientError as e:
            self._err("volumes", e)

    def clear_eips(self):
        try:
            for a in self.ec2.describe_addresses().get("Addresses", []):
                if a.get("AssociationId"):
                    continue  # still in use
                if self._has_cluster_tag(a.get("Tags", [])) or self._is_nat_eip(a.get("Tags", [])):
                    self.ec2.release_address(AllocationId=a["AllocationId"])
                    self.log(f"released unassociated EIP {a.get('PublicIp')}")
        except ClientError as e:
            self._err("EIPs", e)

    def clear_kms(self):
        # Only schedule keys that are clearly this cluster's (the description is
        # exactly the one the EKS module writes) and still Enabled/customer-managed
        # — never touch a key that isn't ours. A key whose description only
        # mentions the cluster name, or was customised, is left for the user.
        try:
            paginator = self.kms.get_paginator("list_keys")
            for page in paginator.paginate():
                for k in page.get("Keys", []):
                    try:
                        md = self.kms.describe_key(KeyId=k["KeyId"])["KeyMetadata"]
                    except ClientError:
                        continue
                    if (md.get("KeyManager") == "CUSTOMER" and md.get("KeyState") == "Enabled"
                            and md.get("Description") == self._kms_description):
                        r = self.kms.schedule_key_deletion(KeyId=k["KeyId"], PendingWindowInDays=7)
                        self.log(f"scheduled KMS key {k['KeyId']} for deletion ({r.get('DeletionDate')})")
        except ClientError as e:
            self._err("KMS", e)

    def clear_instance_profiles(self):
        try:
            paginator = self.iam.get_paginator("list_instance_profiles")
            for page in paginator.paginate():
                for ip in page.get("InstanceProfiles", []):
                    name = ip["InstanceProfileName"]
                    # Anything else that carries the cluster name (a profile
                    # AWS made for a node group, a hand-made one) is left alone.
                    if not self._is_cluster_instance_profile(name, ip.get("Path", "")):
                        continue
                    try:
                        for role in ip.get("Roles", []):
                            self.iam.remove_role_from_instance_profile(InstanceProfileName=name, RoleName=role["RoleName"])
                        self.iam.delete_instance_profile(InstanceProfileName=name)
                        self.log(f"deleted instance-profile {name}")
                    except ClientError as e:
                        self._err(f"instance-profile {name}", e)
        except ClientError as e:
            self._err("instance-profiles", e)

    def clear_log_groups(self):
        try:
            for prefix in (f"/aws/eks/{self.cluster}/", f"/aws/containerinsights/{self.cluster}/"):
                for lg in self.logs.describe_log_groups(logGroupNamePrefix=prefix).get("logGroups", []):
                    self.logs.delete_log_group(logGroupName=lg["logGroupName"])
                    self.log(f"deleted log group {lg['logGroupName']}")
        except ClientError as e:
            self._err("log groups", e)

    def cluster_alive(self):
        """True unless EKS says the cluster does not exist. Any other answer
        (throttling, access denied) counts as alive: the callers skip
        destructive steps on a live cluster, so unknown must mean "keep"."""
        try:
            self.eks.describe_cluster(name=self.cluster)
            return True
        except ClientError as e:
            return e.response["Error"]["Code"] != "ResourceNotFoundException"

    # ---- orchestration -----------------------------------------------------
    def run(self):
        print(f"  [sweep] scanning for '{self.env}' orphans (cluster {self.cluster}, region {self.region})", flush=True)
        vpc = self.find_vpc()
        if vpc:
            print(f"  [sweep] VPC {vpc} still present — clearing its dependencies so destroy can complete", flush=True)
            self.clear_load_balancers(vpc)
            self.clear_vpc_blockers(vpc)
        # cluster-scoped orphans (safe whether or not the VPC is gone)
        self.clear_fleets()
        self.clear_launch_templates()
        self.clear_volumes()
        self.clear_eips()
        # `down` also sweeps between two destroy attempts, while the cluster may
        # still exist. Scheduling its secrets key for deletion then leaves the
        # cluster unable to decrypt secrets or accept config updates, so a later
        # destroy cannot recover it. Keys and log groups wait until it is gone.
        if self.cluster_alive():
            print(f"  [sweep] cluster {self.cluster} still exists; keeping its KMS key and log groups", flush=True)
        else:
            self.clear_kms()
            self.clear_log_groups()
        self.clear_instance_profiles()
        if self.actions:
            print(f"  [sweep] removed {len(self.actions)} orphaned resource(s).", flush=True)
        else:
            print("  [sweep] no orphans found — clean.", flush=True)
        return vpc is not None


def main():
    ap = argparse.ArgumentParser(description="Env-scoped post-teardown orphan sweep.")
    ap.add_argument("--env", required=True)
    ap.add_argument("--cluster", required=True, help="EKS cluster name (<resources_prefix>-<env>)")
    ap.add_argument("--region", required=True)
    args = ap.parse_args()
    # platformctl derives the cluster name from the tfvars and falls back to the
    # bare env name when it cannot read resources_prefix. Terraform never names
    # a cluster that way, so a name that is not "<prefix>-<env>" is not this
    # env's cluster, and sweeping it would be matching by env name alone.
    suffix = f"-{args.env}"
    if not args.cluster.endswith(suffix) or len(args.cluster) == len(suffix):
        print(f"  [sweep] cluster name '{args.cluster}' is not <resources_prefix>-{args.env}; "
              "skipping the orphan sweep (nothing deleted)", flush=True)
        return 0
    try:
        Sweeper(args.env, args.cluster, args.region).run()
    except Exception as e:  # never let the sweep abort the teardown
        print(f"  [sweep] non-fatal error: {e}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
