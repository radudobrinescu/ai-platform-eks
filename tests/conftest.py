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

# Tests build throwaway git repos and commit in them. CI runners have no global
# git identity, so give the test process one (an explicit environment wins).
for _key, _value in (("GIT_AUTHOR_NAME", "lifecycle-tests"), ("GIT_AUTHOR_EMAIL", "tests@example.invalid"),
                     ("GIT_COMMITTER_NAME", "lifecycle-tests"), ("GIT_COMMITTER_EMAIL", "tests@example.invalid")):
    os.environ.setdefault(_key, _value)


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
