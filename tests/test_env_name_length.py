"""`up` refuses an env name whose node group prefix would exceed EKS's
37-character limit, instead of failing after the VPC and cluster exist."""

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _functions(*names):
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        text = fh.read()
    out = []
    for name in names:
        m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, flags=re.S | re.M)
        assert m, name
        out.append(m.group(0))
    return "".join(out)


def _check(tmp_path, env, prefix="ai-platform"):
    vars_dir = tmp_path / "00.global" / "vars"
    vars_dir.mkdir(parents=True)
    (vars_dir / f"{env}.tfvars").write_text(f'shared_config = {{\n  resources_prefix = "{prefix}"\n}}\n')
    with open(os.path.join(ROOT, "platformctl"), encoding="utf-8") as fh:
        text = fh.read()
    helpers = "\n".join(l for l in text.splitlines() if l.startswith(("tfvar_prefix()", "cluster_of()")))
    script = (f'set -euo pipefail\nTF_DIR="{tmp_path}"\ndie(){{ echo "DIE $*"; exit 1; }}\n{helpers}\n'
              + _functions("check_env_name_length") + f'check_env_name_length "{env}"\necho ok\n')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)


def test_nine_characters_fit(tmp_path):
    r = _check(tmp_path, "e2eum9999")
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


def test_ten_characters_are_refused_before_terraform(tmp_path):
    r = _check(tmp_path, "e2e-fd-100")
    assert r.returncode == 1 and "too long" in r.stdout and "at most 9 characters" in r.stdout


def test_up_checks_the_name_before_provisioning():
    body = _functions("cmd_up")
    assert body.index('check_env_name_length "$env"') < body.index("make -C")
