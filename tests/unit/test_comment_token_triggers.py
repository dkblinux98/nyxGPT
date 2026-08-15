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
    "notify_scrum_ready.yml": ("READY_FOR_NEXT_ISSUE", "dispatch-next-issue"),
    "handle_acceptance_failure.yml": ("@acceptance-failure", "handle"),
    "handle_improvement.yml": ("@improvement", "handle"),
}

VERIFY_STEP = "Verify issue is In Progress in ProjectV2 (otherwise exit)"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _step_scripts(step: dict) -> str:
    return "\n".join([step.get("run", "") or "", ((step.get("with") or {}).get("script", "") or "")])


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
        gate_step = next(s for s in gate["steps"] if s.get("uses", "").endswith("comment-token-gate"))
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


class TestRealCommandPostsStillTrigger:
    """The scripts that legitimately POST a retry command keep working: their
    token must open a line, or the anchored gate would swallow it."""

    def test_gh_project_posts_the_retry_command_at_line_start(self):
        source = (REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh").read_text()
        posted = [line for line in source.splitlines() if line.strip() == RETRY]
        assert posted, "no line-start retry command found in gh_project.sh"
        for line in posted:
            assert comment_tokens.is_command(line, RETRY)
