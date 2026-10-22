"""Render an E2E env's tfvars from the subject's own example.tfvars.

The harness copies `terraform/00.global/vars/example.tfvars` exactly as a forker
would and changes only what a forker must (design section 3.2):

  region                                 the E2E region
  cluster_endpoint_public_access_cidrs   the runner's egress IP (uncommented)
  gitops_repo_url / gitops_revision      this repo, throwaway branch e2e/<run-id>
  profile managed only:                  eks_capabilities = true + capabilities_config

Everything else keeps the shipped defaults, so the `default` profile is exactly
what a new forker gets. Pure text functions; no file or network access.
"""
from __future__ import annotations

import re

PLACEHOLDER_RX = re.compile(r"REPLACE|XXXXXXXXXX|YOUR-ORG|YOUR-REPO")
PROFILES = ("default", "managed")


class TfvarsError(ValueError):
    pass


def strip_comments(text: str) -> str:
    """Drop `#` comments (outside double-quoted strings), keep line structure."""
    out = []
    for line in text.splitlines():
        in_str, esc, cut = False, False, len(line)
        for i, ch in enumerate(line):
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif ch == "#" and not in_str:
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def _set_top_level(text: str, name: str, value_hcl: str) -> str:
    rx = re.compile(rf"(?m)^{re.escape(name)}\s*=.*$")
    line = f"{name} = {value_hcl}"
    if rx.search(text):
        return rx.sub(line, text, count=1)
    return text.rstrip("\n") + f"\n\n{line}\n"


def _q(value: str) -> str:
    if '"' in value or "\n" in value or "\\" in value:
        raise TfvarsError(f"refusing to write an unsafe value into tfvars: {value!r}")
    return f'"{value}"'


def _block_span(text: str, name: str) -> tuple[int, int]:
    """(start of `name = {`, index of its matching `}`) for a top-level block,
    ignoring braces in comments and strings."""
    m = re.search(rf"(?m)^{re.escape(name)}\s*=\s*\{{", text)
    if not m:
        raise TfvarsError(f"no top-level `{name} = {{` block in example.tfvars")
    depth, in_str, in_comment = 0, False, False
    for i in range(m.end() - 1, len(text)):
        ch = text[i]
        if in_comment:
            in_comment = ch != "\n"
            continue
        if in_str:
            in_str = ch != '"'
            continue
        if ch == "#":
            in_comment = True
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return m.start(), i
    raise TfvarsError(f"unbalanced braces in `{name}` block")


def capabilities_config_block(idc_instance_arn: str, idc_region: str, idc_group_id: str) -> str:
    return (
        "\n  # E2E profile `managed`: EKS Managed Capabilities with Identity Center.\n"
        "  capabilities_config = {\n"
        f"    argocd_idc_instance_arn = {_q(idc_instance_arn)}\n"
        f"    argocd_idc_region       = {_q(idc_region)}\n"
        "    argocd_rbac_mappings = [\n"
        "      {\n"
        '        role = "ADMIN"\n'
        "        identities = [\n"
        f'          {{ id = {_q(idc_group_id)}, type = "SSO_GROUP" }}\n'
        "        ]\n"
        "      }\n"
        "    ]\n"
        "  }\n"
    )


def render(example: str, *, region: str, repo_url: str, revision: str, operator_cidr: str,
           profile: str = "default", idc: dict | None = None) -> str:
    """Return the env tfvars text. `idc` (profile managed) has keys
    instance_arn, region, group_id."""
    if profile not in PROFILES:
        raise TfvarsError(f"unknown profile {profile!r} (expected one of {PROFILES})")
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}/\d{1,2}", operator_cidr):
        raise TfvarsError(f"operator CIDR must be IPv4 a.b.c.d/nn, got {operator_cidr!r}")
    if operator_cidr.startswith("0.0.0.0"):
        raise TfvarsError("refusing to open the EKS API to 0.0.0.0/0")

    s = example
    s = _set_top_level(s, "region", _q(region))
    cidr_line = f"cluster_endpoint_public_access_cidrs = [{_q(operator_cidr)}]"
    s, n = re.subn(r"(?m)^#\s*cluster_endpoint_public_access_cidrs\s*=.*$", cidr_line, s, count=1)
    if n == 0:
        s = _set_top_level(s, "cluster_endpoint_public_access_cidrs", f"[{_q(operator_cidr)}]")
    s = _set_top_level(s, "gitops_repo_url", _q(repo_url))
    s = _set_top_level(s, "gitops_revision", _q(revision))

    if profile == "managed":
        if not idc or not all(idc.get(k) for k in ("instance_arn", "region", "group_id")):
            raise TfvarsError("profile managed needs E2E_IDC_INSTANCE_ARN, E2E_IDC_REGION and E2E_IDC_GROUP_ID")
        s, n = re.subn(r"(?m)^(\s*eks_capabilities\s*=\s*)false\b", r"\1true", s, count=1)
        if n == 0 and not re.search(r"(?m)^\s*eks_capabilities\s*=\s*true\b", s):
            raise TfvarsError("no `eks_capabilities = false` line to switch on in example.tfvars")
        start, close = _block_span(s, "cluster_config")
        if re.search(r"(?m)^\s*capabilities_config\s*=", strip_comments(s[start:close])):
            raise TfvarsError("example.tfvars already sets capabilities_config; refusing to guess")
        block = capabilities_config_block(idc["instance_arn"], idc["region"], idc["group_id"])
        s = s[:close] + block + s[close:]

    left = [ln.strip() for ln in strip_comments(s).splitlines() if PLACEHOLDER_RX.search(ln)]
    if left:
        raise TfvarsError(f"placeholders left outside comments: {left}")
    return s


# --------------------------------------------------------------------------- #
# Edits a forker makes later in the lifecycle
# --------------------------------------------------------------------------- #

def is_set(text: str, name: str) -> bool:
    """True if `name = ...` is assigned (at any nesting level) outside comments."""
    return re.search(rf"(?m)^\s*{re.escape(name)}\s*=", strip_comments(text)) is not None


def top_level_assignment(text: str, name: str) -> str | None:
    """The full `name = ...` statement (multi-line for {...} / [...]) at top level."""
    m = re.search(rf"(?m)^{re.escape(name)}\s*=", text)
    if not m:
        return None
    i, depth, in_str = m.end(), 0, False
    while i < len(text):
        ch = text[i]
        if in_str:
            in_str = ch != '"'
        elif ch == '"':
            in_str = True
        elif ch == "#":
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif ch == "\n" and depth == 0:
            break
        i += 1
    return text[m.start():i].rstrip()


def add_required(env_text: str, example_text: str, names: list[str]) -> tuple[str, list[str]]:
    """Copy each missing top-level variable from the (new) example.tfvars into the
    env tfvars, as a release's notes tell a forker to. Returns (text, added)."""
    added = []
    for name in names:
        if is_set(env_text, name):
            continue
        stmt = top_level_assignment(example_text, name)
        if stmt is None:
            raise TfvarsError(f"required variable {name!r} is not set in the new example.tfvars")
        env_text = env_text.rstrip("\n") + f"\n\n# Added for the upgrade (new required variable).\n{stmt}\n"
        added.append(name)
    return env_text, added


def kubernetes_version(text: str) -> str | None:
    m = re.search(r'(?m)^\s*kubernetes_version\s*=\s*"([^"]+)"', strip_comments(text))
    return m.group(1) if m else None


def bump_kubernetes_minor(text: str) -> tuple[str, str]:
    """Raise cluster_config.kubernetes_version by exactly one minor."""
    cur = kubernetes_version(text)
    if not cur or not re.fullmatch(r"\d+\.\d+", cur):
        raise TfvarsError(f"cannot read kubernetes_version (got {cur!r})")
    major, minor = (int(x) for x in cur.split("."))
    new = f"{major}.{minor + 1}"
    text = re.sub(r'(?m)^(\s*kubernetes_version\s*=\s*)"[^"]+"', rf'\g<1>"{new}"', text, count=1)
    return text, new
