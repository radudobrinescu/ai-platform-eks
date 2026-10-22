#!/usr/bin/env python3
"""shellcheck every bash `run:` step of the given GitHub workflow files.

GitHub runs `run:` steps as `bash -e -o pipefail`; `${{ ... }}` expressions are
substituted before the shell sees them, so they are replaced by a placeholder
word here. Exit 1 if any step has a warning or error (`-S warning`).

  python3 tools/shellcheck_workflows.py .github/workflows/*.yaml
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

import yaml

EXPR = re.compile(r"\$\{\{.*?\}\}", re.S)


def steps(path: str):
    with open(path, encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)
    default_shell = ((wf.get("defaults") or {}).get("run") or {}).get("shell", "bash")
    for job_id, job in (wf.get("jobs") or {}).items():
        job_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell", default_shell)
        for i, step in enumerate(job.get("steps") or []):
            if "run" in step:
                yield f"{os.path.basename(path)}:{job_id}:{step.get('name') or i}", step.get("shell", job_shell), step["run"]


def main(paths: list[str]) -> int:
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for path in paths:
            for label, shell, script in steps(path):
                if shell.split()[0] not in ("bash", "sh"):
                    continue
                f = os.path.join(tmp, "step.sh")
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write("#!/usr/bin/env bash\nset -eo pipefail\n" + EXPR.sub("EXPR", script))
                r = subprocess.run(["shellcheck", "-S", "warning", "-s", "bash", f], capture_output=True, text=True)
                if r.returncode != 0:
                    failed += 1
                    print(f"--- {label}\n{r.stdout}{r.stderr}")
    print(f"shellcheck: {failed} failing step(s)" if failed else "shellcheck: all workflow run steps clean")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
