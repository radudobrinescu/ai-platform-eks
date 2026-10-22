"""tools/release.py: version and Kubernetes rules, notes generation from two
fixture trees in a temporary git repo, and the product-safety of the output.
No network; `cut` is exercised with --no-push only."""

import json
import os
import subprocess

import pytest

import release

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
                          env=GIT_ENV).stdout


def write(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


LOCALS = 'locals {\n  vllm_image_tag = "{v}"\n}\n'
KARPENTER = 'resource "helm_release" "karpenter" {\n  chart      = "karpenter"\n  version    = "{v}"\n}\n'
LITELLM = "spec:\n  containers:\n    - name: litellm\n      image: litellm/litellm:{v}\n"
EXAMPLE = 'cluster_config = {\n  kubernetes_version = "{v}"\n}\n'


def tree(repo, vllm, karp, litellm, k8s):
    write(repo, "terraform/30.eks/30.cluster/locals.tf", LOCALS.replace("{v}", vllm))
    write(repo, "terraform/30.eks/30.cluster/karpenter.tf", KARPENTER.replace("{v}", karp))
    write(repo, "platform/services/litellm/litellm.yaml", LITELLM.replace("{v}", litellm))
    write(repo, "terraform/00.global/vars/example.tfvars", EXAMPLE.replace("{v}", k8s))


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "product"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    tree(r, "v0.24.0", "1.14.0", "v1.81.9-stable", "1.36")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    git(r, "tag", "base")
    git(r, "switch", "-q", "-c", "release/v2026.11.0")
    tree(r, "v0.32.0", "1.15.0", "v1.83.14-stable", "1.36")
    git(r, "commit", "-qam", "bumps")
    return r


class TestRules:
    @pytest.mark.parametrize("v", ["v2026.11.0", "v2026.01.12"])
    def test_good_versions(self, v):
        assert release.validate_version(v) == v

    @pytest.mark.parametrize("v", ["2026.11.0", "v2026.11", "v2026.13.0", "v1.0.0", "v2026.1.0"])
    def test_bad_versions(self, v):
        with pytest.raises(release.ReleaseError):
            release.validate_version(v)

    def test_first_release_has_no_overlap_constraint(self):
        release.check_kubernetes(None, ["1.36"], "1.36")

    def test_one_newer_minor_is_allowed(self):
        release.check_kubernetes({"kubernetes": {"supported": ["1.36"]}}, ["1.36", "1.37"], "1.37")

    @pytest.mark.parametrize("supported,recommended,msg", [
        (["1.37"], "1.37", "dropped"),
        (["1.36", "1.37", "1.38"], "1.37", "at most one"),
        (["1.36", "1.38"], "1.38", "one minor above"),
        (["1.36"], "1.37", "not in supported"),
    ])
    def test_overlap_rule(self, supported, recommended, msg):
        with pytest.raises(release.ReleaseError, match=msg):
            release.check_kubernetes({"kubernetes": {"supported": ["1.36"]}}, supported, recommended)

    def test_majors(self):
        rows, majors = release.version_table({"a": "1.0", "b": "v2.3", "ci/ruff": "0.6"},
                                              {"a": "1.1", "b": "v3.0", "ci/ruff": "1.0", "c": "1"})
        assert [r[0] for r in rows] == ["a", "b", "c", "ci/ruff"]
        assert majors == ["b"]


class TestNotes:
    def test_generates_the_three_product_files_and_commits(self, repo):
        m = release.notes(str(repo), "v2026.11.0", "base", date="2026-11-02",
                          statuses={"e2e/fresh-default": "success", "e2e/upgrade-default": "failure"},
                          manual_steps=["Rotate nothing; this is a test."], required_tfvars=["new_var"])
        assert m == {
            "version": "v2026.11.0", "previous": "baseline",
            "kubernetes": {"supported": ["1.36"], "recommended": "1.36"},
            "required_tfvars": ["new_var"], "manual_steps": ["Rotate nothing; this is a test."],
            "acknowledged_replacements": [],
        }
        notes = (repo / "docs/releases/v2026.11.0.md").read_text()
        assert "| vllm | `v0.24.0` | `v0.32.0` |" in notes
        assert "| karpenter | `1.14.0` | `1.15.0` |" in notes
        assert "git merge v2026.11.0" in notes and "./platformctl upgrade <env>" in notes
        assert "Supported: 1.36. Recommended: 1.36." in notes and "`new_var`" in notes
        assert json.loads((repo / "releases/v2026.11.0.json").read_text()) == m
        vv = (repo / "docs/validated-versions.md").read_text()
        row = next(ln for ln in vv.splitlines() if ln.startswith("| [v2026.11.0]"))
        assert "`v0.32.0`" in row and "`v1.83.14-stable`" in row
        assert "default pass, managed not run / default fail, managed not run" in row
        assert "docs(release)" in git(repo, "log", "-1", "--format=%s")
        assert not git(repo, "status", "--porcelain").strip()

    def test_second_release_enforces_overlap_and_keeps_hand_edits(self, repo):
        release.notes(str(repo), "v2026.11.0", "base", date="2026-11-02")
        man = json.loads((repo / "releases/v2026.11.0.json").read_text())
        man["acknowledged_replacements"] = [{"stage": "30.eks/30.cluster", "address": "x.y", "reason": "test"}]
        (repo / "releases/v2026.11.0.json").write_text(json.dumps(man))
        git(repo, "commit", "-qam", "ack")
        release.notes(str(repo), "v2026.11.0", "base", date="2026-11-03")
        again = json.loads((repo / "releases/v2026.11.0.json").read_text())
        assert again["acknowledged_replacements"] == man["acknowledged_replacements"]
        git(repo, "tag", "v2026.11.0")

        git(repo, "switch", "-q", "-c", "release/v2026.12.0")
        tree(repo, "v0.33.0", "1.15.0", "v1.83.14-stable", "1.37")
        git(repo, "commit", "-qam", "k8s 1.37")
        m = release.notes(str(repo), "v2026.12.0", "v2026.11.0", date="2026-12-01")
        assert m["previous"] == "v2026.11.0"
        assert m["kubernetes"] == {"supported": ["1.36", "1.37"], "recommended": "1.37"}
        vv = (repo / "docs/validated-versions.md").read_text().splitlines()
        rows = [ln for ln in vv if ln.startswith("| [v")]
        assert rows[0].startswith("| [v2026.12.0]") and rows[1].startswith("| [v2026.11.0]")
        with pytest.raises(release.ReleaseError, match="dropped"):
            release.notes(str(repo), "v2026.12.1", "v2026.11.0", supported=["1.37"], recommended="1.37",
                          commit=False)
        with pytest.raises(release.ReleaseError, match="not newer"):
            release.notes(str(repo), "v2026.10.0", "v2026.11.0", commit=False)

    def test_output_is_product_safe(self, repo):
        release.notes(str(repo), "v2026.11.0", "base", date="2026-11-02", commit=False)
        for rel in ("docs/releases/v2026.11.0.md", "releases/v2026.11.0.json", "docs/validated-versions.md"):
            text = (repo / rel).read_text()
            assert not release.FORBIDDEN_RX.search(text), rel

    def test_unsafe_text_is_refused(self, repo):
        with pytest.raises(release.ReleaseError, match="refusing"):
            release.notes(str(repo), "v2026.11.0", "base", manual_steps=["see account 123456789012"], commit=False)
        with pytest.raises(release.ReleaseError, match="refusing"):
            release.notes(str(repo), "v2026.11.0", "base", manual_steps=["Renovate did it"], commit=False)


class TestCut:
    def test_cut_merges_branches_onto_main(self, tmp_path):
        origin = tmp_path / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        write(origin, "a.txt", "a\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "a")
        git(origin, "switch", "-q", "-c", "update/infra")
        write(origin, "b.txt", "b\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "b")
        git(origin, "switch", "-q", "main")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=GIT_ENV)
        name = release.cut(str(clone), "v2026.11.0", ["update/infra"], push=False)
        assert name == "release/v2026.11.0"
        assert (clone / "b.txt").exists()
        assert git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == name
        with pytest.raises(release.ReleaseError, match="already exists"):
            release.cut(str(clone), "v2026.11.0", ["update/infra"], push=False)

    def test_cut_refuses_bad_version(self, tmp_path):
        with pytest.raises(release.ReleaseError):
            release.cut(str(tmp_path), "2026.11", [], push=False)


GREEN = {c: {"state": "success", "target_url": f"https://example.invalid/run/{i}", "description": "passed"}
         for i, c in enumerate(release.E2E_CONTEXTS)}


class TestRecord:
    """Decision A: one docs-only commit after E2E, verified, statuses carried."""

    def _noted(self, repo):
        release.notes(str(repo), "v2026.11.0", "base", date="2026-11-02")
        return git(repo, "rev-parse", "HEAD").strip()

    def test_records_only_the_e2e_cell_in_one_commit(self, repo):
        tested = self._noted(repo)
        before = (repo / "docs/validated-versions.md").read_text()
        head = release.record(str(repo), "v2026.11.0", GREEN, push=False)
        assert git(repo, "rev-parse", f"{head}^").strip() == tested
        assert git(repo, "diff", "--name-only", tested, head).split() == ["docs/validated-versions.md"]
        after = (repo / "docs/validated-versions.md").read_text()
        old_row = next(ln for ln in before.splitlines() if ln.startswith("| [v2026.11.0]"))
        new_row = next(ln for ln in after.splitlines() if ln.startswith("| [v2026.11.0]"))
        assert "not run" in old_row
        assert new_row.endswith("| default pass, managed pass / default pass, managed pass |")
        assert old_row.split("|")[:-2] == new_row.split("|")[:-2]  # every other cell unchanged
        assert f"Tested commit: {tested}" in git(repo, "log", "-1", "--format=%B")

    def test_refuses_when_any_run_did_not_pass(self, repo):
        self._noted(repo)
        details = {**GREEN, "e2e/upgrade-managed": {"state": "failure", "target_url": ""}}
        with pytest.raises(release.ReleaseError, match="e2e/upgrade-managed=failure"):
            release.record(str(repo), "v2026.11.0", details, push=False)
        missing = {k: v for k, v in GREEN.items() if k != "e2e/fresh-managed"}
        with pytest.raises(release.ReleaseError, match="e2e/fresh-managed=missing"):
            release.record(str(repo), "v2026.11.0", missing, push=False)

    def test_refuses_without_a_notes_row(self, repo):
        with pytest.raises(release.ReleaseError, match="run `notes` first"):
            release.record(str(repo), "v2026.11.0", GREEN, push=False)

    def test_verify_rejects_any_other_change(self, repo):
        tested = self._noted(repo)
        write(repo, "docs/validated-versions.md",
              (repo / "docs/validated-versions.md").read_text() + "\nextra line\n")
        git(repo, "commit", "-qam", "sneaky")
        with pytest.raises(release.ReleaseError, match="exactly one line"):
            release.verify_record_commit(str(repo), tested, "HEAD", "v2026.11.0")

    def test_verify_rejects_other_files_and_other_cells(self, repo):
        tested = self._noted(repo)
        write(repo, "terraform/30.eks/30.cluster/locals.tf", LOCALS.replace("{v}", "v0.33.0"))
        git(repo, "commit", "-qam", "code change after E2E")
        with pytest.raises(release.ReleaseError, match="docs/validated-versions.md only"):
            release.verify_record_commit(str(repo), tested, "HEAD", "v2026.11.0")
        git(repo, "reset", "-q", "--hard", tested)
        vv = (repo / "docs/validated-versions.md").read_text()
        write(repo, "docs/validated-versions.md", vv.replace("`v0.32.0`", "`v0.99.0`"))
        git(repo, "commit", "-qam", "edits the vLLM cell")
        with pytest.raises(release.ReleaseError, match="only the E2E cell"):
            release.verify_record_commit(str(repo), tested, "HEAD", "v2026.11.0")

    def test_verify_rejects_more_than_one_commit(self, repo):
        tested = self._noted(repo)
        release.record(str(repo), "v2026.11.0", GREEN, push=False)
        git(repo, "commit", "-q", "--allow-empty", "-m", "another")
        with pytest.raises(release.ReleaseError, match="single commit"):
            release.verify_record_commit(str(repo), tested, "HEAD", "v2026.11.0")

    def test_carried_statuses_name_the_tested_commit_and_keep_the_run_links(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        payloads = release.carried_status_payloads(GREEN, sha)
        assert [p["context"] for p in payloads] == list(release.E2E_CONTEXTS)
        assert all(p["state"] == "success" for p in payloads)
        assert all("0123456789ab" in p["description"] and len(p["description"]) <= 140 for p in payloads)
        assert [p["target_url"] for p in payloads] == [GREEN[c]["target_url"] for c in release.E2E_CONTEXTS]
