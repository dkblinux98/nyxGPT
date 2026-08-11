"""Resolve the version of nyxGPT that is actually running.

The version a user sees must be the version that is running, so it is read
from installed package metadata rather than from any configuration value
(see #3716, where the web UI badge showed the agent tooling's
`[github] RELEASE_BRANCH` setting and drifted to a stale `v1.0.0`).
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

#: Deliberately implausible: signals "version could not be determined"
#: instead of silently masquerading as a real release.
UNKNOWN_VERSION = "0.0.0"


def running_version() -> str:
    """Return the installed `nyxgpt` package version, e.g. ``"3.0.0"``.

    Reads the distribution metadata, which is present both for an installed
    artifact (wheel, no checkout -- per the repo-less portability
    requirement) and for a `pip install -e .` dev tree. Only when no
    metadata exists at all -- a bare source tree imported via `PYTHONPATH` --
    does it fall back to the checkout's `pyproject.toml`, and finally to
    `UNKNOWN_VERSION`.
    """
    try:
        return version("nyxgpt")
    except PackageNotFoundError:
        return _version_from_pyproject()


def _version_from_pyproject() -> str:
    """Read `project.version` from the checkout's pyproject.toml, if reachable."""
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return UNKNOWN_VERSION
    return str(data.get("project", {}).get("version", UNKNOWN_VERSION))
