"""Resolve the version of nyxGPT that is actually running.

The version a user sees must be the version that is running, so it is read
from installed package metadata rather than from any configuration value
(see #3716, where the web UI badge showed the agent tooling's
`[github] RELEASE_BRANCH` setting and drifted to a stale `v1.0.0`).
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from nyxgpt.release_candidate import is_prerelease

#: Deliberately implausible: signals "version could not be determined"
#: instead of silently masquerading as a real release.
UNKNOWN_VERSION = "0.0.0"

#: The channel a running version belongs to. Not a cosmetic label: acceptance
#: testing installs `3.0.0rcN` kegs alongside -- and sometimes on top of --
#: stable ones, and the operator's only question during an incident is "which
#: of those am I actually looking at?" (#3982). A bare version string cannot
#: answer it, which is how a 2.1.0 web build serving against a 3.0.0-line API
#: went undetected until a feature was visibly missing.
CHANNEL_STABLE = "stable"
CHANNEL_RC = "rc"
CHANNEL_DEV = "dev"
CHANNEL_UNKNOWN = "unknown"

#: A plain PEP 440 final release: `3.0.0`, `3.0`, `12.4.1`. Fully anchored --
#: `3.0.0rc13` must not read as stable, which is the whole point of the split.
_FINAL_RELEASE_RE = re.compile(r"^\d+(?:\.\d+)*$")

#: Marks a build that came from a working tree rather than a published
#: artifact: PEP 440's `.devN` and `+local` segments, plus the `:local` image
#: tag the Kubernetes path builds from a checkout. Such a stack is neither rc
#: nor stable, and reporting it as either is the mixed-tier misdirection
#: #3982 was filed about.
_DEV_RE = re.compile(r"\.dev\d+|\+|(?<![A-Za-z0-9])local(?![A-Za-z0-9])", re.IGNORECASE)


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


def version_channel(value: str | None) -> str:
    """Classify a version string as `stable`, `rc`, `dev` or `unknown`.

    Why this lives beside `running_version()` rather than in
    `release_candidate` (which already models rc numbering): that module is
    the *publish* pipeline's view of a channel -- what to build and where to
    push it. This is the *running stack's* view -- what an operator is
    looking at right now. `/api/v1/info` and the web header need the second.
    The pre-release predicate itself is reused from there, so the publisher
    and the running stack can never disagree about what `3.0.0rc13` is.

    Ordering is load-bearing. `.devN` is a pre-release under PEP 440, so the
    dev test runs first: a working-tree build reported as `rc` would send an
    operator hunting for a published candidate that does not exist (#3982).
    """
    text = (value or "").strip()
    if text[:1] in {"v", "V"}:
        text = text[1:]
    if not text or text == UNKNOWN_VERSION:
        return CHANNEL_UNKNOWN
    if _DEV_RE.search(text):
        return CHANNEL_DEV
    if is_prerelease(text):
        return CHANNEL_RC
    if _FINAL_RELEASE_RE.match(text):
        return CHANNEL_STABLE
    # Parses as none of the above -- a branch name, a bare image tag, a `git
    # describe` string. By elimination that is a working-tree build, and
    # calling it `dev` is more useful than `unknown`: `unknown` is reserved
    # for "no version reported at all", and an operator who saw it here would
    # suspect the reporting rather than the stack.
    return CHANNEL_DEV
