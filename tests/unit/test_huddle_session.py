"""Structural tests for the consolidated huddle session (#3911).

The huddle is a workflow, so most of it can only be executed by GitHub. What
*can* be checked here is the set of properties the consolidation was required
to preserve, each of which is a line in the issue and each of which a plausible
refactor would quietly break:

* the two comment-triggered legs are gone, and nothing re-creates them;
* `huddle_decision_dispatch.yml` is untouched and still keys on the same line;
* every turn is its own `claude-code-action` invocation -- one job must not
  become one long-lived context, because #3687's memorylessness is the reason
  a fresh agent re-reads the thread instead of trusting what it "remembers";
* the races the #3736 guards caught stay closed -- the within-run one by
  construction (one concurrency group per PR, no leg started by a comment),
  the duplicate-trigger one by the `gate` job, which is the guard that did
  *not* become unnecessary;
* the decision comment is authored by the identity `huddle_decision_dispatch`
  requires, because a decision nothing dispatches is the #3733 stall;
* settling is sticky, so an early close is not re-opened by a round that
  never ran;
* the crash markers bracket the rounds, so a session that dies mid-round is
  recoverable;
* the transcript is assembled from the turn files rather than read back from
  Slack, so the record survives both a Slack outage and Slack retention.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SESSION = WORKFLOWS / "huddle_session.yml"
DISPATCH = WORKFLOWS / "huddle_decision_dispatch.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps() -> list[dict]:
    return _load(SESSION)["jobs"]["huddle"]["steps"]


def _action_steps() -> list[dict]:
    return [s for s in _steps() if "anthropics/claude-code-action" in str(s.get("uses", ""))]


class TestTheChainIsGone:
    @pytest.mark.parametrize(
        "retired",
        ["developer_huddle_position.yml", "scrummaster_huddle_mediation.yml"],
    )
    def test_the_comment_triggered_legs_are_deleted(self, retired):
        assert not (WORKFLOWS / retired).exists()

    def test_no_workflow_triggers_on_the_retired_markers(self):
        """A marker that wakes nothing is inert; a marker that wakes something
        is the bus this issue removed."""
        for workflow in WORKFLOWS.glob("*.yml"):
            body = "\n".join(
                line
                for line in workflow.read_text(encoding="utf-8").splitlines()
                if not line.strip().startswith("#")
            )
            for marker in ("HUDDLE_DEV_POSITION", "HUDDLE_MEDIATION_REQUESTED"):
                assert marker not in body, f"{workflow.name} still acts on {marker}"

    def test_the_session_is_started_by_the_trigger_the_review_agent_already_posts(self):
        # On the `gate` job since #3911's review: the trigger conditions and
        # the duplicate stand-down are one decision, taken once, before any
        # step of the huddle itself can run.
        condition = str(_load(SESSION)["jobs"]["gate"]["if"])
        assert "HUDDLE_TRIGGERED" in condition
        assert "vars.REVIEW_AGENT" in condition


class TestDispatchIsUntouched:
    def test_the_dispatch_workflow_still_exists_and_keys_on_the_decision_line(self):
        condition = str(_load(DISPATCH)["jobs"]["dispatch"]["if"])
        assert "HUDDLE_DECISION:" in condition

    def test_the_session_produces_that_line_on_the_pr(self):
        prompts = " ".join(str(s.get("with", {}).get("prompt", "")) for s in _action_steps())
        assert "HUDDLE_DECISION:" in prompts
        posts = " ".join(str(s.get("run", "")) for s in _steps())
        assert "gh pr comment" in posts


class TestMemorylessness:
    def test_every_turn_is_its_own_invocation(self):
        """One job, many sessions. #3687 relies on each agent reasoning from
        what is written down; a single long-lived context would remove that
        without any test noticing."""
        assert len(_action_steps()) >= 3

    def test_each_turn_runs_under_its_own_identity(self):
        tokens = {str(step.get("with", {}).get("github_token", "")) for step in _action_steps()}
        assert any("DEVELOPER_AGENT_TOKEN" in t for t in tokens)
        assert any("REVIEW_AGENT_TOKEN" in t for t in tokens)
        assert any("SCRUMMASTER_AGENT_TOKEN" in t for t in tokens)

    def test_no_turn_may_write_project_code(self):
        for step in _action_steps():
            prompt = str(step.get("with", {}).get("prompt", ""))
            assert "Do NOT write, edit or commit any project code" in prompt or (
                "Do not post the comment yourself" in prompt
            )


class TestRacesAreClosedStructurally:
    def test_one_non_cancelling_concurrency_group_per_pull_request(self):
        concurrency = _load(SESSION)["concurrency"]
        assert "github.event.issue.number" in str(concurrency["group"])
        assert concurrency["cancel-in-progress"] is False


class TestCrashSafety:
    def test_the_started_marker_is_written_before_the_first_turn(self):
        steps = _steps()
        started = next(
            i for i, s in enumerate(steps) if "HUDDLE_SESSION_STARTED" in str(s.get("run", ""))
        )
        first_turn = next(
            i
            for i, s in enumerate(steps)
            if "anthropics/claude-code-action" in str(s.get("uses", ""))
        )
        assert started < first_turn

    def test_the_started_marker_carries_the_thread_id(self):
        run = next(
            str(s.get("run", ""))
            for s in _steps()
            if "HUDDLE_SESSION_STARTED" in str(s.get("run", ""))
        )
        assert "thread=" in run

    def test_a_failed_session_says_so_on_the_pr(self):
        failure_steps = [s for s in _steps() if str(s.get("if", "")).strip() == "failure()"]
        assert failure_steps, "no failure() step -- a dead session would leave no record"
        assert "HUDDLE_FAILED" in str(failure_steps[0].get("run", ""))


class TestTheRecordSurvivesSlack:
    def test_the_transcript_is_assembled_from_the_turn_files(self):
        """Not read back from Slack: a huddle that ran with the channel
        unavailable still has to leave its reasoning on the PR."""
        step = next(s for s in _steps() if "transcript" in str(s.get("name", "")).lower())
        run = str(step.get("run", ""))
        assert "HUDDLE_DIR" in run
        assert "huddle_channel.py read" not in run

    def test_the_transcript_lands_collapsed_so_it_does_not_bury_the_thread(self):
        step = next(s for s in _steps() if "transcript" in str(s.get("name", "")).lower())
        assert "<details>" in str(step.get("run", ""))

    def test_the_transcript_is_written_even_when_a_turn_failed(self):
        step = next(s for s in _steps() if "transcript" in str(s.get("name", "")).lower())
        assert "cancelled()" in str(step.get("if", ""))


class TestSlackIsOptional:
    def test_missing_slack_configuration_does_not_fail_the_run(self):
        """#3910's contract: an unconfigured chat integration degrades, it does
        not break the agent loop."""
        validate = next(
            s for s in _steps() if "Validate required secrets" in str(s.get("name", ""))
        )
        run = str(validate.get("run", ""))
        assert "SLACK" not in run.replace("Slack is NOT validated", "")

    def test_the_round_cap_is_configuration_not_a_literal(self):
        body = SESSION.read_text(encoding="utf-8")
        assert "vars.HUDDLE_MAX_ROUNDS" in body


def _step(predicate):
    return next(s for s in _steps() if predicate(s))


class TestTheDecisionCanActuallyDispatch:
    """The handoff the whole session exists to reach.

    `huddle_decision_dispatch.yml` fires only for a HUDDLE_DECISION comment
    authored by the scrummaster or the owner. The session's job-level GH_TOKEN
    is the *review* agent's, so a decision posted with the job default lands,
    reads correctly, and starts nothing -- the issue sits In Review exactly as
    it did on #3733, which is the stall the dispatch workflow was built to
    close. The old `scrummaster_huddle_mediation.yml` satisfied the gate by
    construction; here it is one env override, and one env override is exactly
    the kind of thing a later edit drops.
    """

    def test_the_dispatch_gate_still_requires_the_scrummaster_as_author(self):
        condition = str(_load(DISPATCH)["jobs"]["dispatch"]["if"])
        assert "github.event.comment.user.login == vars.SCRUM_AGENT" in condition

    def test_the_decision_comment_is_posted_under_the_scrummaster_token(self):
        poster = _step(
            lambda s: "gh pr comment" in str(s.get("run", ""))
            and "decision-comment" in str(s.get("run", ""))
        )
        token = str(poster.get("env", {}).get("GH_TOKEN", ""))
        assert "SCRUMMASTER_AGENT_TOKEN" in token, (
            "the decision comment must be authored by the scrummaster or "
            "huddle_decision_dispatch.yml will never fire"
        )
        assert "REVIEW_AGENT_TOKEN" not in token

    def test_the_decision_turn_shells_out_under_the_scrummaster_token_too(self):
        """`escalate` runs assign_issue_verified / gh pr edit /
        sprint_autopilot_kick through the ambient GH_TOKEN, which at job level
        is the review agent's."""
        decision = _step(
            lambda s: "scrummaster" in str(s.get("name", "")).lower()
            and "claude-code-action" in str(s.get("uses", ""))
        )
        assert "SCRUMMASTER_AGENT_TOKEN" in str(decision.get("env", {}).get("GH_TOKEN", ""))

    def test_the_job_default_is_still_the_review_agent(self):
        """The override is deliberate and local; if the job default ever
        became the scrummaster, the dev/review turns would speak as it."""
        job_env = _load(SESSION)["jobs"]["huddle"]["env"]
        assert "REVIEW_AGENT_TOKEN" in str(job_env["GH_TOKEN"])


class TestADuplicateTriggerStandsDown:
    """The concurrency group serializes a second trigger; it does not stop it.

    #3728 posted two HUDDLE_TRIGGERED comments minutes apart because both of
    `review_agent_auto_review.yml`'s trigger paths passed their
    check-before-post guards, and those guards are unchanged. Queued behind
    the first run, an unguarded second run opens a second Slack thread and
    spends a second huddle's worth of invocations.
    """

    def test_a_gate_job_decides_whether_this_run_owns_the_round(self):
        jobs = _load(SESSION)["jobs"]
        assert "gate" in jobs, "no stand-down job -- a duplicate trigger runs a second huddle"
        assert "primary" in str(jobs["gate"]["outputs"])

    def test_the_gate_asks_the_surviving_state_helper(self):
        run = " ".join(str(s.get("run", "")) for s in _load(SESSION)["jobs"]["gate"]["steps"])
        assert "huddle_state.py" in run
        assert "guard HUDDLE_TRIGGERED" in run

    def test_the_huddle_runs_only_when_the_gate_says_it_is_primary(self):
        huddle = _load(SESSION)["jobs"]["huddle"]
        assert "gate" in str(huddle["needs"])
        assert "needs.gate.outputs.primary == 'true'" in str(huddle["if"])

    def test_the_stand_down_is_a_job_not_a_per_step_condition(self):
        """~20 steps each carrying the same `if:` is one forgotten condition
        away from running the whole huddle anyway."""
        for step in _steps():
            assert "primary" not in str(step.get("if", ""))


class TestSettlingIsSticky:
    """A huddle that settles in round 1 must not pay for round 3.

    Round N gates on round N-1's settle output alone. When round 1 settles,
    round 2 is skipped, so `04-review.md` is never written -- and a settle
    check that only looks at its own round's file reads that absence as "not
    settled" and lets round 3 run: two paid invocations on a closed question.
    """

    @pytest.mark.parametrize(
        "this_round,previous", [("settle2", "settle1"), ("settle3", "settle2")]
    )
    def test_each_settle_check_inherits_the_previous_answer(self, this_round, previous):
        step = _step(lambda s: s.get("id") == this_round)
        assert f"steps.{previous}.outputs.settled" in str(
            step.get("env", {})
        ), f"{this_round} ignores {previous}, so an early settle un-settles itself"
        assert "ALREADY" in str(step.get("run", ""))

    @pytest.mark.parametrize(
        "turn,gate",
        [("dev2", "settle1"), ("rev2", "settle1"), ("dev3", "settle2"), ("rev3", "settle2")],
    )
    def test_no_turn_runs_after_the_huddle_settled(self, turn, gate):
        step = _step(lambda s: s.get("id") == turn)
        assert f"steps.{gate}.outputs.settled != 'true'" in str(step.get("if", ""))


class TestExecutedVerification:
    """#3775: the session's control flow has to be runnable, not just read."""

    def test_the_session_can_be_dispatched_by_hand_for_a_live_run(self):
        triggers = _load(SESSION)["on"] if "on" in _load(SESSION) else _load(SESSION)[True]
        assert "workflow_dispatch" in triggers
        assert "pr_number" in str(triggers["workflow_dispatch"]["inputs"])

    def test_a_smoke_workflow_executes_the_session_shell_bodies(self):
        smoke = WORKFLOWS / "huddle-session-smoke.yml"
        assert smoke.exists(), "nothing executes the session's shell control flow"
        assert "huddle_session" in smoke.read_text(encoding="utf-8")
