"""Guards for how a failed developer run is classified (#3909).

Two defects on 2026-08-18, both of which reached the owner as a FATAL page for
a failure that needed no human at all:

  * `529 Overloaded` matched no signature, and an unmatched signature defaults
    to fatal;
  * the escalation condition ORs the three analysis phases together, so
    Phase 1's `unknown` -- a *non-answer* from a grep over log lines -- fired
    the alarm even though Phase 3 had already concluded TRANSIENT and written
    the reasoning into the run log.

The second is the one worth pinning hardest: the correct answer existed and was
discarded by a less-informed check.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
DEV_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "developer_auto_implement.yml"


def _classify(error_text: str) -> str:
    """Run the real shell classifier, not a reimplementation of it."""
    script = f'source "{LIB}" >/dev/null 2>&1; classify_error "$1"'
    result = subprocess.run(
        ["bash", "-c", script, "_", error_text],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "line",
    [
        "API error: 529 Overloaded",
        "  ⚠ Claude API returned HTTP 529",
        "Error: Overloaded",
    ],
)
def test_api_overload_is_retriable(line: str) -> None:
    """529 is the API describing its own capacity, not this run's work."""
    assert _classify(line) == "retriable:api_overloaded", (
        "HTTP 529 must classify as retriable -- it says nothing about the change "
        "under test, and the failed run leaves nothing to repair"
    )


def test_a_genuinely_fatal_error_still_escalates() -> None:
    """The widening must not swallow the failures that do need a human."""
    assert _classify("remote: Authentication failed for repository").startswith("fatal:")


def test_unknown_is_still_unknown() -> None:
    """Not every unmatched line should be optimistically retried either."""
    assert _classify("something nobody has ever seen before") == "unknown"


def _escalation_condition() -> str:
    workflow = yaml.safe_load(DEV_WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name", "").startswith("Escalate fatal error"):
                return " ".join(str(step["if"]).split())
    raise AssertionError("the fatal-escalation step is gone")


def test_phase_three_transient_suppresses_the_escalation() -> None:
    """The precedence, asserted on the real condition rather than trusted.

    Phase 1 is a grep; Phase 3 is an analysis that reads the run. When they
    disagree and Phase 3 says TRANSIENT, the alarm must stand down -- the
    auto-retry step is what owns the failure then.
    """
    condition = _escalation_condition()
    assert re.search(r"claude_result\.outputs\.status\s*!=\s*'TRANSIENT'", condition), (
        "the fatal escalation must not fire when Phase 3 concluded TRANSIENT; "
        "without this, Phase 1's `unknown` pages the owner over a capacity blip "
        "while the correct diagnosis sits in the same run log"
    )


def test_the_transient_retry_still_owns_those_failures() -> None:
    """Suppressing the alarm is only safe because something else acts."""
    workflow = yaml.safe_load(DEV_WORKFLOW.read_text(encoding="utf-8"))
    conditions = [
        " ".join(str(step.get("if", "")).split())
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name", "").startswith("Auto-retry on failure")
    ]
    assert conditions, "the auto-retry step is gone -- nothing would handle a TRANSIENT"
    assert any(
        "claude_result.outputs.status == 'TRANSIENT'" in c for c in conditions
    ), "auto-retry must still fire on a Phase 3 TRANSIENT verdict"
