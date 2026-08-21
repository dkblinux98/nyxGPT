"""The developer verification gate must render a verdict about the code it ran.

Two ways it has failed to, both costing a full agent retry cycle:

**Stale mypy cache (#3730).** A `.mypy_cache` left in the reused runner
workspace by an earlier run under a *different* dependency set (pre-commit's
isolated mirrors-mypy venv, which carries no opentelemetry) recorded
`opentelemetry` as an unresolvable namespace package. mypy's incremental
staleness check keys on source-file hashes, not on which third-party packages
are importable, so the stale entry survived into the verification run and
reported

    src/nyxgpt/logging.py: error: Module "opentelemetry" has no attribute "trace"

against a branch that never touched `src/nyxgpt/logging.py`. The same mechanism
can mask a real error instead of inventing one, so the gate is pinned to
`--no-incremental`.

**Wrong tree entirely (#3979).** The job checks out `vars.RELEASE_BRANCH`, and
on one path nothing moves it off: a re-assignment where the PR is already open
with no review issues skips both `claude_initial` (gated on
`pr_exists == 'false'`) and `claude_review_fix` (gated on
`has_review_issues == 'true'`). The verification steps carry no `if:`, so they
ran on the release branch and the result was reported as the PR's. #3979 was
told its implementation had failed and handed a pytest log quoting the very
bug it had already fixed -- the release tip was red *with that bug*, which is
why the issue existed. The silent direction is worse: on a green release
branch the gate passes having executed none of the PR's code, and
`Request review for existing PR` forwards that green light to the reviewer.
The gate now repositions to the PR head before dependencies install.

Both are the same defect wearing different clothes -- a verdict that is not
about the thing under test -- so both guards live here.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

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


# --- The gate must judge the PR's tree, not the release branch (#3979) -------


@pytest.fixture(scope="module")
def steps() -> list[dict[str, Any]]:
    """The `implement` job's steps, in declaration order."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job_steps = doc["jobs"]["implement"]["steps"]
    assert isinstance(job_steps, list) and job_steps
    return job_steps


def _index_of(steps: list[dict[str, Any]], needle: str) -> int:
    for i, step in enumerate(steps):
        if needle in (step.get("name") or ""):
            return i
    raise AssertionError(f"no step named like {needle!r} in {WORKFLOW.name}")


def test_the_gate_repositions_to_the_pr_head_when_no_claude_step_ran(
    steps: list[dict[str, Any]],
) -> None:
    """The one path that leaves the workspace on the release branch is covered.

    `claude_initial` needs `pr_exists == 'false'` and `claude_review_fix` needs
    `has_review_issues == 'true'`, so an open PR with neither skips both and
    nothing checks out the branch being judged. Guard the condition, not just
    the step's existence: widening it would reposition paths whose workspace is
    already correct, and narrowing it re-opens #3979.
    """
    step = steps[_index_of(steps, "Check out the PR head for verification")]
    condition = " ".join((step.get("if") or "").split())

    assert "steps.check_pr.outputs.pr_exists == 'true'" in condition
    assert "steps.check_pr.outputs.pr_state == 'OPEN'" in condition
    assert "steps.check_review.outputs.has_review_issues == 'false'" in condition

    body = step.get("run") or ""
    assert "git checkout -B" in body, "the step must actually move the workspace"
    # Falling through to the release branch is the defect, not a degraded mode.
    assert body.count("exit 1") >= 2, (
        "an unresolvable PR head must fail the step, never silently leave the "
        "workspace on the release branch for verification to judge"
    )


def test_the_reposition_precedes_dependency_install_and_every_verification(
    steps: list[dict[str, Any]],
) -> None:
    """Order is load-bearing, twice over.

    `pip install -e .[dev]` and `npm ci` resolve from the checked-out tree, so
    repositioning after them installs the release branch's dependency set
    against the PR's code. And every verification attempt reads the workspace
    left by the one before it, so a reposition that landed after attempt 1
    would still let the first -- the one whose log the fix agent is handed --
    indict the wrong tree.
    """
    reposition = _index_of(steps, "Check out the PR head for verification")

    for later in (
        "Install Python dependencies",
        "Install Node dependencies",
        "Run Verification (Attempt 1)",
        "Run Verification (Attempt 2)",
        "Final Verification",
    ):
        assert reposition < _index_of(steps, later), (
            f"'{later}' must run after the workspace is on the PR head, "
            "or it reads the release branch"
        )
