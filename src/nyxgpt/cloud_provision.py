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

**Who consumes the rendered script (#3867).** `nyxgpt cloud deploy --os`
does: it resolves the target OS family and pipes the matching bootstrap to
the instance over its wrapped SSH path (`cloud_deploy.render_provision_script`
calls `render_user_data` for the macOS family). That is the user-facing
provisioning flow, and it is the only one -- until #3867 the macOS script had
no consumer at all, so bootstrapping an EC2 Mac meant a human pasting this
output into an AWS console instance launch, which CLAUDE.md's Operational
Command Wrapping requirement forbids.

`nyxgpt cloud user-data` remains as the renderer's own command, for the
first-boot `user_data` case an SSH-driven deploy cannot serve (an instance
launched by something other than nyxGPT) and for the CI jobs that execute a
rendered bootstrap directly (`cloud-artifact-smoke.yml`,
`release-artifacts.yml`'s `ec2-linux-user-data-smoke`). Attaching it as
Terraform `user_data` (P6-8) is still not done: the substrate sets none.
"""

from __future__ import annotations

import argparse
import importlib.resources
import sys
from pathlib import Path

from nyxgpt.cloud import CloudCommandError
from nyxgpt.cloud_deploy import DEFAULT_SESSION_BACKEND
from nyxgpt.config import VALID_SESSION_BACKENDS

VERSION_PLACEHOLDER = "__NYXGPT_VERSION__"
SESSION_BACKEND_PLACEHOLDER = "__NYXGPT_SESSION_BACKEND__"

# The macOS bootstrap's Homebrew formula names, resolved from the version the
# deploy declares (#4122). See `homebrew_formulas_for_version`.
BREW_FORMULAS_PLACEHOLDER = "__NYXGPT_BREW_FORMULAS__"
BREW_API_FORMULA_PLACEHOLDER = "__NYXGPT_BREW_API_FORMULA__"
BREW_WEB_FORMULA_PLACEHOLDER = "__NYXGPT_BREW_WEB_FORMULA__"

# One entry per supported target OS family: the `--os` value, the packaged
# template filename (see scripts/cloud/, symlinked into
# src/nyxgpt/resources/cloud/ the same way docker/ and ops/ are), and the
# support matrix documented in docs/cloud.md.
_TEMPLATE_FILENAMES: dict[str, str] = {
    "linux": "ec2-user-data-linux.sh.tmpl",
    "macos": "ec2-user-data-macos.sh.tmpl",
}

OS_FAMILIES: tuple[str, ...] = tuple(_TEMPLATE_FILENAMES)

# Which session backend each target OS gets when `--session-backend` is not
# given (#3865). Not one constant, because the two templates do not provision
# the same stack:
#
#   linux  `nyxgpt ops install` creates the `nyxgpt-cassandra` container as a
#          core service, so `cassandra` is available on the instance the
#          moment the bootstrap finishes. Default it, matching what
#          k8s/configmap.yaml asserts and what `nyxgpt cloud deploy` does --
#          every mode pointed at the same Cassandra then shares one session
#          list.
#
#   macos  The EC2 Mac template installs the two Homebrew formulas and starts
#          them, and deliberately does NOT run `ops install`'s macOS path (see
#          that template's header). Nothing provisions a Cassandra, so
#          defaulting to `cassandra` there would point the API at a database
#          that is not on the machine and break session storage outright.
#          File-backed by default and documented as such
#          (docs/session-storage.md); `--session-backend cassandra` still
#          works for an operator who points `[rag] cassandra_hosts` at a
#          Cassandra they run elsewhere.
DEFAULT_SESSION_BACKEND_BY_OS: dict[str, str] = {
    "linux": DEFAULT_SESSION_BACKEND,
    "macos": "file",
}

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


def packaged_cloud_file(filename: str) -> Path:
    """Return the path of a packaged `scripts/cloud/` file, resolved like the templates.

    The user-data templates are not the only thing that ships from this
    directory -- the containerized artifact-install smoke (#3784) builds its
    AMI-parity image from `al2023-ami-parity.Dockerfile` here, and has to find
    it on a machine with no checkout exactly as `render_user_data` finds a
    template.
    """
    return _template_root() / filename


def homebrew_formulas_for_version(version: str | None) -> tuple[str, str]:
    """The `(api, web)` tap formulas that install `version`, by name.

    The defect this closes (#4122). The macOS bootstrap hardcoded `brew install
    nyxgpt-api nyxgpt-web` and its own header comment declared the version
    "informational only ... this script always installs whatever the tap
    currently serves". Both are wrong for the case that matters, and the project
    already documented why: `release_candidate._guardrails` states in so many
    words that "the stable nyxgpt-api/nyxgpt-web formulas are never written by
    an rc publish, so `brew install nyxgpt-api` still resolves to the latest
    stable release". A candidate is a **separately named formula** by design
    (D-030, #3735), so the unversioned names cannot install one. The owner's
    2026-09-30 run set `NYXGPT_VERSION=3.0.0rc14`, ran the unversioned install,
    and got 2.1.0 -- i.e. the acceptance-testing channel was the one channel
    this path structurally could not deploy.

    So the names are derived here, from the version the deploy declares:

    * a candidate (`3.0.0rc14`) -> `nyxgpt-api@3.0.0rc`, `nyxgpt-web@3.0.0rc`
    * a release (`3.0.0`) or no version at all -> `nyxgpt-api`, `nyxgpt-web`

    Delegated to `release_candidate.rc_formula_name` rather than composed from
    an f-string here, because that function is what the *publisher* uses to
    stamp the formulas -- one definition of the name, so the installer cannot
    ask for a formula the publisher never wrote.
    """
    from nyxgpt import release_candidate

    declared = (version or "").strip()
    if declared and release_candidate.parse_rc_version(declared):
        line = release_candidate.release_line(declared)
        return (
            release_candidate.rc_formula_name("nyxgpt-api", line),
            release_candidate.rc_formula_name("nyxgpt-web", line),
        )
    return ("nyxgpt-api", "nyxgpt-web")


def render_user_data(
    os_family: str, version: str | None = None, session_backend: str | None = None
) -> str:
    """Render the EC2 user-data bootstrap script for `os_family`.

    `version`, when given, pins the Linux template's `pip install
    nyxgpt==<version>` **and** selects the macOS template's Homebrew formulas
    (#4122): a candidate version installs the `@<line>rc` formulas, which are
    the only ones that carry it, and the bootstrap then asserts on the instance
    that the version it got is the version asked for. Omit `version` (or pass
    `None`) to install whatever the tap currently serves as stable, in which
    case there is no declared version to assert against and the bootstrap says
    so instead of checking.

    `session_backend` selects `[nyxgpt] session_backend` on the instance
    (#3865); omitted, it takes the target OS's default from
    `DEFAULT_SESSION_BACKEND_BY_OS`. Both templates seed config.ini from
    `example.config.ini`, which ships the back-compat `file` value -- so
    without this the instance silently stored chats as JSON on its own disk,
    invisible to every other mode pointed at the same Cassandra, and the only
    way to change it was an SSH session and a hand edit.
    """
    if os_family not in _TEMPLATE_FILENAMES:
        raise CloudCommandError(
            f"Unsupported --os {os_family!r} -- choose one of: {', '.join(OS_FAMILIES)}"
        )
    backend = (
        (session_backend or DEFAULT_SESSION_BACKEND_BY_OS.get(os_family, DEFAULT_SESSION_BACKEND))
        .strip()
        .lower()
    )
    if backend not in VALID_SESSION_BACKENDS:
        raise CloudCommandError(
            f"Unsupported --session-backend {session_backend!r} -- choose one of: "
            f"{', '.join(VALID_SESSION_BACKENDS)} (see docs/session-storage.md)"
        )
    template_path = _template_root() / _TEMPLATE_FILENAMES[os_family]
    if not template_path.is_file():
        raise CloudCommandError(f"Missing packaged user-data template: {template_path}")
    rendered = template_path.read_text(encoding="utf-8")
    api_formula, web_formula = homebrew_formulas_for_version(version)
    return (
        rendered.replace(VERSION_PLACEHOLDER, version or "")
        .replace(SESSION_BACKEND_PLACEHOLDER, backend)
        .replace(BREW_FORMULAS_PLACEHOLDER, f"{api_formula} {web_formula}")
        .replace(BREW_API_FORMULA_PLACEHOLDER, api_formula)
        .replace(BREW_WEB_FORMULA_PLACEHOLDER, web_formula)
    )


def user_data(args: argparse.Namespace) -> int:
    """`nyxgpt cloud user-data` entry point: print (or write) the rendered bootstrap script."""
    try:
        rendered = render_user_data(
            args.os,
            getattr(args, "version", None),
            getattr(args, "session_backend", None),
        )
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
