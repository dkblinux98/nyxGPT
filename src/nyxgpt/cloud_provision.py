"""Target-OS provisioning for AWS instances (`nyxgpt cloud user-data`, P6-12/#3511).

Renders the EC2 user-data bootstrap script that installs nyxGPT from
published artifacts and brings up the native stack on a fresh instance --
PyPI + systemd on a Linux AMI, the remote Homebrew tap + launchd on EC2
Mac. Mirrors the OS-dispatch shape `nyxgpt.ops` uses for the local native
install path (`_is_macos`/`_is_linux`, #3508), except the dispatch key here
is the *target* instance's OS family, chosen by the caller with `--os`, not
the machine `nyxgpt` itself runs on: rendering happens on the operator's
workstation (or CI); only the rendered script ever runs on the instance.

Repo-less (CLAUDE.md, 2026-08-01): every rendered script installs nyxGPT
from a published artifact only -- never `git clone` -- so it works on a
target instance with no repo checkout. See `docs/cloud.md`'s target-OS
support matrix for exactly which AMI families and macOS versions this
covers.

This module only renders the bootstrap script; it does not talk to AWS.
Embedding the rendered output into an actual EC2 launch (as Terraform
`user_data`, P6-8) or invoking it as part of `nyxgpt cloud deploy` (P6-11)
is separate, not-yet-implemented scope -- see `nyxgpt.cloud`'s module
docstring.
"""

from __future__ import annotations

import argparse
import importlib.resources
import sys
from pathlib import Path

from nyxgpt.cloud import CloudCommandError

VERSION_PLACEHOLDER = "__NYXGPT_VERSION__"

# One entry per supported target OS family: the `--os` value, the packaged
# template filename (see scripts/cloud/, symlinked into
# src/nyxgpt/resources/cloud/ the same way docker/ and ops/ are), and the
# support matrix documented in docs/cloud.md.
_TEMPLATE_FILENAMES: dict[str, str] = {
    "linux": "ec2-user-data-linux.sh.tmpl",
    "macos": "ec2-user-data-macos.sh.tmpl",
}

OS_FAMILIES: tuple[str, ...] = tuple(_TEMPLATE_FILENAMES)

# Support matrix: what's actually validated (docs/cloud.md renders this same
# data as a table, and tests/unit/test_cloud_provision.py asserts the two
# stay in sync in spirit -- source of truth lives here, not duplicated by
# hand in the docs).
LINUX_AMI_SUPPORT_MATRIX: tuple[dict[str, str], ...] = (
    {
        "family": "Amazon Linux 2023",
        "arch": "x86_64, arm64",
        "package_manager": "dnf",
        "notes": "AWS's own default Linux AMI; systemd present, native path (#3508) applies unmodified.",
    },
    {
        "family": "Ubuntu 22.04 / 24.04 LTS",
        "arch": "x86_64, arm64",
        "package_manager": "apt",
        "notes": "Canonical's official AWS AMIs; same CI-tested distro family as linux-native-smoke.yml.",
    },
)

MACOS_EC2_SUPPORT_MATRIX: tuple[dict[str, str], ...] = (
    {
        "instance_type": "mac2.metal / mac2-m2.metal / mac2-m2pro.metal (Apple Silicon)",
        "macos_version": "Sonoma 14, Sequoia 15",
        "notes": (
            "Homebrew installs to /opt/homebrew, matching the local Apple Silicon "
            "native path unmodified. Requires a Dedicated Host (24h min. allocation)."
        ),
    },
    {
        "instance_type": "mac1.metal (Intel)",
        "macos_version": "Ventura 13, Sonoma 14",
        "notes": (
            "Homebrew installs to /usr/local, matching the local Intel native path "
            "unmodified. Requires a Dedicated Host (24h min. allocation)."
        ),
    },
)


def _template_root() -> Path:
    """Resolve the packaged `scripts/cloud/` template directory.

    Resolves via `importlib.resources`, identically whether nyxGPT runs from
    an editable dev checkout (`src/nyxgpt/resources/cloud` symlinks back to
    `scripts/cloud/`) or an installed, non-editable wheel (a real copy of
    the same files, bundled at build time -- see pyproject.toml's
    `[tool.setuptools.package-data]`, same mechanism as `nyxgpt.ops`'s
    `_packaged_resources_root`).
    """
    return Path(str(importlib.resources.files("nyxgpt.resources").joinpath("cloud")))


def render_user_data(os_family: str, version: str | None = None) -> str:
    """Render the EC2 user-data bootstrap script for `os_family`.

    `version`, when given, pins the Linux template's `pip install
    nyxgpt==<version>`; the macOS template accepts it only for interface
    parity (Homebrew tracks the tap's current formula, not a pinned
    release -- see that template's header comment). Omit `version` (or pass
    `None`) to install whatever's latest.
    """
    if os_family not in _TEMPLATE_FILENAMES:
        raise CloudCommandError(
            f"Unsupported --os {os_family!r} -- choose one of: {', '.join(OS_FAMILIES)}"
        )
    template_path = _template_root() / _TEMPLATE_FILENAMES[os_family]
    if not template_path.is_file():
        raise CloudCommandError(f"Missing packaged user-data template: {template_path}")
    rendered = template_path.read_text(encoding="utf-8")
    return rendered.replace(VERSION_PLACEHOLDER, version or "")


def user_data(args: argparse.Namespace) -> int:
    """`nyxgpt cloud user-data` entry point: print (or write) the rendered bootstrap script."""
    try:
        rendered = render_user_data(args.os, getattr(args, "version", None))
    except CloudCommandError as exc:
        print(f"nyxgpt cloud user-data: {exc}", file=sys.stderr)
        return 1

    output = getattr(args, "output", None)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.os} user-data to {output}", file=sys.stderr)
    else:
        print(rendered, end="")
    return 0
