#!/usr/bin/env python3
"""Merge-conflict routing decisions (#3801).

A PR going conflict-stale because the mainline moved under it is the most
routine event in this pipeline: on 2026-08-15 nine merges landed on the
release branch in one afternoon and four In Review PRs (#3791, #3795, #3797,
#3798) went CONFLICTING as a result. None of those conflicts contained a
question only the owner could answer -- and yet the conflict handler's only
move was to assign the owner and stop.

Owner rule (2026-08-15): *"merge conflicts shouldn't halt progress and
shouldn't be escalated to me unless there's truly a decision to be made only
I can make."*

This module holds the pure routing decision so it can be unit-tested without
GitHub:

    dispatch   hand the conflict to the developer agent, which merges
               `origin/<base>` into the PR branch (never rebases -- owner
               standing rule, developer-runbook §2), resolves with judgment,
               re-runs the gates and pushes. This is the DEFAULT.

    escalate   assign the owner. Reached in exactly two ways: the developer
               agent itself reported that resolution needs an owner-only
               decision (it posts CONFLICT_REQUIRES_OWNER_DECISION plus the
               question), or the automated rounds are exhausted -- the agent
               tried `max_rounds` times and the PR is still conflicted, which
               means the loop is not converging and a human must look.

    noop       nothing to do, or nothing to do *yet*: the PR merges cleanly,
               GitHub has not finished computing mergeability, the PR is not
               open, or a round dispatched inside the cooldown window is
               still in flight (the burst guard -- nine pushes in an
               afternoon must not produce nine rounds on the same PR).

The shell side (`scripts/agents/dispatch_conflict_resolution.sh`) does the
GitHub I/O; every branch above is decided here.

**Author gate.** This repository is public, so the comment thread is an
attacker-writable channel: without a gate, any account could open a comment
with the escalation token (forcing an owner assignment plus a Slack DM whose
question text they wrote), forge round markers to fake exhaustion, or post a
fresh forged marker every few minutes to hold a PR in permanent cooldown and
suppress resolution entirely. `conflict_owner_escalation.yml` gates its
commenter; this polling path must too, or the gate is bypassable by simply
waiting for the sweep. So every *control* comment -- round marker, owner
token, escalation marker -- counts only when its author is one of the
pipeline identities (developer-runbook §3b/#3600). Comments from anyone else
are ordinary conversation and are ignored for routing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from comment_tokens import is_command  # noqa: E402  (sibling lib, path set above)

# Posted by the dispatcher on every automated round. Both the HTML marker
# (what is written now) and the legacy human-readable phrase (written by
# review_accept_and_merge.sh before this issue) count as a round, so PRs
# that already had a round under the old code path are not re-counted from
# zero.
ROUND_MARKER = "<!-- conflict-resolution-round -->"
LEGACY_ROUND_MARKER = "Automated conflict-resolution round"

# Posted by the developer agent when -- and only when -- resolving the
# conflict requires a decision only the owner can make (e.g. two
# owner-accepted behaviors in semantic contradiction). The question itself
# follows the marker in the same comment.
OWNER_DECISION_MARKER = "CONFLICT_REQUIRES_OWNER_DECISION"

# Stamped on the escalation comment this module's caller posts. Without it the
# escalation branch has no memory: every later push to the release branch
# re-fires the handler, finds the same still-conflicted PR, and re-posts the
# escalation plus a fresh owner assignment -- exactly the "interrupted twice in
# an hour" behaviour #3801 exists to end.
ESCALATION_MARKER = "<!-- conflict-resolution-escalated -->"

DEFAULT_MAX_ROUNDS = 3
DEFAULT_COOLDOWN_MINUTES = 45

ACTION_DISPATCH = "dispatch"
ACTION_ESCALATE = "escalate"
ACTION_NOOP = "noop"


def _normalise_authors(authors: Iterable[str] | None) -> set[str] | None:
    """Lower-cased login set, or None meaning "no author gate".

    GitHub logins are case-insensitive, and the config file and the API can
    disagree on case; compare folded. An empty iterable is treated the same as
    `None` so a caller that passes an unpopulated list does not silently get a
    gate that rejects everything (which would freeze routing rather than fail
    loudly). Production callers must supply a non-empty set -- the CLI below
    refuses to run without one.
    """
    if authors is None:
        return None
    folded = {a.strip().lower() for a in authors if a and a.strip()}
    return folded or None


def comment_author(comment: dict[str, Any]) -> str:
    """The commenter's login, from either the flat or the REST-nested shape."""
    author = comment.get("author")
    if not author:
        user = comment.get("user")
        if isinstance(user, dict):
            author = user.get("login")
    return str(author or "").strip().lower()


def is_trusted_author(comment: dict[str, Any], trusted: set[str] | None) -> bool:
    """True if this comment may influence routing.

    `trusted is None` disables the gate (unit tests, and callers that have
    already filtered the thread themselves). Otherwise an unattributed comment
    is untrusted: a missing login is not a pass.
    """
    if trusted is None:
        return True
    return comment_author(comment) in trusted


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a GitHub ISO-8601 timestamp; return None if absent/unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def is_round_comment(body: str) -> bool:
    """True if `body` is an automated conflict-resolution round dispatch."""
    return ROUND_MARKER in body or LEGACY_ROUND_MARKER in body


def is_escalation_comment(body: str) -> bool:
    """True if `body` is an escalation this handler already posted."""
    return ESCALATION_MARKER in body


def is_owner_decision_comment(body: str) -> bool:
    """True if `body` *issues* the owner-decision token about a conflict.

    Matched as a command token, not a bare substring (V-011/#3790): the token
    must open a line, outside code fences and quoted text, on a comment
    carrying no informational marker. Prose that merely names it -- the
    escalation guidance an agent might write -- is a mention, never a
    request for the owner. The rule is the shared one in
    `comment_tokens.is_command`, so this token behaves exactly like every
    other comment token in the pipeline.
    """
    return is_command(body, OWNER_DECISION_MARKER)


def extract_owner_question(body: str) -> str:
    """The question the agent wants the owner to answer, or "".

    Everything after the marker, minus HTML comments and blank padding,
    collapsed to a single line so it can be carried in a Slack DM.
    """
    idx = body.find(OWNER_DECISION_MARKER)
    if idx < 0:
        return ""
    tail = body[idx + len(OWNER_DECISION_MARKER) :]
    tail = tail.lstrip(":").strip()
    tail = re.sub(r"<!--.*?-->", "", tail, flags=re.DOTALL)
    collapsed = " ".join(tail.split())
    return collapsed


def decide(
    mergeable: str,
    comments: list[dict[str, Any]] | None = None,
    *,
    pr_state: str = "OPEN",
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    now: str | None = None,
    trusted_authors: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Route one PR's conflict state.

    `mergeable` is the three-value enum this repo re-derives from REST
    (MERGEABLE / CONFLICTING / UNKNOWN -- see review_accept_and_merge.sh).
    `comments` are the PR's comments in chronological order, each with a
    `body`, optionally a `created_at`, and an `author` (or nested
    `user.login`).

    `trusted_authors` are the logins whose comments may steer routing -- the
    pipeline identities, i.e. the same set `conflict_owner_escalation.yml`
    gates its commenter on. Passing None disables the gate and is for tests
    only; production goes through the CLI, which requires the set.

    Returns {action, reason, rounds, question}.
    """
    comments = comments or []
    trusted = _normalise_authors(trusted_authors)

    if pr_state.upper() != "OPEN":
        return _result(ACTION_NOOP, f"PR is {pr_state.upper()}, not open")

    state = mergeable.upper()
    if state == "MERGEABLE":
        return _result(ACTION_NOOP, "PR merges cleanly")
    if state != "CONFLICTING":
        # UNKNOWN: GitHub is still computing the merge commit. The caller
        # polls; treating it as a conflict would dispatch rounds at random.
        return _result(ACTION_NOOP, "mergeability is UNKNOWN (GitHub still computing)")

    last_round_idx: int | None = None
    last_round_at: datetime | None = None
    last_owner_idx: int | None = None
    last_owner_body = ""
    last_escalation_idx: int | None = None
    rounds = 0

    for idx, comment in enumerate(comments):
        body = comment.get("body") or ""
        # Public repo: only the pipeline's own identities steer this. A
        # stranger's comment containing the token or a forged round marker is
        # conversation, not a control instruction.
        if not is_trusted_author(comment, trusted):
            continue
        if is_round_comment(body):
            rounds += 1
            last_round_idx = idx
            last_round_at = _parse_ts(comment.get("created_at"))
        if is_escalation_comment(body):
            last_escalation_idx = idx
        if is_owner_decision_comment(body):
            last_owner_idx = idx
            last_owner_body = body

    # 1. The agent asked for the owner. This is the ONLY non-exhaustion route
    #    to a human, and it is only honoured when it is the newest word on the
    #    conflict -- a question already answered by a later round is spent.
    if last_owner_idx is not None and (last_round_idx is None or last_owner_idx > last_round_idx):
        if last_escalation_idx is not None and last_escalation_idx > last_owner_idx:
            # Already handed to the owner and not yet answered. Re-posting it
            # on every subsequent push is the interruption this issue removes.
            return _result(
                ACTION_NOOP,
                "the owner-only question was already escalated and is awaiting an answer",
                rounds=rounds,
            )
        question = extract_owner_question(last_owner_body)
        return _result(
            ACTION_ESCALATE,
            "developer agent reported an owner-only decision",
            rounds=rounds,
            question=question,
        )

    # 2. Burst guard: a round dispatched moments ago is still working. Nine
    #    merges landing on the release branch in an afternoon fire this
    #    handler nine times per open PR; without this each one would kick the
    #    same PR again mid-resolution.
    if last_round_at is not None and cooldown_minutes > 0:
        current = _parse_ts(now) or datetime.now(UTC)
        if current - last_round_at < timedelta(minutes=cooldown_minutes):
            return _result(
                ACTION_NOOP,
                f"an automated round dispatched at {last_round_at.isoformat()} is still in "
                f"flight (cooldown {cooldown_minutes}m)",
                rounds=rounds,
            )

    # 3. Rounds exhausted: the agent is not converging, so a human looks --
    #    once. A later push to the release branch re-fires this handler on the
    #    same still-conflicted PR; without the marker check the owner would be
    #    re-assigned and re-notified every time.
    if rounds >= max_rounds:
        if last_escalation_idx is not None and (
            last_round_idx is None or last_escalation_idx > last_round_idx
        ):
            return _result(
                ACTION_NOOP,
                f"already escalated after {rounds} non-converging round(s); waiting on the owner",
                rounds=rounds,
            )
        return _result(
            ACTION_ESCALATE,
            f"{rounds} automated conflict-resolution round(s) already ran and the PR is still "
            f"conflicted (limit {max_rounds})",
            rounds=rounds,
        )

    # 4. The default, and the point of this module.
    return _result(
        ACTION_DISPATCH,
        f"mainline moved under the PR; handing round {rounds + 1} to the developer agent",
        rounds=rounds,
    )


def _result(action: str, reason: str, rounds: int = 0, question: str = "") -> dict[str, Any]:
    return {"action": action, "reason": reason, "rounds": rounds, "question": question}


def _cmd_decide(args: argparse.Namespace) -> int:
    payload = json.load(sys.stdin)
    # Fail closed. The author gate is only a gate if the production entry
    # point cannot run without it; a caller that forgets `trusted_authors`
    # must get an error, never a silently ungated routing decision.
    trusted = _normalise_authors(payload.get("trusted_authors"))
    if trusted is None:
        print(
            "conflict_resolution: refusing to route without `trusted_authors` "
            "(comment thread is attacker-writable on a public repo)",
            file=sys.stderr,
        )
        return 2
    result = decide(
        payload.get("mergeable", "UNKNOWN"),
        payload.get("comments") or [],
        pr_state=payload.get("state", "OPEN"),
        max_rounds=int(payload.get("max_rounds", args.max_rounds)),
        cooldown_minutes=int(payload.get("cooldown_minutes", args.cooldown_minutes)),
        now=payload.get("now"),
        trusted_authors=trusted,
    )
    print(json.dumps(result))
    return 0


def _cmd_question(_args: argparse.Namespace) -> int:
    print(extract_owner_question(sys.stdin.read()))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_decide = sub.add_parser("decide", help="route one PR (JSON on stdin -> JSON on stdout)")
    p_decide.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    p_decide.add_argument("--cooldown-minutes", type=int, default=DEFAULT_COOLDOWN_MINUTES)
    p_decide.set_defaults(func=_cmd_decide)

    p_question = sub.add_parser("question", help="extract the owner question from a comment body")
    p_question.set_defaults(func=_cmd_question)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
