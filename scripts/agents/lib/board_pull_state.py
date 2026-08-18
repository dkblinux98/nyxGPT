#!/usr/bin/env python3
"""Board state the pull step decides on (#3883): candidates, and what is in flight.

`summarize_backlog_page.py` answers "which single issue is lowest-numbered?"
-- the question the push model asked. The pull asks two different ones:

  * which issues are *candidates* (open, Backlog, inside the release wall and
    the active sprint, not a support report, not the release ledger), in board
    order and carrying their assignees, so §5's decision matrix can be applied
    and §6 can reorder them by the sprint plan; and
  * what is already *in flight* (In Progress or In Review), because the WIP
    limit and the file-overlap check must be read from the board and open PRs,
    never from an agent session's memory -- a fresh session has none.

The filters are deliberately the same ones the old summarizer applies, for the
same reasons (the release wall, the hard sprint boundary of #3706, the support
refusal of #3745, and the release tracking issue that once got handed to the
developer agent to "implement", #3521). Only the output shape differs.

Reads the concatenated pages of the pull query on argv[1] (one JSON response
per line, as the caller accumulates them).
"""

from __future__ import annotations

import json
import os
import sys

from support_label import is_support_issue


def _field_values(
    item: dict, status_field: str, sprint_field: str
) -> tuple[str | None, str | None]:
    status = sprint_title = None
    for value in (item.get("fieldValues") or {}).get("nodes", []):
        typ = value.get("__typename")
        field = value.get("field") or {}
        if typ == "ProjectV2ItemFieldSingleSelectValue" and field.get("name") == status_field:
            status = value.get("name")
        elif typ == "ProjectV2ItemFieldIterationValue" and field.get("name") == sprint_field:
            sprint_title = value.get("title")
    return status, sprint_title


def board_state(pages: list[dict]) -> dict:
    status_field = os.getenv("STATUS_FIELD", "Status")
    status_backlog = os.getenv("STATUS_BACKLOG", "Backlog")
    in_flight_statuses = {
        os.getenv("STATUS_IN_PROGRESS", "In Progress"),
        os.getenv("STATUS_IN_REVIEW", "In Review"),
    }
    sprint_field = os.getenv("SPRINT_FIELD", "Sprint")
    sprint_scoped = os.getenv("SPRINT_SCOPED", "0") == "1"
    active_sprint = os.getenv("ACTIVE_SPRINT_TITLE", "")
    release_version = os.getenv("RELEASE_VERSION", "")
    release_issue = os.getenv("RELEASE_ISSUE", "")

    candidates: list[dict] = []
    in_flight: list[int] = []

    for page in pages:
        for item in ((page.get("data") or {}).get("node") or {}).get("items", {}).get("nodes", []):
            content = item.get("content") or {}
            if content.get("__typename") != "Issue" or content.get("state") != "OPEN":
                continue
            number = int(content["number"])
            if release_issue and str(number) == str(release_issue):
                continue
            if is_support_issue((content.get("labels") or {}).get("nodes")):
                continue

            status, sprint_title = _field_values(item, status_field, sprint_field)

            # In-flight WIP is counted across the whole board, not just the
            # active sprint: an issue still In Review from last sprint is
            # still a live conflict surface and still occupies a slot.
            if status in in_flight_statuses:
                in_flight.append(number)
                continue

            if status != status_backlog:
                continue

            milestone = ((content.get("milestone") or {}) or {}).get("title") or ""
            if release_version and release_version not in milestone:
                continue
            if sprint_scoped and sprint_title != active_sprint:
                continue

            candidates.append(
                {
                    "issue": number,
                    "status": status,
                    "sprint": sprint_title or "",
                    "milestone": milestone,
                    "assignees": [
                        a.get("login", "")
                        for a in (content.get("assignees") or {}).get("nodes", [])
                    ],
                }
            )

    candidates.sort(key=lambda c: c["issue"])
    return {"candidates": candidates, "in_flight": sorted(in_flight)}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: board_pull_state.py <pages.json>", file=sys.stderr)
        return 2
    pages: list[dict] = []
    with open(argv[0], encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                pages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    print(json.dumps(board_state(pages)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
