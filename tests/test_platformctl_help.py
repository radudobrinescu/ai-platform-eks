"""`--help` never runs a command, and `up` leaves the environment ready to use.

Commands that take an environment name used to read `--help` as that name:
`tunnel --help` stopped the running tunnel and started a new one, `status --help`
ran status, and `up`, `down` and `use --help` died on a missing tfvars file.

A fresh `up` also left no kubectl context named after the environment, which the
release record, `status --check` and `upgrade` all look for.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("aws", "kubectl", "terraform", "make", "git", "session-manager-plugin", "python3", "curl")
ENV_COMMANDS = ("up", "use", "down", "status", "tunnel", "edge", "list-envs")


def _run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run platformctl with every external tool replaced by one that records the call."""
    calls = tmp_path / "calls"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in TOOLS:
        fake = bindir / tool
        fake.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\nexit 0\n')
        fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bindir}:/usr/bin:/bin", "PLATFORM_ENV": ""}
    result = subprocess.run(["bash", str(ROOT / "platformctl"), *args], capture_output=True, text=True,
                            timeout=30, env=env, stdin=subprocess.DEVNULL)
    return result, calls


@pytest.mark.parametrize("command", ENV_COMMANDS)
@pytest.mark.parametrize("flag", ("--help", "-h"))
def test_help_prints_the_usage_and_runs_nothing(tmp_path: Path, command: str, flag: str) -> None:
    result, calls = _run(tmp_path, command, flag)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "platformctl" in result.stdout and "upgrade" in result.stdout, result.stdout
    assert not calls.exists(), f"{command} {flag} ran: {calls.read_text()}"


def test_help_after_an_environment_name_also_runs_nothing(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "tunnel", "dev", "--help")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not calls.exists()


def _function(name: str) -> str:
    text = (ROOT / "platformctl").read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
    assert match, name
    return match.group(0)


def test_up_needs_kubectl_before_it_provisions() -> None:
    body = _function("cmd_up")
    assert "need kubectl" in body
    assert body.index("need kubectl") < body.index("make -C")


def test_up_names_the_kubectl_context_before_it_records_the_release() -> None:
    body = _function("cmd_up")
    assert 'cmd_use "$env"' in body, "up must create the kubectl context named after the environment"
    assert body.index("apply-all") < body.index('cmd_use "$env"') < body.index('_up_record_release "$env"')
