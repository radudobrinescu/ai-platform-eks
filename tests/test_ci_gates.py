"""Guard: the static CI gate stays a gate.

`main` auto-syncs to the cluster via ArgoCD, so .github/workflows/ci.yaml is the
only thing standing between a bad manifest (or a Renovate bump that breaks a Helm
chart's values) and a live cluster. These tests fail the build when someone
quietly weakens that gate:

  * a validation step is made non-blocking with `|| true` (the ruff style report
    is the one deliberate exception);
  * a Helm element in the platform ApplicationSet lacks one of the four fields the
    CI render step reads (chart, chartRepo, chartVersion, valuesPath), or its
    values file is missing — the step would skip it silently;
  * a `*_VERSION` env in ci.yaml has no `# renovate:` annotation directly above it,
    which would drop that tool out of the upgrade cycle.
"""

import os
import re

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CI = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yaml")
APPSET = os.path.join(REPO_ROOT, "argocd", "bootstrap", "templates", "platform.yaml")


def _ci_text():
    with open(CI, encoding="utf-8") as fh:
        return fh.read()


def _ci():
    return yaml.safe_load(_ci_text())


class TestNoSilentlyNonBlockingSteps:
    def test_only_the_ruff_style_report_may_swallow_failures(self):
        offenders = []
        for job_name, job in _ci()["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run") or ""
                if re.search(r"\|\|\s*true\b", run):
                    offenders.append(f"{job_name}: {step.get('name', '<unnamed>')}")
        assert offenders == ["python: ruff (non-blocking style report)"], (
            "A CI validation step swallows its exit code with `|| true`; that turns the "
            f"pre-merge gate into a report. Offenders: {offenders}"
        )


class TestHelmRenderStepInputs:
    def test_every_helm_element_is_complete_and_values_exist(self):
        with open(APPSET, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        elements = doc["spec"]["generators"][0]["list"]["elements"]
        helm = [e for e in elements if e.get("type") == "helm"]
        assert helm, "expected at least one type: helm element in the platform ApplicationSet"
        for el in helm:
            for key in ("name", "chart", "chartRepo", "chartVersion", "valuesPath", "namespace"):
                assert key in el, f"helm element {el.get('name')} lacks '{key}' (CI render step reads it)"
            assert os.path.isfile(os.path.join(REPO_ROOT, el["valuesPath"])), (
                f"{el['name']}: valuesPath {el['valuesPath']} does not exist"
            )

    def test_ci_renders_charts_from_the_applicationset(self):
        text = _ci_text()
        assert "argocd/bootstrap/templates/platform.yaml" in text
        assert "helm template" in text and "kubeconform" in text


class TestToolPinsAreRenovateVisible:
    def test_every_version_env_has_an_annotation_directly_above(self):
        lines = _ci_text().splitlines()
        missing = []
        for i, line in enumerate(lines):
            m = re.match(r"\s*([A-Z0-9_]+_VERSION):\s*\"?[^\"\s]+\"?\s*$", line)
            if m and not re.match(r"\s*# renovate: datasource=", lines[i - 1]):
                missing.append(m.group(1))
        assert not missing, f"CI tool pins without a '# renovate:' annotation on the line above: {missing}"

    def test_versions_are_used_not_duplicated(self):
        """Each *_VERSION env must be referenced in a run: step; a stale literal
        copy of the version elsewhere would silently diverge from the pin."""
        ci = _ci()
        text = _ci_text()
        for job in ci["jobs"].values():
            for name, value in (job.get("env") or {}).items():
                if not name.endswith("_VERSION"):
                    continue
                assert f"${{{name}}}" in text, f"{name} is declared but never used"
                occurrences = len(re.findall(re.escape(str(value)), text))
                assert occurrences == 1, (
                    f"{name}={value} appears {occurrences} times in ci.yaml; keep the "
                    f"literal only on the annotated env line."
                )
