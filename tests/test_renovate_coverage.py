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
    (".github/workflows/ci.yaml", "hashicorp/terraform"),
    (".github/workflows/ci.yaml", "ruff"),
    (".github/workflows/ci.yaml", "pytest"),
    (".github/workflows/ci.yaml", "pyyaml"),
    (".github/workflows/ci.yaml", "yamllint"),
    (".github/workflows/ci.yaml", "yannh/kubeconform"),
    (".github/workflows/ci.yaml", "kubernetes-sigs/kustomize"),
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
    def test_five_domains(self):
        groups = {r.get("groupName") for r in _renovate()["packageRules"] if r.get("groupName")}
        assert groups == {"infra", "inference", "gateway", "gitops", "tooling"}, groups

    def test_targets_main_with_release_age_and_security_exemption(self):
        cfg = _renovate()
        assert cfg["baseBranchPatterns"] == ["main"]
        assert cfg["forkProcessing"] == "enabled"
        assert cfg["minimumReleaseAge"] == "7 days"
        va = cfg["vulnerabilityAlerts"]
        assert va["minimumReleaseAge"] is None and "security" in va["labels"]

    def test_nothing_automerges(self):
        text = json.dumps(_renovate())
        assert '"automerge": true' not in text

    def test_majors_and_anchors_need_dashboard_approval(self):
        rules = _renovate()["packageRules"]
        assert any(r.get("matchUpdateTypes") == ["major"] and r.get("dependencyDashboardApproval") for r in rules)
        anchors = next(r for r in rules if "vllm/vllm-openai" in r.get("matchDepNames", [])
                       and r.get("dependencyDashboardApproval"))
        assert {"karpenter", "argo-cd", "kro"} <= set(anchors["matchDepNames"])

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
