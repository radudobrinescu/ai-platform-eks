"""Pytest setup for the lifecycle branch.

The harness and the release tool are plain scripts, not an installed package;
put their directories on sys.path. Many tests read the product tree under test
(`$SUBJECT_ROOT`, default ./subject, a checkout of `main`). Without it those
tests skip locally; CI sets REQUIRE_SUBJECT=1 so a missing checkout fails.
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for rel in ("harness", "tools"):
    p = os.path.join(ROOT, rel)
    if p not in sys.path:
        sys.path.insert(0, p)


def subject_dir() -> str:
    return os.path.abspath(os.environ.get("SUBJECT_ROOT") or os.path.join(ROOT, "subject"))


@pytest.fixture(scope="session")
def subject_root() -> str:
    root = subject_dir()
    if not os.path.isfile(os.path.join(root, "platformctl")):
        if os.environ.get("REQUIRE_SUBJECT") == "1":
            pytest.fail(f"no product checkout at {root} (set SUBJECT_ROOT or check out main into ./subject)")
        pytest.skip(f"no product checkout at {root}")
    return root
