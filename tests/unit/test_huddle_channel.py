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

    def test_identities_is_deliberately_not_part_of_the_interface(self):
        """It exists because Slack labels its own messages badly. A transport
        that labels them well would have to implement a method it has no use
        for, so callers ask for it with `getattr` instead."""
        assert not hasattr(hc.Channel, "identities")


class TestIdentitiesAreResolvedToRoles:
    """A user-token message comes back labelled with an opaque `U…` id.

    `parse_replies` reports whatever Slack said, which for the three agent
    accounts is three ids -- so a thread read back into the PR archive would
    name its speakers `**U09ABCDEF:**`, and the reason for posting under three
    identities (that a future session can tell the turns apart) would be lost
    at exactly the moment the record is supposed to outlive Slack.
    """

    def test_each_configured_token_is_asked_who_it_speaks_as(self):
        channel = RecordingSlack("C123", tokens=TOKENS, reply={"ok": True, "user_id": "U1"})
        channel.identities()
        assert [method for method, _t, _p in channel.calls] == ["auth.test"] * 3
        assert {token for _m, token, _p in channel.calls} == set(TOKENS.values())

    def test_an_unconfigured_speaker_is_skipped_rather_than_asked(self):
        channel = RecordingSlack("C123", tokens={"dev": "xoxp-dev"}, reply={"ok": True})
        channel.identities()
        assert [token for _m, token, _p in channel.calls] == ["xoxp-dev"]

    def test_a_refused_auth_test_drops_that_speaker_not_the_mapping(self):
        channel = RecordingSlack("C123", tokens=TOKENS, reply={"ok": False, "error": "invalid"})
        assert channel.identities() == {}

    def test_the_null_channel_maps_nothing(self):
        assert hc.NullChannel("why").identities() == {}

    def test_turns_are_relabelled_by_role_where_the_account_is_known(self):
        turns = [
            hc.Turn("U1", "a", account="U1"),
            hc.Turn("U2", "b", account="U2"),
            hc.Turn("U9", "c", account="U9"),
        ]
        labelled = hc.label_turns(turns, {"U1": "dev", "U2": "review"})
        assert [t.speaker for t in labelled] == ["dev", "review", "U9"]

    def test_the_account_is_what_is_matched_not_the_display_name(self):
        """Slack sometimes attaches a `username` and sometimes does not, and
        the id is the only field that answers "which of our three agents is
        this". Keying on the label would work on one shape and quietly fail
        on the other -- a monologue wearing three names, again."""
        parsed = hc.parse_replies(
            {"messages": [{"user": "U1", "username": "SSC Developer Agent", "text": "a point"}]}
        )
        assert hc.label_turns(parsed, {"U1": "dev"})[0].speaker == "dev"

    def test_a_message_with_no_account_still_parses_with_its_display_name(self):
        parsed = hc.parse_replies({"messages": [{"username": "a-bot", "text": "hello"}]})
        assert (parsed[0].speaker, parsed[0].account) == ("a-bot", "")

    def test_an_unknown_speaker_keeps_its_label_rather_than_being_dropped(self):
        """A human in the thread is the message most worth keeping, and this
        run holds no token for them."""
        kept = hc.label_turns([hc.Turn("U9", "the owner", account="U9")], {"U1": "dev"})
        assert (kept[0].speaker, kept[0].text) == ("U9", "the owner")

    def test_relabelling_preserves_the_text_and_timestamp(self):
        labelled = hc.label_turns([hc.Turn("U1", "my position", "1.2", "U1")], {"U1": "dev"})
        assert (labelled[0].text, labelled[0].ts) == ("my position", "1.2")

    def test_no_identities_is_a_no_op_rather_than_a_rebuild(self):
        turns = [hc.Turn("U1", "a", account="U1")]
        assert hc.label_turns(turns, {}) is turns


class TestTheWorkflowCLI:
    """`_main` is the only caller the workflow has, and it had no coverage.

    Its exit codes are a contract with a `set -e` shell step: a Slack failure
    must not take the huddle down with it (#3910), and a genuine usage error
    must not be swallowed into a silent no-op.
    """

    @pytest.fixture
    def unconfigured(self, monkeypatch):
        """No channel and no tokens: the degradation path, end to end."""
        monkeypatch.delenv(hc.CHANNEL_ENV, raising=False)
        for env in hc.SPEAKER_TOKEN_ENV.values():
            monkeypatch.delenv(env, raising=False)

    def test_a_failed_post_still_exits_zero(self, unconfigured, monkeypatch, capsys):
        """The turn is already safe in the PR transcript; failing the step
        here would abandon the remaining rounds and the decision over a chat
        integration being unavailable."""
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO("## Developer Position"))
        assert hc._main(["post", "1700000000.000100", "dev"]) == 0
        assert "no huddle channel" in capsys.readouterr().err

    def test_post_takes_the_text_from_argv_when_given(self, monkeypatch, capsys):
        sent = []
        monkeypatch.setattr(
            hc, "get_channel", lambda: type("C", (), {"post": lambda _s, *a: sent.append(a)})()
        )
        assert hc._main(["post", "ts-1", "review", "inline text"]) == 0
        assert sent == [("ts-1", "review", "inline text")]

    def test_open_prints_the_thread_id_for_the_step_output(self, monkeypatch, capsys):
        monkeypatch.setattr(
            hc,
            "get_channel",
            lambda: type("C", (), {"open_thread": lambda _s, *a: "1700.0001"})(),
        )
        assert hc._main(["open", "3933", "3911", "why"]) == 0
        assert capsys.readouterr().out.strip() == "1700.0001"

    def test_open_prints_an_empty_line_when_the_thread_could_not_open(self, unconfigured, capsys):
        """The empty thread id is the signal the session checks for."""
        assert hc._main(["open", "1", "2", "why"]) == 0
        assert capsys.readouterr().out.strip() == ""

    def test_permalink_prints_the_url(self, monkeypatch, capsys):
        monkeypatch.setattr(
            hc, "get_channel", lambda: type("C", (), {"permalink": lambda _s, _t: "https://s/x"})()
        )
        assert hc._main(["permalink", "ts-1"]) == 0
        assert capsys.readouterr().out.strip() == "https://s/x"

    def test_read_prints_the_rendered_transcript(self, monkeypatch, capsys):
        monkeypatch.setattr(
            hc,
            "get_channel",
            lambda: type("C", (), {"read": lambda _s, _t: [hc.Turn("dev", "my position")]})(),
        )
        assert hc._main(["read", "ts-1"]) == 0
        assert "my position" in capsys.readouterr().out

    def test_an_empty_thread_prints_nothing_at_all(self, unconfigured, capsys):
        """Not `render_transcript([])`'s placeholder. The caller is the shell
        step deciding whether the PR archive gets a "read back from Slack"
        section, and an empty stdout is how it learns there is none -- the
        placeholder would announce an empty thread on every huddle that ran
        with Slack unconfigured."""
        assert hc._main(["read", "ts-1"]) == 0
        assert capsys.readouterr().out.strip() == ""

    def test_read_labels_the_turns_by_role_when_the_channel_can(self, monkeypatch, capsys):
        channel = type(
            "C",
            (),
            {
                "read": lambda _s, _t: [hc.Turn("U1", "my position", account="U1")],
                "identities": lambda _s: {"U1": "dev"},
            },
        )()
        monkeypatch.setattr(hc, "get_channel", lambda: channel)
        assert hc._main(["read", "ts-1"]) == 0
        assert "**dev:**" in capsys.readouterr().out

    def test_read_still_works_for_a_channel_without_identities(self, monkeypatch, capsys):
        """`identities` is optional, so a transport that never grew one must
        still archive its thread rather than crash the step."""
        monkeypatch.setattr(
            hc,
            "get_channel",
            lambda: type("C", (), {"read": lambda _s, _t: [hc.Turn("someone", "a point")]})(),
        )
        assert hc._main(["read", "ts-1"]) == 0
        assert "**someone:**" in capsys.readouterr().out

    @pytest.mark.parametrize("argv", [[], ["sing"]])
    def test_a_usage_error_is_a_nonzero_exit(self, argv, unconfigured):
        """Distinct from a Slack failure: a mistyped command is a bug in the
        workflow, and a silent 0 would hide it behind a missing turn."""
        assert hc._main(argv) == 2


class TestTheWireFormat:
    """Every call must be form-encoded, because Slack only takes JSON on some.

    The adapter shipped sending `application/json` to all four methods.
    `chat.postMessage` accepts that, so threads opened and turns posted and
    the integration looked healthy; `conversations.replies` and
    `chat.getPermalink` answered `invalid_arguments` on every call, and since
    `read()` degrades to `[]` the only visible symptom was
    "No huddle turns were recorded" on the PR. Two of four operations had
    never worked.

    The existing tests could not catch it: they stub the transport, and a
    stub accepts any encoding you hand it. So these assert on the *request
    the adapter builds* rather than on what a fake would have returned --
    the one property a stubbed transport can still tell the truth about.
    """

    @staticmethod
    def _capture(monkeypatch):
        """Run the real `_call` path, keeping each outgoing Request."""
        import io
        import json as _json

        sent = []

        class _Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def _urlopen(request, *args, **kwargs):
            sent.append(request)
            return _Response(_json.dumps({"ok": True, "ts": "1.2", "messages": []}).encode())

        monkeypatch.setattr(hc.urllib.request, "urlopen", _urlopen)
        return sent

    def test_every_operation_sends_form_encoded_not_json(self, monkeypatch):
        import json as _json
        import urllib.parse

        sent = self._capture(monkeypatch)
        channel = hc.SlackChannel("C123", tokens=TOKENS)
        channel.open_thread(1, 2, "why")
        channel.post("1.2", "dev", "text")
        channel.read("1.2")
        channel.permalink("1.2")

        assert len(sent) == 4, "expected one request per operation"
        for request in sent:
            content_type = request.headers.get("Content-type", "")
            assert content_type.startswith("application/x-www-form-urlencoded"), (
                f"{request.full_url} was sent as {content_type!r}; Slack answers "
                "invalid_arguments to a JSON body on conversations.replies and "
                "chat.getPermalink"
            )
            body = request.data.decode("utf-8")
            assert "channel=C123" in urllib.parse.unquote(body)
            with pytest.raises(_json.JSONDecodeError):
                _json.loads(body)

    def test_the_read_and_permalink_payloads_carry_their_timestamp_keys(self, monkeypatch):
        """Form encoding flattens, so the key names are the whole contract.

        `conversations.replies` wants `ts`; `chat.getPermalink` wants
        `message_ts`. Getting either wrong returns `invalid_arguments` --
        the same symptom the encoding bug produced, from a different cause.
        """
        import urllib.parse

        sent = self._capture(monkeypatch)
        channel = hc.SlackChannel("C123", tokens=TOKENS)
        channel.read("1.2")
        channel.permalink("1.2")

        replies, permalink = (urllib.parse.parse_qs(r.data.decode()) for r in sent)
        assert replies["ts"] == ["1.2"] and "message_ts" not in replies
        assert permalink["message_ts"] == ["1.2"] and "ts" not in permalink
