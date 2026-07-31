#!/usr/bin/env python3
"""Summarizes one page of the project-items GraphQL query shared by
scrummaster_next_issue.sh (issue selection) and count_sprint_backlog_open()
in lib/gh_project.sh (sprint autopilot's stop-condition count, #3480).

Extracted from scrummaster_next_issue.sh's original inline heredoc so both
callers -- and tests -- share one implementation instead of two copies
drifting apart.

Env vars:
  STATUS_FIELD, STATUS_BACKLOG        Which Status field/value counts as
                                       eligible backlog work.
  SPRINT_FIELD, SPRINT_SCOPED,
  ACTIVE_SPRINT_TITLE                 When SPRINT_SCOPED=1, only issues whose
                                       Sprint iteration field matches
                                       ACTIVE_SPRINT_TITLE are eligible --
                                       everything else behaves exactly as
                                       before (no Sprint awareness at all).
"""

from __future__ import annotations

import json
import os
import re
import sys


def phase_num(title: str | None) -> int:
    if not title:
        return 10**9
    m = re.search(r"(\d+)", title)
    return int(m.group(1)) if m else 10**9


def summarize(page: dict) -> dict:
    status_field = os.getenv("STATUS_FIELD", "Status")
    status_backlog = os.getenv("STATUS_BACKLOG", "Backlog")
    sprint_field = os.getenv("SPRINT_FIELD", "Sprint")
    sprint_scoped = os.getenv("SPRINT_SCOPED", "0") == "1"
    active_sprint_title = os.getenv("ACTIVE_SPRINT_TITLE", "")

    items = page["data"]["node"]["items"]["nodes"]
    total = len(items)

    issues = 0
    open_issues = 0
    backlog_open = 0
    best: tuple[int, int] | None = None

    for it in items:
        c = it.get("content") or {}
        if c.get("__typename") != "Issue":
            continue
        issues += 1
        if c.get("state") != "OPEN":
            continue
        open_issues += 1

        status = None
        sprint_title = None
        for fv in (it.get("fieldValues") or {}).get("nodes", []):
            typ = fv.get("__typename")
            field = fv.get("field") or {}
            if typ == "ProjectV2ItemFieldSingleSelectValue" and field.get("name") == status_field:
                status = fv.get("name")
            elif typ == "ProjectV2ItemFieldIterationValue" and field.get("name") == sprint_field:
                sprint_title = fv.get("title")

        if status != status_backlog:
            continue

        if sprint_scoped and sprint_title != active_sprint_title:
            continue

        backlog_open += 1
        ms_title = ((c.get("milestone") or {}) or {}).get("title")
        cand = (phase_num(ms_title), int(c["number"]))
        if best is None or cand < best:
            best = cand

    return {
        "total_items": total,
        "issue_items": issues,
        "open_issues": open_issues,
        "backlog_open": backlog_open,
        "best_issue": (best[1] if best else None),
    }


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: summarize_backlog_page.py <page.json>", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8") as f:
        page = json.load(f)
    print(json.dumps(summarize(page)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
