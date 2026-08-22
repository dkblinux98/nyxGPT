"""Interpreter resolution for the agent shell suites run from pytest (#3983).

The agent scripts under `scripts/agents/` use bash 4 features -- `mapfile`
(`review_accept_and_merge.sh`) and `declare -A` (the drain-gate and
issue-relationship libs) -- and that is fine where they run: every workflow
that invokes them is on a GitHub Actions Linux runner, whose `bash` is 5.x.

macOS ships bash **3.2** as `/bin/bash` (and has since 2007, for licensing
reasons), so a developer running `pytest tests/unit/` on a Mac without a
Homebrew bash gets four failures that say `declare: -A: invalid option` and
`mapfile: command not found` -- nothing to do with the code under test, and
four of the failures in the red baseline #3983 was filed for.

So resolve an interpreter that can actually run the script, and skip with the
reason (and the fix) when the machine has none -- the same shape as the
`shutil.which("jq")` skip these suites already carry for their other external
requirement. The suites still run in CI, which is the environment the scripts
are written for; what stops is a Mac reporting a bash-version mismatch as a
red test.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

# `bash` from PATH first, so a Homebrew/MacPorts bash the developer has put
# ahead of /bin/bash wins; then the two places Homebrew installs one, since
# `brew install bash` does not change what `bash` resolves to in a
# non-interactive shell unless the user also edited their PATH.
_CANDIDATES = ("bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash")


def _major_version(bash: str) -> int:
    """Return `bash`'s major version, or 0 if it cannot be determined."""
    try:
        cp = subprocess.run(
            [bash, "-c", "echo ${BASH_VERSINFO[0]}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    return int(cp.stdout.strip()) if cp.stdout.strip().isdigit() else 0


def bash4_or_skip() -> str:
    """Path to a bash >= 4 on this machine, or skip the calling test."""
    for candidate in _CANDIDATES:
        resolved = shutil.which(candidate)
        if resolved and _major_version(resolved) >= 4:
            return resolved
    pytest.skip(
        "the agent shell suites need bash >= 4 (mapfile / declare -A) and this "
        "machine has only bash 3.2 -- macOS ships that as /bin/bash. Install one "
        "(`brew install bash`) to run them locally; CI runs them on bash 5."
    )
