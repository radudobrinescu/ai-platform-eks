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
        assert "./platformctl upgrade <env>" in notes and "git merge" not in notes   # one command; see UPGRADING.md
        assert "Supported: 1.36. Recommended: 1.36." in notes and "`new_var`" in notes
        assert json.loads((repo / "releases/v2026.11.0.json").read_text()) == m
        vv = (repo / "docs/validated-versions.md").read_text()
        row = next(ln for ln in vv.splitlines() if ln.startswith("| [v2026.11.0]"))
        assert "`v0.32.0`" in row and "`v1.83.14-stable`" in row
        assert row.count("|") == 7 and "default pass" not in vv and "not run" not in vv   # no E2E column:
        assert "E2E" not in next(ln for ln in vv.splitlines() if ln.startswith("| Release"))  # listed = passed
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

    def test_cut_refuses_a_group_branch_that_predates_the_last_release(self, tmp_path):
        origin = tmp_path / "origin"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        write(origin, "a.txt", "a\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "a")
        git(origin, "switch", "-q", "-c", "update/stale")
        write(origin, "b.txt", "b\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "b")
        git(origin, "switch", "-q", "-c", "update/fresh", "main")
        git(origin, "switch", "-q", "main")
        write(origin, "a.txt", "released\n")
        git(origin, "commit", "-qam", "release")
        git(origin, "tag", "v2026.11.1")
        git(origin, "switch", "-q", "update/fresh")
        git(origin, "merge", "-q", "--no-edit", "main")
        write(origin, "c.txt", "c\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "c")
        git(origin, "switch", "-q", "main")
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, env=GIT_ENV)
        with pytest.raises(release.ReleaseError, match="update/stale.*do\\(es\\) not contain v2026.11.1"):
            release.cut(str(clone), "v2026.11.2", ["update/stale", "update/fresh"], push=False)
        assert git(clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
        assert release.cut(str(clone), "v2026.11.2", ["update/fresh"], push=False) == "release/v2026.11.2"

    def test_cut_refuses_bad_version(self, tmp_path):
        with pytest.raises(release.ReleaseError):
            release.cut(str(tmp_path), "2026.11", [], push=False)


OLD_TABLE = (
    "# Validated versions\n\nOld intro.\n\n"
    "| Release | Date | Kubernetes | vLLM | LiteLLM | Karpenter | E2E (fresh / upgrade) |\n"
    "|---|---|---|---|---|---|---|\n"
    "| [v2026.10.1](releases/v2026.10.1.md) | 2026-10-20 | 1.36 | `v0.31.0` | `v1.83.1` | `1.15.0` "
    "| default pass, managed pass / default pass, managed pass |\n"
    "| [v2026.10.0](releases/v2026.10.0.md) | 2026-10-05 | 1.36 | `v0.30.0` | `v1.83.0` | `1.14.0` "
    "| default pass, managed pass / default pass, managed pass |\n")


class TestValidatedVersions:
    def test_a_table_written_with_the_e2e_column_loses_it(self):
        row = "| [v2026.11.0](releases/v2026.11.0.md) | 2026-11-02 | 1.36 | `v0.32.0` | `v1.83.14` | `1.15.1` |"
        out = release.upsert_row(OLD_TABLE, "v2026.11.0", row).splitlines()
        table = [ln for ln in out if ln.startswith("|")]
        assert table[0] == "| Release | Date | Kubernetes | vLLM | LiteLLM | Karpenter |"
        assert table[1] == "|---|---|---|---|---|---|"
        assert table[2] == row
        assert table[3] == "| [v2026.10.1](releases/v2026.10.1.md) | 2026-10-20 | 1.36 | `v0.31.0` | `v1.83.1` | `1.15.0` |"
        assert all(ln.count("|") == 7 for ln in table) and "default pass" not in "\n".join(out)
        assert "Old intro." not in out and any("passed all four" in ln for ln in out)
        assert release.upsert_row("\n".join(out) + "\n", "v2026.11.0", row).splitlines() == out   # idempotent


def details(base="v2026.10.1", llmd="true", **override):
    d = {c: {"state": "success", "target_url": f"https://example.invalid/run/{i}",
             "description": release.status_description(c.split("/")[1].split("-")[0], c.split("-")[1], "passed",
                                                       base=base, llmd=llmd)}
         for i, c in enumerate(release.E2E_CONTEXTS)}
    d.update(override)
    return d


class TestGreen:
    """What counts as 'E2E passed' for a release: four green statuses, from runs
    that upgraded from the previous release with the llm-d tier on."""

    def test_four_green_runs_from_the_previous_release_pass(self):
        release.require_all_green(details(), previous="v2026.10.1")

    def test_refuses_when_any_run_did_not_pass_or_is_missing(self):
        with pytest.raises(release.ReleaseError, match="e2e/upgrade-managed=failure"):
            release.require_all_green(details(**{"e2e/upgrade-managed": {"state": "failure", "target_url": ""}}),
                                      previous="v2026.10.1")
        missing = {k: v for k, v in details().items() if k != "e2e/fresh-managed"}
        with pytest.raises(release.ReleaseError, match="e2e/fresh-managed=missing"):
            release.require_all_green(missing, previous="v2026.10.1")

    def test_refuses_an_upgrade_from_another_base(self):
        with pytest.raises(release.ReleaseError) as e:
            release.require_all_green(details(base="main"), previous="v2026.10.1")
        assert "e2e/upgrade-default=upgraded from main, not v2026.10.1" in str(e.value)
        assert "e2e/fresh-default" not in str(e.value)        # fresh runs have no base

    def test_first_release_has_no_previous_to_compare(self):
        release.require_all_green(details(base="main"), previous="baseline")

    def test_refuses_runs_without_the_llmd_tier_or_without_the_fields(self):
        with pytest.raises(release.ReleaseError, match="without the llm-d tier"):
            release.require_all_green(details(llmd="false"), previous="v2026.10.1")
        old = {c: {"state": "success", "target_url": "", "description": "E2E fresh (default) passed"}
               for c in release.E2E_CONTEXTS}
        with pytest.raises(release.ReleaseError, match="does not say what it tested"):
            release.require_all_green(old, previous="v2026.10.1")

    def test_description_fits_a_commit_status_and_round_trips(self):
        d = release.status_description("upgrade", "managed", "passed", base="release/v2026.11.12", llmd="true")
        assert len(d) <= 140 and d.startswith("E2E upgrade (managed) passed")
        assert release.status_fields(d) == {"base": "release/v2026.11.12", "llmd": "true"}
        assert release.status_fields("E2E fresh (default) passed") == {}

    def test_a_status_text_with_a_field_twice_or_extra_text_says_nothing(self):
        assert release.status_fields("E2E fresh (default) passed; llmd=false; llmd=true") == {}
        assert release.status_fields("E2E upgrade (default) passed; base=main; base=v2026.10.1; llmd=true") == {}
        assert release.status_fields("E2E fresh (default) passed; trust me; llmd=true") == {}
        twice = details(**{"e2e/fresh-default": {"state": "success", "target_url": "",
                                                 "description": "E2E fresh (default) passed; llmd=false; llmd=true"}})
        with pytest.raises(release.ReleaseError, match="e2e/fresh-default=does not say what it tested"):
            release.require_all_green(twice, previous="v2026.10.1")


class TestPlatformChanges:
    def _merge(self, repo, branch, title, files):
        git(repo, "switch", "-q", "-c", branch)
        for rel in files:
            write(repo, rel, branch + "\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", title)
        git(repo, "switch", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "-m", f"Merge pull request #9 from owner/{branch}", "-m", title, branch)

    def test_lists_merged_prs_by_type_without_numbers_or_update_branches(self, tmp_path):
        r = tmp_path / "p"
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        tree(r, "v0.24.0", "1.14.0", "v1.81.9-stable", "1.36")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "base")
        git(r, "tag", "base")
        self._merge(r, "fix/down-keda", "fix(down): clear KEDA finalizers", ["a.txt"])
        self._merge(r, "feat/new-check", "feat: a new health check", ["b.txt"])
        self._merge(r, "docs/readme", "docs: clarify the README", ["c.txt"])
        self._merge(r, "renovate/gateway", "chore(deps): Update gateway", ["d.txt"])
        self._merge(r, "release/v2026.11.0", "release: v2026.11.0", ["e.txt"])
        self._merge(r, "fix/leaky", "fix: mention lifecycle internals", ["f.txt"])
        ch = release.platform_changes(str(r), "base")
        assert ch == {"Fixes": ["Clear KEDA finalizers"], "Improvements": ["A new health check"],
                      "Other changes": ["Clarify the README"]}
        md = release.render_notes(release.build_manifest("v2026.11.0", "baseline", ["1.36"], "1.36", [], [], None),
                                  [], [], 3, "2026-11-12", ch)
        assert "## Platform changes" in md and "- Clear KEDA finalizers" in md and "#9" not in md
        assert md.index("Fixes:") < md.index("Improvements:") < md.index("Other changes:") < md.index("## Component versions")
        release.assert_product_safe(md)

    def test_no_section_without_changes(self):
        md = release.render_notes(release.build_manifest("v2026.11.0", "baseline", ["1.36"], "1.36", [], [], None),
                                  [], [], 3, "2026-11-12", {})
        assert "## Platform changes" not in md
