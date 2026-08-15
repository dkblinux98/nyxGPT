"""Unit tests for scripts/agents/lib/comment_tokens.py (#3790).

The defect these pin down: every comment-driven trigger matched its token as
a bare substring, so an agent comment that merely *named* the token started a
run. On 2026-08-15 the developer agent's own stop message did exactly that to
itself, ~500 times across #3782/#3784 in under two hours.

The contract asserted here:

  * a token issues a command only when it OPENS A LINE
  * prose that names the token mid-sentence is a mention, never a command
  * quoted (`>`) lines and fenced code blocks cannot replay a command
  * the informational marker disqualifies a whole comment regardless of
    where the token sits (the #3706 structural guard, generalised)
  * the real comment bodies this repo posts keep working: the auto-retry
    comment and the auto-resume comment are commands; every guidance and
    escalation comment is not
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "comment_tokens.py"
)
_spec = importlib.util.spec_from_file_location("comment_tokens", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
comment_tokens = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = comment_tokens
_spec.loader.exec_module(comment_tokens)

RETRY = "RETRY_IMPLEMENTATION"
KICK = "READY_FOR_NEXT_ISSUE"


class TestAnchoredMatching:
    def test_bare_token_on_its_own_line_is_a_command(self):
        assert comment_tokens.is_command(RETRY, RETRY)

    def test_token_opening_a_line_of_a_longer_body_is_a_command(self):
        body = "⚠️ Workflow failed - auto-retry triggered.\n\n" f"{RETRY}\n<!-- nyxgpt-retry -->\n"
        assert comment_tokens.is_command(body, RETRY)

    def test_token_named_mid_sentence_is_only_a_mention(self):
        body = "Move the issue back to In Progress and comment `RETRY_IMPLEMENTATION` to resume."
        assert comment_tokens.mentions(body, RETRY)
        assert not comment_tokens.is_command(body, RETRY)

    def test_numbered_guidance_step_is_not_a_command(self):
        body = "**Next steps:**\n1. Review the logs\n3. Once fixed, comment `RETRY_IMPLEMENTATION` to retry\n"
        assert not comment_tokens.is_command(body, RETRY)

    def test_list_bullet_and_markdown_decoration_still_open_a_line(self):
        assert comment_tokens.is_command(f"- `{RETRY}`", RETRY)
        assert comment_tokens.is_command(f"**{RETRY}**", RETRY)
        assert comment_tokens.is_command(f"  {RETRY}  ", RETRY)

    def test_quoted_lines_cannot_replay_a_command(self):
        body = f"Quoting the earlier comment:\n\n> {RETRY}\n\nThat one already ran."
        assert not comment_tokens.is_command(body, RETRY)

    def test_fenced_code_blocks_cannot_replay_a_command(self):
        body = f"To resume, post:\n\n```\n{RETRY}\n```\n"
        assert not comment_tokens.is_command(body, RETRY)

    def test_tilde_fence_is_also_stripped(self):
        body = f"Example:\n\n~~~\n{RETRY}\n~~~\n"
        assert not comment_tokens.is_command(body, RETRY)

    def test_text_after_the_fence_closes_is_matched_again(self):
        body = f"```\nsome log\n```\n{RETRY}\n"
        assert comment_tokens.is_command(body, RETRY)

    def test_token_prefix_of_a_longer_word_is_not_a_command(self):
        assert not comment_tokens.is_command("RETRY_IMPLEMENTATIONS are queued", RETRY)
        assert not comment_tokens.is_command("@improvement-ideas welcome", "@improvement")

    def test_empty_and_missing_inputs_are_not_commands(self):
        assert not comment_tokens.is_command("", RETRY)
        assert not comment_tokens.is_command(RETRY, "")


class TestInformationalMarker:
    def test_marker_disqualifies_even_a_line_start_token(self):
        body = f"{RETRY}\n\n{comment_tokens.MENTION_MARKER}\n"
        assert comment_tokens.is_command(body, RETRY) is False

    def test_autopilot_marker_from_3706_is_honoured(self):
        body = f"{KICK}\n\n{comment_tokens.AUTOPILOT_INFO_MARKER}\n"
        assert not comment_tokens.is_command(body, KICK)

    def test_as_mention_stamps_a_body_and_is_idempotent(self):
        stamped = comment_tokens.as_mention("Naming RETRY_IMPLEMENTATION for guidance.")
        assert comment_tokens.MENTION_MARKER in stamped
        assert comment_tokens.as_mention(stamped) == stamped

    def test_is_informational_detects_both_markers(self):
        assert comment_tokens.is_informational(comment_tokens.MENTION_MARKER)
        assert comment_tokens.is_informational(comment_tokens.AUTOPILOT_INFO_MARKER)
        assert not comment_tokens.is_informational("plain text")


class TestEveryTokenBehavesTheSame:
    """AC: the fix is implemented consistently for ALL comment tokens."""

    @pytest.mark.parametrize("token", comment_tokens.COMMAND_TOKENS)
    def test_line_start_is_a_command_and_mid_sentence_is_not(self, token):
        assert comment_tokens.is_command(f"{token}\n", token)
        assert not comment_tokens.is_command(f"Please comment `{token}` when ready.", token)

    @pytest.mark.parametrize("token", comment_tokens.COMMAND_TOKENS)
    def test_marker_always_wins(self, token):
        assert not comment_tokens.is_command(f"{token}\n{comment_tokens.MENTION_MARKER}", token)

    def test_owner_acceptance_commands_carry_their_description_inline(self):
        # The documented form: the command opens the comment, the description
        # follows on the same line.
        assert comment_tokens.is_command(
            "@improvement the settings page needs a save button", "@improvement"
        )
        assert comment_tokens.is_command(
            "@acceptance-failure install fails on a clean Mac", "@acceptance-failure"
        )


class TestIncidentReplay:
    """The exact 2026-08-15 loop, replayed against the new matcher."""

    STOP_MESSAGE = (
        "⏹️ **Developer Agent**: Stopping -- issue is no longer In Progress in the "
        "project (or is not in the project). This run will not proceed past this "
        "check. If this was a manual circuit breaker, no action is needed; move the "
        "issue back to In Progress and comment `RETRY_IMPLEMENTATION` to resume."
    )

    def test_the_message_that_caused_the_loop_would_no_longer_trigger(self):
        assert comment_tokens.mentions(self.STOP_MESSAGE, RETRY), "substring match is what looped"
        assert not comment_tokens.is_command(self.STOP_MESSAGE, RETRY)

    def test_the_auto_retry_comment_still_triggers(self):
        body = (
            "⚠️ **Developer Agent**: Workflow failed - auto-retry triggered.\n\n"
            "**Failed step:** Check if PR already exists\n\n"
            f"{RETRY}\n"
            "<!-- nyxgpt-retry: step=check_if_pr_already_exists sig=abc123 n=1 -->\n"
        )
        assert comment_tokens.is_command(body, RETRY)

    def test_the_auto_resume_comment_still_triggers(self):
        body = (
            "🔁 **Sprint Autopilot — auto-resume (1/3)**: this issue is In Progress "
            "but parked. Restarting implementation automatically (#3709).\n\n"
            f"{RETRY}\n<!-- nyxgpt-resume: issue=3790 n=1 -->"
        )
        assert comment_tokens.is_command(body, RETRY)


class TestCli:
    def _run(self, args, stdin=""):
        return subprocess.run(
            [sys.executable, str(_MODULE_PATH), *args],
            input=stdin,
            capture_output=True,
            text=True,
        )

    def test_is_command_exit_codes(self):
        assert self._run(["is-command", RETRY], stdin=RETRY).returncode == 0
        mention = f"comment `{RETRY}` to resume"
        assert self._run(["is-command", RETRY], stdin=mention).returncode == 1

    def test_is_command_prints_the_decision(self):
        assert self._run(["is-command", RETRY], stdin=RETRY).stdout.strip() == "true"
        assert self._run(["is-command", RETRY, "--quiet"], stdin=RETRY).stdout.strip() == ""

    def test_reads_the_body_from_an_environment_variable(self, monkeypatch):
        import os

        env = dict(os.environ, COMMENT_BODY=f"comment `{RETRY}` to resume")
        result = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "is-command", RETRY, "--from-env", "COMMENT_BODY"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 1
        assert result.stdout.strip() == "false"

    def test_mark_appends_the_marker(self):
        result = self._run(["mark"], stdin="naming RETRY_IMPLEMENTATION")
        assert comment_tokens.MENTION_MARKER in result.stdout

    def test_tokens_lists_every_command_token(self):
        listed = self._run(["tokens"]).stdout.split()
        assert set(listed) == set(comment_tokens.COMMAND_TOKENS)
