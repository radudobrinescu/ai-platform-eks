"""Packaging guard: every Python package file shipped in a ConfigMap must be
projected back to its package path in every volume that imports the package.

ConfigMap keys cannot contain "/", so `scripts/pha/db.py` ships under the key
`pha__db.py` and volumes map it back with `items`. Forgetting one entry would
only fail at pod start (ImportError), so it is checked here.
"""
import os

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
        expected = {k: "/".join(k.split("__", 1)) for k in keys.get(cm, {})
                    if k.split("__", 1)[0] in {p.split("/")[0] for p in items.values()}}
        assert items == expected, f"{workload}: projection of {cm} out of date"


def test_every_package_file_is_shipped():
    shipped = {src for keys in _package_keys().values() for src in keys.values()}
    for pkg in ("pha",):
        on_disk = {f"scripts/{pkg}/{f}" for f in os.listdir(os.path.join(DASH, "scripts", pkg))
                   if f.endswith(".py")}
        assert on_disk <= shipped, f"not in kustomization.yaml: {sorted(on_disk - shipped)}"
