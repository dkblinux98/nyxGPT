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

# Sample tokens for the matcher, which is generic over the token string.
# Deliberately two *surviving* tokens: the retry and kick tokens these tests
# were written against were deleted by #3882, and naming a dead lever in the
# suite invites someone to wire it back up.
TOKEN = "PAUSE_SPRINT"
OTHER = "@acceptance-failure"


class TestAnchoredMatching:
    def test_bare_token_on_its_own_line_is_a_command(self):
        assert comment_tokens.is_command(TOKEN, TOKEN)

    def test_token_opening_a_line_of_a_longer_body_is_a_command(self):
        body = "⚠️ Workflow failed - auto-retry triggered.\n\n" f"{TOKEN}\n<!-- nyxgpt-retry -->\n"
        assert comment_tokens.is_command(body, TOKEN)

    def test_token_named_mid_sentence_is_only_a_mention(self):
        body = "Move the issue back to In Progress and comment `PAUSE_SPRINT` to hold the loop."
        assert comment_tokens.mentions(body, TOKEN)
        assert not comment_tokens.is_command(body, TOKEN)

    def test_numbered_guidance_step_is_not_a_command(self):
        body = "**Next steps:**\n1. Review the logs\n3. Once fixed, comment `PAUSE_SPRINT` to hold\n"
        assert not comment_tokens.is_command(body, TOKEN)

    def test_list_bullet_and_markdown_decoration_still_open_a_line(self):
        assert comment_tokens.is_command(f"- `{TOKEN}`", TOKEN)
        assert comment_tokens.is_command(f"**{TOKEN}**", TOKEN)
        assert comment_tokens.is_command(f"  {TOKEN}  ", TOKEN)

    def test_quoted_lines_cannot_replay_a_command(self):
        body = f"Quoting the earlier comment:\n\n> {TOKEN}\n\nThat one already ran."
        assert not comment_tokens.is_command(body, TOKEN)

    def test_fenced_code_blocks_cannot_replay_a_command(self):
        body = f"To resume, post:\n\n```\n{TOKEN}\n```\n"
        assert not comment_tokens.is_command(body, TOKEN)

    def test_tilde_fence_is_also_stripped(self):
        body = f"Example:\n\n~~~\n{TOKEN}\n~~~\n"
        assert not comment_tokens.is_command(body, TOKEN)

    def test_text_after_the_fence_closes_is_matched_again(self):
        body = f"```\nsome log\n```\n{TOKEN}\n"
        assert comment_tokens.is_command(body, TOKEN)

    def test_token_prefix_of_a_longer_word_is_not_a_command(self):
        assert not comment_tokens.is_command("PAUSE_SPRINTS are queued", TOKEN)
        assert not comment_tokens.is_command("@improvement-ideas welcome", "@improvement")

    def test_empty_and_missing_inputs_are_not_commands(self):
        assert not comment_tokens.is_command("", TOKEN)
        assert not comment_tokens.is_command(TOKEN, "")


class TestInformationalMarker:
    def test_marker_disqualifies_even_a_line_start_token(self):
        body = f"{TOKEN}\n\n{comment_tokens.MENTION_MARKER}\n"
        assert comment_tokens.is_command(body, TOKEN) is False

    def test_autopilot_marker_from_3706_is_honoured(self):
        body = f"{OTHER}\n\n{comment_tokens.AUTOPILOT_INFO_MARKER}\n"
        assert not comment_tokens.is_command(body, OTHER)

    def test_as_mention_stamps_a_body_and_is_idempotent(self):
        stamped = comment_tokens.as_mention("Naming PAUSE_SPRINT for guidance.")
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
    """The 2026-08-15 loop's *shape*, replayed against the matcher.

    The literal message cannot be replayed any more: the token it named was
    deleted with the trigger that read it (#3882). What is replayed is the
    property that mattered -- an agent comment that discusses a command must
    never issue one -- on a token that still exists.
    """

    STOP_MESSAGE = (
        "⏹️ **Developer Agent**: Stopping -- issue #3790 is in a lane an assignment "
        "cannot claim. If this was a manual circuit breaker, no action is needed; "
        "if you want the whole loop held, comment `PAUSE_SPRINT` instead."
    )

    def test_the_message_shape_that_caused_the_loop_would_no_longer_trigger(self):
        assert comment_tokens.mentions(self.STOP_MESSAGE, TOKEN), "substring match is what looped"
        assert not comment_tokens.is_command(self.STOP_MESSAGE, TOKEN)

    def test_a_deliberate_command_still_triggers(self):
        body = (
            "⏸️ Holding the sprint while the owner tests the candidate.\n\n"
            f"{TOKEN}\n"
        )
        assert comment_tokens.is_command(body, TOKEN)


class TestRetiredLevers:
    """#3882: the two tokens that drove the state machine are deleted, not
    deprecated. A token left in this tuple is a token some workflow can be
    wired back to -- which is how prose became an API in the first place."""

    def test_the_retry_and_kick_tokens_are_not_command_tokens(self):
        # Split so this file is not itself a mention of either token.
        retired = ("RETRY_" + "IMPLEMENTATION", "READY_FOR_NEXT" + "_ISSUE")
        assert not set(retired) & set(comment_tokens.COMMAND_TOKENS)

    def test_every_surviving_token_authors_or_stops_work(self):
        """None of them starts developer work; that is now an assignment."""
        assert set(comment_tokens.COMMAND_TOKENS) == {
            "PAUSE_SPRINT",
            "@acceptance-failure",
            "@improvement",
            "CONFLICT_REQUIRES_OWNER_DECISION",
        }


class TestCli:
    def _run(self, args, stdin=""):
        return subprocess.run(
            [sys.executable, str(_MODULE_PATH), *args],
            input=stdin,
            capture_output=True,
            text=True,
        )

    def test_is_command_exit_codes(self):
        assert self._run(["is-command", TOKEN], stdin=TOKEN).returncode == 0
        mention = f"comment `{TOKEN}` to resume"
        assert self._run(["is-command", TOKEN], stdin=mention).returncode == 1

    def test_is_command_prints_the_decision(self):
        assert self._run(["is-command", TOKEN], stdin=TOKEN).stdout.strip() == "true"
        assert self._run(["is-command", TOKEN, "--quiet"], stdin=TOKEN).stdout.strip() == ""

    def test_reads_the_body_from_an_environment_variable(self, monkeypatch):
        import os

        env = dict(os.environ, COMMENT_BODY=f"comment `{TOKEN}` to resume")
        result = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "is-command", TOKEN, "--from-env", "COMMENT_BODY"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 1
        assert result.stdout.strip() == "false"

    def test_mark_appends_the_marker(self):
        result = self._run(["mark"], stdin=f"naming {TOKEN}")
        assert comment_tokens.MENTION_MARKER in result.stdout

    def test_tokens_lists_every_command_token(self):
        listed = self._run(["tokens"]).stdout.split()
        assert set(listed) == set(comment_tokens.COMMAND_TOKENS)
