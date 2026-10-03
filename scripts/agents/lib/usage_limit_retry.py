"""The usage-limit retry queue, without a label (#4134).

`usage-limit-retry` was an agent-CREATED label, and agents do not create
labels (CLAUDE.md §Tooling -- "If a label/milestone/field option is needed,
ASK the user first"). It was created by `gh label create` in two workflows, it
came back every time the owner deleted it, and it broke the one-label rule on
every issue it touched -- a breakage that had to be worked around by excluding
it from `real_label_names` (`WORKFLOW_CONTROL_LABELS_JSON`, retired with this
module). Even with the workaround it deadlocked PR submission once (#3360).

It was only ever a queue: "this issue/PR is waiting for the Claude usage
window to reset, re-fire it after T". The queue now lives where #3694 already
puts cross-run state -- marker comments on the release tracking issue, read
back from the live thread on every poll, with no hidden counter to drift and
no label to recreate. Deliberately NOT `gh api search/issues`: that endpoint's
deterministic failure is what caused the #3694 incident, and the detector for
a degraded pipeline must not be takeable out by the same class of fault.

Two markers, both on the release tracking issue:

    <!-- usage-limit-retry: target=4134 kind=issue after=1760000000 -->
    <!-- usage-limit-retry-done: target=4134 -->

The LAST marker for a target decides: a schedule marker means pending, a done
marker means handled. `done` is posted when the retry actually fires, when the
8-attempt budget is given up on, and when a later run succeeds while a stale
schedule is still outstanding -- the three cases the old code spent a
`--remove-label` on.

The per-issue `<!-- usage-limit-retry-after: T -->` comment is unchanged and
stays on the issue: it is what the human reads, and counting those comments is
still how the attempt budget is derived. This module is only about *finding*
the targets, which is the single job the label was doing.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from typing import Any

SCHEDULE_PREFIX = "<!-- usage-limit-retry: "
DONE_PREFIX = "<!-- usage-limit-retry-done: "

_SCHEDULE_RE = re.compile(
    r"<!--\s*usage-limit-retry:\s*target=(?P<target>\d+)\s+"
    r"kind=(?P<kind>issue|pr)\s+after=(?P<after>\d+)\s*-->"
)
_DONE_RE = re.compile(r"<!--\s*usage-limit-retry-done:\s*target=(?P<target>\d+)\s*-->")

KINDS = ("issue", "pr")


def schedule_marker(target: int, kind: str, after: int) -> str:
    """The marker that enqueues `target` for a retry at epoch `after`."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
    return f"{SCHEDULE_PREFIX}target={int(target)} kind={kind} after={int(after)} -->"


def done_marker(target: int) -> str:
    """The marker that retires `target` from the queue."""
    return f"{DONE_PREFIX}target={int(target)} -->"


def queue(comment_bodies: Iterable[str] | None) -> list[dict[str, Any]]:
    """Every target whose LAST marker is a schedule, in target order.

    Comments are read in the order given (chronological, as GitHub returns
    them). A target with a later `done` marker is absent from the result; a
    target rescheduled after a `done` is present again with the new deadline,
    which is exactly what a second usage-limit hit on the same issue means.
    """
    latest: dict[int, dict[str, Any] | None] = {}
    for body in comment_bodies or []:
        text = body or ""
        # Both marker kinds are collected with their positions and replayed in
        # document order, so a comment carrying a schedule and a later `done`
        # (or a workflow that concatenated two markers) still resolves to the
        # last one written. The two patterns are matched separately rather
        # than as one alternation: they share the `target` group name, which
        # a combined pattern cannot hold.
        events: list[tuple[int, dict[str, Any] | None]] = []
        for match in _SCHEDULE_RE.finditer(text):
            events.append(
                (
                    match.start(),
                    {
                        "target": int(match.group("target")),
                        "kind": match.group("kind"),
                        "after": int(match.group("after")),
                    },
                )
            )
        for match in _DONE_RE.finditer(text):
            events.append((match.start(), {"target": int(match.group("target")), "done": True}))
        for _, event in sorted(events, key=lambda pair: pair[0]):
            assert event is not None
            if event.get("done"):
                latest[int(event["target"])] = None
            else:
                latest[int(event["target"])] = event
    return [entry for _, entry in sorted(latest.items()) if entry is not None]


def due(comment_bodies: Iterable[str] | None, now: int) -> list[dict[str, Any]]:
    """The pending targets whose retry deadline has passed.

    A target with no recorded deadline cannot happen (the marker carries one),
    so there is no "fire immediately on a missing timestamp" fallback here --
    unlike the old label path, where a label with no marker comment fired at
    once. The marker IS the schedule.
    """
    return [entry for entry in queue(comment_bodies) if int(now) >= entry["after"]]


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: usage_limit_retry.py "
            "{schedule-marker <target> <issue|pr> <after>|done-marker <target>"
            "|queue|due <now>}",
            file=sys.stderr,
        )
        return 2
    cmd = argv[0]

    if cmd == "schedule-marker":
        if len(argv) < 4:
            print("usage: schedule-marker <target> <issue|pr> <after_epoch>", file=sys.stderr)
            return 2
        print(schedule_marker(int(argv[1]), argv[2], int(argv[3])))
        return 0
    if cmd == "done-marker":
        if len(argv) < 2:
            print("usage: done-marker <target>", file=sys.stderr)
            return 2
        print(done_marker(int(argv[1])))
        return 0
    if cmd == "queue":
        # stdin: JSON array of the release issue's comment bodies.
        print(json.dumps(queue(json.load(sys.stdin))))
        return 0
    if cmd == "due":
        if len(argv) < 2:
            print("usage: due <now_epoch>  # comment bodies JSON on stdin", file=sys.stderr)
            return 2
        print(json.dumps(due(json.load(sys.stdin), int(argv[1]))))
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
