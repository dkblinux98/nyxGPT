"""Interpreter resolution for the agent shell suites run from pytest (#3983).

Also the one place that answers, for the whole unit suite, "can this
interpreter *run* `python -m <module>`?" and "what does a shell suite's
`could not run` status mean?" -- see `runnable_module_or_skip` and
`NO_PYTEST_PYTHON_EXIT` below (#4195). The shell-side half of the same two
decisions is `tests/pytest_python.sh`.

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

import importlib.util
import shutil
import subprocess
import sys

import pytest

#: The status `tests/pytest_python.sh`'s `require_pytest_python` exits with when
#: no interpreter on the machine can run `python -m pytest`. Kept in step with
#: `NO_PYTEST_PYTHON_EXIT` there (pinned by
#: tests/unit/test_invocation_independent_tests.py). 77 is deliberately neither
#: 0 nor any pytest status: "this machine cannot answer the question" is a third
#: outcome, and reporting it as either of the other two is the #4195 defect --
#: as 0 it is a green over an unexecuted gate, as 1 it is a red that says
#: nothing about the code.
NO_PYTEST_PYTHON_EXIT = 77

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


def runnable_module_or_skip(module: str, *, needed_for: str) -> None:
    """Skip the calling test unless `python -m <module>` can actually run.

    THE DEFECT THIS EXISTS FOR (#4195). `tests/unit/test_resources_packaging.py`
    guarded its `python -m build` subprocess with
    `pytest.importorskip("build")`, and the same checkout then gave two
    different answers depending on the command typed:

        pytest           tests/unit/test_resources_packaging.py  -> 1 skipped
        python -m pytest tests/unit/test_resources_packaging.py  -> 1 FAILED
            No module named build.__main__; 'build' is a package and
            cannot be directly executed

    `python -m pytest` prepends the current directory to `sys.path`; the
    console script does not. The repo root holds a **gitignored `build/`
    directory** that setuptools leaves behind on any `pip install -e .`, and
    with the repo root on `sys.path` that bare directory imports as a
    *namespace package* -- so `importorskip("build")` answers yes while
    `python -m build` still cannot start.

    The bug is not the shadow, it is the question: `importorskip` asks "is this
    name importable?" when what the test needs is "can this interpreter
    execute `-m <module>`?" Those differ for exactly the namespace-directory
    case, which is the one a Python repository manufactures for itself. So ask
    the question the subprocess asks -- `<module>.__main__` -- which a
    directory with no `__main__.py` in it cannot answer, whatever is on
    `sys.path`.

    Use this for any test whose real dependency is a `python -m X` subprocess.
    `importorskip` remains right for a module the test imports itself.

    See also `assert_shell_suite_ran`, which applies the same "distinguish
    `could not run` from `ran and passed`" rule to the shell suites.
    """
    try:
        spec = importlib.util.find_spec(f"{module}.__main__")
    except (ImportError, AttributeError, ValueError):
        # Parent not importable at all, or importable but not a package.
        spec = None
    if spec is None:
        pytest.skip(
            f"`{sys.executable} -m {module}` cannot run on this interpreter, so "
            f"{needed_for} cannot be verified here. Install it "
            f'(`pip install -e ".[dev]"`) to run this test. NOTE: a bare '
            f"`{module}/` directory on sys.path imports as a namespace package "
            f"and does NOT make `-m {module}` runnable -- see #4195."
        )


def assert_shell_suite_ran(
    result: subprocess.CompletedProcess[str], *, proof_of_execution: str
) -> None:
    """Assert a shell suite subprocess ran AND passed -- three outcomes, not two.

    Every pytest wrapper around a `tests/*.sh` suite that EXECUTES something
    has to tell three states apart, and #4195 is what happens when it tells
    only two:

    * **ran and passed** (0, and its executed section's own `[ok]` line in the
      output) -- the only pass;
    * **ran and failed** (non-zero, other than 77) -- a real red;
    * **could not run here** (`NO_PYTEST_PYTHON_EXIT`) -- a skip, with the
      suite's own explanation as the reason.

    `proof_of_execution` is a line the suite only prints once its executed
    section has actually run. Exit 0 alone is not evidence: before #4195
    `tests/test_escalation_evidence.sh` printed "All tests passed." and exited
    0 with its entire EXECUTED half skipped, because the interpreter it picked
    had no pytest. A green over an unexecuted gate is worse than a red.
    """
    combined = result.stdout + result.stderr
    if result.returncode == NO_PYTEST_PYTHON_EXIT:
        pytest.skip(f"the shell suite could not run on this machine:\n{combined}")
    assert result.returncode == 0, combined
    assert proof_of_execution in result.stdout, (
        f"the suite exited 0 but never printed {proof_of_execution!r}, so its "
        f"executed section did not run -- this would be a green over nothing "
        f"(#4195):\n{combined}"
    )
