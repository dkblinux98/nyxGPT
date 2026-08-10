#!/usr/bin/env python3
"""Pure cross-issue infrastructure-anomaly detection for #3694.

No network/gh calls live here on purpose, mirroring retry_budget.py (#3689)
and sprint_calc.py (#3480): developer_auto_implement.yml gathers the release
tracking issue's comment thread via `gh api .../comments --paginate` and
hands it to this module (as JSON on stdin) so the cross-issue-collapse
decision is unit-testable without mocking the GitHub API.

Background (#3694): a 2026-08-09 runner-image change made
`gh api search/issues` fail deterministically in the "Check if PR already
exists" step. Five issues were in flight, so the pipeline ran five
independent self-heal diagnosis loops against the same infrastructure
fault -- the same step failing on *different* issues within a short window
is one infrastructure event, not N coding problems. This module lets the
first issue to hit a given failed step open a single tracking record (a
marker comment on the release tracking issue); every other issue that hits
the same step while that record is still open links to it instead of
re-diagnosing.

The tracking record is a comment marker, not a hidden counter -- re-derived
fresh from the release issue's live comment thread on every check, the same
level-triggered shape as escalation_pause_gate (lib/gh_project.sh, #3687).
It self-expires after `window_minutes` and can be cleared early by an
OWNER-authored `RESOLVE_ANOMALY` comment.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retry_budget import slugify_step  # noqa: E402

DEFAULT_WINDOW_MINUTES = 60
RESOLVE_KEYWORD = "RESOLVE_ANOMALY"

_MARKER_RE = re.compile(
    r"<!--\s*nyxgpt-anomaly:\s*step=(?P<step>\S+)\s+issue=(?P<issue>\d+)\s+opened=(?P<opened>\d+)\s*-->"
)


def render_marker(step_name: str, origin_issue: int, opened_epoch: int) -> str:
    """The machine-readable marker embedded in the tracking-record comment."""
    return f"<!-- nyxgpt-anomaly: step={slugify_step(step_name)} issue={origin_issue} opened={opened_epoch} -->"


def parse_markers(comment_body: str) -> list[dict[str, Any]]:
    """All nyxgpt-anomaly markers found in a single comment body (normally 0 or 1)."""
    return [
        {"step": m.group("step"), "issue": int(m.group("issue")), "opened": int(m.group("opened"))}
        for m in _MARKER_RE.finditer(comment_body)
    ]


def _is_resolve_comment(comment: dict[str, Any]) -> bool:
    body = (comment.get("body") or "").strip()
    return comment.get("author_association") == "OWNER" and body == RESOLVE_KEYWORD


def _latest_marker_by_step(comments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Most recent marker per step slug, each tagged with its comment index."""
    latest: dict[str, dict[str, Any]] = {}
    for i, c in enumerate(comments):
        for marker in parse_markers(c.get("body") or ""):
            latest[marker["step"]] = {**marker, "index": i}
    return latest


def _is_open(
    marker: dict[str, Any], comments: list[dict[str, Any]], now_epoch: int, window_minutes: int
) -> bool:
    if any(_is_resolve_comment(c) for c in comments[marker["index"] + 1 :]):
        return False
    return bool(now_epoch - int(marker["opened"]) <= window_minutes * 60)


def find_open_anomaly(
    comments: list[dict[str, Any]],
    step_name: str,
    now_epoch: int,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> dict[str, Any] | None:
    """The currently-open tracking-record marker for `step_name`, or None.

    `comments` is the release tracking issue's comment thread in
    chronological (ascending) order, as returned by
    `gh api .../issues/<n>/comments`; each item needs at least `body` and
    `author_association`. A marker is open when it is the most recent one
    for this step, no OWNER `RESOLVE_ANOMALY` comment follows it, and it is
    within `window_minutes` of `now_epoch`.
    """
    step_slug = slugify_step(step_name)
    marker = _latest_marker_by_step(comments).get(step_slug)
    if marker is None or not _is_open(marker, comments, now_epoch, window_minutes):
        return None
    return {"step": step_slug, "origin_issue": marker["issue"], "opened_epoch": marker["opened"]}


def decide(
    comments: list[dict[str, Any]],
    issue: int,
    step_name: str,
    now_epoch: int,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> dict[str, Any]:
    """Cross-issue anomaly decision for `issue` hitting `step_name` now.

    - `{"action": "skip", "origin_issue": N, ...}` -- a DIFFERENT issue
      already opened a matching, unresolved, in-window tracking record for
      this step. The caller should skip its own diagnosis and link to N.
    - `{"action": "open", "origin_issue": issue, ...}` -- no matching open
      record exists; this issue becomes the tracking record's origin. The
      caller should post the marker and proceed with its own diagnosis.
    - `{"action": "proceed", "origin_issue": issue, ...}` -- this issue is
      already the recorded origin (e.g. re-hitting the same failure on a
      later run); no new marker needed, proceed with diagnosis as normal.
    """
    step_slug = slugify_step(step_name)
    existing = find_open_anomaly(comments, step_name, now_epoch, window_minutes)
    if existing is None:
        return {"action": "open", "origin_issue": issue, "step": step_slug}
    if existing["origin_issue"] == issue:
        return {"action": "proceed", **existing}
    return {"action": "skip", **existing}


def any_open_anomaly(
    comments: list[dict[str, Any]],
    now_epoch: int,
    window_minutes: int = DEFAULT_WINDOW_MINUTES,
) -> bool:
    """True if any step currently has an open tracking-record marker.

    Used by the dispatch-pause gate: dispatch pauses while at least one
    cross-issue infrastructure anomaly is open, regardless of which step.
    """
    return any(
        _is_open(marker, comments, now_epoch, window_minutes)
        for marker in _latest_marker_by_step(comments).values()
    )


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: cross_issue_anomaly.py <decide|marker|any-open> ...", file=sys.stderr)
        return 2

    cmd = argv[0]
    if cmd == "marker":
        if len(argv) != 4:
            print(
                "usage: cross_issue_anomaly.py marker <step_name> <origin_issue> <opened_epoch>",
                file=sys.stderr,
            )
            return 2
        step_name, origin_issue, opened_epoch = argv[1], int(argv[2]), int(argv[3])
        print(render_marker(step_name, origin_issue, opened_epoch))
        return 0

    if cmd == "decide":
        if len(argv) not in (4, 5):
            print(
                "usage: cross_issue_anomaly.py decide <issue> <step_name> <now_epoch> [window_minutes]",
                file=sys.stderr,
            )
            return 2
        issue, step_name, now_epoch = int(argv[1]), argv[2], int(argv[3])
        window_minutes = int(argv[4]) if len(argv) == 5 else DEFAULT_WINDOW_MINUTES
        comments = json.loads(sys.stdin.read())
        print(json.dumps(decide(comments, issue, step_name, now_epoch, window_minutes)))
        return 0

    if cmd == "any-open":
        if len(argv) not in (2, 3):
            print(
                "usage: cross_issue_anomaly.py any-open <now_epoch> [window_minutes]",
                file=sys.stderr,
            )
            return 2
        now_epoch = int(argv[1])
        window_minutes = int(argv[2]) if len(argv) == 3 else DEFAULT_WINDOW_MINUTES
        comments = json.loads(sys.stdin.read())
        print("true" if any_open_anomaly(comments, now_epoch, window_minutes) else "false")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
