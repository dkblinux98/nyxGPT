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

              It also reports which `Acceptance Failed` items are CLOSED
              (`acceptance_failed_parked`). That is the dual-lane rule of
              owner decision 2026-08-14 (#3780): the owner parks features
              they have tested and FAILED in the same lane, "so that I
              don't get lost as to what I've tested that has failed". A
              closed item there is such a parked feature -- already
              implemented and merged, waiting on its blockers; an OPEN one
              is holding-pen work filed this round (#3730), which is what
              the gate releases.

  decide      merged lane snapshot -> is the gate open? The gate opens
              when `Acceptance Testing` holds nothing except the release
              tracking issue (exempt: it stays in that lane until the
              whole release is accepted) and the features that are only
              still sitting there because their own failures are held --
              see `rework_features` below.

              `held` (what a gate opening releases into Backlog) is the
              `Acceptance Failed` lane MINUS the parked features above.
              Their lane placement is owner signal: agents read it, they
              never rearrange it. Only promote_accepted_features.sh moves
              such a feature, and only once its whole blocked-by closure
              has reached `For Release`.

  rework      the currently HELD issues -> the issues they BLOCK, read
              from GitHub's native relationships (`blocks`, owner decision
              2026-08-12 / #3731) with the retired "Related feature: #N"
              body marker as the historical fallback. A failed feature is
              not "still under test": the owner has already failed it and
              it parks closed in `Acceptance Testing` OR in `Acceptance
              Failed` (#3780) until everything blocking it reaches
              `For Release`
              (scripts/agents/promote_accepted_features.sh). Counting such
              a feature as a blocker would deadlock the gate -- the feature
              waits on its failure, the failure waits on the gate, the gate
              waits on the feature -- so `decide` subtracts them from the
              blockers alongside the release issue, in whichever of the two
              lanes the owner parked them.

              The label filter mirrors the one the promotion sweep applies,
              so the two sweeps can never disagree about which held issue
              parks which issue. Since #3731 that is BOTH handler labels:
              `@improvement` writes the same blocking relationship as
              `@acceptance-failure`, so a held Improvement parks its issue
              exactly like a held failure -- and must, or the same deadlock
              reappears for improvements.

  bypass      one issue's labels/title/body -> may it skip the gate?
              The gate is for PRODUCT acceptance work. Agent-process
              issues are worked immediately.

  role        one issue -> "rework" | "original" | "work". THE
              discriminator of owner standard D-042 (2026-08-22), read
              from a rework label plus the native blocked-by/blocks edges
              and from nothing else. `is-rework` is its boolean form. See
              `acceptance_role` for why each signal was chosen, and why
              open-vs-closed state -- what the code read before -- is no
              longer a safe proxy now that an owner reopen means "this did
              not pass acceptance".

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
  DRAIN_GATE_REWORK_LABELS     comma-separated labels a held issue must
                                carry for its blocking relationship to park
                                the issue it blocks (`rework`). Default
                                "Acceptance Failure,Improvement" -- the same
                                labels promote_accepted_features.sh sweeps.
                                `DRAIN_GATE_REWORK_LABEL` (singular) is
                                still honoured as a back-compatible alias.
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

# Retired body marker, read-only fallback for issues filed before #3731.
# The link from a held failure/improvement back to the issue it was filed
# against now lives in GitHub's native relationships; gh_project.sh supplies
# them as `blocks` on each held issue and they win whenever present.
RELATED_FEATURE_RE = re.compile(r"(?:Parent|Related)\s+feature:\s*#(\d+)", re.IGNORECASE)

# Labels whose held issues park the issue they block. Mirrors the labels
# promote_accepted_features.sh sweeps (#3731) — if the gate exempted fewer
# than the sweep parks, the gate would deadlock on its own held work.
DEFAULT_REWORK_LABELS = ("Acceptance Failure", "Improvement")


def _lane(page: dict) -> tuple[list[int], list[int], list[int]]:
    status_field = os.getenv("STATUS_FIELD", "Status")
    testing = os.getenv("STATUS_ACCEPTANCE_TESTING", "Acceptance Testing")
    failed = os.getenv("STATUS_ACCEPTANCE_FAILED", "Acceptance Failed")

    in_testing: list[int] = []
    in_failed: list[int] = []
    parked: list[int] = []

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
            # CLOSED in the holding lane == a feature the owner tested,
            # failed and parked there (#3780). Held rework filed this round
            # is OPEN. State answers only this narrow question -- "did the
            # owner park an already-merged feature here?" -- and a label
            # check would misread a closed failure issue the owner parked
            # after re-testing it. It does NOT answer "is this open item
            # work?": since D-042 an owner reopen means the opposite, and
            # `acceptance_role` is what decides that (see `classify_held`).
            if str(content.get("state") or "").upper() == "CLOSED":
                parked.append(int(content["number"]))

    return in_testing, in_failed, parked


def summarize(page: dict) -> dict:
    in_testing, in_failed, parked = _lane(page)
    return {
        "acceptance_testing": in_testing,
        "acceptance_failed": in_failed,
        "acceptance_failed_parked": parked,
    }


def _label_names(issue: dict) -> set[str]:
    """Casefolded label names, tolerating both the API's `{"name": ...}`
    objects and a plain list of strings."""
    return {
        ((lbl.get("name") if isinstance(lbl, dict) else str(lbl)) or "").casefold()
        for lbl in issue.get("labels") or []
    }


def _rework_labels() -> set[str]:
    """Labels whose held issues park what they block, case-folded."""
    raw = os.getenv("DRAIN_GATE_REWORK_LABELS") or os.getenv("DRAIN_GATE_REWORK_LABEL")
    if raw is None:
        raw = ",".join(DEFAULT_REWORK_LABELS)
    return {part.strip().casefold() for part in raw.split(",") if part.strip()}


def related_targets(issue: dict) -> list[int]:
    """The issues this one was FILED AGAINST, from its native `blocks`
    edges (#3731) with the retired body marker as the historical fallback.

    Same resolution `related_feature_of` performs in gh_project.sh; kept
    here so the shell and the gate cannot drift apart.
    """
    native = [int(n) for n in issue.get("blocks") or []]
    if native:
        return sorted(set(native))
    return sorted({int(m.group(1)) for m in RELATED_FEATURE_RE.finditer(issue.get("body") or "")})


ROLE_REWORK = "rework"
ROLE_ORIGINAL = "original"
ROLE_WORK = "work"


def acceptance_role(issue: dict) -> str:
    """THE discriminator of owner standard D-042 (2026-08-22).

    Answers, for one issue sitting OPEN in the `Acceptance Failed` lane,
    which of three things it is. Every consumer -- the handlers that file,
    the gate that releases, the sweep that promotes -- asks here, so no two
    of them can disagree about one issue.

      "rework"    a handler-FILED acceptance failure / improvement: it
                  carries a rework label AND it natively BLOCKS another
                  issue, i.e. it was filed *against* something. The gate
                  releases it to Backlog; the sweep never promotes it,
                  because its own fix has not shipped.
                  This is bit-for-bit the test
                  handle_acceptance_failure.yml already applies to choose
                  its reopen-for-rework path, which is why #3850 and #3862
                  keep behaving exactly as they do today.

      "original"  an issue that is BLOCKED BY other issues: its work lives
                  somewhere else, and the reopen is the owner's signal that
                  it did not pass acceptance (D-042 (a)). The gate leaves
                  it parked -- dispatching a developer against it is the
                  #3999 defect -- and the sweep promotes, reassigns and
                  closes it once its whole blocked-by closure is accepted.

      "work"      anything else: an issue that carries its own work and is
                  waiting on nothing. That is D-042 (b) -- a failure
                  spanning several issues or fitting none, filed with no
                  relationship and worked like a brand-new issue -- and it
                  is released with the batch, unchanged from today.

    WHY THESE SIGNALS. Both are *records the process deliberately writes*:
    the label the handler applies, and the native blocked-by/blocks edges
    that #3731 made the single storage for issue relationships. Neither is
    read from the issue's shape or from open/closed state.

    WHY NOT STATE, which is what the code read until 2026-08-22: D-008 took
    "open in that lane" to mean held rework, and D-042 makes an owner's
    reopen mean the opposite -- "this did not pass acceptance". On
    2026-08-22 that released #3835 (blocked by #3984/#3985/#3989) and #3829
    (blocked by #3991) to a developer with nothing to implement.

    WHY NOT THE LABEL ALONE: #3829 carries "Acceptance Failure" and is an
    original the owner reopened; #3835 carries none of the rework labels and
    is one too. WHY NOT THE EDGE ALONE: a plain feature can block a
    sequenced successor. The two together classify every issue in the
    2026-08-22 incident correctly, in both directions.
    """
    labels = _rework_labels()
    labelled = bool(labels) and bool(labels & _label_names(issue))
    if labelled and related_targets(issue):
        return ROLE_REWORK
    if issue.get("blocked_by"):
        return ROLE_ORIGINAL
    return ROLE_WORK


def is_rework_issue(issue: dict) -> bool:
    """True when `acceptance_role` calls this issue handler-filed rework."""
    return acceptance_role(issue) == ROLE_REWORK


def rework_features(issues: list[dict]) -> list[int]:
    """The issues parked awaiting rework by the currently held issues.

    An issue whose failures/improvements are held is awaiting rework, not
    under test: it parks closed in `Acceptance Testing` -- or, since #3780,
    in `Acceptance Failed` where the owner keeps what they have tested and
    failed -- until everything blocking it reaches `For Release`. Exempting
    it is what keeps the gate from deadlocking on the work it is itself
    holding, in either lane.

    Only issues `is_rework_issue` recognises contribute: the same filter
    promote_accepted_features.sh applies, so the sweep that parks an issue
    and the gate that exempts it always agree. Since both handler commands
    write the relationship, that covers Improvements too.
    """
    found: set[int] = set()
    for issue in issues:
        if is_rework_issue(issue):
            found.update(related_targets(issue))
    return sorted(found)


def classify_held(issues: list[dict]) -> dict:
    """Split the OPEN holding-lane items by `acceptance_role`.

    `rework_issues` plus the "work" ones are what a gate opening releases to
    Backlog; `parked_open` are the reopened originals (D-042), left exactly
    where the owner's reopen put them and moved only by
    promote_accepted_features.sh.

    An entry with no `number` is counted for `rework_features` only --
    callers that do not supply one get the pre-D-042 output shape.
    """
    rework: list[int] = []
    parked: list[int] = []
    work: list[int] = []
    for issue in issues:
        number = issue.get("number")
        if number is None:
            continue
        role = acceptance_role(issue)
        {ROLE_REWORK: rework, ROLE_ORIGINAL: parked, ROLE_WORK: work}[role].append(int(number))
    return {
        "rework_features": rework_features(issues),
        "rework_issues": sorted(set(rework)),
        "parked_open": sorted(set(parked)),
        "unrelated_work": sorted(set(work)),
    }


def decide(snapshot: dict) -> dict:
    """Gate state from a merged lane snapshot.

    Open when `Acceptance Testing` holds nothing but exempt items: the
    release tracking issue, and any feature that a currently held issue
    names as its related feature (that feature is parked awaiting rework,
    and the rework cannot start until this very gate opens).

    `blockers` is what is still holding the gate closed; `held` is what
    gets released into Backlog when it opens; `parked` is what sits in the
    holding lane as owner signal and is deliberately left where it is --
    the CLOSED features of #3780 and, since D-042, the OPEN originals the
    owner reopened because they failed acceptance.
    """
    release_issue = os.getenv("RELEASE_ISSUE", "").strip()
    release_exempt = {int(release_issue)} if release_issue.isdigit() else set()

    testing = sorted({int(n) for n in snapshot.get("acceptance_testing", [])})
    lane = sorted({int(n) for n in snapshot.get("acceptance_failed", [])})
    # Features the owner parked in the holding lane are not held work: the
    # gate never moves them (owner decision 2026-08-14, #3780). Absent key
    # -> the whole lane is held, which is the pre-#3780 behavior.
    parked = {int(n) for n in snapshot.get("acceptance_failed_parked", [])} & set(lane)
    # ... and neither are the OPEN originals an owner reopened because they
    # failed acceptance (owner standard D-042, 2026-08-22). Those are the
    # items `is_rework_issue` rejects; the gate leaves them parked and the
    # promotion sweep is the only thing that moves them. Absent key -> every
    # open lane item is held, which is the pre-D-042 behavior.
    parked_open = {int(n) for n in snapshot.get("acceptance_failed_parked_open", [])} & set(lane)
    parked |= parked_open
    held = [n for n in lane if n not in parked]
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
        "parked": sorted(parked),
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
    merged: dict[str, list[int]] = {
        "acceptance_testing": [],
        "acceptance_failed": [],
        "acceptance_failed_parked": [],
    }
    for snap in snapshots:
        for key in merged:
            merged[key].extend(int(n) for n in snap.get(key, []))
    return {key: sorted(set(values)) for key, values in merged.items()}


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: drain_gate.py "
            "{summarize <page.json>|decide|merge|bypass|rework|role|is-rework}",
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
        # stdin: JSON array of held issues ({"number":…, "body":…,
        # "labels":[…], "blocks":[…]} each, extra keys ignored) ->
        # {"rework_features":[…], "rework_issues":[…], "parked_open":[…]}.
        # Every key matters: the relationship (or the retired marker in the
        # body) says what the issue was filed against, and the label says
        # whether it is handler-filed rework at all.
        issues = json.load(sys.stdin)
        print(json.dumps(classify_held(issues)))
        return 0
    if cmd == "role":
        # stdin: ONE issue ({"labels":…, "blocks":…, "blocked_by":…,
        # "body":…}) -> "rework" | "original" | "work". THE D-042
        # discriminator; the shell asks here rather than re-implementing it.
        print(acceptance_role(json.load(sys.stdin)))
        return 0
    if cmd == "is-rework":
        # The boolean form of the same question, for callers that only care
        # whether an issue is handler-filed rework.
        print("true" if is_rework_issue(json.load(sys.stdin)) else "false")
        return 0

    print(f"unknown subcommand: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
