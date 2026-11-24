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


def test_every_watch_has_a_matching_rbac_rule():
    """A watch the ClusterRole does not allow fails at run time, in the cluster,
    with a 403 that no unit test sees. Cluster-scoped and all-namespace watches
    need the ClusterRole; this checks those."""
    import re

    import yaml

    from dashboard import config
    from dashboard.app import cache_specs

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "manifests.yaml"), encoding="utf-8") as f:
        docs = [d for d in yaml.safe_load_all(f) if d]
    rules = next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "cluster-dashboard")["rules"]
    allowed = {(g, r) for rule in rules if {"list", "watch"} <= set(rule["verbs"])
               for g in rule["apiGroups"] for r in rule["resources"]}
    settings = config.load({"CLUSTER_NAME": "c", "AWS_REGION": "eu-central-1"})
    missing = []
    for name, path, *_ in cache_specs(settings):
        m = re.fullmatch(r"/api(?:s/(?P<group>[^/]+))?/v[^/]+(?:/namespaces/[^/]+)?/(?P<resource>[^/]+)", path)
        assert m, f"unexpected watch path for {name}: {path}"
        if "/namespaces/" in path and (m.group("group") or "", m.group("resource")) not in allowed:
            continue      # namespaced watches may be granted by a Role in that namespace
        if (m.group("group") or "", m.group("resource")) not in allowed:
            missing.append(f"{name}: {path}")
    assert missing == []
