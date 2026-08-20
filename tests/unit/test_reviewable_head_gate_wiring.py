"""The gate sits between the trigger and the paid invocation (#3971).

`tests/test_reviewable_head_gate.sh` proves the *decision* is right when it
runs. This file pins the wiring around it, which that test cannot see: that
the decision is actually in front of the Claude invocation, that standing down
is free, and that the paths this change promised not to touch are untouched.

Each assertion here corresponds to a way the change could be silently undone
by an ordinary-looking edit: dropping `needs`, relaxing the `if`, adding an
action call to the gate, or making the merge path depend on the gate.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

REVIEW_WF = yaml.safe_load((WORKFLOWS / "claude-code-review.yml").read_text())
MERGE_WF = yaml.safe_load((WORKFLOWS / "review_agent_auto_review.yml").read_text())


def _steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job].get("steps") or []


def test_the_review_runs_only_on_a_head_the_gate_passed() -> None:
    job = REVIEW_WF["jobs"]["claude-review"]
    assert job["needs"] == "head-gate"
    assert "needs.head-gate.outputs.decision == 'proceed'" in str(job["if"])


def test_waiting_costs_no_claude_invocation() -> None:
    """The whole point: a pending or red head must not reach the action.

    A gate that waited and *then* invoked Claude anyway would cost exactly
    what it was built to save, and nothing else in the file would look wrong.
    """
    for job in ("head-gate", "head-not-reviewable"):
        uses = [step.get("uses", "") for step in _steps(REVIEW_WF, job)]
        assert not [u for u in uses if "claude-code-action" in u], (
            f"{job} invokes claude-code-action; standing down must be free"
        )


def test_the_hand_back_posts_no_verdict() -> None:
    """A red head is not a REQUEST_CHANGES round.

    Posting a review here would increment the cumulative REQUEST_CHANGES count
    that `review_handoff.py` routes on -- so relaying a red check would still
    consume a review cycle and still march the PR toward the 3-strike
    escalation, which is most of the cost this change removes.
    """
    for step in _steps(REVIEW_WF, "head-not-reviewable"):
        assert "gh pr review" not in step.get("run", "")


def test_the_gate_delegates_to_the_tested_helper() -> None:
    """Two copies of "is this head reviewable" would be free to disagree, and
    the tested one would be the copy nothing runs."""
    runs = " ".join(step.get("run", "") for step in _steps(REVIEW_WF, "head-gate"))
    assert "scripts/agents/lib/gh_project.sh" in runs
    assert "await_required_checks" in runs


def test_the_owner_carried_bypass_is_untouched() -> None:
    """Merges that deliberately skip agent review must still work.

    `@approve-merge` runs in a different workflow, on `issue_comment`, and
    that is what makes it a bypass: it does not pass the review trigger at
    all, so no head gate can stand in front of it. Adding `needs: head-gate`
    there (or anywhere in that file) would quietly make the owner's manual
    merge depend on the very automation they are overriding.
    """
    condition = str(MERGE_WF["jobs"]["execute-review-decision"]["if"])
    assert "@approve-merge" in condition
    assert "vars.HUMAN_OWNER" in condition
    for job in MERGE_WF["jobs"].values():
        assert "head-gate" not in str(job.get("needs", ""))


def test_the_wait_cap_cannot_outlive_its_job() -> None:
    """A job that dies mid-wait is a review that never starts.

    The wait is capped by `REVIEW_CI_WAIT_MINUTES` (default 60) and the job by
    `timeout-minutes`; if the job cap were the smaller of the two, the gate
    would be killed by the runner instead of reporting a timeout -- no
    comment, no notification, no review, and nothing on the PR to say why.
    """
    gate = REVIEW_WF["jobs"]["head-gate"]
    assert gate["timeout-minutes"] > 60
