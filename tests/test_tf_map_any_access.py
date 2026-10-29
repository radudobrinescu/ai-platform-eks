"""Guard: keys of `optional(map(any), {})` config maps are read with lookup().

Terraform before 1.12 evaluates both sides of `&&`, so
`var.x.aws_oss_tooling && var.x.aws_oss_tooling_config.enable_y` fails with
"Missing map element" whenever the map is empty, including with the shipped
example.tfvars (aws_oss_tooling = false, no config map). required_version is
">= 1.1.0", so the code must work on those versions.
"""

import glob
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOT_ACCESS = re.compile(r"\bvar\.observability_configuration\.aws_oss_tooling_config\.[A-Za-z_]+")


def test_no_dot_access_into_aws_oss_tooling_config():
    hits = []
    for path in glob.glob(os.path.join(ROOT, "terraform", "**", "*.tf"), recursive=True):
        if "/.terraform/" in path:
            continue
        with open(path, encoding="utf-8") as fh:
            lines = [ln.split("#", 1)[0] for ln in fh]
        for n, code in enumerate(lines, 1):
            # try(..., default) also absorbs the error; it may open a few lines up.
            if DOT_ACCESS.search(code) and "try(" not in "".join(lines[max(0, n - 4):n]):
                hits.append(f"{os.path.relpath(path, ROOT)}:{n}")
    assert not hits, "use lookup(map, key, default) instead of dot access: " + ", ".join(hits)
