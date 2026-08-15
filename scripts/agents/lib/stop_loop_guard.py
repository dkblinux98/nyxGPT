#!/usr/bin/env python3
"""Loop guard for stop-without-progress cycles on an issue (#3790).

The 2026-08-15 incident had a self-feeding shape: a run stops at the
"is the issue still In Progress?" gate, posts its stop message, and something
about that message starts another run, which stops at the same gate. #3790's
anchored token matching (`comment_tokens.py`) closes the specific feedback
path; this module is the backstop for *any* future one.

The rule: N stop-without-progress cycles on the same issue inside M minutes
halts further automatic retries and posts ONE escalation comment instead of
an (N+1)th stop message. A repo-owner comment after the escalation clears the
halt immediately, exactly like the retry budget's owner reset (#3689); agent
comments never reset it, or a chatty loop would clear its own guard. Absent
that, the halt lapses with the window itself -- every comment older than M
minutes drops out of the count, escalation marker included. That is
deliberate: the guard bounds spend to ~N cycles per window, it is not a
lockout that survives until a human arrives.

Comments are counted through markers, not prose:

  * ``<!-- nyxgpt-dev-stop-cycle -->``  -- one stop-without-progress cycle
  * ``<!-- nyxgpt-dev-stop-halted -->`` -- the single escalation comment

Pure functions only: developer_auto_implement.yml gathers the comment thread
with `gh api ... --paginate` and pipes it in as JSON, so the decision math is
unit-testable without mocking the GitHub API (same shape as retry_budget.py).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

#: Stamped on every stop-without-progress comment.
STOP_CYCLE_MARKER = "<!-- nyxgpt-dev-stop-cycle -->"

#: Stamped on the one escalation comment that replaces the Nth stop message.
HALT_MARKER = "<!-- nyxgpt-dev-stop-halted -->"

#: Cycles (this one included) that trip the guard.
MAX_CYCLES = 3

#: Window the cycles have to land in, in minutes.
WINDOW_MINUTES = 30


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_owner(comment: dict[str, Any]) -> bool:
    return str(comment.get("author_association") or "").upper() == "OWNER"


def evaluate(
    comments: list[dict[str, Any]],
    now: datetime,
    *,
    max_cycles: int = MAX_CYCLES,
    window_minutes: int = WINDOW_MINUTES,
) -> dict[str, Any]:
    """Decide what a stop-without-progress cycle should post right now.

    `comments` is the issue's comment thread in chronological order, each
    entry carrying at least ``body``, ``created_at`` and
    ``author_association``. Returns the action to take:

      * ``stop-comment`` -- post the normal stop message (cycle < N)
      * ``escalate``     -- post the single halt escalation instead
      * ``silent``       -- already halted; post nothing, say nothing twice
    """
    window_start = now - timedelta(minutes=max(0, window_minutes))
    cycles = 0
    halted = False
    for comment in comments or []:
        created = _parse_timestamp(comment.get("created_at"))
        if created is None or created < window_start:
            continue
        body = comment.get("body") or ""
        if HALT_MARKER in body:
            halted = True
            continue
        if STOP_CYCLE_MARKER in body:
            cycles += 1
            continue
        if _is_owner(comment):
            # Owner intervention resets the guard: whatever they did, this
            # issue gets a clean budget (#3689's convention).
            cycles = 0
            halted = False

    cycle_number = cycles + 1
    if halted:
        action = "silent"
    elif cycle_number >= max_cycles:
        action = "escalate"
    else:
        action = "stop-comment"

    return {
        "action": action,
        "cycle_number": cycle_number,
        "prior_cycles": cycles,
        "halted": halted,
        "max_cycles": max_cycles,
        "window_minutes": window_minutes,
    }


def gate(
    comments: list[dict[str, Any]],
    now: datetime,
    *,
    author_is_owner: bool = False,
    max_cycles: int = MAX_CYCLES,
    window_minutes: int = WINDOW_MINUTES,
) -> dict[str, Any]:
    """Whether an automatic retry trigger may start a run on this issue.

    Once the guard has escalated, agent-authored retry triggers are ignored
    until the owner steps in; the owner's own trigger always proceeds (the
    halt is a spend guard, not a lockout).
    """
    state = evaluate(
        comments,
        now,
        max_cycles=max_cycles,
        window_minutes=window_minutes,
    )
    if state["halted"] and not author_is_owner:
        return {
            "proceed": False,
            "reason": (
                f"stop-loop guard halted this issue "
                f"({state['max_cycles']} stop-without-progress cycles within "
                f"{state['window_minutes']} minutes); only the repo owner can resume it"
            ),
            **{k: v for k, v in state.items() if k != "action"},
        }
    return {
        "proceed": True,
        "reason": "no active stop-loop halt",
        **{k: v for k, v in state.items() if k != "action"},
    }


def _now(value: str | None) -> datetime:
    parsed = _parse_timestamp(value) if value else None
    return parsed or datetime.now(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("evaluate", "gate"))
    parser.add_argument("--now", help="ISO-8601 timestamp to evaluate against (default: now)")
    parser.add_argument(
        "--author-association",
        default="",
        help="author_association of the comment that triggered the run (gate mode)",
    )
    parser.add_argument("--max-cycles", type=int, default=MAX_CYCLES)
    parser.add_argument("--window-minutes", type=int, default=WINDOW_MINUTES)
    args = parser.parse_args(argv)

    try:
        comments = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        comments = []
    if not isinstance(comments, list):
        comments = []

    now = _now(args.now)
    if args.mode == "gate":
        result = gate(
            comments,
            now,
            author_is_owner=args.author_association.upper() == "OWNER",
            max_cycles=args.max_cycles,
            window_minutes=args.window_minutes,
        )
    else:
        result = evaluate(
            comments,
            now,
            max_cycles=args.max_cycles,
            window_minutes=args.window_minutes,
        )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
