#!/usr/bin/env python3
"""Release-ceremony prerequisite inventory: the derivations and the gate decision (#4166).

`scripts/release_ceremony.sh` Phase 0 used to be a partial entry gate. Two of
the ceremony's prerequisites -- the next line's `(vX.Y.Z)` milestone and an
active/upcoming `Sprint` iteration -- were only checked in **Phase 4**, i.e.
after master had been fast-forwarded, the tag and GitHub Release published and
`stable` pushed to PyPI. A missing one therefore surfaced as a half-finished
ceremony with the agent flags paused, not as an up-front "not ready" (runs
37569589877 / 37569759241).

Owner requirement 2026-10-07: Phase 0 checks EVERY prerequisite for all five
phases, reports all gaps in one pass, and **puts in place whatever is
missing** -- so a dispatched ceremony either completes or stops before
anything irreversible.

Two classes, and only two:

* **provisionable** -- automation can create it (the next-line milestone, the
  next sprint iteration, this release's draft release, the release issue's
  `Release Management` label and milestone). Created, then re-verified by
  query.
* **gate-only** -- automation cannot create it, and must not pretend to (open
  issues in the release milestone, unchecked release-issue tasks, open
  critical/high code-scanning alerts, a `pyproject.toml` version mismatch, a
  tag that already exists, missing ceremony/tap/Slack wiring). The run stops
  before Phase 1 with EVERY gate failure listed.

No network calls live here on purpose: the ceremony gathers facts with `gh`
and hands them over as JSON, so the math -- next patch version, placeholder
milestone title, next sprint title/start/duration, the iteration resubmit
payload, and the gate decision -- is unit-testable without mocking GitHub.

## The iteration hazard, and why the resubmit payload is built here

The only API for adding an iteration is
`updateProjectV2Field(iterationConfiguration: ...)`, which **replaces the
whole iteration list**. The same mutation family with `singleSelectOptions`
wiped Status on all 1018 board items on 2026-08-10, so #4166 required this be
proven on a throwaway project before any code touched project 2.

Proven 2026-10-08 on a scratch user project (reproducible with
`scripts/sprint-iteration-preservation-proof.sh`; transcript in the PR):

* resubmitting **every** iteration **with its `id`**, plus the new one without
  an id, preserves every item's iteration value exactly -- including items
  assigned to *completed* iterations;
* omitting the ids creates fresh iterations and **wipes all four** test items'
  values;
* keeping the ids but omitting `completedIterations` -- which the GraphQL
  `configuration { iterations }` field does not return -- wipes exactly the
  items assigned to those completed iterations.

So `iteration_resubmit` is the load-bearing part: it concatenates
`completedIterations` + `iterations` (each carrying its id) + the new
iteration, and `next_sprint_plan` returns that payload rather than just the
new iteration. Building it in one tested place is what stops a future caller
from reconstructing the list and silently dropping half of it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, timedelta
from typing import Any

#: The next line is named by the lowest open milestone whose title carries a
#: `(vX.Y.Z)` above the release being shipped. Phase 4 has derived it this way
#: since D-061; Phase 0's placeholder has to match the same pattern or Phase 4
#: would not pick it up.
_MILESTONE_VERSION_RE = re.compile(r"\(v(\d+)\.(\d+)\.(\d+)\)")

#: `Sprint 12` -> 12. The project's iterations are numbered, and the next one
#: continues that numbering rather than restarting it.
_SPRINT_NUMBER_RE = re.compile(r"\bSprint\s+(\d+)\b", re.IGNORECASE)

#: A placeholder is a stand-in for a decision the owner has not made yet, and
#: says so in its title. Phase 0 creates it so the ceremony can finish; the
#: owner renames and re-scopes it afterwards.
PLACEHOLDER_PREFIX = "Placeholder — next line"


# --------------------------------------------------------------------------
# version / milestone derivations
# --------------------------------------------------------------------------
def parse_version(version: str) -> tuple[int, int, int]:
    """'3.0.1' -> (3, 0, 1). Raises ValueError on anything else."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version.strip().lstrip("v"))
    if not m:
        raise ValueError(f"not an x.y.z version: {version!r}")
    major, minor, patch = (int(x) for x in m.groups())
    return major, minor, patch


def next_patch_version(version: str) -> str:
    """The next patch line after `version`: '3.0.1' -> '3.0.2'.

    Patch, not minor: the placeholder exists only so the ceremony has a line
    to cut, and the smallest possible step is the one least likely to collide
    with whatever the owner actually intends next.
    """
    major, minor, patch = parse_version(version)
    return f"{major}.{minor}.{patch + 1}"


def placeholder_milestone_title(version: str) -> str:
    """The title Phase 0 gives the placeholder milestone it creates.

    It must carry `(vX.Y.Z)` so `pick_next_line` finds it, and must read as a
    placeholder so the owner knows it is theirs to rename.
    """
    return f"{PLACEHOLDER_PREFIX} (v{next_patch_version(version)})"


def is_placeholder_title(title: str) -> bool:
    """Did the ceremony create this milestone, rather than the owner?"""
    return title.startswith(PLACEHOLDER_PREFIX)


def title_version(title: str) -> tuple[int, int, int] | None:
    """The version a milestone title names as `(vX.Y.Z)`, or None.

    One definition, used by every caller: `pick_next_line` (which line comes
    next) and `milestone_for_version` (which milestone names a given line)
    must agree about what version a title carries, or `--next-branch` and the
    derivation could pick different milestones for the same branch.
    """
    m = _MILESTONE_VERSION_RE.search(title)
    if not m:
        return None
    major, minor, patch = (int(x) for x in m.groups())
    return major, minor, patch


def milestone_for_version(titles: list[str], version: str) -> str | None:
    """The open milestone naming exactly `version` as `(vX.Y.Z)`, or None.

    `--next-branch` overrides the derivation, so the milestone that matters is
    the one naming THAT version rather than whatever the derivation picked --
    an explicit branch that took its milestone title from the derivation would
    mis-title the next line's release issue and draft.

    Matched on the anchored `(vX.Y.Z)` form, deliberately: a bare substring
    search also hits a longer patch number that merely starts with the one
    wanted (`(vX.Y.ZN)`), and any prose mention of the version anywhere in a
    title -- so the ceremony could adopt a milestone that Phase 4 then parses
    as a different line entirely.
    """
    want = parse_version(version)
    for title in titles:
        if title_version(title) == want:
            return title
    return None


def pick_next_line(titles: list[str], version: str) -> tuple[str, str] | None:
    """The next line's branch and milestone title, or None if nothing qualifies.

    The lowest `(vX.Y.Z)` strictly above `version` among the open milestone
    titles. This is Phase 4's derivation (D-061), extracted so Phase 0 can run
    the identical one before anything irreversible happens -- two copies of it
    would be two chances for Phase 0 to pass and Phase 4 to fail.
    """
    current = parse_version(version)
    best: tuple[tuple[int, int, int], str] | None = None
    for title in titles:
        found = title_version(title)
        if found is None:
            continue
        if found > current and (best is None or found < best[0]):
            best = (found, title)
    if best is None:
        return None
    return "v" + ".".join(str(x) for x in best[0]), best[1]


def next_release_title(milestone_title: str) -> str:
    """The suffix for the next line's release issue / draft release name.

    The milestone title minus the `(vX.Y.Z)` and any `Phase N —` prefix --
    Phase 4's existing `sed`, in a form the tests can reach.
    """
    out = re.sub(r"\s*\(v\d+\.\d+\.\d+\)\s*$", "", milestone_title)
    out = re.sub(r"^Phase\s+[0-9.]+\s*[—-]\s*", "", out)
    return out.strip()


# --------------------------------------------------------------------------
# sprint iteration derivations
# --------------------------------------------------------------------------
def sprint_number(title: str) -> int | None:
    """`Sprint 12` -> 12; anything else -> None."""
    m = _SPRINT_NUMBER_RE.search(title)
    return int(m.group(1)) if m else None


def _iteration_end_exclusive(iteration: dict[str, Any]) -> date:
    """The day AFTER an iteration's last day.

    GitHub lays iterations out contiguously: an iteration starting on
    `startDate` with duration `d` is followed by one starting on
    `startDate + d` (verified on the scratch project -- Sprint 3 started
    2026-10-06 with duration 18 and Sprint 4 started 2026-10-24). So
    `startDate + duration` is both "the day after this one ends" and "the
    natural start of the next one".
    """
    return date.fromisoformat(iteration["startDate"]) + timedelta(
        days=int(iteration.get("duration") or 0)
    )


def iteration_resubmit(
    configuration: dict[str, Any], new_iterations: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The `iterations` list to hand `updateProjectV2Field`.

    EVERY existing iteration -- completed ones included -- carrying its `id`,
    in start-date order, followed by the new one(s). Dropping an id recreates
    that iteration and clears every item pointing at it; dropping a completed
    iteration clears exactly the items in it. Both were reproduced on a
    throwaway project (see the module docstring); this function is the only
    place that list is built.
    """
    existing = [
        {
            "id": it["id"],
            "title": it["title"],
            "startDate": it["startDate"],
            "duration": int(it["duration"]),
        }
        for it in list(configuration.get("completedIterations") or [])
        + list(configuration.get("iterations") or [])
        if it.get("id")
    ]
    existing.sort(key=lambda it: it["startDate"])
    return existing + list(new_iterations)


def next_sprint_plan(
    configuration: dict[str, Any], today: date, title_prefix: str = "Sprint"
) -> dict[str, Any]:
    """Whether a sprint must be created, and the exact mutation payload if so.

    `configuration` is the GraphQL `ProjectV2IterationField.configuration`
    object: `duration`, `startDay`, `iterations` (active + upcoming ONLY) and
    `completedIterations`. A non-empty `iterations` means the line gate is
    already satisfied, so nothing is created -- the ceremony never adds a
    sprint the project does not need.

    Otherwise: title `<prefix> <N+1>` continuing the highest number seen in
    any existing iteration, the field's own default `duration`, starting the
    day after the last iteration ends -- or today if that date has already
    passed, which is the normal case when every iteration is completed.
    """
    active = list(configuration.get("iterations") or [])
    completed = list(configuration.get("completedIterations") or [])
    all_iterations = completed + active

    if active:
        return {
            "needed": False,
            "reason": "an active or upcoming Sprint iteration already exists",
            "existing": [it.get("title", "") for it in active],
        }

    duration = int(configuration.get("duration") or 0)
    if duration <= 0:
        # Without the field's own default there is no honest duration to pick,
        # and inventing one would silently reshape the owner's cadence.
        return {
            "needed": True,
            "provisionable": False,
            "reason": "the Sprint field reports no default duration",
        }

    numbers = [n for n in (sprint_number(it.get("title", "")) for it in all_iterations) if n]
    number = (max(numbers) + 1) if numbers else 1

    start = today
    if all_iterations:
        after_last = max(_iteration_end_exclusive(it) for it in all_iterations)
        if after_last > today:
            start = after_last

    new = {
        "title": f"{title_prefix} {number}",
        "startDate": start.isoformat(),
        "duration": duration,
    }
    # The config anchor: keep the earliest existing iteration's start date so
    # the field's own cadence is not re-anchored by a provisioning run.
    anchor = min((it["startDate"] for it in all_iterations), default=new["startDate"])
    return {
        "needed": True,
        "provisionable": True,
        "reason": "no active or upcoming Sprint iteration",
        "new": new,
        "startDate": anchor,
        "duration": duration,
        "iterations": iteration_resubmit(configuration, [new]),
    }


#: What the owner has to click when a sprint cannot be provisioned. A gate
#: failure that does not say how to clear it is a stop with no exit.
SPRINT_MANUAL_STEPS = (
    "Project > Settings > Sprint (iteration field) > 'Add iteration', "
    "then keep the field's default duration. Do NOT delete or re-create the "
    "existing iterations: that clears the Sprint value on every item assigned "
    "to them."
)


# --------------------------------------------------------------------------
# the inventory report and its decision
# --------------------------------------------------------------------------
GATE = "gate"
PROVISION = "provision"


def decide(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Split an inventory into what blocks the run and what Phase 0 will create.

    Reports ALL gaps: the point of the inventory is that one dispatch tells
    the owner everything that is not ready, instead of stopping at the first
    thing and discovering the next one on the re-run.
    """
    gate_failures = [f for f in findings if f.get("kind") == GATE and not f.get("ok")]
    to_provision = [f for f in findings if f.get("kind") == PROVISION and not f.get("ok")]
    unknown = sorted({f.get("kind", "") for f in findings} - {GATE, PROVISION})
    if unknown:
        raise ValueError(
            f"prerequisite kinds must be '{GATE}' or '{PROVISION}', got: {', '.join(unknown)}"
        )
    return {
        "ok": not gate_failures,
        "gate_failures": gate_failures,
        "provision": to_provision,
        "checked": len(findings),
    }


def render(findings: list[dict[str, Any]], decision: dict[str, Any]) -> str:
    """The Phase 0 inventory, in the ceremony's own log format."""
    lines = []
    for f in findings:
        if f.get("ok"):
            lines.append(f"  ok: {f.get('detail', '')}")
        elif f.get("kind") == GATE:
            lines.append(f"  GATE FAIL [{f.get('key', '?')}]: {f.get('detail', '')}")
        else:
            lines.append(f"  PROVISION [{f.get('key', '?')}]: {f.get('detail', '')}")
    lines.append(
        f"  inventory: {decision['checked']} prerequisite(s) checked, "
        f"{len(decision['gate_failures'])} gate failure(s), "
        f"{len(decision['provision'])} to provision"
    )
    if decision["gate_failures"]:
        lines.append("  Phase 0 STOPS -- these cannot be provisioned by automation:")
        lines += [
            f"    - [{f.get('key', '?')}] {f.get('detail', '')}" for f in decision["gate_failures"]
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI -- the shape release_ceremony.sh consumes
# --------------------------------------------------------------------------
def _cmd_next_patch(args: argparse.Namespace) -> int:
    print(next_patch_version(args.version))
    return 0


def _cmd_placeholder_title(args: argparse.Namespace) -> int:
    print(placeholder_milestone_title(args.version))
    return 0


def _cmd_next_line(args: argparse.Namespace) -> int:
    titles = json.load(sys.stdin)
    picked = pick_next_line(titles, args.version)
    if picked is None:
        return 0
    branch, title = picked
    print(f"{branch}\t{title}")
    return 0


def _cmd_milestone_for(args: argparse.Namespace) -> int:
    titles = json.load(sys.stdin)
    title = milestone_for_version(titles, args.version)
    if title is None:
        return 0
    print(title)
    return 0


def _cmd_next_release_title(args: argparse.Namespace) -> int:
    print(next_release_title(args.milestone))
    return 0


def _cmd_sprint_plan(args: argparse.Namespace) -> int:
    configuration = json.load(sys.stdin) or {}
    today = date.fromisoformat(args.today) if args.today else date.today()
    print(json.dumps(next_sprint_plan(configuration, today)))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    findings = json.load(sys.stdin) or []
    decision = decide(findings)
    print(render(findings, decision))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(decision, fh)
    return 0 if decision["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("next-patch", help="the next patch version after VERSION")
    p.add_argument("version")
    p.set_defaults(func=_cmd_next_patch)

    p = sub.add_parser("placeholder-title", help="the placeholder milestone title for VERSION")
    p.add_argument("version")
    p.set_defaults(func=_cmd_placeholder_title)

    p = sub.add_parser("next-line", help="branch<TAB>milestone for the line above VERSION")
    p.add_argument("version")
    p.set_defaults(func=_cmd_next_line)

    p = sub.add_parser("milestone-for", help="the open milestone naming exactly VERSION")
    p.add_argument("version")
    p.set_defaults(func=_cmd_milestone_for)

    p = sub.add_parser("next-release-title", help="release-issue suffix from a milestone title")
    p.add_argument("milestone")
    p.set_defaults(func=_cmd_next_release_title)

    p = sub.add_parser("sprint-plan", help="whether to create a sprint, and the payload")
    p.add_argument("--today", default=None, help="YYYY-MM-DD (default: today)")
    p.set_defaults(func=_cmd_sprint_plan)

    p = sub.add_parser("report", help="render an inventory; exit 1 on any gate failure")
    p.add_argument("--json", default=None, help="also write the decision to this file")
    p.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
