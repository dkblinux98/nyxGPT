"""A PR's issue is a link, not a sentence (owner rule, 2026-08-19).

GitHub already stores which issue a PR closes -- the "Development" sidebar
link and the `closingIssuesReferences` edge behind it, which is what actually
closes the issue on merge. Every consumer here used to re-derive that by
grepping `Closes #N` out of the PR body: prose standing in for a relationship
the platform stores, the same mistake as driving workflows from comment
tokens (#3882) and as the retired `Related feature: #N` convention (D-002).

The cost was not theoretical. "PR #N body does not contain 'Closes #N'"
hard-failed the merge automation on #3921, #3927, #3929 and #3933 in one
night, each needing a manual override.

These tests pin the shape rather than the transport: one helper, native edge
first, body only as a fallback, and no consumer left grepping on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
SCRIPTS = ROOT / "scripts" / "agents"
WORKFLOWS = ROOT / ".github" / "workflows"

#: A body-grep for the closing keyword, in any of the shapes used here.
_BODY_GREP = re.compile(r"(sed -n 's/\.\*Closes #|grep -oiE 'closes #\[0-9\]\+)")


def _uncommented(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


class TestTheHelper:
    def test_it_exists_and_asks_github_for_the_link_first(self):
        body = LIB.read_text(encoding="utf-8")
        assert "pr_linked_issue()" in body
        assert "closingIssuesReferences" in body

    def test_the_body_convention_is_the_fallback_not_the_source(self):
        body = LIB.read_text(encoding="utf-8")
        helper = body[body.index("pr_linked_issue()") :]
        helper = helper[: helper.index("\n}\n") + 3]
        assert helper.index("closingIssuesReferences") < helper.index("closes #[0-9]+")

    def test_no_linked_issue_prints_nothing_rather_than_failing(self):
        """A PR that closes no issue is legitimate -- rare, but real. The
        helper reports the absence; each caller decides what it means."""
        body = LIB.read_text(encoding="utf-8")
        helper = body[body.index("pr_linked_issue()") :]
        helper = helper[: helper.index("\n}\n") + 3]
        assert "|| true" in helper
        assert "exit 1" not in helper


class TestNoConsumerGrepsTheBodyItself:
    def test_every_caller_goes_through_the_helper(self):
        """A body read is allowed only as the fallback arm of a native
        lookup -- never as the source of truth."""
        offenders = []
        sources = list(SCRIPTS.rglob("*.sh")) + sorted(WORKFLOWS.glob("*.yml"))
        for path in sources:
            text = _uncommented(path)
            if not _BODY_GREP.search(text):
                continue
            # A body read is legitimate as the FALLBACK arm of a native
            # lookup -- PRs opened before the link existed still have to
            # resolve. What is forbidden is deriving the link from prose with
            # no native query anywhere in the file.
            native = (
                "closingIssuesReferences" in text
                or "closedByPullRequestsReferences" in text
                or "pr_linked_issue" in text
            )
            if not native:
                offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, (
            "these still derive a PR's issue by grepping its body instead of "
            f"reading the native link through pr_linked_issue: {offenders}"
        )

    @pytest.mark.parametrize(
        "consumer",
        [
            "review_ensure_handoff.sh",
            "dispatch_conflict_resolution.sh",
            "scrummaster_sprint_report.sh",
        ],
    )
    def test_the_shell_consumers_call_it(self, consumer):
        assert "pr_linked_issue" in (SCRIPTS / consumer).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "consumer",
        [
            "review_agent_auto_review.yml",
            "ensure_project_hygiene.yml",
            "huddle_decision_dispatch.yml",
            "link_revert_pr_to_issue.yml",
            "huddle_session.yml",
            "notify-merge-conflicts.yml",
            "developer_auto_implement.yml",
        ],
    )
    def test_the_workflow_consumers_query_the_link_directly(self, consumer):
        """Workflows do NOT source the helper. A PR-triggered workflow runs
        the PR's copy of the file against a RELEASE_BRANCH checkout, so a
        helper the base branch lacks fails with exit 127 -- which is how the
        PR introducing that helper turned two gates red. A query needs
        nothing checked out. `link_revert_pr_to_issue.yml` has no checkout
        step at all, so for it this is the only form that can work."""
        body = (WORKFLOWS / consumer).read_text(encoding="utf-8")
        assert "closingIssuesReferences" in body or "closedByPullRequestsReferences" in body
        assert "source scripts/agents/lib/gh_project.sh" not in body.split("linked")[0][-400:]


class TestTheErrorNoLongerDemandsTheSentence:
    @pytest.mark.parametrize(
        "path",
        [
            WORKFLOWS / "review_agent_auto_review.yml",
            WORKFLOWS / "huddle_decision_dispatch.yml",
            SCRIPTS / "review_ensure_handoff.sh",
        ],
    )
    def test_it_reports_a_missing_issue_not_missing_prose(self, path):
        body = path.read_text(encoding="utf-8")
        assert "does not contain 'Closes #N'" not in body
        assert "closes no issue" in body


class TestAnIssuelessPRCanStillBeMerged:
    """ "Bubble up" has to mean a human decides, not that the pipeline jams.

    The first cut of this change kept the `exit 1` for a PR with no linked
    issue, in a step that runs ahead of EVERY verdict branch. That made an
    issue-less PR unmergeable, un-hand-backable and un-escalatable: the
    review agent's APPROVE on PR #3935 was submitted and then stranded, and
    each new verdict re-produced the same red check. The split below is by
    decision, because that is where the two cases actually differ -- APPROVE
    needs no issue (merge the PR, skip the bookkeeping), while every
    hand-back outcome IS issue-side (it assigns an issue to somebody).
    """

    def test_the_extract_step_does_not_fail_the_run(self):
        body = (WORKFLOWS / "review_agent_auto_review.yml").read_text(encoding="utf-8")
        step = body.split("- name: Extract issue number from PR", 1)[1]
        step = step.split("- name: ", 1)[0]
        assert "has_issue=false" in step
        assert "::warning::" in step
        # The old shape: an unconditional stop before any decision ran.
        # Comments are stripped -- this file explains the retired `exit 1`
        # in prose, and the prose is not the code.
        code = "\n".join(line for line in step.splitlines() if not line.strip().startswith("#"))
        assert "exit 1" not in code

    def test_the_hand_back_decisions_are_gated_on_having_one(self):
        body = (WORKFLOWS / "review_agent_auto_review.yml").read_text(encoding="utf-8")
        for step_name in (
            "Escalate to human",
            "Return to developer",
            "Trigger huddle",
            "Send to developer",
        ):
            head = body.split(f"- name: {step_name}", 1)[1].split("run:", 1)[0]
            assert "steps.get_issue.outputs.has_issue == 'true'" in head, step_name

    def test_a_hand_back_without_an_issue_stops_and_says_so_on_the_pr(self):
        body = (WORKFLOWS / "review_agent_auto_review.yml").read_text(encoding="utf-8")
        assert "Stop -- hand-back decision on a PR that closes no issue" in body
        step = body.split("- name: Stop -- hand-back decision", 1)[1].split("- name: ", 1)[0]
        # It must reach a human where a human looks -- a red check alone is
        # not a bubble-up -- and it must still fail the run.
        assert "/comments" in step
        assert "exit 1" in step

    def test_the_merge_step_is_not_gated_on_having_one(self):
        body = (WORKFLOWS / "review_agent_auto_review.yml").read_text(encoding="utf-8")
        head = body.split("- name: Execute merge (if approved)", 1)[1].split("run:", 1)[0]
        assert "has_issue" not in head

    def test_the_merge_script_treats_an_empty_issue_as_input_not_misuse(self):
        body = (SCRIPTS / "review_accept_and_merge.sh").read_text(encoding="utf-8")
        # Only the PR number is required.
        assert 'if [[ -z "$PR" ]]; then usage >&2; exit 2; fi' in body
        assert 'if [[ -z "$PR" || -z "$ISSUE" ]]' not in body
        assert "HAS_ISSUE=0" in body

    def test_every_issue_side_step_in_the_merge_script_is_guarded(self):
        body = (SCRIPTS / "review_accept_and_merge.sh").read_text(encoding="utf-8")
        # The close/status/assign/kick block and the conflict hand-back are
        # the four places that need an issue to act on.
        assert body.count('"$HAS_ISSUE"') >= 4
        # And the skip is recorded where somebody reads it, not just in logs.
        assert "no issue-side bookkeeping ran" in body


class TestAlreadyHandledMatchesNoStep:
    """`ALREADY_HANDLED` is a sentinel, and a negation quietly recruited it.

    `parse_decision` resolves a duplicated trigger to `ALREADY_HANDLED`, and
    the workflow documents it as a value that matches no downstream step.
    The no-issue stop step was first written `decision != 'APPROVE'`, which
    matched it — so on an issue-less PR the structured-comment fallback run
    would post "cannot execute ALREADY_HANDLED" onto a PR the primary path
    had just merged, and redden the run. That is the #3728/#3733
    false-escalation shape, re-created by a negation rather than a list.

    Pinned as an invariant over every step, not just the one that broke it:
    the next `!=` will fail here instead of in production.
    """

    def _step_conditions(self) -> list[tuple[str, str]]:
        body = (WORKFLOWS / "review_agent_auto_review.yml").read_text(encoding="utf-8")
        out = []
        for chunk in body.split("      - name: ")[1:]:
            name = chunk.splitlines()[0].strip()
            head = chunk.split("run:", 1)[0]
            if "steps.parse_decision.outputs.decision" in head:
                out.append((name, head))
        return out

    def test_every_decision_gate_names_its_decisions(self):
        gates = self._step_conditions()
        assert gates, "no decision-gated steps found -- the parser drifted"
        for name, head in gates:
            assert "decision != " not in head, (
                f"step {name!r} gates on a negation of the decision, which also "
                "matches the ALREADY_HANDLED sentinel; name the decisions it is for"
            )

    def test_no_step_is_gated_on_already_handled(self):
        body = (WORKFLOWS / "review_agent_auto_review.yml").read_text(encoding="utf-8")
        for chunk in body.split("      - name: ")[1:]:
            head = chunk.split("run:", 1)[0]
            assert "'ALREADY_HANDLED'" not in head


class TestTheApprovePathCanRunDuringTheTransition:
    """The change has to be able to merge itself.

    `Execute merge` runs the PR's copy of the workflow against a
    `ref: RELEASE_BRANCH` checkout, so it calls the BASE's
    `review_accept_and_merge.sh` — which rejects an empty issue until this
    branch lands. Without a guard, an approved issue-less PR exits 2 and
    waits for a human, which is the incident this whole change removes.
    """

    def test_the_merge_step_carries_a_self_retiring_transition_guard(self):
        body = (WORKFLOWS / "review_agent_auto_review.yml").read_text(encoding="utf-8")
        step = body.split("- name: Execute merge (if approved)", 1)[1]
        step = step.split("      - name: ", 1)[0]
        # Guarded on BOTH conditions: no issue, and a base script that
        # predates the guard. Either alone would fork behavior permanently.
        assert '-z "$ISSUE"' in step
        assert "grep -q 'HAS_ISSUE' scripts/agents/review_accept_and_merge.sh" in step
        # And it still calls the real script in every other case.
        assert "bash scripts/agents/review_accept_and_merge.sh '$PR' '$ISSUE'" in step


class TestAFailedReadIsNotAnAnswer:
    """ "No issue" and "could not tell" must not be the same result.

    `pr_linked_issue` piped `graphql` straight into `jq`, so the wrapper's
    exit status was the pipeline's first segment and got discarded (V-043):
    a rate-limited or errored read returned empty output and exit 0. Empty
    means "this PR closes no issue", and since #3935 that answer makes the
    merge path skip every issue-side step -- so one transient GraphQL
    failure would merge a linked PR without closing its issue, moving its
    lane, or handing it to the owner. Silently.
    """

    def test_the_helper_does_not_pipe_the_graphql_call(self):
        body = LIB.read_text(encoding="utf-8")
        fn = body.split("pr_linked_issue() {", 1)[1].split("\n}", 1)[0]
        assert "resp=" in fn, "the response must land in a variable first"
        assert "graphql " in fn
        for line in fn.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "graphql " in stripped and stripped.endswith("\\"):
                # a continuation is fine; a pipe into jq on the same
                # logical command is what discards the status
                assert "| jq" not in stripped

    def test_a_failed_read_returns_non_zero(self):
        body = LIB.read_text(encoding="utf-8")
        fn = body.split("pr_linked_issue() {", 1)[1].split("\n}", 1)[0]
        assert "return 1" in fn, "a failed read must be distinguishable from 'no issue'"

    @pytest.mark.parametrize(
        "script",
        [
            "review_ensure_handoff.sh",
            "dispatch_conflict_resolution.sh",
            "scrummaster_sprint_report.sh",
        ],
    )
    def test_every_caller_handles_the_failure_status(self, script):
        body = (SCRIPTS / script).read_text(encoding="utf-8")
        calls = [
            ln
            for ln in body.splitlines()
            if "pr_linked_issue" in ln and "#" not in ln.split("pr_linked_issue")[0]
        ]
        assert calls, f"{script} no longer calls pr_linked_issue -- update this test"
        for call in calls:
            # Either the failure is caught (`if ! VAR=...`) or explicitly
            # tolerated (`|| true`). A bare assignment under `set -e` would
            # abort mid-run with no explanation.
            assert ("if ! " in call) or ("|| true" in call), call
