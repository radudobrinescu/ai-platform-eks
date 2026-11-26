"""What this repository ships is the product, and only the product.

The tree is published as it is. It must not name the tooling or the private
repository that maintain it, and must not carry an AWS account id or an
internal hostname. Checked on
every tracked text file, so a stray comment fails here and not in public.
"""

import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

FORBIDDEN = {
    "update tooling": re.compile(r"renovate", re.I),
    "private repository": re.compile(r"radudobrinescu|ai-platform-eks\b", re.I),
    "AWS account id": re.compile(r"(?<![0-9A-Za-z_.-])[0-9]{12}(?![0-9A-Za-z_-])"),
    "internal hostname": re.compile(r"[A-Za-z0-9.-]+\.(?:corp\.amazon\.com|a2z\.com|aws\.dev|amazon\.dev)\b", re.I),
}
# Provider lock files and recorded Terraform plans hold hashes and sample ids; this file names the words.
SKIP = re.compile(r"(\.terraform\.lock\.hcl$|^tests/fixtures/|^tests/test_public_tree\.py$|^THIRD-PARTY-LICENSES$)")
# Documentation examples that are not real accounts.
EXAMPLE_IDS = {"123456789012", "111122223333", "000000000000"}


def tracked_files():
    out = subprocess.run(["git", "-C", REPO, "ls-files"], stdout=subprocess.PIPE, universal_newlines=True, check=True)
    return [f for f in out.stdout.splitlines() if f and not SKIP.search(f)]


def test_the_tree_names_no_tooling_no_private_repository_and_no_account():
    found = []
    for rel in tracked_files():
        try:
            with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                text = fh.read()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue  # binary, a submodule path, or deleted in the working tree
        for what, rx in FORBIDDEN.items():
            for m in rx.finditer(text):
                if what == "AWS account id" and m.group(0) in EXAMPLE_IDS:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                found.append("{}:{}: {} ({!r})".format(rel, line, what, m.group(0)))
    assert not found, "not fit for the public tree:\n  " + "\n  ".join(found[:40])
