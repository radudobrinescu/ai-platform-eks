"""Helpers shared by the dashboard and agent tests.

Kept out of conftest.py on purpose: the repo-root tests/ also has a conftest.py,
so `from conftest import ...` would resolve to whichever one Python imported
first. This module has a unique name.
"""
import json
import os

# Fixture files end in .json.in (recorded inputs) and .json.golden (expected
# output), not .json, so CI's kubeconform scan of platform/services skips them.
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def check_golden(name, actual):
    """Compare `actual` to fixtures/<name>. Set UPDATE_GOLDEN=1 to rewrite it
    (only when a behaviour change is intended and reviewed)."""
    path = os.path.join(FIXTURES, name)
    text = json.dumps(actual, indent=2, sort_keys=True, default=str) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    assert os.path.exists(path), f"missing golden file {name}; run with UPDATE_GOLDEN=1"
    with open(path, encoding="utf-8") as f:
        expected = json.load(f)
    assert json.loads(text) == expected


# ---------------------------------------------------------------------------
# Fake psycopg connection
# ---------------------------------------------------------------------------

class FakeCursor:
    """Records every statement; answers from scripted (sql-fragment, rows) pairs.

    The first script entry whose fragment occurs in the statement wins and is
    consumed, so the same query can be answered differently on repeat calls."""

    def __init__(self, conn):
        self.conn = conn
        self.rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(sql.split()), params))
        self.rows, self.rowcount = [], 0
        for i, (fragment, rows) in enumerate(self.conn.script):
            if fragment in sql:
                self.conn.script.pop(i)
                if isinstance(rows, Exception):
                    raise rows
                if isinstance(rows, int):
                    self.rowcount = rows
                else:
                    self.rows = list(rows)
                    self.rowcount = len(self.rows)
                break

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, script=None):
        self.script = list(script or [])
        self.executed = []
        self.closed = False

    def cursor(self, row_factory=None):
        return FakeCursor(self)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sql(self):
        return [s for s, _ in self.executed]
