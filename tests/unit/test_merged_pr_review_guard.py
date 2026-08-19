"""Nothing automatic may review or act on an already-merged pull request.

#3815, round 2. On PR #3817 two `Claude Code Review` runs read the same head
(`6b72c2d9`) concurrently. One approved and merged at 16:45:30Z; the other was
still reading the diff and submitted `CHANGES_REQUESTED` at 16:47:23Z --- two
minutes after the branch was already on the release branch --- followed by an
`APPROVED` two seconds later. `review_agent_auto_review.yml` executed both:

* three "Code review approved and PR merged" comments on one merge,
* the linked issue assigned owner -> review-agent -> owner -> developer,
* a first-time finding counted as an unconverged *second* REQUEST_CHANGES
  cycle, so a huddle was convened on a merged, unfixable PR,
* review-round statistics the retrospective reports, inflated by the phantom
  rounds.

Two invariants close it, and both live in workflow YAML that no runtime test
can execute: GitHub evaluates `concurrency:` and the guard runs only inside a
real Actions job. These are contract assertions over the workflow text --- they
hold the fix in place, they do not prove GitHub's semantics.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
REVIEW = WORKFLOWS / "claude-code-review.yml"
EXECUTE = WORKFLOWS / "review_agent_auto_review.yml"


def _load(path: Path) -> dict:
    # `on:` parses as the boolean True in YAML 1.1; irrelevant here, but it is
    # why the keys are read defensively.
    return yaml.safe_load(path.read_text())


class TestOneReviewRunPerPullRequest:
    def test_review_workflow_declares_a_concurrency_group(self):
        assert "concurrency" in _load(REVIEW), (
            "claude-code-review.yml has no concurrency group, so two runs can "
            "review the same head at once (#3815)"
        )

    def test_the_group_is_keyed_on_the_pull_request(self):
        group = _load(REVIEW)["concurrency"]["group"]
        # Every trigger this workflow accepts must resolve to the PR number,
        # or two runs over one PR land in different groups and race anyway.
        for expr in (
            "github.event.pull_request.number",
            "github.event.issue.number",
            "inputs.pr_number",
        ):
            assert expr in group, f"concurrency group does not cover {expr}: {group!r}"

    def test_a_superseded_review_run_is_cancelled(self):
        assert _load(REVIEW)["concurrency"]["cancel-in-progress"] is True, (
            "without cancel-in-progress a run over a stale head survives to "
            "submit the verdict that started #3815 round 2"
        )


@pytest.fixture(name="parse_step")
def _parse_step() -> str:
    steps = _load(EXECUTE)["jobs"]["execute-review-decision"]["steps"]
    for step in steps:
        if step.get("id") == "parse_decision":
            return step["run"]
    pytest.fail("review_agent_auto_review.yml has no parse_decision step")


class TestMergedPullRequestsAreInert:
    """The execute-decision workflow must refuse automatic rounds on a merge."""

    def test_the_merged_state_is_read(self, parse_step: str):
        assert re.search(r"pulls/\$PR_NUM.*--jq\s+'\.merged", parse_step, re.S), (
            "parse_decision never asks whether the PR is already merged, so a "
            "stale verdict is executed against it (#3815)"
        )

    def test_a_merged_pr_short_circuits_to_already_handled(self, parse_step: str):
        block = parse_step[parse_step.index('if [[ "$PR_MERGED"') :]
        assert 'DECISION="ALREADY_HANDLED"' in block.split("fi")[0], (
            "a merged PR must resolve to ALREADY_HANDLED, which matches no "
            "downstream step condition"
        )

    def test_only_automatic_rounds_are_gated(self, parse_step: str):
        # An owner typing @approve-merge / @send-to-developer on a merged PR
        # means it deliberately; only the event-driven paths are suppressed.
        assert (
            '"$TRIGGER_TYPE" == "auto"' in parse_step
        ), "the merged-PR guard must not swallow a human's manual trigger"

    def test_no_mutating_step_runs_on_already_handled(self):
        """ALREADY_HANDLED only helps if every acting step is decision-gated."""
        job = _load(EXECUTE)["jobs"]["execute-review-decision"]
        acting = {
            "Execute merge (if approved)",
            "Count review iterations and classify disagreement (if changes requested)",
            "Trigger huddle (judgment call, or 2nd failed verifiable-defect cycle)",
            "Post completion comment",
        }
        seen = set()
        for step in job["steps"]:
            name = step.get("name")
            if name not in acting:
                continue
            seen.add(name)
            cond = str(step.get("if", ""))
            assert "steps.parse_decision.outputs.decision ==" in cond, (
                f"step {name!r} is not gated on the parsed decision, so "
                f"ALREADY_HANDLED would not stop it (#3815)"
            )
        assert seen == acting, f"steps renamed or removed: {acting - seen}"
