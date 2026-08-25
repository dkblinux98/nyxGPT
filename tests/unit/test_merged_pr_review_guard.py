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


_GUARDED_JOBS = ("head-gate", "claude-review")


class TestOneReviewRunPerPullRequest:
    def test_concurrency_is_declared_per_job_not_per_workflow(self):
        """Workflow-level concurrency let a review cancel itself by commenting.

        GitHub evaluates workflow-level `concurrency:` BEFORE job-level `if:`,
        so a run whose every job skips still claims the group and cancels
        whatever is in it. An `issue_comment` on a PR shares the review's
        group -- including the review agent's own handback, escalation and
        verdict comments. Observed on #4025: a run started 14:23:39, posted
        its gate comment 14:23:45, and was killed 14:23:47 by the run that
        comment triggered. A skipped JOB never enters a group, so moving the
        declaration down fixes the mechanism.
        """
        wf = _load(REVIEW)
        assert "concurrency" not in wf, (
            "workflow-level concurrency is back: a comment-triggered run that "
            "skips every job will cancel an in-flight review again"
        )
        for job in _GUARDED_JOBS:
            assert "concurrency" in wf["jobs"][job], (
                f"{job} has no concurrency group, so two runs can review the "
                "same head at once (#3815)"
            )

    def test_the_two_stages_do_not_share_a_group(self):
        """One group across both jobs would make a single run cancel itself."""
        wf = _load(REVIEW)
        groups = {j: wf["jobs"][j]["concurrency"]["group"] for j in _GUARDED_JOBS}
        assert len(set(groups.values())) == len(groups), (
            f"the guarded jobs share a concurrency group and would cancel each "
            f"other within one run: {groups}"
        )

    def test_each_group_is_keyed_on_the_pull_request(self):
        wf = _load(REVIEW)
        # Every trigger this workflow accepts must resolve to the PR number,
        # or two runs over one PR land in different groups and race anyway.
        for job in _GUARDED_JOBS:
            group = wf["jobs"][job]["concurrency"]["group"]
            for expr in (
                "github.event.pull_request.number",
                "github.event.issue.number",
                "inputs.pr_number",
            ):
                assert expr in group, f"{job} group does not cover {expr}: {group!r}"

    def test_a_superseded_review_run_is_cancelled(self):
        wf = _load(REVIEW)
        for job in _GUARDED_JOBS:
            assert wf["jobs"][job]["concurrency"]["cancel-in-progress"] is True, (
                f"{job}: without cancel-in-progress a run over a stale head "
                "survives to submit the verdict that started #3815 round 2"
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
        # `$PR` is the step's own env: binding of steps.get_pr.outputs.pr_number.
        # It is deliberately NOT `${{ steps.get_pr.outputs.pr_number }}`: a step
        # output interpolated into a run: body is shell source, which is the
        # #3820/#3837 class the no-interpolation-in-script-bodies guard fails on.
        assert re.search(
            r"pulls/\$PR\"\s*(?:\\\s*)?--jq\s+'\.merged\|tostring'", parse_step, re.S
        ), (
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
