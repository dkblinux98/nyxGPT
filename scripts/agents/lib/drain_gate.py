#!/usr/bin/env python3
"""Acceptance drain gate (#3730).

The owner's testing rhythm is: test everything in the acceptance round
first, THEN let the agents drain the failures, then test the next
candidate. Before this, an `@acceptance-failure` comment put the failure
straight into In Progress, so fixes merged into Acceptance Testing while
the owner was still testing the round -- flooding the lane and burning RC
cycles (owner decision 2026-08-12, #3730).

Three pure decisions live here so they can be unit-tested without GitHub:

  summarize   one page of the shared project-items GraphQL query ->
              the issue numbers sitting in the `Acceptance Testing` and
              `Acceptance Failed` Status lanes. Unlike
              summarize_backlog_page.py this counts CLOSED issues too:
              an item awaiting acceptance is normally a closed issue, so
              "has the lane drained?" is a question about every item in
              it, not just the open ones.

  decide      merged lane snapshot -> is the gate open? The gate opens
              when `Acceptance Testing` holds nothing except the release
              tracking issue, which is exempt: it stays in that lane
              until the whole release is accepted.

  bypass      one issue's labels/title/body -> may it skip the gate?
              The gate is for PRODUCT acceptance work. Agent-process
              issues are worked immediately.

Env vars:
  STATUS_FIELD                 Status field name (default "Status")
  STATUS_ACCEPTANCE_TESTING    lane the gate watches (default
                                "Acceptance Testing")
  STATUS_ACCEPTANCE_FAILED     lane the gate holds (default
                                "Acceptance Failed")
  RELEASE_ISSUE                release tracking issue number -- exempt
                                from the drain check (`decide`)
  DRAIN_GATE_BYPASS_LABELS     comma-separated labels that mark an issue
                                as agent-process work (`bypass`).
                                Default empty: no label exists for this
                                today and agents may not create one, so
                                the marker/heading rules below carry the
                                rule until the owner adds a label.
"""

from __future__ import annotations

import json
import os
import re
import sys

# Explicit, owner-authored process exception. #3730's own body carries
# "This issue bypasses the drain gate it implements -- work immediately.";
# any issue may opt out the same way. Matched case-insensitively on the
# body so the owner can phrase it naturally.
BYPASS_PROSE_RE = re.compile(r"bypass(?:es)?\s+the\s+drain\s+gate", re.IGNORECASE)

# Machine marker for the same thing, for automation that files process
# work (no prose to match against).
BYPASS_MARKER = "<!-- drain-gate: bypass -->"


def _lane(page: dict) -> tuple[list[int], list[int]]:
    status_field = os.getenv("STATUS_FIELD", "Status")
    testing = os.getenv("STATUS_ACCEPTANCE_TESTING", "Acceptance Testing")
    failed = os.getenv("STATUS_ACCEPTANCE_FAILED", "Acceptance Failed")

    in_testing: list[int] = []
    in_failed: list[int] = []

    for it in page["data"]["node"]["items"]["nodes"]:
        content = it.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        status = None
        for fv in (it.get("fieldValues") or {}).get("nodes", []):
            field = fv.get("field") or {}
            if (
                fv.get("__typename") == "ProjectV2ItemFieldSingleSelectValue"
                and field.get("name") == status_field
            ):
                status = fv.get("name")
        if status == testing:
            in_testing.append(int(content["number"]))
        elif status == failed:
            in_failed.append(int(content["number"]))

    return in_testing, in_failed


def summarize(page: dict) -> dict:
    in_testing, in_failed = _lane(page)
    return {"acceptance_testing": in_testing, "acceptance_failed": in_failed}


def decide(snapshot: dict) -> dict:
    """Gate state from a merged lane snapshot.

    Open when `Acceptance Testing` is empty except for the release issue.
    `blockers` is what is still holding it closed; `held` is what gets
    released into Backlog when it opens.
    """
    release_issue = os.getenv("RELEASE_ISSUE", "").strip()
    exempt = {int(release_issue)} if release_issue.isdigit() else set()

    testing = sorted({int(n) for n in snapshot.get("acceptance_testing", [])})
    held = sorted({int(n) for n in snapshot.get("acceptance_failed", [])})
    blockers = [n for n in testing if n not in exempt]

    return {
        "open": not blockers,
        "blockers": blockers,
        "held": held,
        "release_issue_exempt": sorted(exempt & set(testing)),
    }


def bypass(issue: dict) -> bool:
    """True when this issue is agent-process work, not product acceptance
    work, and may therefore be worked immediately (owner decision
    2026-08-12, #3730).

    Two rules, both explicit rather than incidental:
      1. a machine marker in the body, for automation; or
      2. an owner-authored process exception in the body prose.

    A third, label-based rule is configurable but off by default:
    creating labels needs owner permission (CLAUDE.md), so the rule is
    wired and ready rather than assuming a label that does not exist.

    Everything else -- every acceptance failure and improvement filed
    against a feature under test -- is gated.
    """
    body = issue.get("body") or ""
    if BYPASS_MARKER in body:
        return True
    if BYPASS_PROSE_RE.search(body):
        return True

    configured = {
        name.strip().casefold()
        for name in os.getenv("DRAIN_GATE_BYPASS_LABELS", "").split(",")
        if name.strip()
    }
    if configured:
        labels = {
            (lbl.get("name") if isinstance(lbl, dict) else str(lbl)) or ""
            for lbl in issue.get("labels") or []
        }
        if {name.casefold() for name in labels} & configured:
            return True

    return False


def _merge(snapshots: list[dict]) -> dict:
    merged: dict[str, list[int]] = {"acceptance_testing": [], "acceptance_failed": []}
    for snap in snapshots:
        for key in merged:
            merged[key].extend(int(n) for n in snap.get(key, []))
    return {key: sorted(set(values)) for key, values in merged.items()}


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: drain_gate.py {summarize <page.json>|decide|merge|bypass}", file=sys.stderr)
        return 2

    cmd = argv[0]
    if cmd == "summarize":
        if len(argv) < 2:
            print("usage: drain_gate.py summarize <page.json>", file=sys.stderr)
            return 2
        with open(argv[1], encoding="utf-8") as fh:
            print(json.dumps(summarize(json.load(fh))))
        return 0
    if cmd == "merge":
        # One JSON object per line on stdin (one per fetched page).
        snapshots = [json.loads(line) for line in sys.stdin if line.strip()]
        print(json.dumps(_merge(snapshots)))
        return 0
    if cmd == "decide":
        print(json.dumps(decide(json.load(sys.stdin))))
        return 0
    if cmd == "bypass":
        print("true" if bypass(json.load(sys.stdin)) else "false")
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
