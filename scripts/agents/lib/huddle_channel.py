#!/usr/bin/env python3
"""The huddle's conversation channel (#3910), used by the huddle session (#3911).

The #3687 huddle used PR comments as both the **bus** (what wakes the next
agent) and the **venue** (where the prose lands). That conflation is what made
the protocol fragile -- each leg was a separate comment-triggered workflow, so
the essay *was* the trigger, and two race guards had to be added after the fact
(#3736) -- and it is what buried three long structured essays in every PR
thread.

This module is the venue, separated out. The bus is now a single workflow run
(`huddle_session.yml`), so nothing here wakes anything: it only carries the
conversation.

Four operations, deliberately no more:

    open_thread(pr, issue, reason) -> thread id
    post(thread, speaker, text)
    read(thread) -> [Turn, ...]
    permalink(thread) -> url

**No Slack type appears in those signatures.** A thread is a string, a turn is
this module's own dataclass, and a speaker is one of three role names. Slack's
stance on automating user accounts is unfavourable enough that a forced move to
another transport is a real possibility (#3910 records the fallback), and a
caller written against `chat.postMessage` semantics would have to be rewritten
for it. `Channel` is the interface; `SlackChannel` is one implementation.

**Credentials degrade, never fail.** A missing token yields `NullChannel`,
whose operations warn once and no-op -- the same contract as
`notify_human_escalation` (#3695). A huddle whose Slack thread is unavailable
still runs, still decides, and still writes its transcript to the PR; it just
loses the live venue. Breaking the agent loop because a chat integration is
unconfigured would be a worse failure than the one it reports.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

SLACK_API = "https://slack.com/api"

#: The three speakers, and the environment variable holding each one's token.
#: Posting *as an agent* needs a user token with the `chat:write` user scope --
#: a bot token would put every turn under one identity, which defeats the point
#: of a huddle you can read back.
SPEAKER_TOKEN_ENV = {
    "dev": "SLACK_USER_TOKEN_DEV",
    "review": "SLACK_USER_TOKEN_REVIEW",
    "scrum": "SLACK_USER_TOKEN_SCRUM",
}

#: Channel id is configuration, not a literal (#3910): the workflow passes
#: `vars.SLACK_HUDDLE_CHANNEL` through this variable.
CHANNEL_ENV = "SLACK_HUDDLE_CHANNEL"


@dataclass(frozen=True)
class Turn:
    """One message in a huddle thread, transport-independent."""

    speaker: str
    text: str
    ts: str = ""


class Channel(Protocol):
    """What a huddle venue must provide. Implement this, not Slack."""

    def open_thread(self, pr: int, issue: int, reason: str) -> str: ...

    def post(self, thread: str, speaker: str, text: str) -> bool: ...

    def read(self, thread: str) -> list[Turn]: ...

    def permalink(self, thread: str) -> str: ...


def _warn(message: str) -> None:
    print(f"[huddle-channel] {message}", file=sys.stderr)


class NullChannel:
    """The graceful-degradation path: warn once, no-op, never raise.

    Its arguments are unused by design -- the signatures exist to satisfy
    `Channel`, not to be read -- hence the ARG002 waivers.

    Returned whenever the channel is unconfigured or a token is missing. The
    empty thread id is the signal to callers that there is no live venue --
    the session checks for it and falls back to the PR transcript alone.
    """

    def __init__(self, why: str) -> None:
        self._why = why
        self._warned = False

    def _once(self) -> None:
        if not self._warned:
            _warn(f"no huddle channel ({self._why}) -- the huddle runs without a live thread")
            self._warned = True

    def open_thread(self, pr: int, issue: int, reason: str) -> str:  # noqa: ARG002
        self._once()
        return ""

    def post(self, thread: str, speaker: str, text: str) -> bool:  # noqa: ARG002
        self._once()
        return False

    def read(self, thread: str) -> list[Turn]:  # noqa: ARG002
        self._once()
        return []

    def permalink(self, thread: str) -> str:  # noqa: ARG002
        self._once()
        return ""


class SlackChannel:
    """Slack implementation of `Channel`.

    Speaker tokens are read at call time rather than at construction so a
    single missing one degrades that turn instead of the whole session.
    """

    def __init__(self, channel: str, tokens: dict[str, str] | None = None) -> None:
        self.channel = channel
        self._tokens = tokens if tokens is not None else {}

    # -- transport ---------------------------------------------------------
    def _token(self, speaker: str) -> str:
        if speaker not in SPEAKER_TOKEN_ENV:
            raise ValueError(
                f"unknown speaker {speaker!r}; expected one of {sorted(SPEAKER_TOKEN_ENV)}"
            )
        if speaker in self._tokens:
            return self._tokens[speaker]
        return os.environ.get(SPEAKER_TOKEN_ENV[speaker], "")

    def _call(self, method: str, token: str, payload: dict) -> dict:
        """One Slack API call. Any transport failure is a warned no-op."""
        if not token:
            _warn(f"{method}: no token for this speaker -- skipping")
            return {}
        request = urllib.request.Request(
            f"{SLACK_API}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, TimeoutError) as exc:
            _warn(f"{method}: transport failure ({exc}) -- continuing without Slack")
            return {}
        if not body.get("ok"):
            _warn(f"{method}: Slack returned {body.get('error', 'an unknown error')}")
            return {}
        return body

    # -- the four operations ----------------------------------------------
    def open_thread(self, pr: int, issue: int, reason: str) -> str:
        """Post the thread head and return its id ("" if it could not open)."""
        text = (
            f":speech_balloon: *Review huddle* on PR #{pr} (issue #{issue})\n"
            f"*Why:* {reason}\n"
            "_Turns are posted by each agent under its own identity; the decision "
            "and a link back to this thread land on the PR._"
        )
        body = self._call(
            "chat.postMessage", self._token("scrum"), {"channel": self.channel, "text": text}
        )
        return str(body.get("ts", ""))

    def post(self, thread: str, speaker: str, text: str) -> bool:
        if not thread:
            return False
        body = self._call(
            "chat.postMessage",
            self._token(speaker),
            {"channel": self.channel, "thread_ts": thread, "text": text},
        )
        return bool(body)

    def read(self, thread: str) -> list[Turn]:
        if not thread:
            return []
        body = self._call(
            "conversations.replies",
            self._token("scrum"),
            {"channel": self.channel, "ts": thread, "limit": 200},
        )
        return parse_replies(body)

    def permalink(self, thread: str) -> str:
        if not thread:
            return ""
        body = self._call(
            "chat.getPermalink",
            self._token("scrum"),
            {"channel": self.channel, "message_ts": thread},
        )
        return str(body.get("permalink", ""))


def parse_replies(body: dict) -> list[Turn]:
    """Slack's `conversations.replies` payload -> transport-free turns.

    Split out from `SlackChannel.read` so the transcript parsing is testable
    without a transport, and so a second implementation can reuse the shape.
    The speaker is the message's `username`/`user` as Slack reports it; the
    session maps it back to a role when it renders the transcript.
    """
    turns: list[Turn] = []
    for message in body.get("messages", []) or []:
        text = str(message.get("text", "")).strip()
        if not text:
            continue
        speaker = str(message.get("username") or message.get("user") or "unknown")
        turns.append(Turn(speaker=speaker, text=text, ts=str(message.get("ts", ""))))
    return turns


def render_transcript(turns: list[Turn]) -> str:
    """The transcript as it lands on the PR.

    The record has to survive Slack: retention is a setting somebody else
    controls, and a decision whose reasoning is unreachable to a future session
    is precisely the failure `agents/LEDGER.md` exists to prevent.
    """
    if not turns:
        return "_No huddle turns were recorded._"
    return "\n\n".join(f"**{turn.speaker}:**\n\n{turn.text}" for turn in turns)


def get_channel(channel: str = "", tokens: dict[str, str] | None = None) -> Channel:
    """The channel this run should use -- `NullChannel` if it cannot be had."""
    channel = channel or os.environ.get(CHANNEL_ENV, "")
    if not channel:
        return NullChannel(f"{CHANNEL_ENV} is not set")
    have = (
        tokens
        if tokens is not None
        else {speaker: os.environ.get(env, "") for speaker, env in SPEAKER_TOKEN_ENV.items()}
    )
    if not any(have.values()):
        return NullChannel("no speaker tokens are configured")
    return SlackChannel(channel, tokens=tokens)


def _main(argv: list[str]) -> int:
    """CLI for the workflow's shell steps: open | post | read | permalink.

    Prints the thread id, the transcript, or the permalink on stdout, and
    nothing else -- callers capture it into a step output.
    """
    if not argv:
        print("usage: huddle_channel.py {open|post|read|permalink} [...]", file=sys.stderr)
        return 2

    command, rest = argv[0], argv[1:]
    channel = get_channel()

    if command == "open":
        pr, issue, reason = int(rest[0]), int(rest[1]), (rest[2] if len(rest) > 2 else "")
        print(channel.open_thread(pr, issue, reason))
        return 0
    if command == "post":
        thread, speaker = rest[0], rest[1]
        text = rest[2] if len(rest) > 2 else sys.stdin.read()
        return 0 if channel.post(thread, speaker, text) else 0
    if command == "read":
        print(render_transcript(channel.read(rest[0])))
        return 0
    if command == "permalink":
        print(channel.permalink(rest[0]))
        return 0

    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
