"""aws_cognito_user custom attributes must be written without the "custom:"
prefix. The provider strips the prefix on read, so a prefixed key never matches
state and every apply after the first deletes the attribute (seed users lost
their role on upgrade)."""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_seed_user_attributes_have_no_custom_prefix():
    with open(os.path.join(ROOT, "terraform/30.eks/30.cluster/cognito.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    block = re.search(r'resource "aws_cognito_user" "seed" \{(.*?)\n\}', tf, re.S).group(1)
    attrs = re.search(r"attributes = \{(.*?)\n  \}", block, re.S).group(1)
    assert "custom:" not in attrs
    assert re.search(r"^\s+role\s+= local\.sso_litellm_roles\[each\.value\]", attrs, re.M)


def test_seed_users_get_no_invitation_email():
    with open(os.path.join(ROOT, "terraform/30.eks/30.cluster/cognito.tf"), encoding="utf-8") as fh:
        tf = fh.read()
    block = re.search(r'resource "aws_cognito_user" "seed" \{(.*?)\n\}', tf, re.S).group(1)
    assert re.search(r'^\s+message_action\s+= "SUPPRESS"$', block, re.M)
