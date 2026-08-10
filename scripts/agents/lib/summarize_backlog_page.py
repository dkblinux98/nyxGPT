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
  RELEASE_VERSION                     When set (e.g. "v2.0.0"), only issues
                                       whose milestone title contains this
                                       version string are eligible. This is
                                       the release wall (owner decision
                                       2026-07-31): the autopilot and the
                                       selector must never cross into the
                                       next release's work -- sprint dates
                                       drift, so the gate is the release
                                       version carried in milestone titles,
                                       not the calendar. Empty/unset means
                                       no release filtering (pre-wall
                                       behavior).
  EXCLUDE_ISSUES                      Comma-separated issue numbers to skip
                                       as candidates (#3665: lets the
                                       dispatch fall through to the next
                                       eligible issue within one run after
                                       an earlier candidate turned out to be
                                       unclaimable, without re-selecting the
                                       same blocked issue forever).
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
    release_version = os.getenv("RELEASE_VERSION", "")
    release_issue = os.getenv("RELEASE_ISSUE", "")
    exclude_issues = {n.strip() for n in os.getenv("EXCLUDE_ISSUES", "").split(",") if n.strip()}

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

        if str(c.get("number")) in exclude_issues:
            continue

        ms_title = ((c.get("milestone") or {}) or {}).get("title")

        # Release wall: an issue outside the current release's milestone is
        # never eligible, regardless of sprint. Issues with NO milestone are
        # also excluded when the wall is up -- unmilestoned work has no
        # release membership, so continuing into it automatically would be
        # exactly the boundary-crossing the wall exists to prevent.
        if release_version and release_version not in (ms_title or ""):
            continue

        # The release tracking issue is a ledger, never work. Left
        # unguarded, project hygiene stamping it Backlog + the current
        # milestone made the selector hand it to the developer agent, which
        # crash-looped trying to "implement" it (#3521, 2026-07-31).
        if release_issue and str(c.get("number")) == release_issue:
            continue

        if sprint_scoped and sprint_title != active_sprint_title:
            continue

        backlog_open += 1
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
