"""Security-group descriptions must satisfy EC2's character restrictions.

EC2 validates `aws_security_group` description fields -- the group's own and
each `ingress`/`egress` rule's -- against a charset that notably excludes the
apostrophe:

    ^[0-9A-Za-z_ .:/()#,@\\[\\]+=&;{}!$*-]*$

A violation is not caught by `terraform validate` (it is provider-side
validation) and produces no error until `terraform apply` runs. For most
modules that means a failed deploy. For `terraform/aws/mac` it meant something
worse, found by owner acceptance testing on 2026-08-26: the apostrophe in
"SSH from the operator's address" made `nyxgpt cloud deploy --os macos` fail
100% of the time, *after* the operator had read a cost disclosure for a
non-refundable 24-hour Dedicated Host charge and typed `allocate` to consent to
it. Consent was collected for an action the configuration could never perform.

This test exists because that class of defect is cheap to prevent and expensive
to discover: no AWS account, no Mac hardware and no spend are needed to catch
it, but nothing in CI planned these modules, so nothing did.

Scope note: the charset applies to *EC2* description fields. Terraform-side
`variable`/`output` descriptions never reach the AWS API, and other services'
description fields (e.g. `aws_scheduler_schedule`) have their own, laxer rules
-- applying EC2's charset to those produces false positives, so this test looks
only inside `aws_security_group` blocks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TERRAFORM_ROOT = REPO_ROOT / "terraform"

# EC2's documented charset for security-group descriptions.
EC2_DESCRIPTION_CHARSET = re.compile(r"^[0-9A-Za-z_ .:/()#,@\[\]+=&;{}!$*-]*$")

_SG_BLOCK_START = re.compile(r'^\s*resource\s+"aws_security_group"\s')
_RESOURCE_START = re.compile(r"^\s*(resource|data|module|variable|output|locals)\s")
_DESCRIPTION = re.compile(r'^\s*description\s*=\s*"((?:[^"\\]|\\.)*)"\s*$')


def _security_group_descriptions() -> list[tuple[Path, int, str]]:
    """Every `description = "..."` lexically inside an `aws_security_group` block.

    A brace-depth scan rather than a full HCL parse: these modules are
    hand-written and conventionally formatted, and a parser dependency would
    cost more than it buys for one charset check.
    """
    found: list[tuple[Path, int, str]] = []
    for tf_file in sorted(TERRAFORM_ROOT.rglob("*.tf")):
        depth = 0
        in_sg = False
        for lineno, line in enumerate(tf_file.read_text(encoding="utf-8").splitlines(), 1):
            if not in_sg and _SG_BLOCK_START.match(line):
                in_sg, depth = True, 0
            elif in_sg and depth == 0 and _RESOURCE_START.match(line):
                # A new top-level block began without the brace count ever
                # rising -- defensive, so a formatting change cannot make this
                # scan silently swallow the rest of the file.
                in_sg = False

            if in_sg:
                match = _DESCRIPTION.match(line)
                if match:
                    found.append((tf_file, lineno, match.group(1)))
                depth += line.count("{") - line.count("}")
                if depth <= 0 and "{" not in line and "}" in line:
                    in_sg = False
    return found


def test_the_scan_actually_finds_security_group_descriptions():
    """Guard the guard: a scan that silently matches nothing proves nothing."""
    found = _security_group_descriptions()
    assert found, (
        "no aws_security_group descriptions found under terraform/ -- the scan is "
        "broken or the modules moved, either way this file is no longer guarding "
        "anything"
    )
    files = {path.relative_to(REPO_ROOT).as_posix() for path, _, _ in found}
    assert any("mac" in f for f in files), (
        f"the EC2 Mac module's security group is not being scanned; found only {sorted(files)}"
    )


@pytest.mark.parametrize(
    "tf_file,lineno,description",
    [
        pytest.param(p, n, d, id=f"{p.relative_to(REPO_ROOT).as_posix()}:{n}")
        for p, n, d in _security_group_descriptions()
    ],
)
def test_security_group_descriptions_satisfy_the_ec2_charset(tf_file, lineno, description):
    offending = sorted({c for c in description if not EC2_DESCRIPTION_CHARSET.match(c)})
    assert not offending, (
        f"{tf_file.relative_to(REPO_ROOT).as_posix()}:{lineno} has "
        f"{offending} in a security-group description, which EC2 rejects at apply "
        f"time (charset: 0-9 A-Z a-z _ space . : / ( ) # , @ [ ] + = & ; {{ }} ! $ * -). "
        f"terraform validate does not catch this. Description was: {description!r}"
    )
