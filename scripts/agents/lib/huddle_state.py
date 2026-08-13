#!/usr/bin/env python3
"""Huddle state derived from a PR's comment thread (#3687 protocol, #3736 race).

The huddle protocol is carried entirely by marker comments on the PR
(`HUDDLE_TRIGGERED` -> `HUDDLE_DEV_POSITION` -> `HUDDLE_MEDIATION_REQUESTED`
-> `HUDDLE_DECISION:`), each one triggering the next workflow. Nothing
recorded *whether a huddle was already in flight*, so two things raced
(#3736):

- **Double trigger.** `review_agent_auto_review.yml` can execute the same
  REQUEST_CHANGES verdict twice -- once from the `pull_request_review` event,
  once from the `nyxgpt-structured-review` comment fallback. Its dedupe
  looked only for the return-to-developer and escalation footprints, never
  for `HUDDLE_TRIGGERED`, so a huddle round posted two triggers and ran two
  developer-position and two mediation runs (PR #3728, 2026-08-11).
- **Escalation beating the huddle.** The 3-cycle breaker counted cycles with
  no idea a huddle had already resolved the round, so a `proceed` decision
  was followed minutes later by an owner escalation of the very change
  nobody disputed (PR #3733, 2026-08-12).

Both are fixed by making the thread readable as state. Everything here is
pure -- comment dicts in, decisions out -- so the shapes of both incidents
are unit-testable without GitHub (`tests/unit/test_huddle_state.py`); the
`gh` half lives in the workflows and `scripts/agents/review_ensure_handoff.sh`.

Round scoping: a *review round* starts at the newest
`nyxgpt-structured-review` comment (one is persisted per review, dispatched
or not). Markers at or after it belong to the round being decided; anything
older is a previous round's history and must not gate this one.

CLI:
    huddle_state.py status                     # {"comments": [...]} on stdin
    huddle_state.py guard <marker> <id>        # is <id> the round's first?
    huddle_state.py escalation-recorded [since]  # already escalated?
    huddle_state.py decision <<< "body text"   # parse one decision comment
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

#: Machine-readable review payload `claude-code-review.yml` persists after
#: every review. It both delimits a round and embeds the review's free text,
#: so it is skipped when scanning for markers -- a review that *discusses*
#: the huddle machinery (as reviews of this very issue do) would otherwise
#: read as a huddle trigger.
STRUCTURED_REVIEW_MARKER = "nyxgpt-structured-review"

HUDDLE_TRIGGER_MARKER = "HUDDLE_TRIGGERED"
HUDDLE_POSITION_MARKER = "HUDDLE_DEV_POSITION"
HUDDLE_MEDIATION_MARKER = "HUDDLE_MEDIATION_REQUESTED"
HUDDLE_DECISION_MARKER = "HUDDLE_DECISION:"
#: Written by `huddle_decision_dispatch.yml` once it has executed a
#: decision's "what happens next" (dev reassigned + retry posted), so the
#: dispatch is idempotent against duplicate decision comments.
HUDDLE_DISPATCH_MARKER = "HUDDLE_DECISION_DISPATCHED"

#: Headline text of the two escalation comments the review path posts
#: (`review_agent_auto_review.yml`, `review_ensure_handoff.sh`), used to keep
#: a second run of the same round from escalating again (#3736). Deliberately
#: the headlines and not the looser phrases `review_handoff.HANDOFF_MARKERS`
#: uses: "spec ambiguity" is ordinary prose in a developer position or an
#: owner comment, and mistaking one of those for an escalation would suppress
#: a real one -- a stall, which is the failure mode this issue is about.
ESCALATION_MARKERS = (
    "Escalated after 3 review cycles",
    "Escalated immediately",
)

#: Decisions that hand the work back to the developer agent. `escalate` is
#: the fourth decision and is terminal -- the mediation run performs the
#: escalation itself, so the review path must not add another.
RESUMABLE_DECISIONS = ("proceed", "change-approach", "descope")
DECISION_VALUES = RESUMABLE_DECISIONS + ("escalate",)

_DECISION_RE = re.compile(rf"{HUDDLE_DECISION_MARKER}\s*([^\n\r]*)")


def _body(comment: dict[str, Any]) -> str:
    return str(comment.get("body") or "")


def _created(comment: dict[str, Any]) -> str:
    return str(comment.get("created_at") or "")


def _sort_key(comment: dict[str, Any]) -> tuple[str, int]:
    """Chronological, with the comment id breaking same-second ties.

    Two runs racing the same round can post within the same second; ids are
    monotonic, so this still yields one deterministic winner.
    """
    try:
        comment_id = int(comment.get("id") or 0)
    except (TypeError, ValueError):
        comment_id = 0
    return (_created(comment), comment_id)


def parse_decision(body: str) -> str:
    """The decision carried by a `HUDDLE_DECISION:` line, normalized.

    Accepts the prose the mediation prompt produces ("proceed as-is",
    "change approach", "escalate to owner", bolded or backticked). Returns
    "" for anything unrecognized -- including the unfilled
    `[proceed|change-approach|descope|escalate]` template, which a mediation
    run has been seen to echo. An unparsable decision must read as "no
    decision yet" rather than silently picking one.
    """
    matches = _DECISION_RE.findall(body or "")
    for raw in reversed(matches):
        text = raw.strip().strip("*`_ ").lower().replace("[", "").replace("]", "")
        if "|" in text:  # unfilled template
            continue
        text = re.sub(r"[^a-z\- ]+", " ", text).strip()
        if text.startswith("change"):
            return "change-approach"
        for value in DECISION_VALUES:
            if text.startswith(value):
                return value
    return ""


def round_start(comments: list[dict[str, Any]]) -> str:
    """`created_at` of the newest structured-review comment ("" if none).

    With no structured comment (a PR reviewed before that step existed) the
    whole thread is in scope, which is the conservative reading: a huddle
    anywhere on the PR still suppresses a duplicate.
    """
    starts = [_created(c) for c in comments if STRUCTURED_REVIEW_MARKER in _body(c)]
    return max(starts) if starts else ""


def marker_comments(
    comments: list[dict[str, Any]],
    marker: str,
    since: str = "",
) -> list[dict[str, Any]]:
    """Non-structured comments containing `marker`, at/after `since`, oldest first."""
    matched = [
        c
        for c in comments
        if marker in _body(c)
        and STRUCTURED_REVIEW_MARKER not in _body(c)
        and (not since or _created(c) >= since)
    ]
    return sorted(matched, key=_sort_key)


def is_primary_marker_comment(
    comments: list[dict[str, Any]],
    marker: str,
    comment_id: int | str,
    since: str | None = None,
) -> bool:
    """True when `comment_id` is the round's *first* comment carrying `marker`.

    The dedupe of last resort: two racing runs can both pass a
    check-before-posting guard and both post. Whichever landed first owns
    the round, and the workflows that consume the marker
    (`developer_huddle_position.yml`, `scrummaster_huddle_mediation.yml`,
    `huddle_decision_dispatch.yml`) run only for that one -- so a double
    trigger still produces a single huddle.

    An id that carries no such marker (or is unknown) is not primary: a
    consumer that cannot identify itself in the thread must stand down
    rather than assume it won.
    """
    scope = round_start(comments) if since is None else since
    matched = marker_comments(comments, marker, scope)
    if not matched:
        return False
    try:
        wanted = int(comment_id)
    except (TypeError, ValueError):
        return False
    return _sort_key(matched[0])[1] == wanted


def latest_decision(
    comments: list[dict[str, Any]],
    since: str = "",
) -> dict[str, Any] | None:
    """The newest parsable huddle decision at/after `since`, or None.

    Returns `{"decision": ..., "created_at": ..., "id": ...}`.
    """
    for comment in reversed(marker_comments(comments, HUDDLE_DECISION_MARKER, since)):
        decision = parse_decision(_body(comment))
        if decision:
            return {
                "decision": decision,
                "created_at": _created(comment),
                "id": _sort_key(comment)[1],
            }
    return None


def escalation_recorded(comments: list[dict[str, Any]], since: str = "") -> bool:
    """True if an escalation comment was already posted at/after `since`.

    Guards the escalation step against firing twice for one event: the two
    trigger paths of `review_agent_auto_review.yml` raced and each posted
    its own escalation pair on PR #3733.
    """
    return any(marker_comments(comments, marker, since) for marker in ESCALATION_MARKERS)


def huddle_status(comments: list[dict[str, Any]]) -> dict[str, Any]:
    """State of the huddle for the current review round.

    Keys:
      round_start     boundary the state was computed against
      triggered       a HUDDLE_TRIGGERED landed this round
      trigger_count   how many did (>1 means the trigger raced)
      trigger_at      when the first one landed
      decision        proceed|change-approach|descope|escalate|"" (this round)
      decision_at     when it landed
      pending         triggered, no decision yet -- nothing else may act
      resumable       decision hands the work back to the developer agent
      escalated       the mediation escalated; the review path must not re-escalate
      dispatched      the decision's "what happens next" has been executed
    """
    start = round_start(comments)
    triggers = marker_comments(comments, HUDDLE_TRIGGER_MARKER, start)
    trigger_at = _created(triggers[0]) if triggers else ""

    decision_entry = latest_decision(comments, trigger_at) if triggers else None
    decision = str(decision_entry["decision"]) if decision_entry else ""
    decision_at = str(decision_entry["created_at"]) if decision_entry else ""

    dispatched = bool(
        decision_entry and marker_comments(comments, HUDDLE_DISPATCH_MARKER, decision_at)
    )

    return {
        "round_start": start,
        "triggered": bool(triggers),
        "trigger_count": len(triggers),
        "trigger_at": trigger_at,
        "decision": decision,
        "decision_at": decision_at,
        "pending": bool(triggers) and not decision,
        "resumable": decision in RESUMABLE_DECISIONS,
        "escalated": decision == "escalate",
        "dispatched": dispatched,
    }


def _shell_lines(state: dict[str, Any]) -> str:
    """Render state as `key=value` lines for `eval` in a bash caller.

    Booleans become true/false so the lines drop straight into a step's
    `$GITHUB_OUTPUT`; values are enum-ish strings and ints by construction,
    so no quoting is needed.
    """
    rendered = []
    for key, value in state.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        rendered.append(f"{key}={value}")
    return "\n".join(rendered)


def _main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: huddle_state.py <status|guard|escalation-recorded|decision>",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]
    if cmd == "status":
        payload = json.loads(sys.stdin.read())
        print(_shell_lines(huddle_status(payload.get("comments") or [])))
        return 0
    if cmd == "escalation-recorded":
        payload = json.loads(sys.stdin.read())
        comments = payload.get("comments") or []
        since = argv[1] if len(argv) > 1 else round_start(comments)
        print("true" if escalation_recorded(comments, since) else "false")
        return 0
    if cmd == "guard":
        if len(argv) != 3:
            print("usage: huddle_state.py guard <marker> <comment_id>", file=sys.stderr)
            return 2
        payload = json.loads(sys.stdin.read())
        primary = is_primary_marker_comment(payload.get("comments") or [], argv[1], argv[2])
        print("true" if primary else "false")
        return 0
    if cmd == "decision":
        print(parse_decision(sys.stdin.read()))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
