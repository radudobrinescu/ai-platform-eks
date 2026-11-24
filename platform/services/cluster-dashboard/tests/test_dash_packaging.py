"""Packaging guard: every Python package file shipped in a ConfigMap must be
projected back to its package path in every volume that imports the package.

ConfigMap keys cannot contain "/", so `scripts/pha/db.py` ships under the key
`pha__db.py` and volumes map it back with `items`. Forgetting one entry would
only fail at pod start (ImportError), so it is checked here.
"""
import ast
import os
import re
import shutil
import subprocess

import pytest
import yaml

DASH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name):
    with open(os.path.join(DASH, name), encoding="utf-8") as f:
        return [d for d in yaml.safe_load_all(f) if d]


def _package_keys():
    """{configmap name: {key: source path}} for keys of the form <pkg>__<file>."""
    out = {}
    for gen in _load("kustomization.yaml")[0]["configMapGenerator"]:
        for entry in gen.get("files", []):
            if "=" in entry:
                key, src = entry.split("=", 1)
                out.setdefault(gen["name"], {})[key] = src
    return out


def _projecting_volumes():
    """(workload, configmap, {key: path}) for every configMap volume with items."""
    found = []
    for manifest in ("manifests.yaml", "pha-event-watcher.yaml"):
        for doc in _load(manifest):
            if doc.get("kind") != "Deployment":
                continue
            for vol in doc["spec"]["template"]["spec"].get("volumes", []):
                cm = vol.get("configMap") or {}
                if cm.get("items"):
                    found.append((doc["metadata"]["name"], cm["name"],
                                  {i["key"]: i["path"] for i in cm["items"]}))
    return found


def test_keys_follow_the_naming_rule():
    for keys in _package_keys().values():
        for key, src in keys.items():
            pkg, filename = key.split("__", 1)
            assert src == f"scripts/{pkg}/{filename}", key
            assert os.path.exists(os.path.join(DASH, src)), src


def test_every_package_file_is_projected_wherever_the_package_is_mounted():
    keys = _package_keys()
    volumes = _projecting_volumes()
    assert {w for w, _, _ in volumes} >= {"cluster-dashboard", "event-watcher"}
    for workload, cm, items in volumes:
        pkg_items = {k: v for k, v in items.items() if "__" in k}
        packages = {v.split("/")[0] for v in pkg_items.values()}
        expected = {k: "/".join(k.split("__", 1)) for k in keys.get(cm, {}) if k.split("__", 1)[0] in packages}
        assert pkg_items == expected, f"{workload}: projection of {cm} out of date"
        # Plain files keep their key as their path.
        assert all(k == v for k, v in items.items() if "__" not in k), workload


def test_every_package_file_is_shipped():
    shipped = {src for keys in _package_keys().values() for src in keys.values()}
    for pkg in ("pha", "dashboard"):
        on_disk = {f"scripts/{pkg}/{f}" for f in os.listdir(os.path.join(DASH, "scripts", pkg))
                   if f.endswith(".py")}
        assert on_disk <= shipped, f"not in kustomization.yaml: {sorted(on_disk - shipped)}"


# ---------------------------------------------------------------------------
# Package install (init container)
# ---------------------------------------------------------------------------

def _install_script():
    dep = next(d for d in _load("manifests.yaml")
               if d.get("kind") == "Deployment" and d["metadata"]["name"] == "cluster-dashboard")
    (init,) = dep["spec"]["template"]["spec"]["initContainers"]
    assert init["command"] == ["/bin/sh", "-c"]
    (script,) = init["args"]
    return script


@pytest.mark.parametrize("pip_exit", [0, 1])
def test_install_container_exits_with_pips_status(tmp_path, pip_exit):
    """All Kubernetes data depends on the installed packages. A failed install
    must fail the init container (the kubelet then retries it with backoff
    and the pod is not Ready), not start a backend that stays empty."""
    bin_dir, pydeps = tmp_path / "bin", tmp_path / "pydeps"
    bin_dir.mkdir()
    pydeps.mkdir()
    pip = bin_dir / "pip"
    pip.write_text(f"#!/bin/sh\necho 'ERROR: no route to PyPI' >&2\nexit {pip_exit}\n")
    pip.chmod(0o755)
    script = _install_script().replace("/pydeps", str(pydeps)).replace("/tmp/", f"{tmp_path}/")
    done = subprocess.run(["/bin/sh", "-c", script], env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
                          capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == pip_exit
    if pip_exit:
        assert "ERROR: no route to PyPI" in done.stderr, "pip's error must reach the container log"
    assert list(pydeps.iterdir()) == [], "no failure marker: the exit status is the signal"


# ---------------------------------------------------------------------------
# A change to code the dashboard imports must restart the dashboard
# ---------------------------------------------------------------------------

BACKEND_CM = "cluster-dashboard-backend"


def _pha_imports(path):
    """Names of the `pha` modules a Python file imports (`from pha import x`,
    `import pha.x`, `from pha.x import y`), at any nesting level."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "pha":
            found |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and (n.module or "").startswith("pha."):
            found.add(n.module.split(".")[1])
        elif isinstance(n, ast.Import):
            found |= {a.name.split(".")[1] for a in n.names if a.name.startswith("pha.")}
    return found


def _dashboard_pha_modules():
    """Every pha module the dashboard process loads, directly or through
    another pha module, plus the package's __init__."""
    scripts = os.path.join(DASH, "scripts")
    todo = _pha_imports(os.path.join(scripts, "backend.py"))
    for name in os.listdir(os.path.join(scripts, "dashboard")):
        if name.endswith(".py"):
            todo |= _pha_imports(os.path.join(scripts, "dashboard", name))
    seen = set()
    while todo:
        mod = todo.pop()
        seen.add(mod)
        todo |= _pha_imports(os.path.join(scripts, "pha", f"{mod}.py")) - seen
    return seen | {"__init__"}


def _dashboard_volumes():
    dep = next(d for d in _load("manifests.yaml")
               if d.get("kind") == "Deployment" and d["metadata"]["name"] == "cluster-dashboard")
    return dep["spec"]["template"]["spec"]["volumes"]


def test_dashboard_code_comes_only_from_the_hashed_configmap():
    """Python is loaded once at process start. Everything the dashboard
    imports must be in the ConfigMap whose name carries a content hash, so a
    change gives the Deployment a new pod template and restarts the pod. The
    agent's ConfigMap has a fixed name: a change there restarts nothing."""
    assert _dashboard_pha_modules() >= {"__init__", "db", "jobs", "log"}
    gen = {g["name"]: g for g in _load("kustomization.yaml")[0]["configMapGenerator"]}
    assert not (gen[BACKEND_CM].get("options") or {}).get("disableNameSuffixHash")
    shipped = _package_keys()[BACKEND_CM]
    for mod in sorted(_dashboard_pha_modules()):
        assert shipped.get(f"pha__{mod}.py") == f"scripts/pha/{mod}.py", f"pha/{mod}.py is not in {BACKEND_CM}"
    code_volumes = {v["name"]: v["configMap"]["name"] for v in _dashboard_volumes()
                    if (v.get("configMap") or {}).get("items")}
    assert code_volumes == {"backend-script": BACKEND_CM, "pha-lib": BACKEND_CM}


def _kustomize():
    for cmd in (["kustomize", "build"], ["kubectl", "kustomize"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


def _rendered_pod_template(directory):
    out = subprocess.run([*_kustomize(), directory], capture_output=True, text=True, timeout=120, check=True).stdout
    dep = next(d for d in yaml.safe_load_all(out)
               if d and d.get("kind") == "Deployment" and d["metadata"]["name"] == "cluster-dashboard")
    return dep["spec"]["template"]


@pytest.mark.skipif(_kustomize() is None, reason="needs kustomize or kubectl")
@pytest.mark.parametrize("changed,restarts", [
    ("scripts/pha/db.py", True),               # approval SQL
    ("scripts/pha/jobs.py", True),             # Remediator Job spec
    ("scripts/pha/log.py", True),
    ("scripts/pha/__init__.py", True),
    ("scripts/dashboard/api.py", True),
    ("scripts/pha/watch.py", False),           # agent only: the dashboard does not import it
])
def test_rendered_pod_template_changes_with_the_code_the_dashboard_imports(tmp_path, changed, restarts):
    work = tmp_path / "cluster-dashboard"
    shutil.copytree(DASH, work, ignore=shutil.ignore_patterns("tests", "__pycache__"))
    before = _rendered_pod_template(str(work))
    with open(work / changed, "a", encoding="utf-8") as f:
        f.write("\n# a change\n")
    after = _rendered_pod_template(str(work))
    assert (after != before) is restarts


# ---------------------------------------------------------------------------
# Comments in the shipped code point only at things that exist
# ---------------------------------------------------------------------------

def _shipped_python():
    for root, _dirs, files in os.walk(os.path.join(DASH, "scripts")):
        for name in sorted(files):
            if name.endswith(".py"):
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    yield os.path.relpath(os.path.join(root, name), DASH), f.read()


def test_shipped_code_does_not_cite_the_unpublished_design_notes():
    """The design notes (aidlc-docs/, with their rule numbers R1.., L1..) are
    not in the repository, so a reference to them leads nowhere."""
    for path, text in _shipped_python():
        flat = " ".join(text.split())
        assert "aidlc-docs" not in flat, path
        assert "functional design" not in flat and "functional-design" not in flat, path
        assert not re.search(r"\((?:[RL]\d{1,2})(?:,\s*[RL]\d{1,2})*\)", flat), f"{path}: rule number without its document"


def test_derive_docstring_names_something_that_exists():
    from dashboard import app, derive
    names = re.findall(r"\bapp\.(\w+)", derive.__doc__)
    assert names and all(hasattr(app, n) for n in names), names
