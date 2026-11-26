"""Scoping-safety tests for the post-teardown orphan sweep.

sweep_env deletes real AWS resources, so its one non-negotiable invariant is:
it must NEVER act on a resource that isn't clearly part of THIS cluster, even
when another environment/project shares an `Environment` tag value. These tests
drive each destructive path with fake AWS clients and assert exactly which
resources get mutated — an over-broad match here is a data-loss bug.
"""

import pytest

import sweep_env

CLUSTER = "ai-platform-dev"
ENV = "dev"
REGION = "us-west-2"

# Other environments whose cluster name CONTAINS this one. A substring or
# "<cluster>-" prefix match treats their resources as ours; none of them may
# ever be touched when sweeping CLUSTER.
SIBLINGS = [f"{CLUSTER}2", f"{CLUSTER}-eu", f"my-{CLUSTER}"]
# Terraform's unique suffix after a name_prefix (26 characters, digits + hex).
TF_SUFFIX = "20261126101500000000000001"


@pytest.fixture
def sweeper(monkeypatch):
    # Don't build real boto3 clients; the test overrides the ones it exercises.
    monkeypatch.setattr(sweep_env.boto3, "client", lambda *a, **k: object())
    return sweep_env.Sweeper(ENV, CLUSTER, REGION)


# --------------------------------------------------------------------------- #
# _has_cluster_tag — the core "is this ours?" predicate                       #
# --------------------------------------------------------------------------- #
class TestHasClusterTag:
    def test_true_for_cluster_ownership_tags(self, sweeper):
        assert sweeper._has_cluster_tag([{"Key": f"kubernetes.io/cluster/{CLUSTER}", "Value": "owned"}])
        assert sweeper._has_cluster_tag([{"Key": "eks:cluster-name", "Value": CLUSTER}])
        assert sweeper._has_cluster_tag([{"Key": "karpenter.sh/discovery", "Value": CLUSTER}])
        assert sweeper._has_cluster_tag([{"Key": "Name", "Value": CLUSTER}])

    @pytest.mark.parametrize("sibling", SIBLINGS)
    def test_name_of_a_sibling_environment_is_not_ours(self, sweeper, sibling):
        # `ai-platform-dev` is a substring of `ai-platform-dev2`, and a prefix
        # of `ai-platform-dev-eu` up to and including the dash.
        assert not sweeper._has_cluster_tag([{"Key": "Name", "Value": sibling}])
        assert not sweeper._has_cluster_tag([{"Key": "Name", "Value": f"{sibling}-nat"}])
        assert not sweeper._has_cluster_tag([{"Key": "eks:cluster-name", "Value": sibling}])
        assert not sweeper._has_cluster_tag([{"Key": f"kubernetes.io/cluster/{sibling}", "Value": "owned"}])

    def test_false_without_a_clear_cluster_tie(self, sweeper):
        assert not sweeper._has_cluster_tag([])
        assert not sweeper._has_cluster_tag(None)
        # Different cluster value must not match.
        assert not sweeper._has_cluster_tag([{"Key": "eks:cluster-name", "Value": "other-cluster"}])
        assert not sweeper._has_cluster_tag([{"Key": "kubernetes.io/cluster/other", "Value": "owned"}])

    def test_environment_tag_alone_is_not_sufficient(self, sweeper):
        # Critical: a shared `Environment=dev` tag must NOT authorize destruction
        # via the tag predicate — otherwise the sweep could hit another project.
        assert not sweeper._has_cluster_tag([{"Key": "Environment", "Value": ENV}])


# --------------------------------------------------------------------------- #
# clear_kms — only this cluster's customer-managed, enabled keys               #
# --------------------------------------------------------------------------- #
class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_):
        return iter(self._pages)


class FakeKms:
    def __init__(self, metadata_by_id):
        self._meta = metadata_by_id
        self.scheduled = []

    def get_paginator(self, name):
        assert name == "list_keys"
        return _Paginator([{"Keys": [{"KeyId": k} for k in self._meta]}])

    def describe_key(self, KeyId):
        return {"KeyMetadata": self._meta[KeyId]}

    def schedule_key_deletion(self, KeyId, PendingWindowInDays):
        self.scheduled.append(KeyId)
        return {"DeletionDate": "2026-01-01"}


class TestClearKms:
    def test_schedules_only_in_scope_customer_enabled_keys(self, sweeper):
        sweeper.kms = FakeKms({
            "in-scope":  {"KeyManager": "CUSTOMER", "KeyState": "Enabled",
                          "Description": f"{CLUSTER} cluster encryption key"},
            "aws-mgd":   {"KeyManager": "AWS", "KeyState": "Enabled",
                          "Description": f"{CLUSTER}"},
            "disabled":  {"KeyManager": "CUSTOMER", "KeyState": "Disabled",
                          "Description": f"{CLUSTER}"},
            "other":     {"KeyManager": "CUSTOMER", "KeyState": "Enabled",
                          "Description": "some other cluster"},
        })
        sweeper.clear_kms()
        assert sweeper.kms.scheduled == ["in-scope"]

    def test_never_schedules_a_sibling_environments_key(self, sweeper):
        # The description is the EKS module's default, "<cluster> cluster
        # encryption key"; a sibling's contains this cluster's name.
        keys = {"own": {"KeyManager": "CUSTOMER", "KeyState": "Enabled",
                        "Description": f"{CLUSTER} cluster encryption key"},
                "mentions": {"KeyManager": "CUSTOMER", "KeyState": "Enabled",
                             "Description": f"backup key for {CLUSTER}"}}
        for sibling in SIBLINGS:
            keys[sibling] = {"KeyManager": "CUSTOMER", "KeyState": "Enabled",
                             "Description": f"{sibling} cluster encryption key"}
        sweeper.kms = FakeKms(keys)
        sweeper.clear_kms()
        assert sweeper.kms.scheduled == ["own"]


# --------------------------------------------------------------------------- #
# clear_eips — release only unassociated, in-scope addresses                   #
# --------------------------------------------------------------------------- #
class FakeEipEc2:
    def __init__(self, addresses):
        self._addresses = addresses
        self.released = []

    def describe_addresses(self):
        return {"Addresses": self._addresses}

    def release_address(self, AllocationId):
        self.released.append(AllocationId)


def _nat_eip_tags(cluster, env, az="usw2-az1"):
    """Tags Terraform puts on a NAT gateway address (10.networking: the vpc
    module's Name "<vpc name>-<az id>" plus the provider's default tags)."""
    return [{"Key": "Name", "Value": f"{cluster}-{az}"}, {"Key": "Environment", "Value": env}]


class TestClearEips:
    def test_scoping_and_in_use_protection(self, sweeper):
        sweeper.ec2 = FakeEipEc2([
            # in use (associated) even though in-scope -> must be left alone
            {"AllocationId": "a-assoc", "AssociationId": "eipassoc-1",
             "Tags": _nat_eip_tags(CLUSTER, ENV)},
            # unassociated + cluster tag -> release
            {"AllocationId": "a-cluster", "PublicIp": "1.1.1.1",
             "Tags": [{"Key": f"kubernetes.io/cluster/{CLUSTER}", "Value": "owned"}]},
            # unassociated + this VPC's NAT address, as Terraform tags it -> release
            {"AllocationId": "a-nat", "PublicIp": "4.4.4.4",
             "Tags": _nat_eip_tags(CLUSTER, ENV)},
            # unassociated + only a shared Environment value: another project's
            # address, must be left alone
            {"AllocationId": "a-env", "PublicIp": "2.2.2.2",
             "Tags": [{"Key": "Environment", "Value": ENV}]},
            # unassociated + unrelated -> leave alone
            {"AllocationId": "a-other", "PublicIp": "3.3.3.3",
             "Tags": [{"Key": "Environment", "Value": "prod"}]},
            # no tags at all -> leave alone
            {"AllocationId": "a-untagged", "PublicIp": "5.5.5.5"},
        ])
        sweeper.clear_eips()
        assert sorted(sweeper.ec2.released) == ["a-cluster", "a-nat"]

    def test_never_releases_a_sibling_environments_nat_address(self, sweeper):
        addresses = [{"AllocationId": "own", "PublicIp": "1.1.1.1", "Tags": _nat_eip_tags(CLUSTER, ENV)}]
        for n, sibling in enumerate(SIBLINGS):
            # my-ai-platform-dev is prefix "my-ai-platform" + the SAME env name.
            for env in (ENV, sibling.split("-")[-1]):
                addresses.append({"AllocationId": f"{sibling}/{env}", "PublicIp": f"9.9.9.{n}",
                                  "Tags": _nat_eip_tags(sibling, env)})
        sweeper.ec2 = FakeEipEc2(addresses)
        sweeper.clear_eips()
        assert sweeper.ec2.released == ["own"]


# --------------------------------------------------------------------------- #
# clear_launch_templates — name match OR cluster tag                           #
# --------------------------------------------------------------------------- #
class FakeLtEc2:
    def __init__(self, templates, tags_by_id):
        self._templates = templates
        self._tags_by_id = tags_by_id
        self.deleted = []

    def describe_launch_templates(self):
        return {"LaunchTemplates": self._templates}

    def describe_tags(self, Filters):
        rid = Filters[0]["Values"][0]
        return {"Tags": self._tags_by_id.get(rid, [])}

    def delete_launch_template(self, LaunchTemplateId):
        self.deleted.append(LaunchTemplateId)


class TestClearLaunchTemplates:
    def test_deletes_by_name_or_tag_only(self, sweeper):
        sweeper.ec2 = FakeLtEc2(
            templates=[
                {"LaunchTemplateId": "lt-name", "LaunchTemplateName": f"{CLUSTER}-criticaladdons-{TF_SUFFIX}"},
                {"LaunchTemplateId": "lt-tag", "LaunchTemplateName": "karpenter-abc"},
                {"LaunchTemplateId": "lt-other", "LaunchTemplateName": "unrelated"},
            ],
            tags_by_id={
                "lt-tag": [{"Key": "eks:cluster-name", "Value": CLUSTER}],
                "lt-other": [{"Key": "eks:cluster-name", "Value": "other-cluster"}],
            },
        )
        sweeper.clear_launch_templates()
        assert sorted(sweeper.ec2.deleted) == ["lt-name", "lt-tag"]

    def test_never_deletes_a_sibling_environments_template(self, sweeper):
        templates = [{"LaunchTemplateId": "own", "LaunchTemplateName": f"{CLUSTER}-criticaladdons-{TF_SUFFIX}"},
                     {"LaunchTemplateId": "own-karpenter", "LaunchTemplateName": "karpenter.k8s.aws/123"},
                     # names this cluster does not create, although they contain its name
                     {"LaunchTemplateId": "mentions", "LaunchTemplateName": f"{CLUSTER}-workers"},
                     {"LaunchTemplateId": "nested", "LaunchTemplateName": f"{CLUSTER}-criticaladdons-criticaladdons-{TF_SUFFIX}"}]
        tags = {"own-karpenter": [{"Key": "karpenter.sh/discovery", "Value": CLUSTER}]}
        for sibling in SIBLINGS:
            templates.append({"LaunchTemplateId": f"{sibling}/mng",
                              "LaunchTemplateName": f"{sibling}-criticaladdons-{TF_SUFFIX}"})
            templates.append({"LaunchTemplateId": f"{sibling}/karpenter", "LaunchTemplateName": "karpenter.k8s.aws/456"})
            tags[f"{sibling}/karpenter"] = [{"Key": "karpenter.sh/discovery", "Value": sibling},
                                            {"Key": "Name", "Value": f"{sibling}-node"}]
        sweeper.ec2 = FakeLtEc2(templates=templates, tags_by_id=tags)
        sweeper.clear_launch_templates()
        assert sorted(sweeper.ec2.deleted) == ["own", "own-karpenter"]


# --------------------------------------------------------------------------- #
# clear_instance_profiles — name or path scoped                                #
# --------------------------------------------------------------------------- #
class FakeIam:
    def __init__(self, profiles):
        self._profiles = profiles
        self.deleted = []
        self.removed_roles = []

    def get_paginator(self, name):
        assert name == "list_instance_profiles"
        return _Paginator([{"InstanceProfiles": self._profiles}])

    def remove_role_from_instance_profile(self, InstanceProfileName, RoleName):
        self.removed_roles.append((InstanceProfileName, RoleName))

    def delete_instance_profile(self, InstanceProfileName):
        self.deleted.append(InstanceProfileName)


class TestClearInstanceProfiles:
    def test_scopes_by_name_or_path(self, sweeper):
        sweeper.iam = FakeIam([
            {"InstanceProfileName": f"{CLUSTER}-soci-builder", "Path": "/", "Roles": [{"RoleName": "r1"}]},
            {"InstanceProfileName": "karpenter", "Path": f"/eks/{CLUSTER}/", "Roles": []},
            {"InstanceProfileName": "unrelated", "Path": "/", "Roles": [{"RoleName": "r2"}]},
        ])
        sweeper.clear_instance_profiles()
        assert sorted(sweeper.iam.deleted) == [f"{CLUSTER}-soci-builder", "karpenter"]
        # Roles are detached only from in-scope profiles before deletion.
        assert sweeper.iam.removed_roles == [(f"{CLUSTER}-soci-builder", "r1")]

    def test_never_deletes_a_sibling_environments_profile(self, sweeper):
        own = [f"{CLUSTER}-soci-builder",       # Terraform: image-optimization.tf
               f"{CLUSTER}_1234567890123456789"]  # Karpenter: <cluster>_<hash>
        profiles = [{"InstanceProfileName": n, "Path": "/", "Roles": []} for n in own]
        profiles.append({"InstanceProfileName": "by-path", "Path": f"/karpenter/{REGION}/{CLUSTER}/uid-1/", "Roles": []})
        # a name this cluster does not create, although it starts with its name
        profiles.append({"InstanceProfileName": f"{CLUSTER}-node", "Path": "/", "Roles": [{"RoleName": "keep"}]})
        for sibling in SIBLINGS:
            for name in (f"{sibling}-soci-builder", f"{sibling}_1234567890123456789", f"KarpenterNode-{sibling}"):
                profiles.append({"InstanceProfileName": name, "Path": "/", "Roles": [{"RoleName": "keep"}]})
            profiles.append({"InstanceProfileName": f"path-of-{sibling}",
                             "Path": f"/karpenter/{REGION}/{sibling}/uid-2/", "Roles": [{"RoleName": "keep"}]})
        # an underscore is a valid cluster-name character
        profiles.append({"InstanceProfileName": f"{CLUSTER}_2_1234567890123456789", "Path": "/", "Roles": []})
        sweeper.iam = FakeIam(profiles)
        sweeper.clear_instance_profiles()
        assert sorted(sweeper.iam.deleted) == sorted(own + ["by-path"])
        assert sweeper.iam.removed_roles == []


# --------------------------------------------------------------------------- #
# run — never schedule the KMS key / log groups of a cluster that still exists #
# --------------------------------------------------------------------------- #
from botocore.exceptions import ClientError  # noqa: E402


class FakeEks:
    def __init__(self, code=None):
        self.code = code

    def describe_cluster(self, name):
        if self.code:
            raise ClientError({"Error": {"Code": self.code, "Message": "x"}}, "DescribeCluster")
        return {"cluster": {"name": name, "status": "ACTIVE"}}


class TestRunOnLiveCluster:
    def _run(self, sweeper, monkeypatch, eks):
        called = []
        sweeper.eks = eks
        monkeypatch.setattr(sweeper, "find_vpc", lambda: None)
        for m in ("clear_fleets", "clear_launch_templates", "clear_volumes", "clear_eips",
                  "clear_kms", "clear_instance_profiles", "clear_log_groups"):
            monkeypatch.setattr(sweeper, m, lambda m=m: called.append(m))
        sweeper.run()
        return called

    def test_live_cluster_keeps_kms_key_and_log_groups(self, sweeper, monkeypatch):
        called = self._run(sweeper, monkeypatch, FakeEks())
        assert "clear_kms" not in called and "clear_log_groups" not in called
        assert "clear_eips" in called

    def test_unknown_state_counts_as_alive(self, sweeper, monkeypatch):
        called = self._run(sweeper, monkeypatch, FakeEks("AccessDeniedException"))
        assert "clear_kms" not in called

    def test_deleted_cluster_clears_them(self, sweeper, monkeypatch):
        called = self._run(sweeper, monkeypatch, FakeEks("ResourceNotFoundException"))
        assert "clear_kms" in called and "clear_log_groups" in called


# --------------------------------------------------------------------------- #
# find_vpc — only a VPC tied to THIS cluster                                   #
# --------------------------------------------------------------------------- #
def _tag_dict(resource):
    return {t["Key"]: t["Value"] for t in resource.get("Tags", [])}


class FakeVpcEc2:
    """describe_vpcs with EC2's `tag:<key>` filter semantics (all must match)."""

    def __init__(self, vpcs):
        self._vpcs = vpcs

    def describe_vpcs(self, Filters):
        def matches(vpc):
            tags = _tag_dict(vpc)
            return all(f["Name"].startswith("tag:") and tags.get(f["Name"][4:]) in f["Values"] for f in Filters)
        return {"Vpcs": [v for v in self._vpcs if matches(v)]}


def _vpc(vpc_id, **tags):
    return {"VpcId": vpc_id, "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}


# What Terraform's 10.networking puts on the env's own VPC.
OWN_VPC = _vpc("vpc-own", Name=CLUSTER, Environment=ENV)
# Another project (or a fork with a different resources_prefix) in the same
# region that happens to use the same Environment value.
FOREIGN_VPC = _vpc("vpc-foreign", Name="billing-dev", Environment=ENV)


class TestFindVpc:
    def test_finds_this_clusters_vpc_by_exact_name(self, sweeper):
        sweeper.ec2 = FakeVpcEc2([FOREIGN_VPC, OWN_VPC])
        assert sweeper.find_vpc() == "vpc-own"

    def test_finds_this_clusters_vpc_by_cluster_tag(self, sweeper):
        sweeper.ec2 = FakeVpcEc2([FOREIGN_VPC, {"VpcId": "vpc-tagged", "Tags": [
            {"Key": f"kubernetes.io/cluster/{CLUSTER}", "Value": "shared"}]}])
        assert sweeper.find_vpc() == "vpc-tagged"

    def test_environment_tag_alone_never_selects_a_vpc(self, sweeper):
        # After a successful destroy the env's own VPC is gone. A VPC that only
        # shares the Environment value belongs to someone else.
        sweeper.ec2 = FakeVpcEc2([FOREIGN_VPC])
        assert sweeper.find_vpc() is None

    @pytest.mark.parametrize("sibling", SIBLINGS)
    def test_sibling_environment_vpc_is_never_selected(self, sweeper, sibling):
        sweeper.ec2 = FakeVpcEc2([
            _vpc("vpc-sibling", Name=sibling, Environment=ENV),
            {"VpcId": "vpc-sibling-tagged", "Tags": [
                {"Key": f"kubernetes.io/cluster/{sibling}", "Value": "owned"}, {"Key": "Environment", "Value": ENV}]},
        ])
        assert sweeper.find_vpc() is None

    def test_two_vpcs_with_the_cluster_name_are_ambiguous_and_refused(self, sweeper, capsys):
        # A Name tag is not unique. Clearing "the first one" could empty a VPC
        # that is not this environment's, so the sweep picks neither.
        sweeper.ec2 = FakeVpcEc2([_vpc("vpc-a", Name=CLUSTER), _vpc("vpc-b", Name=CLUSTER)])
        assert sweeper.find_vpc() is None
        assert "vpc-a" in capsys.readouterr().out


class TestRunAfterSuccessfulDestroy:
    """`down` sweeps after a clean destroy too: this env's VPC no longer exists."""

    def _run(self, sweeper, monkeypatch, vpcs):
        vpc_calls = []
        sweeper.ec2 = FakeVpcEc2(vpcs)
        sweeper.eks = FakeEks("ResourceNotFoundException")
        monkeypatch.setattr(sweeper, "clear_vpc_blockers", lambda vpc: vpc_calls.append(("blockers", vpc)))
        monkeypatch.setattr(sweeper, "clear_load_balancers", lambda vpc: vpc_calls.append(("lbs", vpc)))
        for m in ("clear_fleets", "clear_launch_templates", "clear_volumes", "clear_eips",
                  "clear_kms", "clear_instance_profiles", "clear_log_groups"):
            monkeypatch.setattr(sweeper, m, lambda: None)
        return sweeper.run(), vpc_calls

    def test_foreign_vpc_sharing_the_environment_tag_is_left_alone(self, sweeper, monkeypatch):
        found, vpc_calls = self._run(sweeper, monkeypatch, [FOREIGN_VPC])
        assert vpc_calls == []
        assert found is False

    def test_own_vpc_left_by_a_failed_destroy_is_cleared(self, sweeper, monkeypatch):
        found, vpc_calls = self._run(sweeper, monkeypatch, [FOREIGN_VPC, OWN_VPC])
        assert vpc_calls == [("lbs", "vpc-own"), ("blockers", "vpc-own")]
        assert found is True


# --------------------------------------------------------------------------- #
# clear_vpc_blockers — everything it removes is inside the VPC it was given    #
# --------------------------------------------------------------------------- #
class _Waiter:
    def wait(self, **_):
        return None


class FakeBlockerEc2:
    """Two VPCs with the same kinds of resources. Every describe call must name
    a vpc-id (a call without one raises), and only that VPC's resources exist
    for it."""

    def __init__(self):
        self.instances = {"vpc-own": ["i-own-1", "i-own-2"], "vpc-foreign": ["i-foreign"]}
        self.nats = {"vpc-own": [("nat-own", "available"), ("nat-own-gone", "deleted")], "vpc-foreign": [("nat-foreign", "available")]}
        self.enis = {"vpc-own": ["eni-own"], "vpc-foreign": ["eni-foreign"]}
        self.groups = {
            "vpc-own": [{"GroupId": "sg-own-default", "GroupName": "default"},
                        {"GroupId": "sg-own-cluster", "GroupName": f"eks-cluster-sg-{CLUSTER}-1",
                         "IpPermissions": [{"IpProtocol": "-1"}], "IpPermissionsEgress": [{"IpProtocol": "-1"}]},
                        {"GroupId": "sg-own-traffic", "GroupName": "k8s-traffic-aiplatformdev-1"}],
            "vpc-foreign": [{"GroupId": "sg-foreign", "GroupName": "app", "IpPermissions": [{"IpProtocol": "tcp"}]}],
        }
        self.mutated = []

    @staticmethod
    def _vpc(filters):
        return next(f["Values"][0] for f in filters if f["Name"] == "vpc-id")

    def describe_instances(self, Filters):
        return {"Reservations": [{"Instances": [{"InstanceId": i} for i in self.instances[self._vpc(Filters)]]}]}

    def terminate_instances(self, InstanceIds):
        self.mutated += [("terminate", i) for i in InstanceIds]

    def get_waiter(self, name):
        assert name == "instance_terminated"
        return _Waiter()

    def describe_nat_gateways(self, Filter):
        return {"NatGateways": [{"NatGatewayId": n, "State": s} for n, s in self.nats[self._vpc(Filter)]]}

    def delete_nat_gateway(self, NatGatewayId):
        self.mutated.append(("nat", NatGatewayId))

    def describe_network_interfaces(self, Filters):
        assert {"Name": "status", "Values": ["available"]} in Filters
        return {"NetworkInterfaces": [{"NetworkInterfaceId": e} for e in self.enis[self._vpc(Filters)]]}

    def delete_network_interface(self, NetworkInterfaceId):
        self.mutated.append(("eni", NetworkInterfaceId))

    def describe_security_groups(self, Filters):
        return {"SecurityGroups": self.groups[self._vpc(Filters)]}

    def revoke_security_group_ingress(self, GroupId, IpPermissions):
        self.mutated.append(("revoke-ingress", GroupId))

    def revoke_security_group_egress(self, GroupId, IpPermissions):
        self.mutated.append(("revoke-egress", GroupId))

    def delete_security_group(self, GroupId):
        self.mutated.append(("sg", GroupId))


class TestClearVpcBlockers:
    def test_clears_this_vpc_and_nothing_outside_it(self, sweeper):
        sweeper.ec2 = FakeBlockerEc2()
        sweeper.clear_vpc_blockers("vpc-own")
        assert sweeper.ec2.mutated == [
            ("terminate", "i-own-1"), ("terminate", "i-own-2"),
            ("nat", "nat-own"),
            ("eni", "eni-own"),
            ("revoke-ingress", "sg-own-cluster"), ("revoke-egress", "sg-own-cluster"),
            ("sg", "sg-own-cluster"), ("sg", "sg-own-traffic"),
        ]
        # The default group cannot be deleted and is never touched; nothing of
        # the other VPC is.
        assert not [m for m in sweeper.ec2.mutated if "foreign" in m[1] or "default" in m[1]]


# --------------------------------------------------------------------------- #
# the remaining deletion paths: exact cluster tags / a delimited name prefix   #
# --------------------------------------------------------------------------- #
class FakeFleetEc2:
    def __init__(self, fleets):
        self._fleets = fleets
        self.deleted = []

    def describe_fleets(self):
        return {"Fleets": self._fleets}

    def delete_fleets(self, FleetIds, TerminateInstances):
        self.deleted += FleetIds


class TestClearFleets:
    def test_only_fleets_tagged_for_this_cluster(self, sweeper):
        fleets = [{"FleetId": "own", "FleetState": "active", "Tags": [{"Key": "karpenter.sh/discovery", "Value": CLUSTER}]},
                  {"FleetId": "gone", "FleetState": "deleted", "Tags": [{"Key": "karpenter.sh/discovery", "Value": CLUSTER}]},
                  {"FleetId": "env-only", "FleetState": "active", "Tags": [{"Key": "Environment", "Value": ENV}]}]
        for sibling in SIBLINGS:
            fleets.append({"FleetId": sibling, "FleetState": "active", "Tags": [
                {"Key": "karpenter.sh/discovery", "Value": sibling}, {"Key": "Name", "Value": f"{sibling}-fleet"}]})
        sweeper.ec2 = FakeFleetEc2(fleets)
        sweeper.clear_fleets()
        assert sweeper.ec2.deleted == ["own"]


class FakeElbv2:
    def __init__(self, lbs, tags_by_arn):
        self._lbs = lbs
        self._tags = tags_by_arn
        self.deleted = []

    def describe_load_balancers(self):
        return {"LoadBalancers": self._lbs}

    def describe_tags(self, ResourceArns):
        return {"TagDescriptions": [{"Tags": self._tags.get(ResourceArns[0], [])}]}

    def delete_load_balancer(self, LoadBalancerArn):
        self.deleted.append(LoadBalancerArn)


class TestClearLoadBalancers:
    def test_needs_both_this_vpc_and_this_clusters_tag(self, sweeper):
        def lb(arn, vpc):
            return {"LoadBalancerArn": arn, "LoadBalancerName": arn, "VpcId": vpc}
        ours = [{"Key": "elbv2.k8s.aws/cluster", "Value": CLUSTER}]
        sweeper.elbv2 = FakeElbv2(
            [lb("own", "vpc-own"), lb("same-tag-other-vpc", "vpc-foreign"), lb("untagged", "vpc-own"),
             lb("sibling-name", "vpc-own"), lb("sibling-tag", "vpc-own")],
            {"own": ours, "same-tag-other-vpc": ours,
             "sibling-name": [{"Key": "Name", "Value": f"{CLUSTER}-eu-alb"}],
             "sibling-tag": [{"Key": "elbv2.k8s.aws/cluster", "Value": f"{CLUSTER}2"}]})
        sweeper.clear_load_balancers("vpc-own")
        assert sweeper.elbv2.deleted == ["own"]


class FakeVolumeEc2:
    def __init__(self, volumes):
        self._volumes = volumes
        self.deleted = []

    def describe_volumes(self, Filters):
        def matches(vol):
            tags = _tag_dict(vol)
            for f in Filters:
                if f["Name"] == "status":
                    if vol["State"] not in f["Values"]:
                        return False
                elif tags.get(f["Name"][4:]) not in f["Values"]:
                    return False
            return True
        return {"Volumes": [v for v in self._volumes if matches(v)]}

    def delete_volume(self, VolumeId):
        self.deleted.append(VolumeId)


class TestClearVolumes:
    def test_only_available_volumes_owned_by_this_cluster(self, sweeper):
        def vol(vid, cluster, state="available"):
            return {"VolumeId": vid, "State": state, "Tags": [
                {"Key": f"kubernetes.io/cluster/{cluster}", "Value": "owned"}, {"Key": "Environment", "Value": ENV}]}
        sweeper.ec2 = FakeVolumeEc2([vol("own", CLUSTER), vol("attached", CLUSTER, "in-use")]
                                    + [vol(s, s) for s in SIBLINGS]
                                    + [{"VolumeId": "env-only", "State": "available",
                                        "Tags": [{"Key": "Environment", "Value": ENV}]}])
        sweeper.clear_volumes()
        assert sweeper.ec2.deleted == ["own"]


class FakeLogs:
    def __init__(self, names):
        self._names = names
        self.deleted = []

    def describe_log_groups(self, logGroupNamePrefix):
        return {"logGroups": [{"logGroupName": n} for n in self._names if n.startswith(logGroupNamePrefix)]}

    def delete_log_group(self, logGroupName):
        self.deleted.append(logGroupName)


class TestClearLogGroups:
    def test_only_this_clusters_log_groups(self, sweeper):
        own = [f"/aws/eks/{CLUSTER}/cluster", f"/aws/containerinsights/{CLUSTER}/application"]
        others = [f"/aws/eks/{s}/cluster" for s in SIBLINGS] + [f"/aws/containerinsights/{s}/application" for s in SIBLINGS]
        sweeper.logs = FakeLogs(own + others + [f"/aws/lambda/{CLUSTER}"])
        sweeper.clear_log_groups()
        assert sweeper.logs.deleted == own


# --------------------------------------------------------------------------- #
# main — refuse a cluster name that is not "<resources_prefix>-<env>"          #
# --------------------------------------------------------------------------- #
class TestMainRefusesAnUnscopedClusterName:
    """platformctl falls back to the bare env name when it cannot read
    resources_prefix from the tfvars. Sweeping "the cluster called dev" would be
    matching by environment name alone."""

    def _main(self, monkeypatch, cluster):
        built = []

        class Recorder:
            def __init__(self, *a):
                built.append(a)

            def run(self):
                return False

        monkeypatch.setattr(sweep_env, "Sweeper", Recorder)
        monkeypatch.setattr(sweep_env.sys, "argv", ["sweep_env.py", "--env", ENV, "--cluster", cluster, "--region", REGION])
        assert sweep_env.main() == 0  # never fatal to the teardown
        return built

    @pytest.mark.parametrize("cluster", [ENV, "", f"{CLUSTER}2", "something-else"])
    def test_refuses(self, monkeypatch, capsys, cluster):
        assert self._main(monkeypatch, cluster) == []
        assert "skipping" in capsys.readouterr().out

    def test_sweeps_the_prefixed_cluster(self, monkeypatch):
        assert self._main(monkeypatch, CLUSTER) == [(ENV, CLUSTER, REGION)]
