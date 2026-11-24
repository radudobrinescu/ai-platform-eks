"""Packaging guard: every Python package file shipped in a ConfigMap must be
projected back to its package path in every volume that imports the package.

ConfigMap keys cannot contain "/", so `scripts/pha/db.py` ships under the key
`pha__db.py` and volumes map it back with `items`. Forgetting one entry would
only fail at pod start (ImportError), so it is checked here.
"""
import os
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
