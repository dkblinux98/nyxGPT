"""Unit tests for the huddle channel adapter (#3910, consumed by #3911).

Three properties matter here and none of them need a live Slack:

* **speaker -> token**, because a wrong token silently puts every turn under
  one identity, which is the whole point of a huddle you can read back;
* **graceful degradation**, because an unconfigured chat integration must not
  break the agent loop -- the same contract `notify_human_escalation` has
  carried since #3695;
* **transcript parsing**, because the record has to survive Slack retention on
  the PR, and a parser that drops turns loses the reasoning silently.

The transport is stubbed at `_call`, so these run offline and assert what was
*sent*, not what Slack did with it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
_spec = importlib.util.spec_from_file_location("huddle_channel", _LIB / "huddle_channel.py")
assert _spec is not None and _spec.loader is not None
hc = importlib.util.module_from_spec(_spec)
# Registered before exec: @dataclass resolves its own module out of
# sys.modules, and a module loaded by spec alone is not there yet.
sys.modules["huddle_channel"] = hc
_spec.loader.exec_module(hc)


class RecordingSlack(hc.SlackChannel):
    """A SlackChannel whose transport records instead of calling out."""

    def __init__(self, *args, reply=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: list[tuple[str, str, dict]] = []
        self._reply = reply or {"ok": True, "ts": "1700000000.000100"}

    def _call(self, method, token, payload):  # type: ignore[override]
        self.calls.append((method, token, payload))
        reply = dict(self._reply)
        # Mirrors the real `_call` contract: a failed call yields {}, which is
        # what the four operations branch on. A stub that returned the error
        # body would make `post` look successful.
        return reply if reply.get("ok") else {}


TOKENS = {"dev": "xoxp-dev", "review": "xoxp-review", "scrum": "xoxp-scrum"}


class TestSpeakerTokenSelection:
    def test_each_speaker_posts_under_its_own_token(self):
        channel = RecordingSlack("C123", tokens=TOKENS)
        for speaker in ("dev", "review", "scrum"):
            channel.post("1700000000.000100", speaker, f"a {speaker} turn")
        assert [token for _, token, _ in channel.calls] == ["xoxp-dev", "xoxp-review", "xoxp-scrum"]

    def test_turns_are_threaded_under_the_opening_message(self):
        channel = RecordingSlack("C123", tokens=TOKENS)
        channel.post("1700000000.000100", "dev", "hello")
        _, _, payload = channel.calls[0]
        assert payload["thread_ts"] == "1700000000.000100"
        assert payload["channel"] == "C123"

    def test_an_unknown_speaker_is_a_programming_error_not_a_silent_default(self):
        channel = RecordingSlack("C123", tokens=TOKENS)
        with pytest.raises(ValueError, match="unknown speaker"):
            channel.post("1700000000.000100", "owner", "hello")

    def test_posting_without_a_thread_is_refused_rather_than_leaking_to_the_channel(self):
        channel = RecordingSlack("C123", tokens=TOKENS)
        assert channel.post("", "dev", "hello") is False
        assert channel.calls == []


class TestGracefulDegradation:
    def test_no_channel_configured_yields_a_null_channel(self, monkeypatch):
        monkeypatch.delenv(hc.CHANNEL_ENV, raising=False)
        assert isinstance(hc.get_channel(), hc.NullChannel)

    def test_no_tokens_configured_yields_a_null_channel(self, monkeypatch):
        monkeypatch.setenv(hc.CHANNEL_ENV, "C123")
        for env in hc.SPEAKER_TOKEN_ENV.values():
            monkeypatch.delenv(env, raising=False)
        assert isinstance(hc.get_channel(), hc.NullChannel)

    def test_one_configured_token_is_enough_to_get_a_real_channel(self, monkeypatch):
        monkeypatch.setenv(hc.CHANNEL_ENV, "C123")
        monkeypatch.setenv("SLACK_USER_TOKEN_DEV", "xoxp-dev")
        for env in ("SLACK_USER_TOKEN_REVIEW", "SLACK_USER_TOKEN_SCRUM"):
            monkeypatch.delenv(env, raising=False)
        assert isinstance(hc.get_channel(), hc.SlackChannel)

    def test_the_null_channel_never_raises_and_reports_no_thread(self, capsys):
        channel = hc.NullChannel("unconfigured")
        assert channel.open_thread(1, 2, "why") == ""
        assert channel.post("t", "dev", "x") is False
        assert channel.read("t") == []
        assert channel.permalink("t") == ""
        assert "no huddle channel" in capsys.readouterr().err

    def test_it_warns_once_not_once_per_turn(self, capsys):
        channel = hc.NullChannel("unconfigured")
        for _ in range(5):
            channel.post("t", "dev", "x")
        assert capsys.readouterr().err.count("no huddle channel") == 1

    def test_a_missing_token_for_one_speaker_skips_that_turn_only(self, capsys):
        channel = hc.SlackChannel("C123", tokens={"dev": "", "review": "xoxp-review"})
        assert channel.post("ts", "dev", "x") is False
        assert "no token for this speaker" in capsys.readouterr().err

    def test_a_slack_error_is_reported_and_swallowed(self, capsys, monkeypatch):
        """Exercises the real transport: an `ok: false` body must warn with
        Slack's own reason and return no result, not raise."""
        import io
        import json as _json

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(
            hc.urllib.request,
            "urlopen",
            lambda *a, **k: _Response(
                _json.dumps({"ok": False, "error": "not_in_channel"}).encode()
            ),
        )
        channel = hc.SlackChannel("C123", tokens=TOKENS)
        assert channel.post("ts", "dev", "x") is False
        assert "not_in_channel" in capsys.readouterr().err

    def test_a_transport_failure_is_reported_and_swallowed(self, capsys, monkeypatch):
        def _boom(*args, **kwargs):
            raise hc.urllib.error.URLError("dns is down")

        monkeypatch.setattr(hc.urllib.request, "urlopen", _boom)
        channel = hc.SlackChannel("C123", tokens=TOKENS)
        assert channel.post("ts", "dev", "x") is False
        assert "transport failure" in capsys.readouterr().err


class TestTranscript:
    REPLIES = {
        "ok": True,
        "messages": [
            {"username": "myGPT-scrummaster-agent", "text": "Review huddle on PR #1", "ts": "1.1"},
            {"username": "myGPT-developer-agent", "text": "my position", "ts": "1.2"},
            {"user": "U123", "text": "the review position", "ts": "1.3"},
            {"username": "myGPT-developer-agent", "text": "   ", "ts": "1.4"},
        ],
    }

    def test_every_turn_is_parsed_with_its_speaker(self):
        turns = hc.parse_replies(self.REPLIES)
        assert [(t.speaker, t.text) for t in turns] == [
            ("myGPT-scrummaster-agent", "Review huddle on PR #1"),
            ("myGPT-developer-agent", "my position"),
            ("U123", "the review position"),
        ]

    def test_empty_messages_are_dropped_rather_than_rendered_blank(self):
        assert all(turn.text for turn in hc.parse_replies(self.REPLIES))

    def test_a_malformed_payload_parses_to_nothing_instead_of_raising(self):
        assert hc.parse_replies({}) == []
        assert hc.parse_replies({"messages": None}) == []

    def test_the_rendered_transcript_names_each_speaker(self):
        rendered = hc.render_transcript(hc.parse_replies(self.REPLIES))
        assert "**myGPT-developer-agent:**" in rendered
        assert "my position" in rendered

    def test_an_empty_transcript_says_so_rather_than_rendering_nothing(self):
        assert "No huddle turns" in hc.render_transcript([])


class TestThreadLifecycle:
    def test_opening_a_thread_returns_its_id_and_names_the_pr_and_reason(self):
        channel = RecordingSlack("C123", tokens=TOKENS)
        thread = channel.open_thread(42, 7, "2nd unconverged cycle")
        assert thread == "1700000000.000100"
        _, token, payload = channel.calls[0]
        assert token == "xoxp-scrum"
        assert "#42" in payload["text"] and "#7" in payload["text"]
        assert "2nd unconverged cycle" in payload["text"]
        assert "thread_ts" not in payload

    def test_a_failed_open_returns_no_thread_id(self):
        channel = RecordingSlack(
            "C123", tokens=TOKENS, reply={"ok": False, "error": "invalid_auth"}
        )
        assert channel.open_thread(1, 2, "why") == ""

    def test_permalink_asks_for_the_thread_head(self):
        channel = RecordingSlack(
            "C123", tokens=TOKENS, reply={"ok": True, "permalink": "https://slack/x"}
        )
        assert channel.permalink("1.1") == "https://slack/x"
        assert channel.calls[0][2]["message_ts"] == "1.1"


class TestInterfaceIsTransportFree:
    def test_the_interface_speaks_only_in_builtins_and_this_modules_types(self):
        """A caller written against this must not need rewriting for another
        transport -- #3910 records that a forced move is a real possibility.
        So every parameter and return type is a builtin or `Turn`; nothing
        Slack-shaped (a client, a channel object, a raw payload) may appear."""
        import typing

        allowed = {"int", "str", "bool", "list[Turn]", "None"}
        for name in ("open_thread", "post", "read", "permalink"):
            hints = typing.get_type_hints(getattr(hc.Channel, name))
            rendered = {
                getattr(hint, "__name__", str(hint)).replace("typing.", "")
                for hint in hints.values()
            }
            rendered = {"list[Turn]" if r.startswith("list") else r for r in rendered}
            assert rendered <= allowed, f"{name} exposes {rendered - allowed}"

    def test_both_implementations_satisfy_the_interface(self):
        for implementation in (hc.SlackChannel("C1"), hc.NullChannel("why")):
            for operation in ("open_thread", "post", "read", "permalink"):
                assert callable(getattr(implementation, operation))
