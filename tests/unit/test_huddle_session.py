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
* the races the #3736 guards caught are closed structurally (one concurrency
  group per PR), not merely detected;
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
        condition = str(_load(SESSION)["jobs"]["huddle"]["if"])
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
