"""The suite-wide Node stub must hand out an interpreter that EXISTS (#4194).

`tests/conftest.py`'s `_runner_runs_the_declared_node` answers "what Node does
this host run" for the whole suite, so that three hundred tests about `npm ci`
failure modes and `doctor` output stop depending on which Node the runner
happens to carry.

The first version of it answered with the path `/usr/bin/node`, which on most
runners is not a file. That is not a cosmetic difference: `ops` does not only
*name* the resolved interpreter, it RUNS it -- `_node_can_resolve` shells out
to `node -p require.resolve(...)`. Pointed at a path that does not exist, that
raised `FileNotFoundError`, which the helper caught and logged at WARNING,
which in turn broke `test_ops_doctor_logs_issues_at_warning` -- but only on a
machine where `web/node_modules` existed, because that is what makes `doctor`
reach the probe at all. So it passed in a fresh checkout and failed in CI,
where `npm ci` runs before pytest.

The class is "a stub hands out a filesystem path nothing ever tries to use",
and it stays fixed only if something asserts the path is usable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nyxgpt import node_runtime, ops

pytestmark = pytest.mark.unit


def test_the_stubbed_node_and_npm_are_executable_files():
    """Resolved paths point at something that can actually be run."""
    for label, path in (
        ("node", node_runtime.resolve_node_bin()),
        ("npm", node_runtime.resolve_npm_bin()),
    ):
        assert path, f"the suite-wide stub resolved no {label} at all"
        assert os.access(path, os.X_OK), (
            f"the suite-wide stub resolves {label} to {path!r}, which is not an "
            "executable file -- any code under test that RUNS it gets an OSError "
            "whose timing depends on the machine"
        )


def test_the_report_agrees_with_the_path_it_resolves():
    """The declaration stub and the path stub must not describe two hosts."""
    assert node_runtime.node_version_report().path == node_runtime.resolve_node_bin()


def test_probing_a_package_answers_false_without_warning(caplog, tmp_path: Path):
    """The probe returns a verdict, not a stack trace, under the stub.

    This is the assertion that would have failed before the fix: the resolved
    `node` could not be executed, so the probe logged a WARNING on its way to
    returning `False`, and every caller counting warnings inherited it.
    """
    with caplog.at_level("WARNING", logger="nyxgpt.ops"):
        assert ops._node_can_resolve("undici", tmp_path) is False
    assert not [
        r for r in caplog.records if r.levelname == "WARNING"
    ], "probing for a package under the suite-wide Node stub logged a warning: " + "; ".join(
        r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    )
