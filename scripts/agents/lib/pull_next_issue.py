#!/usr/bin/env python3
"""The pull decision: which issue a developer takes next (#3883).

Selection used to be `lowest Phase, then lowest issue number`, decided by the
scrummaster and pushed at the developer. Two things were wrong with that. It
was a condition expression sitting exactly where D-004 reserves judgment, and
it was a push -- so nothing ever compared a candidate's likely file footprint
against work already in flight. On 2026-08-18, with ~10 streams running, four
PRs conflicted simultaneously on `src/nyxgpt/app.py`, each costing a full
conflict-resolution round.

The algorithm here is `product_management/AGENTIC_SDLC_DESIGN.md` §6, run per
free WIP slot:

  1. candidates = active-sprint issues in **plan-doc order** (#3908), in a
     claimable state (§5), Status Backlog;
  2. filter by **relationships eligibility** -- blocked by any unmerged issue
     means ineligible (native `blocked by` edges, D-002);
  3. **WIP limit** (default 2), read from the board and open PRs, never from
     session memory -- a fresh agent session has none;
  4. **file-overlap check** against in-flight work: the candidate's
     expected-files against the in-flight issue's actual diff (or, before a PR
     exists, its own expected-files). Disjoint pulls; overlapping takes the
     next eligible candidate.
  5. the caller then sets Status In Progress and assigns the developer (§5
     ordering) -- the actor doing the work owns the transition.

Scheduling *is* the conflict strategy: an overlap yields the next candidate,
never a parallel pull. The conflict machinery (D-011) stays as the safety net
for what scheduling cannot foresee.

Pure decision, no I/O: the caller supplies board state as JSON on stdin, which
is what makes this testable without a GitHub round trip and what keeps the
"never from session memory" rule honest -- the function cannot remember
anything it was not handed.
"""

from __future__ import annotations

import json
import sys
from typing import Any

DEFAULT_WIP_LIMIT = 2


# §5 decision matrix: who may hold a Backlog issue and still have it be
# claimable. Owner-assigned means the owner is holding it -- skip, never
# reassign. Anyone else is an anomaly: skip and report, do not silently take
# it. The caller passes the concrete logins for these roles.
def _claimable_assignees(roles: dict[str, str]) -> set[str]:
    return {
        roles.get("scrum", "") or "",
        roles.get("dev", "") or "",
    } - {""}


def _norm(path: str) -> str:
    return str(path).strip().lstrip("./").rstrip()


def paths_overlap(a: str, b: str) -> bool:
    """Do two expected-files entries touch the same code?

    Exact match, or one is a directory prefix of the other. A trailing slash
    (or a directory-shaped entry from a globbed plan line) means "anything
    under here", which is how a plan can say `scripts/agents/` without listing
    every script.
    """
    left, right = _norm(a), _norm(b)
    if not left or not right:
        return False
    if left == right:
        return True
    return any(x.endswith("/") and y.startswith(x) for x, y in ((left, right), (right, left)))


def files_overlap(candidate: list[str], in_flight: list[str]) -> list[str]:
    """The overlapping paths, so a skip can say what it collided with."""
    hits: list[str] = []
    for path in candidate:
        for other in in_flight:
            if paths_overlap(path, other):
                hits.append(_norm(path))
                break
    return hits


def _ordered_candidates(candidates: list[dict[str, Any]], order: list[int]) -> list[dict[str, Any]]:
    """Plan order first; anything the plan does not mention keeps board order.

    A candidate absent from the plan is not refused. The plan is the intended
    order, not an allowlist -- refusing would mean a sprint stalls completely
    until it is groomed. Note the tradeoff this accepts: an ungroomed issue
    usually has no expected-files, and the overlap check is *permissive*
    there -- it pulls, and says in its reason that overlap could not be
    checked. Blocking instead would make an unfilled field able to stop the
    queue.
    """
    position = {issue: index for index, issue in enumerate(order)}
    planned = [c for c in candidates if int(c["issue"]) in position]
    unplanned = [c for c in candidates if int(c["issue"]) not in position]
    planned.sort(key=lambda c: position[int(c["issue"])])
    return planned + unplanned


def select(state: dict[str, Any]) -> dict[str, Any]:
    """Decide the next pull. Returns {issue, reason, considered, wip}."""
    plan = state.get("plan") or {}
    order = [int(e["issue"]) for e in plan.get("order", []) if str(e.get("issue", "")).isdigit()]
    plan_files = {
        int(e["issue"]): [str(f) for f in (e.get("expected_files") or [])]
        for e in plan.get("order", [])
        if str(e.get("issue", "")).isdigit()
    }

    candidates = [c for c in (state.get("candidates") or []) if str(c.get("issue", "")).isdigit()]
    in_flight = state.get("in_flight") or []
    wip_limit = int(state.get("wip_limit") or DEFAULT_WIP_LIMIT)
    exclude = {int(n) for n in (state.get("exclude") or [])}
    roles = state.get("roles") or {}
    owner_login = (roles.get("owner") or "").lower()
    claimable = {login.lower() for login in _claimable_assignees(roles)}

    considered: list[dict[str, Any]] = []

    def skip(issue: int, reason: str, detail: str = "") -> None:
        entry: dict[str, Any] = {"issue": issue, "skipped": reason}
        if detail:
            entry["detail"] = detail
        considered.append(entry)

    if len(in_flight) >= wip_limit:
        return {
            "issue": None,
            "reason": (
                f"WIP limit {wip_limit} reached -- "
                + ", ".join(f"#{item['issue']}" for item in in_flight)
                + " already in flight. Pulling another would schedule a conflict, "
                "not avoid one."
            ),
            "considered": considered,
            "wip": len(in_flight),
        }

    in_flight_files: list[str] = []
    for item in in_flight:
        in_flight_files.extend(str(f) for f in (item.get("files") or []))

    blocked_by = {int(k): [int(n) for n in v] for k, v in (state.get("blocked_by") or {}).items()}

    for candidate in _ordered_candidates(candidates, order):
        issue = int(candidate["issue"])

        if issue in exclude:
            skip(issue, "excluded", "an earlier attempt in this run could not claim it")
            continue

        status = str(candidate.get("status") or "")
        if status and status != str(state.get("status_backlog") or "Backlog"):
            skip(issue, "not_backlog", f"Status is '{status}'")
            continue

        assignees = [str(a).lower() for a in (candidate.get("assignees") or [])]
        if owner_login and owner_login in assignees:
            skip(issue, "owner_held", "the owner is holding it -- never reassign (§5)")
            continue
        foreign = [a for a in assignees if a not in claimable]
        if foreign:
            skip(issue, "anomalous_assignee", ", ".join(foreign))
            continue

        open_blockers = list(blocked_by.get(issue, []))
        if open_blockers:
            skip(
                issue,
                "blocked",
                "blocked by " + ", ".join(f"#{n}" for n in open_blockers) + " (unmerged)",
            )
            continue

        files = plan_files.get(issue) or [str(f) for f in (candidate.get("expected_files") or [])]
        hits = files_overlap(files, in_flight_files)
        if hits:
            skip(issue, "file_overlap", ", ".join(hits))
            continue

        planned = issue in order
        where = (
            f"position {order.index(issue) + 1} in the sprint plan"
            if planned
            else "not in the sprint plan; taken in board order after every planned issue"
        )
        overlap_note = (
            "no expected-files recorded, so overlap could not be checked -- "
            "pulled on the plan's order alone"
            if not files
            else "expected-files disjoint from every in-flight footprint"
        )
        return {
            "issue": issue,
            "reason": f"Pulled #{issue}: {where}; eligible (no unmerged blockers); {overlap_note}.",
            "considered": considered,
            "wip": len(in_flight),
            "expected_files": files,
        }

    return {
        "issue": None,
        "reason": "No eligible candidate: every Backlog issue was blocked, held, or "
        "overlapped work already in flight.",
        "considered": considered,
        "wip": len(in_flight),
    }


def main() -> int:
    state = json.load(sys.stdin)
    decision = select(state)
    json.dump(decision, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
