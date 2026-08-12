"""The developer verification gate's mypy runs must not be cache-dependent.

#3730 lost a full agent retry cycle to a phantom mypy error: a `.mypy_cache`
left in the reused runner workspace by an earlier run under a *different*
dependency set (pre-commit's isolated mirrors-mypy venv, which carries no
opentelemetry) recorded `opentelemetry` as an unresolvable namespace package.
mypy's incremental staleness check keys on source-file hashes, not on which
third-party packages are importable, so the stale entry survived into the
verification run and reported

    src/nyxgpt/logging.py: error: Module "opentelemetry" has no attribute "trace"

against a branch that never touched `src/nyxgpt/logging.py`. The same mechanism
can mask a real error instead of inventing one, so the gate is pinned to
`--no-incremental` and these tests keep it that way.
"""

from pathlib import Path

import pytest

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "developer_auto_implement.yml"
)


@pytest.fixture(scope="module")
def mypy_invocations() -> list[str]:
    """Every line in the workflow that actually invokes mypy on `src/`."""
    assert WORKFLOW.is_file(), f"missing workflow: {WORKFLOW}"
    lines = [
        line.strip()
        for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if "python -m mypy" in line and not line.strip().startswith("#")
    ]
    # attempt 1, attempt 2, and the final verification pass.
    assert len(lines) == 3, f"unexpected mypy invocation count: {lines}"
    return lines


def test_every_gate_mypy_run_is_non_incremental(mypy_invocations: list[str]) -> None:
    for line in mypy_invocations:
        assert "--no-incremental" in line, (
            "verification mypy must run with --no-incremental so a stale "
            f"workspace .mypy_cache cannot decide the gate: {line}"
        )


def test_gate_mypy_runs_target_src(mypy_invocations: list[str]) -> None:
    for line in mypy_invocations:
        assert "src/" in line, f"mypy gate must still check src/: {line}"
