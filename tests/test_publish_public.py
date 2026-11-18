"""release.py publish-public: a release reaches the public repository as one
commit. Real git against local bare repositories; gh is not involved."""

import json
import os
import subprocess

import pytest

import release

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env=GIT_ENV).stdout.strip()


def manifest(version, previous):
    return json.dumps({"version": version, "previous": previous,
                       "kubernetes": {"supported": ["1.37"], "recommended": "1.37"}, "required_tfvars": [],
                       "manual_steps": [], "acknowledged_replacements": []})


def release_commit(factory, version, previous, text):
    """A factory release the way finish leaves it: update branch merged, manifest, tag."""
    git(factory, "switch", "-q", "-c", f"renovate/{version}")
    (factory / "pins.txt").write_text(text)
    git(factory, "add", "-A")
    git(factory, "commit", "-qm", "chore(deps): Update gateway")
    git(factory, "switch", "-q", "main")
    git(factory, "merge", "-q", "--no-ff", "-m", f"Merge remote-tracking branch 'origin/renovate/{version}'",
        f"renovate/{version}")
    (factory / "releases").mkdir(exist_ok=True)
    (factory / "releases" / f"{version}.json").write_text(manifest(version, previous))
    (factory / "docs" / "releases").mkdir(parents=True, exist_ok=True)
    (factory / "docs" / "releases" / f"{version}.md").write_text(f"# Release {version}\n")
    git(factory, "add", "-A")
    git(factory, "commit", "-qm", f"docs(release): notes and manifest for {version}")
    git(factory, "tag", "-a", version, "-m", f"Release {version}")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """public (bare): the product as it was first shared. factory: the same history plus a first release."""
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    factory = tmp_path / "factory"
    factory.mkdir()
    git(factory, "init", "-q", "-b", "main")
    (factory / "pins.txt").write_text("v1\n")
    git(factory, "add", "-A")
    git(factory, "commit", "-qm", "the product, as shared")
    public = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(public)], check=True, env=GIT_ENV)
    git(factory, "push", "-q", str(public), "main")
    release_commit(factory, "v2027.01.0", "baseline", "v2\n")
    return factory, public


def test_a_release_is_one_commit_with_the_release_tree_and_its_tag(repos):
    factory, public = repos
    shared = git(public, "rev-parse", "main")
    res = release.publish_public(str(factory), "v2027.01.0", str(public))
    assert res["pushed"] and res["parent"] == shared
    head = git(public, "rev-parse", "main")
    assert head == res["commit"] and git(public, "rev-parse", "v2027.01.0^{commit}") == head
    assert git(public, "rev-parse", "main^") == shared                                   # exactly one commit
    assert git(public, "rev-parse", "main^{tree}") == git(factory, "rev-parse", "v2027.01.0^{tree}")
    log = git(public, "log", "--format=%s%n%b", "main")
    assert log.startswith("Release v2027.01.0") and not release.FORBIDDEN_RX.search(log)  # none of the factory's history
    assert git(public, "cat-file", "-t", "v2027.01.0") == "tag"                           # annotated, as release tags are
    assert git(factory, "rev-parse", "v2027.01.0^{commit}") != head                       # the factory's tag is untouched
    assert "public-release/" not in git(factory, "tag", "--list")


def test_the_next_release_goes_on_top_and_a_fork_merges_it_cleanly(repos, tmp_path):
    factory, public = repos
    release.publish_public(str(factory), "v2027.01.0", str(public))
    fork = tmp_path / "fork"
    subprocess.run(["git", "clone", "-q", str(public), str(fork)], check=True, env=GIT_ENV)
    (fork / "workloads.yaml").write_text("my model\n")
    git(fork, "add", "-A")
    git(fork, "commit", "-qm", "my model")
    release_commit(factory, "v2027.04.0", "v2027.01.0", "v3\n")
    release.publish_public(str(factory), "v2027.04.0", str(public))
    assert git(public, "rev-list", "--count", "main") == "3"                             # shared + two releases
    git(fork, "fetch", "-q", "--tags", "origin")
    git(fork, "merge", "-q", "--no-edit", "v2027.04.0")
    assert (fork / "pins.txt").read_text() == "v3\n" and (fork / "workloads.yaml").exists()
    assert sorted(x.name for x in (fork / "releases").iterdir()) == ["v2027.01.0.json", "v2027.04.0.json"]


def test_publishing_twice_does_nothing_the_second_time(repos):
    factory, public = repos
    first = release.publish_public(str(factory), "v2027.01.0", str(public))
    again = release.publish_public(str(factory), "v2027.01.0", str(public))
    assert not again["pushed"] and again["commit"] == first["commit"]
    assert git(public, "rev-parse", "main") == first["commit"]


def test_dry_run_pushes_nothing(repos):
    factory, public = repos
    before = git(public, "for-each-ref")
    res = release.publish_public(str(factory), "v2027.01.0", str(public), dry_run=True)
    assert not res["pushed"] and git(public, "for-each-ref") == before


def test_refuses_when_something_was_merged_into_the_public_main_directly(repos, tmp_path):
    factory, public = repos
    release.publish_public(str(factory), "v2027.01.0", str(public))
    other = tmp_path / "contributor"
    subprocess.run(["git", "clone", "-q", str(public), str(other)], check=True, env=GIT_ENV)
    (other / "fix.txt").write_text("fix\n")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "a pull request merged in the public repository")
    git(other, "push", "-q", "origin", "main")
    release_commit(factory, "v2027.04.0", "v2027.01.0", "v3\n")
    with pytest.raises(release.ReleaseError, match="something was merged there directly"):
        release.publish_public(str(factory), "v2027.04.0", str(public))
    assert not git(public, "tag", "--list", "v2027.04.0")


def test_refuses_an_untagged_version_and_a_release_whose_predecessor_is_not_public(repos):
    factory, public = repos
    with pytest.raises(release.ReleaseError, match="there is no tag v2027.02.0"):
        release.publish_public(str(factory), "v2027.02.0", str(public))
    release_commit(factory, "v2027.04.0", "v2027.01.0", "v3\n")
    with pytest.raises(release.ReleaseError, match="v2027.01.0, which is not published"):
        release.publish_public(str(factory), "v2027.04.0", str(public))


def test_cli_reads_the_url_from_the_environment_and_never_prints_it(repos, monkeypatch, capsys):
    factory, public = repos
    assert release.main(["--repo", str(factory), "publish-public", "v2027.01.0", "--url-env", "PUBLIC_URL"]) == 2
    assert "PUBLIC_URL is not set" in capsys.readouterr().err
    monkeypatch.setenv("PUBLIC_URL", str(public))
    assert release.main(["--repo", str(factory), "publish-public", "v2027.01.0", "--url-env", "PUBLIC_URL",
                         "--dry-run"]) == 0
    assert release.main(["--repo", str(factory), "publish-public", "v2027.01.0", "--url-env", "PUBLIC_URL"]) == 0
    out = capsys.readouterr()
    assert "published v2027.01.0 as" in out.out and str(public) not in out.out + out.err
    monkeypatch.setenv("PUBLIC_URL", str(public) + "-gone")      # as if it carried a token: must not be echoed
    assert release.main(["--repo", str(factory), "publish-public", "v2027.01.0", "--url-env", "PUBLIC_URL"]) == 2
    err = capsys.readouterr().err
    assert "<public repository>" in err and str(public) not in err
