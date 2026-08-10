#!/usr/bin/env python3
"""Pure sprint-autopilot calculations for #3480.

No network/gh calls live here on purpose: scrummaster_sprint_report.sh,
scrummaster_sprint_reorg_apply.sh, and review_accept_and_merge.sh gather data
via gh/GraphQL and hand it to this module (as JSON on stdin, or as plain
args) so the actual math -- velocity, verdict, reorg candidate selection,
autopilot stop condition -- is unit-testable without mocking the GitHub API.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date
from typing import Any

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def sprint_velocity(done_count: float, elapsed_days: float) -> float:
    """Issues/day completed so far this sprint. Guards elapsed_days <= 0."""
    elapsed = elapsed_days if elapsed_days > 0 else 1.0
    return round(done_count / elapsed, 4)


def sprint_verdict(remaining: float, velocity: float, days_left: float | None) -> str:
    """on-track / at-risk / off-track, with a 20% buffer before off-track."""
    if remaining <= 0:
        return "on-track"
    if days_left is None:
        return "unknown"
    needed = (remaining / velocity) if velocity > 0 else math.inf
    if needed <= days_left:
        return "on-track"
    if needed <= days_left * 1.2:
        return "at-risk"
    return "off-track"


def reorg_target_count(remaining: float, velocity: float, days_left: float | None) -> int:
    """How many backlog issues to propose moving out of the sprint.

    Callers must clamp the result to the number of available candidates:
    a zero-velocity sprint can't be projected, so this deliberately returns
    a large number meaning "propose clearing everything not yet started"
    rather than guessing a specific count.
    """
    if days_left is None:
        return 0
    if velocity <= 0:
        return remaining if remaining > 0 else 0
    needed = remaining / velocity
    excess_days = needed - days_left
    if excess_days <= 0:
        return 0
    return math.ceil(excess_days * velocity)


# Unknown/missing priority defaults to the P2 (medium) tier rather than the
# lowest tier: we don't actually know an unprioritized issue is safe to bump
# out of the sprint, so it shouldn't jump the queue ahead of confirmed P3s.
_DEFAULT_RANK = _PRIORITY_RANK["P2"]


def _priority_rank(priority: str | None) -> int:
    if not priority:
        return _DEFAULT_RANK
    upper = priority.upper()
    for key, rank in _PRIORITY_RANK.items():
        if key in upper:
            return rank
    return _DEFAULT_RANK


def select_reorg_candidates(issues: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    """Picks `count` issues to move out: lowest priority first, then the
    highest (most likely not-yet-started) issue number first."""
    if count <= 0:
        return []
    ordered = sorted(
        issues,
        key=lambda i: (-_priority_rank(i.get("priority")), -int(i["number"])),
    )
    return ordered[:count]


def autopilot_decision(remaining_open_backlog: int) -> str:
    """ "continue" while eligible backlog work remains in the active sprint,
    "complete" once it hits zero -- the sprint autopilot's stop condition."""
    return "continue" if remaining_open_backlog > 0 else "complete"


_HUDDLE_DECISION_TYPES = frozenset({"a", "b", "c"})


def huddle_routing_decision(disagreement_type: str, request_changes_count: int) -> str:
    """#3687 review-huddle routing: given the review agent's classification
    of a REQUEST_CHANGES round and the running count of REQUEST_CHANGES
    reviews on the PR (including the one just submitted), decides what
    review_agent_auto_review.yml does next. One of:

    - "escalate_spec_ambiguity": type (c) -- the issue itself is unclear,
      or resolution needs owner authority. Escalates immediately,
      cycle zero, regardless of count.
    - "huddle": type (b) judgment call (never loops -- huddles
      immediately), or type (a) on its 2nd cycle (a verifiable-defect
      loop that hasn't converged after one retry huddles instead of a
      3rd blind retry).
    - "escalate_cycle_limit": the existing 3-cycle outer breaker,
      unchanged -- type (a) reaching its 3rd REQUEST_CHANGES review.
    - "normal": type (a), cycle 1 -- return to developer as today.

    An unrecognized disagreement_type is treated as "a" (the pre-#3687
    default), so a missing/malformed classification never blocks the
    existing loop -- it degrades to prior behavior rather than failing
    closed or open unpredictably.
    """
    dtype = disagreement_type if disagreement_type in _HUDDLE_DECISION_TYPES else "a"

    if dtype == "c":
        return "escalate_spec_ambiguity"
    if dtype == "b":
        return "huddle"
    # dtype == "a"
    if request_changes_count >= 3:
        return "escalate_cycle_limit"
    if request_changes_count == 2:
        return "huddle"
    return "normal"


_VERDICT_LABELS = {
    "on-track": "\U0001f7e2 On track",
    "at-risk": "\U0001f7e1 At risk",
    "off-track": "\U0001f534 Off track",
    "unknown": "⚪ Unknown (no sprint end date configured)",
}


def build_sprint_report(payload: dict[str, Any]) -> dict[str, Any]:
    """Computes standing + (if off-track) a reorg proposal, and renders the
    markdown comment body posted on the release tracking issue."""
    sprint_title = payload["sprint_title"]
    start_date = date.fromisoformat(payload["start_date"])
    end_date = date.fromisoformat(payload["end_date"])
    today = date.fromisoformat(payload["today"]) if payload.get("today") else date.today()

    counts = payload.get("counts", {})
    backlog = int(counts.get("backlog", 0))
    in_progress = int(counts.get("in_progress", 0))
    in_review = int(counts.get("in_review", 0))
    done = int(counts.get("done", 0))
    remaining = backlog + in_progress + in_review

    elapsed_days = (today - start_date).days
    days_left = (end_date - today).days

    velocity = sprint_velocity(done, elapsed_days)
    verdict = sprint_verdict(remaining, velocity, days_left)

    needed_days = round(remaining / velocity, 1) if velocity > 0 else None

    backlog_issues = payload.get("backlog_issues", [])
    proposal: dict[str, Any] | None = None
    if verdict == "off-track" and backlog_issues:
        target = min(reorg_target_count(remaining, velocity, days_left), len(backlog_issues))
        candidates = select_reorg_candidates(backlog_issues, target)
        if candidates:
            proposal = {
                "sprint": sprint_title,
                "action": "move_out",
                "candidates": [c["number"] for c in candidates],
            }

    blockers = payload.get("blockers", [])

    lines = [f"### Sprint Report — {sprint_title}", ""]
    lines.append(f"**Verdict: {_VERDICT_LABELS.get(verdict, verdict)}**")
    lines.append("")
    lines.append(f"- Done: {done}")
    lines.append(f"- In Review: {in_review}")
    lines.append(f"- In Progress: {in_progress}")
    lines.append(f"- Backlog (remaining): {backlog}")
    lines.append(f"- Velocity: {velocity} issues/day (elapsed {elapsed_days}d)")
    if needed_days is not None:
        lines.append(
            f"- Projected: {needed_days}d needed vs {days_left}d left "
            f"(sprint ends {end_date.isoformat()})"
        )
    else:
        lines.append(
            "- Projected: velocity is 0 — cannot project a completion date "
            f"({days_left}d left, sprint ends {end_date.isoformat()})"
        )

    lines.append("")
    if blockers:
        lines.append("**Blockers:**")
        for b in blockers:
            lines.append(
                f"- PR #{b['pr']} (issue #{b['issue']}) stuck in review — "
                f"{b.get('changes_requested', 0)} change-request cycle(s)"
            )
    else:
        lines.append("**Blockers:** none detected")

    if proposal:
        lines.append("")
        lines.append(
            "**Reorganization proposal** (stakeholder approval required — "
            "comment `APPROVE_SPRINT_REORG` to apply):"
        )
        for n in proposal["candidates"]:
            lines.append(f'- Move #{n} out of "{sprint_title}" (lowest priority / not started)')
        lines.append("")
        lines.append(
            "Nothing changes until approved. Declining or ignoring this proposal "
            "leaves the sprint as-is."
        )
        marker = json.dumps(proposal, separators=(",", ":"))
        lines.append("")
        lines.append(f"<!-- SPRINT_REORG_PROPOSAL: {marker} -->")
    elif verdict == "off-track":
        lines.append("")
        lines.append(
            "**Reorganization proposal:** off-track, but no eligible Backlog "
            "issues in this sprint to move out."
        )

    return {
        "markdown": "\n".join(lines),
        "verdict": verdict,
        "remaining": remaining,
        "velocity": velocity,
        "days_left": days_left,
        "proposal": proposal,
    }


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: sprint_calc.py <report|autopilot-decision|huddle-routing>", file=sys.stderr)
        return 2

    cmd = argv[0]
    if cmd == "report":
        payload = json.loads(sys.stdin.read())
        print(json.dumps(build_sprint_report(payload)))
        return 0
    if cmd == "autopilot-decision":
        remaining = int(argv[1])
        print(autopilot_decision(remaining))
        return 0
    if cmd == "huddle-routing":
        disagreement_type, request_changes_count = argv[1], int(argv[2])
        print(huddle_routing_decision(disagreement_type, request_changes_count))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
