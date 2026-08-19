"""Structural tests for the comment-token triggers (#3790).

2026-08-15: the developer agent's stop message ("...move the issue back to In
Progress and comment `RETRY_IMPLEMENTATION` to resume") was matched by its own
trigger's bare `contains()` test, so posting it started another run, which
stopped and posted it again -- ~500 runs and ~500 comments across #3782/#3784
in under two hours. #3706 was the same defect on the kick token.

These are cheap structural guards on the shape of the fix, not a workflow
runner. They fail if:

  * the stop message names the retry token again (the #3706 test pattern,
    extended to this message)
  * an agent comment that names a token stops carrying the informational
    marker, or a token trigger stops excluding marked comments
  * any of the four comment-token triggers loses its anchored gate job, or
    starts work without consulting it
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
GATE_ACTION = REPO_ROOT / ".github" / "actions" / "comment-token-gate" / "action.yml"
DEV_WF = WORKFLOWS / "developer_auto_implement.yml"

_MODULE_PATH = REPO_ROOT / "scripts" / "agents" / "lib" / "comment_tokens.py"
_spec = importlib.util.spec_from_file_location("comment_tokens", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
comment_tokens = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = comment_tokens
_spec.loader.exec_module(comment_tokens)

RETRY = "RETRY_IMPLEMENTATION"

#: workflow file -> (token, name of the job that does the work)
TOKEN_TRIGGERS = {
    "developer_auto_implement.yml": (RETRY, "implement"),
    "handle_acceptance_failure.yml": ("@acceptance-failure", "handle"),
    "handle_improvement.yml": ("@improvement", "handle"),
    "conflict_owner_escalation.yml": ("CONFLICT_REQUIRES_OWNER_DECISION", "escalate"),
}

# Renamed by #3882: the step now *claims* the issue on assignment rather than
# demanding a second actor stage the board first.
VERIFY_STEP = "Claim the issue (assignment is the dispatch)"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _step_scripts(step: dict) -> str:
    return "\n".join(
        [step.get("run", "") or "", ((step.get("with") or {}).get("script", "") or "")]
    )


def _steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job].get("steps", []) or []


class TestStopMessageIsTokenFree:
    """AC: the stop/status-check message no longer contains the retry token."""

    def _verify_step(self) -> dict:
        steps = _steps(_load(DEV_WF), "implement")
        matches = [s for s in steps if s.get("name") == VERIFY_STEP]
        assert matches, f"step {VERIFY_STEP!r} not found -- did it get renamed?"
        return matches[0]

    def test_the_status_check_step_never_names_the_retry_token(self):
        assert RETRY not in _step_scripts(self._verify_step())

    def test_the_stop_message_points_at_the_runbook_instead(self):
        script = _step_scripts(self._verify_step())
        assert "agents/runbooks/developer-runbook.md" in script

    def test_the_stop_message_is_stamped_informational(self):
        script = _step_scripts(self._verify_step())
        assert "nyxgpt-token-mention" in script

    def test_the_stop_path_consults_the_loop_guard(self):
        script = _step_scripts(self._verify_step())
        assert "stop_loop_guard.py" in script
        assert "nyxgpt-dev-stop-cycle" in script
        assert "nyxgpt-dev-stop-halted" in script

    def test_the_escalation_reports_the_cycle_reached_not_the_threshold(self):
        """The halt can fire above `max_cycles` once the halt marker ages out
        of the window while stop markers remain, so the count in the prose
        comes from `.cycle_number`. `%sth` also rendered "the 3th time"."""
        script = _step_scripts(self._verify_step())
        assert ".cycle_number" in script
        assert "%sth" not in script

    def test_the_escalation_does_not_promise_an_owner_only_release(self):
        """The halt also lapses as cycles age out of the window; the prose
        must not read as a lockout that waits for a human."""
        script = _step_scripts(self._verify_step())
        assert "window, or until the repo owner comments" in script


class TestAgentCommentsCannotTriggerThemselves:
    """Any comment this workflow posts that NAMES the retry token must either
    be a deliberate command (token on its own line) or be stamped inert."""

    def test_every_comment_posting_step_is_command_or_marked(self):
        offenders = []
        for step in _steps(_load(DEV_WF), "implement"):
            script = _step_scripts(step)
            if RETRY not in script:
                continue
            if "gh issue comment" not in script and "createComment" not in script:
                continue
            deliberate_command = f"\\n{RETRY}\\n" in script
            marked = "nyxgpt-token-mention" in script
            if not (deliberate_command or marked):
                offenders.append(step.get("name", "<unnamed>"))
        assert not offenders, (
            "these steps post a comment naming the retry token without stamping it "
            f"informational (#3790): {offenders}"
        )


class TestEveryTokenTriggerIsGated:
    """AC: implemented consistently for ALL comment-token triggers."""

    @pytest.mark.parametrize("filename", sorted(TOKEN_TRIGGERS))
    def test_trigger_has_a_gate_job_using_the_shared_action(self, filename):
        workflow = _load(WORKFLOWS / filename)
        token, _ = TOKEN_TRIGGERS[filename]
        gate = workflow["jobs"].get("comment_gate")
        assert gate, f"{filename} has no comment_gate job"
        uses = [s.get("uses", "") for s in gate.get("steps", [])]
        assert "./.github/actions/comment-token-gate" in uses
        gate_step = next(
            s for s in gate["steps"] if s.get("uses", "").endswith("comment-token-gate")
        )
        assert str(gate_step["with"]["token"]) == token

    @pytest.mark.parametrize("filename", sorted(TOKEN_TRIGGERS))
    def test_gate_excludes_comments_stamped_informational(self, filename):
        gate = _load(WORKFLOWS / filename)["jobs"]["comment_gate"]
        condition = str(gate.get("if", ""))
        assert "!contains(github.event.comment.body, 'nyxgpt-token-mention')" in condition

    @pytest.mark.parametrize("filename", sorted(TOKEN_TRIGGERS))
    def test_work_job_requires_the_gate_verdict(self, filename):
        token, work_job = TOKEN_TRIGGERS[filename]
        job = _load(WORKFLOWS / filename)["jobs"][work_job]
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert "comment_gate" in needs, f"{filename}:{work_job} does not depend on the gate"
        assert "needs.comment_gate.outputs.proceed == 'true'" in str(job.get("if", ""))

    @pytest.mark.parametrize("filename", sorted(TOKEN_TRIGGERS))
    def test_work_job_no_longer_substring_matches_the_token_itself(self, filename):
        token, work_job = TOKEN_TRIGGERS[filename]
        condition = str(_load(WORKFLOWS / filename)["jobs"][work_job].get("if", ""))
        assert f"contains(github.event.comment.body, '{token}')" not in condition

    def test_every_token_the_library_knows_about_is_covered(self):
        gated = {token for token, _ in TOKEN_TRIGGERS.values()}
        # PAUSE_SPRINT has no workflow trigger of its own; it is read by the
        # autopilot calculation, and the library treats it like the rest.
        assert gated == set(comment_tokens.COMMAND_TOKENS) - {"PAUSE_SPRINT"}


class TestGateAction:
    def test_action_runs_both_decision_modules(self):
        action = _load(GATE_ACTION)
        script = "\n".join(step.get("run", "") for step in action["runs"]["steps"])
        assert "comment_tokens.py" in script
        assert "stop_loop_guard.py" in script

    def test_comment_body_is_passed_through_an_env_var_not_interpolated(self):
        """Untrusted comment text must never be pasted into the shell body."""
        step = _load(GATE_ACTION)["runs"]["steps"][0]
        assert step["env"]["COMMENT_BODY"] == "${{ inputs.comment-body }}"
        assert "inputs.comment-body" not in step["run"]

    def test_action_exposes_a_proceed_output(self):
        assert "proceed" in _load(GATE_ACTION)["outputs"]


class TestClaudeBotIsAnAllowedTriggerAuthor:
    """D-020 (#3870): every GitHub write from a Claude remote session carries
    the `claude[bot]` App identity, so the two sanctioned triggers name it in
    their author allowlists. Pinned here so a later allowlist tidy-up has to
    revisit the decision rather than silently re-break remote sessions."""

    @pytest.mark.parametrize(
        "filename,job",
        # The author allowlist is layer 1 -- on the gate job where there is
        # one, on the work job where there is not.
        [
            # notify_scrum_ready.yml is no longer comment-triggered (#3882):
            # it runs on a repository_dispatch, so it has no author to allow.
            ("claude-code-review.yml", "claude-review"),
        ],
    )
    def test_trigger_allows_the_claude_app_identity(self, filename, job):
        condition = str(_load(WORKFLOWS / filename)["jobs"][job].get("if", ""))
        assert "github.event.comment.user.login == 'claude[bot]'" in condition

    def test_review_action_permits_the_bot_actor(self):
        """`claude-code-action` refuses bot-actored runs unless listed, so the
        allowlist entry alone is not enough for the review trigger."""
        steps = _steps(_load(WORKFLOWS / "claude-code-review.yml"), "claude-review")
        allowed = [(s.get("with") or {}).get("allowed_bots") for s in steps]
        assert "claude" in [a for a in allowed if a]

    def test_every_action_call_site_reachable_by_a_bot_actor_permits_it(self):
        """The pairing, stated once: a workflow that treats `claude[bot]` as a
        permitted actor must also pass `allowed_bots` to `claude-code-action`.

        These are two independent gates gating one identity, edited in
        different places, and that is exactly how they drifted apart. #3870
        fixed both for `claude-code-review.yml`. #3882 then made assignment
        the dispatch and named `claude[bot]` a permitted *assigner* in
        `developer_auto_implement.yml`'s claim step -- updating the workflow's
        own gate but not the action's, which refuses a bot-actored run
        outright. The lever half-worked: the issue was claimed and moved to In
        Progress, then the run died at action init with "Workflow initiated by
        non-human actor", leaving four issues (#3855/#3858/#3860/#3864) claimed
        with nothing implementing them (2026-08-18).

        Iterating every step of every job means a seventh call site added
        later is covered without anyone remembering to extend this list.
        """
        for filename in ("developer_auto_implement.yml", "claude-code-review.yml"):
            workflow = _load(WORKFLOWS / filename)
            names_the_bot = "claude[bot]" in (WORKFLOWS / filename).read_text()
            assert names_the_bot, (
                f"{filename} no longer names claude[bot] as a permitted actor -- "
                "if that is deliberate, remove it from this test with the reason"
            )
            for job, spec in (workflow.get("jobs") or {}).items():
                for index, step in enumerate(spec.get("steps") or []):
                    uses = str(step.get("uses", ""))
                    if "anthropics/claude-code-action" not in uses:
                        continue
                    allowed = str((step.get("with") or {}).get("allowed_bots", ""))
                    assert "claude" in allowed, (
                        f"{filename}: job '{job}' step {index} calls "
                        "claude-code-action without allowed_bots containing "
                        "'claude', so a claude[bot]-actored run of this workflow "
                        "is refused at action init even though the workflow's own "
                        "gate lets it through"
                    )


class TestRealCommandPostsStillTrigger:
    """The scripts that legitimately POST a retry command keep working: their
    token must open a line, or the anchored gate would swallow it."""

    def test_gh_project_posts_the_retry_command_at_line_start(self):
        source = (REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh").read_text()
        posted = [line for line in source.splitlines() if line.strip() == RETRY]
        assert posted, "no line-start retry command found in gh_project.sh"
        for line in posted:
            assert comment_tokens.is_command(line, RETRY)
