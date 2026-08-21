#!/usr/bin/env python3
"""Prove the huddle channel really works, by using it (#3910).

`huddle_channel.py` is written to degrade rather than fail: a missing token,
a refused call or a dead network all warn to stderr and return an empty
result, because breaking the agent loop over a chat integration would be a
worse failure than the one it reports. That contract is correct, and it means
**an unconfigured or broken integration looks exactly like a quiet one**.

#3933 shipped in precisely that state. `_call` sent JSON to every method;
Slack accepts JSON on `chat.postMessage` but answers `invalid_arguments` to
`conversations.replies` and `chat.getPermalink`, so two of the four operations
had never worked. The 22 unit tests stayed green throughout -- they stub the
transport, and a stub accepts any encoding it is handed. Nothing short of
calling the real API could have found it, which is what #3910's
executed-evidence criterion (and D-006) asks for.

So this script inverts the degradation contract on purpose: every warned
no-op is a hard failure here. It is the one place where "Slack is
unreachable" must be loud.

What it asserts, in order:

1. the channel resolves to a real `SlackChannel`, not `NullChannel`
   (i.e. the channel id and at least one token actually reached the runner);
2. `open_thread` returns a thread id;
3. all three speakers can `post` into it -- one turn each;
4. `read` returns the head plus three turns, and the three replies come back
   under **three distinct** Slack accounts. That last part is the real
   assertion: speaker->token selection is what makes a huddle readable, and
   if every turn used one token the transcript would still look plausible
   while being a monologue;
5. `permalink` returns a URL.

Messages are left in the channel. `chat:delete` is deliberately not among the
granted scopes -- an agent that can delete its own history is a worse idea
than a channel with occasional smoke threads in it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

SPEAKERS = ("scrum", "dev", "review")


def _load_adapter():
    """Import `huddle_channel` by path -- `scripts/agents/lib` is not a package."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "agents" / "lib" / "huddle_channel.py"
    spec = importlib.util.spec_from_file_location("huddle_channel", path)
    if spec is None or spec.loader is None:  # pragma: no cover - import plumbing
        sys.exit(f"could not load the adapter from {path}")
    module = importlib.util.module_from_spec(spec)
    # `@dataclass` resolves its class's module through `sys.modules`, and a
    # module loaded by spec alone is not there yet -- registering after
    # `exec_module` is too late and raises on 3.12+. Same recipe, and same
    # reason, as `tests/unit/test_huddle_channel.py`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    hc = _load_adapter()
    run = os.environ.get("GITHUB_RUN_ID", "local")

    channel = hc.get_channel()
    if isinstance(channel, hc.NullChannel):
        sys.exit(
            "FAIL: the channel degraded to NullChannel -- SLACK_HUDDLE_CHANNEL or the "
            "SLACK_USER_TOKEN_* secrets did not reach this job. That is the state this "
            "smoke exists to distinguish from a working integration."
        )

    thread = channel.open_thread(0, 3910, f"automated huddle-channel smoke (run {run})")
    if not thread:
        sys.exit(
            "FAIL: open_thread returned no thread id -- see the warning above for Slack's reason"
        )
    print(f"open_thread   ok  ts={thread}")

    for speaker in SPEAKERS:
        text = f":white_check_mark: `{speaker}` turn from run {run}. Smoke test -- ignore."
        if not channel.post(thread, speaker, text):
            sys.exit(f"FAIL: post as {speaker!r} was refused -- its token or scope is wrong")
        print(f"post          ok  speaker={speaker}")

    turns = channel.read(thread)
    if not turns:
        sys.exit("FAIL: read returned nothing -- the thread has messages, so this is a read defect")
    if len(turns) != len(SPEAKERS) + 1:
        sys.exit(
            f"FAIL: read returned {len(turns)} turns, expected {len(SPEAKERS) + 1} (head + one each)"
        )

    replies = {turn.speaker for turn in turns[1:]}
    if len(replies) != len(SPEAKERS):
        sys.exit(
            f"FAIL: the three turns came back under {len(replies)} account(s): {sorted(replies)}. "
            "Speaker->token selection collapsed -- the huddle would be a monologue wearing three names."
        )
    print(
        f"read          ok  {len(turns)} turns, {len(replies)} distinct accounts: {sorted(replies)}"
    )

    link = channel.permalink(thread)
    if not link.startswith("https://"):
        sys.exit(f"FAIL: permalink returned {link!r} -- huddle_session.yml posts this onto the PR")
    print(f"permalink     ok  {link}")

    print("\nPASS: all four operations exercised against the live Slack API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
