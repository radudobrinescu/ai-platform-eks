"""The upgrade helpers must run on the oldest Python a forker is likely to have.

`platformctl upgrade` calls plan_guard.py and release_manifest.py with the
system python3, which is 3.9 on macOS (/usr/bin/python3). Parsing with
feature_version=(3, 9) rejects 3.10+ syntax such as `match` and `X | Y`
annotations. Both modules must also stay standard-library only.
"""

import ast
import os
import sys

import pytest

LIB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "lib")
MODULES = ("plan_guard.py", "release_manifest.py")


def source(name):
    with open(os.path.join(LIB, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.parametrize("name", MODULES)
def test_parses_as_python_39(name):
    ast.parse(source(name), filename=name, feature_version=(3, 9))


@pytest.mark.parametrize("name", MODULES)
def test_standard_library_only(name):
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is None:  # Python < 3.10
        pytest.skip("sys.stdlib_module_names needs Python 3.10+")
    imported = set()
    for node in ast.walk(ast.parse(source(name))):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported <= set(stdlib), imported - set(stdlib)


@pytest.mark.parametrize("name", MODULES)
def test_no_binary_union_annotations(name):
    # `int | None` in an annotation parses on 3.9 but fails at import time there.
    for node in ast.walk(ast.parse(source(name))):
        ann = getattr(node, "annotation", None) or getattr(node, "returns", None)
        if ann is not None:
            assert not any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr) for n in ast.walk(ann)), name
