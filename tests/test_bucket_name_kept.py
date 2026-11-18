"""An upgrade never renames the model cache bucket.

The bucket's name gained the account id after the first public state
(`<cluster>-model-cache` -> `<cluster>-<account>-model-cache`). A bucket cannot
be renamed: for an environment built before the change Terraform planned to
replace it, and with it the cached weights and every fine-tuned model under
`fine-tuned/`. The plan guard stopped that upgrade (2026-11-18). The name is
therefore fixed at creation, and everything else reads it from the bucket.
"""
import pathlib
import re

CLUSTER = pathlib.Path(__file__).resolve().parents[1] / "terraform" / "30.eks" / "30.cluster"


def _resource(text: str, kind: str, name: str) -> str:
    start = text.index(f'resource "{kind}" "{name}" {{')
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += {"{": 1, "}": -1}.get(text[j], 0)
        if depth == 0:
            return text[start:j + 1]
    raise AssertionError(f"unbalanced braces in {kind}.{name}")


def test_an_existing_bucket_keeps_its_name():
    body = _resource((CLUSTER / "capabilities.tf").read_text(), "aws_s3_bucket", "model_cache")
    lifecycle = re.search(r"lifecycle\s*\{[^}]*\}", body)
    assert lifecycle, "aws_s3_bucket.model_cache needs a lifecycle block"
    assert re.search(r"ignore_changes\s*=\s*\[\s*bucket\s*\]", lifecycle.group(0))


def test_nothing_else_builds_the_name_itself():
    """Only the bucket resource may use the computed name; a policy or a ConfigMap
    built from it would point at a bucket that an upgraded environment does not have."""
    users = []
    for tf in sorted(CLUSTER.glob("*.tf")):
        for n, line in enumerate(tf.read_text().splitlines(), 1):
            if "local.model_cache_bucket" in line:
                users.append(f"{tf.name}:{n}: {line.strip()}")
    assert len(users) == 1 and re.search(r"^capabilities\.tf:\d+: bucket\s*=\s*local\.model_cache_bucket$", users[0]), users
