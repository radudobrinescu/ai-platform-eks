"""Coverage guard: every component pin on `main` is visible to Renovate.

`main` carries no `# renovate:` annotations (it ships to the public product repo
as is), so detection lives entirely in renovate.json on this branch, keyed on the
shapes that exist on `main`. This test reads the product tree from $SUBJECT_ROOT
(default ./subject; CI checks out `main` there) and fails when:

  * a pin of the inventory below is no longer extracted with the expected dep
    name (a variable was renamed or a line reformatted);
  * a custom regex manager matches nothing in its files;
  * a file holding a pin is matched by no manager file pattern;
  * a `# renovate:` annotation appears on `main` (it must stay config-only);
  * the same pip package is pinned to two versions under platform/services/;
  * a Terraform helm_release has no version (it floats, nothing can bump it).

Static: it never calls Renovate. The end-to-end proof is Renovate's own
extraction (`renovate --platform=local --dry-run=extract`, see README.md).
.github/workflows/coverage.yaml runs this weekly and on every push.
"""

import glob
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (file on main, depName the custom managers must extract from it)
CUSTOM_INVENTORY = [
    ("argocd/bootstrap/templates/platform.yaml", "gpu-operator"),
    ("argocd/bootstrap/templates/platform.yaml", "langfuse"),
    ("platform/config/kro/vllm-endpoint.yaml", "vllm/vllm-openai"),
    ("platform/config/kro/llmd-endpoint.yaml", "vllm/vllm-openai"),
    ("platform/config/kro/llmd-disagg-endpoint.yaml", "vllm/vllm-openai"),
    ("platform/config/kro/llmd-endpoint.yaml", "llm-d-router-standalone"),
    ("platform/config/kro/llmd-disagg-endpoint.yaml", "llm-d-router-standalone"),
    ("platform/services/cluster-dashboard/pha-config.yaml", "python"),
    ("platform/services/litellm-sync/deployment.yaml", "kubernetes"),
    ("platform/services/cluster-dashboard/manifests.yaml", "psycopg"),
    ("platform/services/cluster-dashboard/manifests.yaml", "pyyaml"),
    ("platform/services/cluster-dashboard/manifests.yaml", "pyjwt"),
    ("platform/services/cluster-dashboard/pha-event-watcher.yaml", "kubernetes"),
    ("platform/services/cluster-dashboard/pha-event-watcher.yaml", "psycopg"),
    ("platform/services/cluster-dashboard/scripts/backend.py", "psycopg"),
    ("platform/services/cluster-dashboard/scripts/event_watcher.py", "psycopg"),
    ("terraform/30.eks/30.cluster/locals.tf", "vllm/vllm-openai"),
    ("terraform/30.eks/35.addons/main.tf", "aws-load-balancer-controller"),
    ("terraform/30.eks/30.cluster/karpenter.tf", "karpenter"),
    ("terraform/30.eks/30.cluster/capabilities-selfmanaged.tf", "kro"),
    (".github/workflows/ci.yaml", "hashicorp/terraform"),
    (".github/workflows/ci.yaml", "ruff"),
    (".github/workflows/ci.yaml", "pytest"),
    (".github/workflows/ci.yaml", "pyyaml"),
    (".github/workflows/ci.yaml", "yamllint"),
    (".github/workflows/ci.yaml", "yannh/kubeconform"),
    (".github/workflows/ci.yaml", "kubernetes-sigs/kustomize"),
    ("terraform/00.global/vars/example.tfvars", "amazon-eks"),
    ("terraform/00.global/vars/example-esc.tfvars", "amazon-eks"),
]

# Pins read by Renovate's built-in managers (terraform helm_release / modules,
# kubernetes image:, kustomize ?ref=, github-actions uses:). Here only the file
# coverage can be checked statically: (regex identifying a pin, glob).
BUILTIN_SHAPES = [
    (r'resource\s+"helm_release"', "terraform/**/*.tf"),
    (r'(?m)^\s*version\s*=\s*"', "terraform/**/*.tf"),
    (r"(?m)^\s*image:\s*[\w./-]+:[\w.-]+\s*$", "platform/**/*.yaml"),
    (r"\?ref=v?\d", "platform/**/kustomization.yaml"),
    (r"(?m)^\s*-?\s*uses:\s*\S+@", ".github/workflows/*.yaml"),
]

# Product defects tracked for a product PR (not hidden): helm_release blocks on
# `main` with no version. ack_selfmanaged is a for_each over a user map (opt-in).
KNOWN_UNPINNED_HELM_RELEASES = {
    "terraform/30.eks/30.cluster/capabilities-selfmanaged.tf:ack_selfmanaged",
}


def _renovate():
    with open(os.path.join(ROOT, "renovate.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _py(rx: str) -> re.Pattern:
    """Renovate regexes use JS named groups (?<name>); Python spells them (?P<name>)."""
    return re.compile(rx.replace("(?<", "(?P<"))


def _file_pattern(p: str) -> re.Pattern:
    assert p.startswith("/") and p.endswith("/"), f"use the /regex/ form: {p}"
    return re.compile(p[1:-1])


def _read(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return fh.read()


def _files(root):
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in {".git", ".terraform", "node_modules", "__pycache__"}]
        out += [os.path.relpath(os.path.join(base, f), root) for f in files]
    return out


def extract(cm: dict, text: str) -> list[dict]:
    """Python model of Renovate's regex manager (strategies `any` and `recursive`)."""
    strategy = cm.get("matchStringsStrategy", "any")
    found = []

    def emit(m):
        g = m.groupdict()
        found.append({"depName": g.get("depName") or cm.get("depNameTemplate"),
                      "currentValue": g.get("currentValue")})

    if strategy == "recursive":
        chunks = [text]
        for ms in cm["matchStrings"][:-1]:
            chunks = [m.group(0) for c in chunks for m in _py(ms).finditer(c)]
        for c in chunks:
            for m in _py(cm["matchStrings"][-1]).finditer(c):
                emit(m)
    else:
        assert strategy == "any", f"strategy {strategy} not modelled"
        for ms in cm["matchStrings"]:
            for m in _py(ms).finditer(text):
                emit(m)
    return found


def extracted_by_file(cfg, root):
    out = {}
    files = _files(root)
    for cm in cfg["customManagers"]:
        pats = [_file_pattern(p) for p in cm["managerFilePatterns"]]
        for rel in files:
            if any(p.search(rel) for p in pats):
                for dep in extract(cm, _read(root, rel)):
                    out.setdefault(rel, []).append(dep)
    return out


class TestPolicy:
    def test_domains(self):
        groups = {r.get("groupName") for r in _renovate()["packageRules"] if r.get("groupName")}
        assert groups == {"infra", "inference", "gateway", "gitops", "tooling", "eks-kubernetes",
                          "vllm", "kro minor", "llm-d router minor"}, groups

    def test_targets_main_without_a_release_age(self):
        """No minimumReleaseAge: the E2E gate judges every version on real clusters,
        and a release age only produced stale group branches (branches frozen while
        an update was 'pending') and Docker Hub re-push delays (postgres, python)."""
        cfg = _renovate()
        assert cfg["baseBranchPatterns"] == ["main"]
        assert cfg["forkProcessing"] == "enabled"
        assert "minimumReleaseAge" not in cfg
        assert not any("minimumReleaseAge" in r or "minimumReleaseAgeBehaviour" in r for r in cfg["packageRules"])
        assert "security" in cfg["vulnerabilityAlerts"]["labels"]

    def test_ticks_only_where_e2e_cannot_judge(self):
        """A dashboard tick is required for: every major; the EKS version (one-way);
        every vLLM change (it runs users' models); kro and llm-d router minors
        (0.x APIs users write against). Nothing else: E2E judges Karpenter, Argo CD,
        Prometheus, the GPU operator and the rest. Each ticked update gets its own
        branch, so it never holds back the group it would otherwise sit in, and
        closing its PR means 'not now' (recreateWhen: always)."""
        rules = _renovate()["packageRules"]
        gated = [r for r in rules if r.get("dependencyDashboardApproval")]
        got = {(tuple(r.get("matchDepNames") or ()), tuple(r.get("matchUpdateTypes") or ())) for r in gated}
        assert got == {((), ("major",)), (("vllm/vllm-openai",), ()), (("kro",), ("minor",)),
                       (("llm-d-router-standalone",), ("minor",)), (("amazon-eks",), ())}, got
        for r in gated:
            assert r["recreateWhen"] == "always", r["description"][:60]
            if r.get("matchDepNames"):
                assert r.get("groupSlug"), f"{r['matchDepNames']}: a ticked update needs its own branch"
        for dep in ("karpenter", "argo-cd", "prom/prometheus", "gpu-operator", "keda"):
            assert not any(dep in (r.get("matchDepNames") or []) for r in gated), f"{dep} should flow without a tick"

    def test_eks_kubernetes_anchor_is_watched_but_never_proposed_unasked(self):
        """The EKS version follows endoflife.date, so a new minor shows up on the
        dashboard, but a PR exists only after a tick, on its own branch."""
        cfg = _renovate()
        rules = [r for r in cfg["packageRules"] if r.get("matchDepNames") == ["amazon-eks"]]
        assert len(rules) == 1
        r = rules[0]
        assert r["matchDatasources"] == ["endoflife-date"]
        assert r["dependencyDashboardApproval"] is True and r["recreateWhen"] == "always"
        assert r["groupName"] == "eks-kubernetes" and r["automerge"] is False
        assert r["separateMultipleMinor"] is True  # 1.37 and 1.38 as two ticks, never a two-minor jump
        rx = r["extractVersion"].replace("(?<", "(?P<")  # Renovate uses JS named groups
        assert re.match(rx, "1.37-eks-4").group("version") == "1.37"
        managers = [m for m in cfg["customManagers"] if m.get("depNameTemplate") == "amazon-eks"]
        assert len(managers) == 1 and managers[0]["datasourceTemplate"] == "endoflife-date"

    def test_kubectl_skew_ceilings_match_the_kubernetes_default_on_main(self, subject_root):
        """alpine/k8s (kubectl) and the kubernetes Python client must stay within one
        minor of the API server. Their allowedVersions ceilings are set by hand, so
        this fails when the EKS default moves and the ceilings were not raised."""
        text = _read(subject_root, "terraform/00.global/vars/example.tfvars")
        minor = int(re.search(r'kubernetes_version\s*=\s*"1\.(\d+)"', text).group(1))
        rules = {tuple(r["matchDepNames"]): r["allowedVersions"] for r in _renovate()["packageRules"]
                 if r.get("allowedVersions") and r.get("matchDepNames") in (["alpine/k8s"], ["kubernetes"])}
        assert rules[("alpine/k8s",)] == f"<1.{minor + 2}", "raise the alpine/k8s ceiling with the EKS anchor"
        # kubernetes-client major N tracks Kubernetes 1.N; ceiling = N+1 (one minor above) exclusive -> < minor+2
        assert rules[("kubernetes",)] == f"<{minor + 2}", "raise the kubernetes-client ceiling with the EKS anchor"

    def test_interpolated_terraform_versions_are_not_looked_up(self):
        """karpenter.tf: version = local.karpenter_chart_version. The built-in
        terraform manager reports that reference as a version; only the regex
        manager on the literal may propose updates."""
        rules = [r for r in _renovate()["packageRules"] if r.get("matchManagers") == ["terraform"]
                 and r.get("enabled") is False]
        assert any(re.match(r["matchCurrentValue"].strip("/"), "${local.karpenter_chart_version}") for r in rules)

    def test_open_pr_limit_never_starves_the_groups(self):
        """Ticked and held PRs stay open for weeks. With Renovate's limit of 5 open
        PRs, five of them (kro, vLLM, router, two held majors) rate-limited all five
        normal groups, which would have left the next release empty. Groups are
        few (5 domains + one branch per ticked kind + majors), so 20 never binds."""
        cfg = _renovate()
        assert cfg["prConcurrentLimit"] >= 20 and cfg.get("prHourlyLimit", 0) == 0
        assert cfg.get("branchConcurrentLimit") in (None, 0) or cfg["branchConcurrentLimit"] >= 20

    def test_group_branches_are_rebased_when_main_moves(self):
        """Group PRs are never merged; release branches merge them onto main, so
        every release leaves them behind main until Renovate rebases them."""
        assert _renovate()["rebaseWhen"] == "behind-base-branch"

    def test_nothing_automerges(self):
        text = json.dumps(_renovate())
        assert '"automerge": true' not in text

    def test_no_annotation_manager(self):
        for cm in _renovate()["customManagers"]:
            assert "# renovate:" not in json.dumps(cm["matchStrings"]), cm["description"]


class TestEveryPinOnMainIsDetected:
    def test_inventory_pins_are_extracted_with_expected_dep_names(self, subject_root):
        got = extracted_by_file(_renovate(), subject_root)
        missing = []
        for rel, dep in CUSTOM_INVENTORY:
            hits = [d for d in got.get(rel, []) if d["depName"] == dep]
            if not hits:
                missing.append(f"{rel}: {dep}")
                continue
            for d in hits:
                assert re.fullmatch(r"v?\d+(\.\d+)*([-.+][\w.]+)?", d["currentValue"] or ""), (rel, d)
        assert not missing, "pins on main no longer detected:\n  " + "\n  ".join(missing)

    def test_each_custom_manager_matches_at_least_once(self, subject_root):
        cfg = _renovate()
        files = _files(subject_root)
        for cm in cfg["customManagers"]:
            pats = [_file_pattern(p) for p in cm["managerFilePatterns"]]
            mine = [f for f in files if any(p.search(f) for p in pats)]
            assert mine, f"customManager '{cm['description'][:60]}' matches no files on main"
            n = sum(len(extract(cm, _read(subject_root, f))) for f in mine)
            assert n > 0, f"customManager '{cm['description'][:60]}' matches no line in {mine}"

    def test_every_file_with_a_pin_is_matched_by_a_manager(self, subject_root):
        cfg = _renovate()
        pats = [_file_pattern(p) for mgr in ("kubernetes", "kustomize", "terraform")
                for p in cfg.get(mgr, {}).get("managerFilePatterns", [])]
        pats += [_file_pattern(p) for cm in cfg["customManagers"] for p in cm["managerFilePatterns"]]
        pats.append(re.compile(r"^\.github/workflows/.+\.ya?ml$"))  # github-actions default
        holders = {rel for rel, _ in CUSTOM_INVENTORY}
        for rx, pattern in BUILTIN_SHAPES:
            for path in glob.glob(os.path.join(subject_root, pattern), recursive=True):
                rel = os.path.relpath(path, subject_root)
                if "/.terraform/" not in f"/{rel}" and os.path.isfile(path) and re.search(rx, _read(subject_root, rel)):
                    holders.add(rel)
        uncovered = sorted(rel for rel in holders if not any(p.search(rel) for p in pats))
        assert not uncovered, f"files with pins that no manager reads: {uncovered}"

    def test_main_has_no_renovate_annotations(self, subject_root):
        offenders = []
        for rel in _files(subject_root):
            # renovate.json on main is the stale v1 config (removed by a product PR);
            # its text is not an annotation.
            if rel == "renovate.json" or rel.endswith((".png", ".jpg", ".gif", ".ico", ".pdf")):
                continue
            try:
                text = _read(subject_root, rel)
            except UnicodeDecodeError:
                continue
            if re.search(r"#\s*renovate:", text):
                offenders.append(rel)
        assert not offenders, f"`# renovate:` annotations must not exist on main: {offenders}"

    def test_pip_pins_agree_across_files(self, subject_root):
        """Init containers and runtime-built Jobs share PYTHON_IMAGE; a split pin
        means one copy was missed (the v1 psycopg-on-python-3.15 break)."""
        rx = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?==([0-9][A-Za-z0-9.]*)")
        seen = {}
        for rel in _files(subject_root):
            if rel.startswith("platform/services/") and rel.endswith((".yaml", ".py")):
                for m in rx.finditer(_read(subject_root, rel)):
                    seen.setdefault(m.group(1).lower(), set()).add(m.group(2))
        split = {k: sorted(v) for k, v in seen.items() if len(v) > 1}
        assert not split, f"pip pins disagree across files: {split}"


class TestHelmReleasesArePinned:
    def _offenders(self, root):
        out = []
        for path in glob.glob(os.path.join(root, "terraform", "**", "*.tf"), recursive=True):
            if "/.terraform/" in path:
                continue
            hcl = _read(root, os.path.relpath(path, root))
            for m in re.finditer(r'resource\s+"helm_release"\s+"(\w+)"\s*\{', hcl):
                depth, j = 0, m.end() - 1
                for j in range(m.end() - 1, len(hcl)):
                    if hcl[j] == "{":
                        depth += 1
                    elif hcl[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                if not re.search(r"(?m)^\s*version\s*=", hcl[m.start():j + 1]):
                    out.append(f"{os.path.relpath(path, root)}:{m.group(1)}")
        return set(out)

    def test_every_helm_release_has_a_version(self, subject_root):
        new = self._offenders(subject_root) - KNOWN_UNPINNED_HELM_RELEASES
        assert not new, f"helm_release without `version` floats on every apply: {sorted(new)}"
