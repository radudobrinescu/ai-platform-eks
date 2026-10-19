"""Guard: every OSS component version pin is visible to Renovate.

The upgrade lifecycle (renovate.json + COMPATIBILITY.md) only works if Renovate can
*see* each pin. Pins live in six different shapes (ArgoCD ApplicationSet chart
versions, image tags in raw manifests, inline `pip ==`, kustomize `?ref=`,
Terraform, CI tool versions). A new pin added in a shape no manager covers would
silently fall out of the cycle and rot — exactly the drift this initiative exists
to prevent. This test fails the build when:

  * a file that contains an inventoried pin is matched by no manager file pattern;
  * a custom regex manager no longer matches the line it was written for
    (someone reformatted the line and broke the regex);
  * a `# renovate:` annotation points at a line the generic annotation regex
    cannot parse;
  * any Terraform `helm_release` has no `version` (a floating GitOps engine or
    operator is the one pin shape Renovate cannot propose a bump for).

It is a static test: it reads renovate.json and the repo, never calls Renovate.
The real end-to-end proof is Renovate's own extraction (`renovate --platform=local`
or the Dependency Dashboard); this test keeps the static invariants honest between
those runs.
"""

import glob
import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _renovate():
    return json.loads(_read("renovate.json"))


def _js_pattern_to_py(p):
    """Renovate managerFilePatterns are '/regex/' strings (or plain globs). We only
    use the regex form; strip the slashes."""
    assert p.startswith("/") and p.endswith("/"), f"use /regex/ form: {p}"
    return re.compile(p[1:-1])


def _tracked_files():
    out = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in {".git", ".terraform", "node_modules", "__pycache__"}]
        for f in files:
            out.append(os.path.relpath(os.path.join(root, f), REPO_ROOT))
    return out


def _all_manager_patterns(cfg):
    pats = []
    for mgr in ("kubernetes", "kustomize", "terraform"):
        for p in cfg.get(mgr, {}).get("managerFilePatterns", []):
            pats.append(_js_pattern_to_py(p))
    for cm in cfg.get("customManagers", []):
        for p in cm["managerFilePatterns"]:
            pats.append(_js_pattern_to_py(p))
    # github-actions manager is enabled by config:recommended with its default
    # file pattern; model it explicitly so workflows count as covered.
    pats.append(re.compile(r"^\.github/workflows/.+\.ya?ml$"))
    return pats


# --- inventory -----------------------------------------------------------------
# (regex that identifies a pin, file glob it may appear in). Keep in sync with
# the pin table in COMPATIBILITY.md when a new pin shape is introduced.
PIN_SHAPES = [
    (r"(?m)^\s*chartVersion:\s*\S+", "argocd/bootstrap/templates/*.yaml"),
    (r"(?m)^\s*image:\s*[\w./-]+:[\w.-]+\s*$", "platform/**/*.yaml"),
    (r"default=vllm/vllm-openai:v?\d", "platform/config/kro/*.yaml"),
    (r"routerChartVersion:\s*\"string\s*\|\s*default=", "platform/config/kro/*.yaml"),
    (r"\?ref=v?\d", "platform/**/kustomization.yaml"),
    (r"[A-Za-z0-9_-]+(\[[^\]]+\])?==\d", "platform/services/**/*.yaml"),
    (r"PYTHON_IMAGE:", "platform/services/cluster-dashboard/*.yaml"),
    (r"(?m)^\s*vllm_image_tag\s*=", "terraform/**/*.tf"),
    (r"(?m)^\s*chart_version\s*=", "terraform/**/*.tf"),
    (r'resource\s+"helm_release"', "terraform/**/*.tf"),
    (r"# renovate:", "**/*.tf"),
    (r"# renovate:", ".github/workflows/*.yaml"),
    (r"# renovate:", "platform/**/*.yaml"),
]


def _files_with_pins():
    hits = {}
    for rx, pattern in PIN_SHAPES:
        for path in glob.glob(os.path.join(REPO_ROOT, pattern), recursive=True):
            rel = os.path.relpath(path, REPO_ROOT)
            if "/.terraform/" in f"/{rel}" or rel.startswith(("aidlc-docs/", "docs/")):
                continue
            if rel.endswith((".md", ".json", ".py", ".example")):
                continue
            if not os.path.isfile(path):
                continue
            if re.search(rx, _read(rel)):
                hits.setdefault(rel, set()).add(rx)
    return hits


class TestEveryPinIsRenovateVisible:
    def test_renovate_json_parses_and_has_five_domains(self):
        cfg = _renovate()
        groups = {r.get("groupName") for r in cfg["packageRules"] if r.get("groupName")}
        assert groups == {"infra", "inference", "gateway", "gitops", "tooling"}, groups

    def test_every_file_with_a_pin_is_matched_by_some_manager(self):
        cfg = _renovate()
        pats = _all_manager_patterns(cfg)
        uncovered = {
            rel: shapes
            for rel, shapes in _files_with_pins().items()
            if not any(p.search(rel) for p in pats)
        }
        assert not uncovered, (
            "These files contain version pins that no Renovate manager file pattern "
            "matches; add a managerFilePatterns entry or a customManager in "
            f"renovate.json:\n{json.dumps({k: sorted(v) for k, v in uncovered.items()}, indent=2)}"
        )

    def test_each_custom_regex_matches_at_least_one_line_in_its_files(self):
        cfg = _renovate()
        for cm in cfg["customManagers"]:
            file_pats = [_js_pattern_to_py(p) for p in cm["managerFilePatterns"]]
            files = [f for f in _tracked_files() if any(p.search(f) for p in file_pats)]
            assert files, f"customManager '{cm['description'][:60]}' matches no files"
            total = 0
            for rel in files:
                text = _read(rel)
                for ms in cm["matchStrings"]:
                    # JS named groups (?<name>) are valid in Python 3 as (?P<name>).
                    rx = re.compile(ms.replace("(?<", "(?P<"))
                    total += len(list(rx.finditer(text)))
            assert total > 0, (
                f"customManager '{cm['description'][:60]}' matches no line in "
                f"{files}; a reformat probably broke the regex."
            )

    def test_known_pins_are_extracted_with_expected_dep_names(self):
        """Spot-check the regexes extract what we think they do."""
        cfg = _renovate()
        by_desc = {cm["description"][:20]: cm for cm in cfg["customManagers"]}

        appset = by_desc["Helm charts declared"]
        rx = re.compile(appset["matchStrings"][0].replace("(?<", "(?P<"))
        found = {m.group("depName"): m.group("currentValue")
                 for m in rx.finditer(_read("argocd/bootstrap/templates/platform.yaml"))}
        assert set(found) == {"gpu-operator", "langfuse"}, found

        pip = by_desc["Inline 'pip install "]
        rx = re.compile(pip["matchStrings"][0].replace("(?<", "(?P<"))
        names = set()
        for p in pip["managerFilePatterns"]:
            for rel in _tracked_files():
                if _js_pattern_to_py(p).search(rel):
                    names |= {m.group("depName") for m in rx.finditer(_read(rel))}
        assert {"kubernetes", "psycopg", "pyjwt", "pyyaml"} <= names, names

        generic = by_desc["Generic '# renovate:"]
        rx = re.compile(generic["matchStrings"][0].replace("(?<", "(?P<"))
        deps = {}
        for rel in _tracked_files():
            if rel.endswith(".tf") or rel.startswith(".github/workflows/"):
                if "/.terraform/" in f"/{rel}":
                    continue
                for m in rx.finditer(_read(rel)):
                    deps[m.group("depName")] = m.group("currentValue")
        assert deps.get("vllm/vllm-openai", "").startswith("v0."), deps
        # Shape, not value: Renovate bumps this line, and the test must keep passing.
        assert re.fullmatch(r"\d+\.\d+\.\d+", deps.get("aws-load-balancer-controller", "")), deps
        assert "gitleaks/gitleaks" in deps, deps

    def test_every_renovate_annotation_is_parsed(self):
        """A '# renovate:' comment that the generic regex cannot parse is a silent
        no-op; count annotations vs. parsed matches."""
        cfg = _renovate()
        generic = next(cm for cm in cfg["customManagers"] if cm["description"].startswith("Generic"))
        rx = re.compile(generic["matchStrings"][0].replace("(?<", "(?P<"))
        file_pats = [_js_pattern_to_py(p) for p in generic["managerFilePatterns"]]
        for rel in _tracked_files():
            if not any(p.search(rel) for p in file_pats) or "/.terraform/" in f"/{rel}":
                continue
            text = _read(rel)
            annotations = len(re.findall(r"# renovate: datasource=", text))
            parsed = len(list(rx.finditer(text)))
            assert annotations == parsed, (
                f"{rel}: {annotations} '# renovate:' annotation(s) but the generic regex "
                f"parsed {parsed}; the annotated line must look like KEY = \"value\" / "
                f"KEY: \"value\" / KEY=value on the very next line."
            )


class TestNoFloatingHelmRelease:
    def test_every_helm_release_has_a_version(self):
        offenders = []
        for path in glob.glob(os.path.join(REPO_ROOT, "terraform", "**", "*.tf"), recursive=True):
            if "/.terraform/" in path:
                continue
            with open(path, encoding="utf-8") as fh:
                hcl = fh.read()
            for m in re.finditer(r'resource\s+"helm_release"\s+"(\w+)"\s*\{', hcl):
                depth, j = 0, m.end() - 1
                for j in range(m.end() - 1, len(hcl)):
                    if hcl[j] == "{":
                        depth += 1
                    elif hcl[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                block = hcl[m.start():j + 1]
                if not re.search(r"(?m)^\s*version\s*=", block):
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{m.group(1)}")
        # ACK controllers are a for_each over a user-supplied map with no single
        # upstream version; they are opt-in and off by default. Everything else
        # must be pinned.
        offenders = [o for o in offenders if not o.endswith(":ack_selfmanaged")]
        assert not offenders, (
            "helm_release without `version` floats to latest on every apply and "
            f"Renovate cannot propose a bump for it: {offenders}"
        )
