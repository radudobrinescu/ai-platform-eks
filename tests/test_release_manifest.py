"""Release manifests: validation, release detection from git, and pre-upgrade checks.

Release detection reads `releases/*.json` from git objects of the branch
upstream (deployed) and HEAD (target). These tests build throwaway repos to
cover the merge shapes forkers produce: fast-forward, merge commit, the very
first release, and several releases at once (refused).
"""

import copy
import json
import os
import subprocess

import pytest

import release_manifest as rm

GOOD = {
    "version": "v2026.11.0",
    "previous": "v2026.10.0",
    "kubernetes": {"supported": ["1.36", "1.37"], "recommended": "1.37"},
    "required_tfvars": ["some_new_variable"],
    "manual_steps": ["Free text shown before anything changes."],
    "acknowledged_replacements": [
        {"stage": "30.eks/30.cluster", "address": "aws_s3_bucket.model_cache[0]",
         "reason": "bucket name now includes the account id; regenerable cache"},
    ],
}


def manifest(version, previous, **over):
    m = copy.deepcopy(GOOD)
    m.update(version=version, previous=previous, required_tfvars=[], manual_steps=[],
             acknowledged_replacements=[])
    m.update(over)
    return m


# --------------------------------------------------------------------------- #
# Validation                                                                  #
# --------------------------------------------------------------------------- #
class TestValidate:
    def test_good_manifest(self):
        assert rm.validate(GOOD, filename="releases/v2026.11.0.json") == []

    @pytest.mark.parametrize("key", rm.REQUIRED_KEYS)
    def test_missing_key(self, key):
        m = copy.deepcopy(GOOD)
        del m[key]
        assert any(key in e for e in rm.validate(m))

    @pytest.mark.parametrize("version", ["2026.11.0", "v2026.13.0", "v2026.1.0", "v26.11.0",
                                         "v2026.11", "v2026.11.01", "v1.0.0"])
    def test_invalid_version_refused(self, version):
        m = copy.deepcopy(GOOD)
        m["version"] = version
        assert any("'version'" in e for e in rm.validate(m))

    def test_file_name_must_match_version(self):
        assert any("does not match" in e for e in rm.validate(GOOD, filename="releases/v2026.12.0.json"))

    def test_previous_must_be_older(self):
        m = copy.deepcopy(GOOD)
        m["previous"] = "v2026.12.0"
        assert any("older" in e for e in rm.validate(m))

    def test_previous_may_be_baseline(self):
        m = copy.deepcopy(GOOD)
        m["previous"] = "baseline"
        assert rm.validate(m) == []

    def test_recommended_must_be_supported(self):
        m = copy.deepcopy(GOOD)
        m["kubernetes"] = {"supported": ["1.36"], "recommended": "1.37"}
        assert any("recommended" in e for e in rm.validate(m))

    def test_acknowledgement_needs_reason(self):
        m = copy.deepcopy(GOOD)
        m["acknowledged_replacements"] = [{"stage": "10.networking", "address": "aws_vpc.main"}]
        assert any("acknowledged_replacements[0]" in e for e in rm.validate(m))

    def test_load_reports_problems(self, tmp_path):
        p = tmp_path / "v2026.11.0.json"
        p.write_text(json.dumps({"version": "v2026.11.0"}))
        with pytest.raises(rm.ReleaseError, match="missing key 'previous'"):
            rm.load(str(p))


# --------------------------------------------------------------------------- #
# Release detection in temporary git repos                                    #
# --------------------------------------------------------------------------- #
def git(repo, *args):
    # Isolated from the operator's git config (signing, hooks, default branch).
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return subprocess.run(["git", "-C", str(repo)] + list(args), check=True, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True).stdout.strip()


def commit_release(repo, m=None, message="change"):
    if m is not None:
        d = repo / "releases"
        d.mkdir(exist_ok=True)
        (d / "{}.json".format(m["version"])).write_text(json.dumps(m, indent=2))
    else:
        (repo / "file.txt").write_text(message)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit_release(r, message="baseline product")
    return r


class TestReleaseDetection:
    def test_baseline_without_manifests(self, repo):
        assert rm.release_of(str(repo), "HEAD") == "baseline"

    def test_baseline_to_first_release(self, repo):
        deployed = git(repo, "rev-parse", "HEAD")
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        assert rm.release_path(str(repo), deployed, "HEAD") == ("baseline", "v2026.10.0")

    def test_fast_forward_merge(self, repo):
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        deployed = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-q", "-b", "upstream")
        commit_release(repo, manifest("v2026.11.0", "v2026.10.0"))
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--ff-only", "upstream")
        assert rm.release_path(str(repo), deployed, "HEAD") == ("v2026.10.0", "v2026.11.0")

    def test_merge_commit(self, repo):
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        git(repo, "checkout", "-q", "-b", "upstream")
        commit_release(repo, manifest("v2026.11.0", "v2026.10.0"))
        git(repo, "checkout", "-q", "main")
        deployed = commit_release(repo, message="forker's own change")
        git(repo, "merge", "-q", "--no-edit", "--no-ff", "upstream")
        assert len(git(repo, "rev-list", "--parents", "-1", "HEAD").split()) == 3
        assert rm.release_path(str(repo), deployed, "HEAD") == ("v2026.10.0", "v2026.11.0")

    def test_upstream_ref_syntax(self, repo, tmp_path):
        bare = tmp_path / "remote.git"
        git(tmp_path, "init", "-q", "--bare", str(bare))
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        git(repo, "remote", "add", "origin", str(bare))
        git(repo, "push", "-q", "-u", "origin", "main")
        commit_release(repo, manifest("v2026.11.0", "v2026.10.0"))
        assert rm.release_path(str(repo)) == ("v2026.10.0", "v2026.11.0")

    def test_two_releases_at_once_refused(self, repo):
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        deployed = git(repo, "rev-parse", "HEAD")
        commit_release(repo, manifest("v2026.11.0", "v2026.10.0"))
        commit_release(repo, manifest("v2026.12.0", "v2026.11.0"))
        with pytest.raises(rm.ReleaseError) as exc:
            rm.release_path(str(repo), deployed, "HEAD")
        msg = str(exc.value)
        assert "2 releases ahead" in msg and "v2026.11.0, v2026.12.0" in msg
        assert msg.index("git reset --hard ORIG_HEAD") < msg.index("./platformctl upgrade <env>")
        assert "@{u}" not in msg               # would drop the fork's own unpushed commits

    def test_deployed_release_from_the_cluster_wins_over_the_branch(self, repo):
        """Pushed before upgrading: the branch upstream already has the release."""
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        commit_release(repo, manifest("v2026.11.0", "v2026.10.0"))
        head = git(repo, "rev-parse", "HEAD")
        with pytest.raises(rm.ReleaseError, match="nothing to upgrade"):
            rm.release_path(str(repo), head, "HEAD")
        assert rm.release_path(str(repo), head, "HEAD", deployed_release="v2026.10.0") == ("v2026.10.0", "v2026.11.0")
        with pytest.raises(rm.ReleaseError, match="downgrades are not supported"):
            rm.release_path(str(repo), head, "HEAD", deployed_release="v2026.12.0")
        with pytest.raises(rm.ReleaseError, match="not a release"):
            rm.release_path(str(repo), head, "HEAD", deployed_release="main")

    def test_target_without_manifest_refused(self, repo):
        deployed = git(repo, "rev-parse", "HEAD")
        commit_release(repo, message="no release here")
        with pytest.raises(rm.ReleaseError, match="no release manifest"):
            rm.release_path(str(repo), deployed, "HEAD")

    def test_same_release_refused(self, repo):
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        deployed = git(repo, "rev-parse", "HEAD")
        commit_release(repo, message="forker's own change")
        with pytest.raises(rm.ReleaseError, match="nothing to upgrade"):
            rm.release_path(str(repo), deployed, "HEAD")

    def test_previous_must_match_deployed(self, repo):
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        deployed = git(repo, "rev-parse", "HEAD")
        commit_release(repo, manifest("v2026.11.0", "v2026.10.1"))
        with pytest.raises(rm.ReleaseError, match="upgrades from v2026.10.1"):
            rm.release_path(str(repo), deployed, "HEAD")

    def test_upgrade_path_lists_every_release_between(self, repo):
        for v, prev in (("v2026.10.0", "baseline"), ("v2026.11.0", "v2026.10.0"), ("v2026.11.1", "v2026.11.0"),
                        ("v2026.12.0", "v2026.11.1")):
            commit_release(repo, manifest(v, prev))
            git(repo, "tag", v)
        r = str(repo)
        assert rm.upgrade_path(r, "v2026.10.0", "v2026.12.0") == ["v2026.11.0", "v2026.11.1", "v2026.12.0"]
        assert rm.upgrade_path(r, "baseline", "v2026.11.0") == ["v2026.10.0", "v2026.11.0"]
        assert rm.upgrade_path(r, "v2026.11.1", "v2026.12.0") == ["v2026.12.0"]
        assert rm.upgrade_path(r, "v2026.12.0", "v2026.12.0") == []
        with pytest.raises(rm.ReleaseError, match="downgrades are not supported"):
            rm.upgrade_path(r, "v2026.12.0", "v2026.11.0")
        assert rm.upgrade_path(r, "v2026.12.0", "v2026.11.0", older_ok=True) == []
        with pytest.raises(rm.ReleaseError, match="no upgrade path from v2026.10.5"):
            rm.upgrade_path(r, "v2026.10.5", "v2026.12.0")

    def test_upgrade_path_needs_a_release_tag_as_target(self, repo):
        commit_release(repo, manifest("v2026.10.0", "baseline"))
        git(repo, "tag", "v2026.10.0")
        commit_release(repo, message="after the release")
        git(repo, "tag", "v2026.10.1")                      # a tag without its own manifest
        r = str(repo)
        with pytest.raises(rm.ReleaseError, match="vYYYY.MM.N"):
            rm.upgrade_path(r, "baseline", "main")
        with pytest.raises(rm.ReleaseError, match="no release tag v2026.11.0"):
            rm.upgrade_path(r, "baseline", "v2026.11.0")
        with pytest.raises(rm.ReleaseError, match="does not carry releases/v2026.10.1.json"):
            rm.upgrade_path(r, "baseline", "v2026.10.1")

    def test_newest_tag_of_a_remote(self, repo, tmp_path):
        bare = tmp_path / "product.git"
        git(tmp_path, "init", "-q", "--bare", str(bare))
        git(repo, "remote", "add", "upstream", str(bare))
        assert rm.newest_tag(str(repo), "upstream") is None
        for t in ("v2026.9.0", "v2026.10.2", "v2026.10.10", "not-a-release", "v2026.09.1"):
            git(repo, "tag", "-a", t, "-m", t)
        git(repo, "push", "-q", "upstream", "--tags")
        git(repo, "tag", "v2027.01.0")                      # local only: not a published release
        assert rm.newest_tag(str(repo), "upstream") == "v2026.10.10"

    def test_invalid_manifest_in_tree_refused(self, repo):
        deployed = git(repo, "rev-parse", "HEAD")
        bad = manifest("v2026.10.0", "baseline")
        del bad["kubernetes"]
        commit_release(repo, bad)
        with pytest.raises(rm.ReleaseError, match="missing key 'kubernetes'"):
            rm.release_path(str(repo), deployed, "HEAD")


# --------------------------------------------------------------------------- #
# tfvars and Kubernetes checks                                                #
# --------------------------------------------------------------------------- #
TFVARS = """\
region = "eu-central-1"
# some_new_variable = "only in a comment"
cluster_config = {
  kubernetes_version = "1.36" # current
  eks_auto_mode      = false
}
/* block_var = 1 */
"""

EXAMPLE = """\
region = "us-east-1"
# A new knob introduced by the release.
some_new_variable = "default"
# cluster_endpoint_public_access_cidrs = ["<REPLACE>/32"]
"""


class TestTfvars:
    def test_sets_ignores_comments(self):
        assert rm.tfvars_sets(TFVARS, "region")
        assert rm.tfvars_sets(TFVARS, "kubernetes_version")  # nested in an object
        assert not rm.tfvars_sets(TFVARS, "some_new_variable")
        assert not rm.tfvars_sets(TFVARS, "block_var")

    def test_missing_required_tfvars_reported_with_example_line(self, tmp_path):
        tf = tmp_path / "dev.tfvars"
        tf.write_text(TFVARS)
        ex = tmp_path / "example.tfvars"
        ex.write_text(EXAMPLE)
        m = copy.deepcopy(GOOD)
        m["required_tfvars"] = ["region", "some_new_variable", "cluster_endpoint_public_access_cidrs"]
        with pytest.raises(rm.ReleaseError) as exc:
            rm.check_tfvars(m, str(tf), str(ex))
        msg = str(exc.value)
        assert "some_new_variable" in msg and ":3:" in msg and 'some_new_variable = "default"' in msg
        assert "cluster_endpoint_public_access_cidrs" in msg and ":4:" in msg
        assert "  - region" not in msg

    def test_all_required_present(self, tmp_path):
        tf = tmp_path / "dev.tfvars"
        tf.write_text(TFVARS + 'some_new_variable = "x"\n')
        rm.check_tfvars(GOOD, str(tf))

    def test_current_kubernetes(self, tmp_path):
        tf = tmp_path / "dev.tfvars"
        tf.write_text(TFVARS)
        assert rm.current_kubernetes(str(tf)) == "1.36"


class TestKubernetes:
    def test_supported_passes(self):
        rm.check_kubernetes(GOOD, "1.36")

    def test_older_unsupported_refused_with_one_minor_step(self):
        with pytest.raises(rm.ReleaseError) as exc:
            rm.check_kubernetes(GOOD, "1.34", env="dev")
        msg = str(exc.value)
        assert "supports Kubernetes 1.36, 1.37" in msg
        assert 'kubernetes_version = "1.35"' in msg and "./platformctl up dev" in msg

    def test_newer_unsupported_refused(self):
        with pytest.raises(rm.ReleaseError, match="cannot be downgraded"):
            rm.check_kubernetes(GOOD, "1.38")

    def test_hint_when_recommended_is_newer(self):
        hint = rm.kubernetes_hint(GOOD, "1.36", env="dev")
        assert hint and "recommends Kubernetes 1.37" in hint and 'kubernetes_version = "1.37"' in hint

    def test_no_hint_when_current(self):
        assert rm.kubernetes_hint(GOOD, "1.37") is None


class TestCli:
    def test_release_of_and_upgrade_path_cli(self, repo, capsys):
        assert rm.main(["release-of", "--repo", str(repo), "--rev", "HEAD"]) == 0
        assert capsys.readouterr().out.strip() == "baseline"
        for v, prev in (("v2026.10.0", "baseline"), ("v2026.11.0", "v2026.10.0")):
            commit_release(repo, manifest(v, prev))
            git(repo, "tag", v)
        assert rm.main(["release-of", "--repo", str(repo), "--rev", "HEAD"]) == 0
        assert capsys.readouterr().out.strip() == "v2026.11.0"
        assert rm.main(["upgrade-path", "--repo", str(repo), "--deployed-release", "baseline",
                        "--target", "v2026.11.0"]) == 0
        assert capsys.readouterr().out.split() == ["v2026.10.0", "v2026.11.0"]
        assert rm.main(["upgrade-path", "--repo", str(repo), "--deployed-release", "v2026.11.0",
                        "--target", "v2026.11.0"]) == 0
        assert capsys.readouterr().out.strip() == ""
        assert rm.main(["upgrade-path", "--repo", str(repo), "--deployed-release", "baseline",
                        "--target", "v2026.12.0"]) == 1
        assert "no release tag v2026.12.0" in capsys.readouterr().err

    def test_validate_cli(self, tmp_path):
        good = tmp_path / "v2026.11.0.json"
        good.write_text(json.dumps(GOOD))
        bad = tmp_path / "v2026.12.0.json"
        bad.write_text(json.dumps(GOOD))  # version does not match the file name
        assert rm.main(["validate", str(good)]) == 0
        assert rm.main(["validate", str(good), str(bad)]) == 1

    def test_check_kubernetes_cli(self, tmp_path, capsys):
        m = tmp_path / "v2026.11.0.json"
        m.write_text(json.dumps(GOOD))
        tf = tmp_path / "dev.tfvars"
        tf.write_text(TFVARS.replace('"1.36"', '"1.35"'))
        assert rm.main(["check-kubernetes", "--manifest", str(m), "--tfvars", str(tf), "--env", "dev"]) == 1
        assert "./platformctl up dev" in capsys.readouterr().err
