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
              tracking issue (exempt: it stays in that lane until the
              whole release is accepted) and the features that are only
              still sitting there because their own failures are held --
              see `rework_features` below.

  rework      the currently HELD issues -> the features their bodies name
              ("Related feature: #N", legacy "Parent feature: #N" -- the
              same markers the promotion sweep reads), counting ONLY the
              held issues labeled "Acceptance Failure". A failed feature
              is not "still under test": the owner has already failed it
              and it parks closed in `Acceptance Testing` until every
              related FAILURE reaches `For Release`
              (scripts/agents/promote_accepted_features.sh, owner flow
              2026-08-02). Counting such a feature as a blocker would
              deadlock the gate -- the feature waits on its failure, the
              failure waits on the gate, the gate waits on the feature --
              so `decide` subtracts them from the blockers alongside the
              release issue.

              The label filter mirrors the one the promotion sweep applies
              (`labels=Acceptance%20Failure`), so the two sweeps can never
              disagree about which held issue parks which feature. It
              matters because a held **Improvement** parks nothing: an
              improvement never blocks its related feature's acceptance
              (owner decision 2026-08-01), so a feature named only by held
              improvements is genuinely still under test and must keep the
              gate closed.

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
  DRAIN_GATE_REWORK_LABEL      the label a held issue must carry for its
                                related-feature marker to park that
                                feature (`rework`). Default "Acceptance
                                Failure" -- the same label
                                promote_accepted_features.sh filters on.
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

# The link from a held failure/improvement back to the feature it was filed
# against. Same marker pair promote_accepted_features.sh reads, so the two
# sweeps always agree on which failure belongs to which feature.
RELATED_FEATURE_RE = re.compile(r"(?:Parent|Related)\s+feature:\s*#(\d+)", re.IGNORECASE)


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


def _label_names(issue: dict) -> set[str]:
    """Casefolded label names, tolerating both the API's `{"name": ...}`
    objects and a plain list of strings."""
    return {
        ((lbl.get("name") if isinstance(lbl, dict) else str(lbl)) or "").casefold()
        for lbl in issue.get("labels") or []
    }


def rework_features(issues: list[dict]) -> list[int]:
    """The features parked awaiting rework by the currently held issues.

    A feature whose **failures** are held is awaiting rework, not under
    test: it parks closed in `Acceptance Testing` until every related
    failure reaches `For Release`. Exempting it is what keeps the gate
    from deadlocking on the work it is itself holding.

    Only held issues labeled `DRAIN_GATE_REWORK_LABEL` ("Acceptance
    Failure") count -- the same filter promote_accepted_features.sh
    applies, so the sweep that parks a feature and the gate that exempts
    it always agree. A held **Improvement** parks nothing (it never blocks
    its related feature, owner decision 2026-08-01), so a feature named
    only by held improvements stays a blocker and the gate stays closed
    while the owner is still testing it.
    """
    label = os.getenv("DRAIN_GATE_REWORK_LABEL", "Acceptance Failure").strip().casefold()
    found: set[int] = set()
    for issue in issues:
        if label and label not in _label_names(issue):
            continue
        for match in RELATED_FEATURE_RE.finditer(issue.get("body") or ""):
            found.add(int(match.group(1)))
    return sorted(found)


def decide(snapshot: dict) -> dict:
    """Gate state from a merged lane snapshot.

    Open when `Acceptance Testing` holds nothing but exempt items: the
    release tracking issue, and any feature that a currently held issue
    names as its related feature (that feature is parked awaiting rework,
    and the rework cannot start until this very gate opens).

    `blockers` is what is still holding the gate closed; `held` is what
    gets released into Backlog when it opens.
    """
    release_issue = os.getenv("RELEASE_ISSUE", "").strip()
    release_exempt = {int(release_issue)} if release_issue.isdigit() else set()

    testing = sorted({int(n) for n in snapshot.get("acceptance_testing", [])})
    held = sorted({int(n) for n in snapshot.get("acceptance_failed", [])})
    rework = {int(n) for n in snapshot.get("rework_features", [])}

    # A feature only earns the exemption while its failures are actually
    # held: once they are released the feature is back to waiting on
    # ordinary in-flight work, which does move on its own.
    rework = rework if held else set()
    exempt = release_exempt | rework
    blockers = [n for n in testing if n not in exempt]

    return {
        "open": not blockers,
        "blockers": blockers,
        "held": held,
        "release_issue_exempt": sorted(release_exempt & set(testing)),
        "rework_exempt": sorted(rework & set(testing)),
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
    return bool(configured and _label_names(issue) & configured)


def _merge(snapshots: list[dict]) -> dict:
    merged: dict[str, list[int]] = {"acceptance_testing": [], "acceptance_failed": []}
    for snap in snapshots:
        for key in merged:
            merged[key].extend(int(n) for n in snap.get(key, []))
    return {key: sorted(set(values)) for key, values in merged.items()}


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: drain_gate.py {summarize <page.json>|decide|merge|bypass|rework}",
            file=sys.stderr,
        )
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
    if cmd == "rework":
        # stdin: JSON array of held issues ({"body": ..., "labels": [...]}
        # each, extra keys ignored) -> {"rework_features": [...]}. Both
        # keys matter: the marker lives in the body, and only issues
        # labeled "Acceptance Failure" park a feature.
        issues = json.load(sys.stdin)
        print(json.dumps({"rework_features": rework_features(issues)}))
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
