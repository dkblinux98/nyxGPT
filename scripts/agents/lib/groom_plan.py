#!/usr/bin/env python3
"""Seed a sprint plan from the board, for the scrummaster to groom (#3908).

This builds the *draft*: the sprint's issues, their fields, their native
relationships, an expected-files list seeded from each issue body, and a
starting order. It is deliberately not the final word -- D-004 puts judgment
at decision points, and ordering a sprint is one. The scrummaster reviews the
draft, reorders where the evidence says so, records *why* in the plan's
rationale, and takes developer feedback on the estimates it cannot derive.

The seed order is the cheapest defensible one:

  1. **dependencies first** -- an issue blocked by another issue in the same
     sprint can never be pulled before it, so ordering it earlier would only
     produce a candidate the pull step must skip;
  2. then **priority**, highest first;
  3. then **effort**, smallest first -- small work finishes and frees a WIP
     slot, which matters because WIP is 2;
  4. then issue number, purely so the output is stable.

Note what is *not* in the seed: file overlap. Overlap is a scheduling decision
made at pull time against what is actually in flight (§6), not a property of
the plan -- two issues touching one file are fine in the same sprint as long
as they are not pulled together.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import sprint_plan

# Board values, best first. Anything unrecognized sorts last: an ungroomed
# field should never outrank a deliberate one.
PRIORITY_RANK = {"P0 - Critical": 0, "P1 - High": 1, "P2 - Medium": 2, "P3 - Low": 3}
EFFORT_RANK = {"XS": 0, "S": 1, "M": 2, "L": 3, "XL": 4}


def _rank(table: dict[str, int], value: str | None) -> int:
    if not value:
        return len(table)
    for key, rank in table.items():
        if value == key or value.split(" ")[0] == key.split(" ")[0]:
            return rank
    return len(table)


def topological(issues: list[int], blocked_by: dict[int, list[int]]) -> list[int]:
    """Order so an in-sprint blocker always precedes what it blocks.

    Kahn's algorithm over the in-sprint subgraph only -- a blocker outside the
    sprint cannot be ordered here, and the pull step refuses the blocked issue
    anyway while that blocker is unmerged. A cycle (two issues each blocking
    the other, which is a data error) degrades to the input order rather than
    dropping issues: a plan that lists everything in a questionable order is
    recoverable; one that silently omits work is not.
    """
    inside = set(issues)
    incoming = {n: [b for b in blocked_by.get(n, []) if b in inside] for n in issues}
    order: list[int] = []
    remaining = list(issues)
    while remaining:
        ready = [n for n in remaining if not incoming[n]]
        if not ready:
            order.extend(remaining)
            break
        for node in ready:
            order.append(node)
            remaining.remove(node)
        for node in remaining:
            incoming[node] = [b for b in incoming[node] if b not in order]
    return order


def seed_order(
    items: list[dict[str, Any]], blocked_by: dict[int, list[int]]
) -> list[dict[str, Any]]:
    """Apply the seed ordering described in the module docstring."""
    by_number = {int(item["issue"]): item for item in items}
    ranked = sorted(
        by_number,
        key=lambda n: (
            _rank(PRIORITY_RANK, by_number[n].get("priority")),
            _rank(EFFORT_RANK, by_number[n].get("effort")),
            n,
        ),
    )
    return [by_number[n] for n in topological(ranked, blocked_by)]


def build_plan(
    *,
    sprint: str,
    window: dict[str, str],
    milestone: str,
    items: Iterable[dict[str, Any]],
    blocked_by: dict[int, list[int]],
    blocks: dict[int, list[int]],
    capacity: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the plan structure `sprint_plan.render_plan` writes.

    A previous plan for the same sprint is not overwritten wholesale: its
    regroom log, its rationale and any hand-curated expected-files survive, so
    re-running the groomer mid-sprint is a regroom (§4.5) rather than a reset.
    """
    previous = previous or {}
    prior_entries = {
        int(entry["issue"]): entry
        for entry in previous.get("order", [])
        if str(entry.get("issue", "")).isdigit()
    }

    enriched: list[dict[str, Any]] = []
    for item in items:
        number = int(item["issue"])
        prior = prior_entries.get(number, {})
        files = item.get("expected_files") or sprint_plan.expected_files_from_body(item.get("body"))
        entry = {
            "issue": number,
            "title": item.get("title", ""),
            "priority": item.get("priority", ""),
            "effort": item.get("effort", ""),
            # A hand-curated list always wins over the heuristic re-seed:
            # someone corrected it for a reason.
            "expected_files": prior.get("expected_files") or files,
            "blocked_by": sorted(blocked_by.get(number, [])),
            "blocks": sorted(blocks.get(number, [])),
            "why_here": prior.get("why_here", ""),
        }
        enriched.append(entry)

    ordered = seed_order(enriched, blocked_by)

    return {
        "sprint": sprint,
        "window": window,
        "milestone": milestone,
        "order": ordered,
        "order_rationale": previous.get("order_rationale", ""),
        "deferred": previous.get("deferred", []),
        "capacity": capacity or previous.get("capacity", {}),
        "regroom_log": previous.get("regroom_log", []),
    }
