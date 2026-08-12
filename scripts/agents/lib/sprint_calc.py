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
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parked_resume import build_gate_lines  # noqa: E402

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
        return int(remaining) if remaining > 0 else 0
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
    "complete" once it hits zero -- the sprint autopilot's stop condition.

    The count handed in is the ACTIVE SPRINT's open Backlog count, not the
    release's (owner policy 2026-08-10, #3706): the automatic loop is bound
    by the current sprint, so a release that still has work queued in a
    future sprint parks here rather than pulling it forward.
    """
    return "continue" if remaining_open_backlog > 0 else "complete"


# Park states (#3709). The pre-#3709 decision counted only open *Backlog*
# issues, so a sprint with an empty Backlog but issues still In Progress /
# In Review announced "sprint complete" while work was demonstrably
# unfinished. The owner's definition (2026-08-10, #3709): "the sprint isn't
# done until all agentic work is complete AND in For Release status" -- so
# completion is two distinct states, and only the second is sprint
# completion.
PARK_CONTINUE = "continue"  # eligible Backlog work left -- kick, don't park
PARK_WORK_IN_FLIGHT = "work_in_flight"  # Backlog empty, but issues still open
PARK_AWAITING_ACCEPTANCE = "awaiting_acceptance"  # all closed, not all accepted
PARK_SPRINT_COMPLETE = "sprint_complete"  # every item accepted to For Release
PARK_EMPTY = "empty"  # no items in the sprint at all


def sprint_park_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Classifies the active sprint's population into a park state.

    Payload keys:
      open, closed        {status name or "": [issue numbers]} for the
                          active sprint's OPEN and CLOSED issues
      status_backlog      the Backlog status value (default "Backlog")
      status_for_release  the For Release status value (default "For Release")

    Returns the state plus the numbers behind it, so the caller can report
    what it is parked in front of instead of just announcing a verdict.
    """
    open_by_status: dict[str, list[int]] = {
        str(k): [int(n) for n in v] for k, v in (payload.get("open") or {}).items()
    }
    closed_by_status: dict[str, list[int]] = {
        str(k): [int(n) for n in v] for k, v in (payload.get("closed") or {}).items()
    }
    status_backlog = payload.get("status_backlog") or "Backlog"
    status_for_release = payload.get("status_for_release") or "For Release"

    backlog_open = len(open_by_status.get(status_backlog, []))
    open_total = sum(len(v) for v in open_by_status.values())
    accepted = sorted(closed_by_status.get(status_for_release, []))
    awaiting_acceptance = sorted(
        n for s, nums in closed_by_status.items() if s != status_for_release for n in nums
    )
    closed_total = len(accepted) + len(awaiting_acceptance)

    if autopilot_decision(backlog_open) == "continue":
        state = PARK_CONTINUE
    elif open_total > 0:
        # The #3709 defect: this used to be indistinguishable from "sprint
        # complete". In Progress / In Review work is real, unfinished work.
        state = PARK_WORK_IN_FLIGHT
    elif closed_total == 0:
        state = PARK_EMPTY
    elif awaiting_acceptance:
        state = PARK_AWAITING_ACCEPTANCE
    else:
        state = PARK_SPRINT_COMPLETE

    return {
        "state": state,
        "backlog_open": backlog_open,
        "open_total": open_total,
        "open_by_status": {s: len(v) for s, v in open_by_status.items() if v},
        "open_issues_by_status": {s: sorted(v) for s, v in open_by_status.items() if v},
        "awaiting_acceptance": awaiting_acceptance,
        "accepted": accepted,
    }


def _sprint_sort_key(title: str) -> tuple[int, int | str, str]:
    """Orders sprint buckets for the park note: numbered sprints in numeric
    order ("Sprint 9" before "Sprint 10", which a plain string sort gets
    backwards), then any non-numbered titles, then the no-sprint bucket."""
    if not title:
        return (2, 0, "")
    m = re.search(r"(\d+)", title)
    if m:
        return (0, int(m.group(1)), title)
    return (1, title, title)


# Machine marker for autopilot notes that are INFORMATIONAL (park notes,
# paused notices) rather than dispatch kicks. `notify_scrum_ready.yml`
# negates this marker in its job `if:`, so such a note can never trigger a
# dispatch run even if its prose later drifts back to naming the kick token
# (#3706 review finding). Belt and braces: informational notes also avoid
# writing the literal kick token at all -- see `_kick_token_ref()`.
AUTOPILOT_INFO_MARKER = "<!-- nyxgpt-autopilot-informational -->"


# How an informational note refers to the manual kick signal WITHOUT
# spelling it out. `notify_scrum_ready.yml` triggers on a bare
# `contains(comment.body, "<kick token>")` substring test with the agent
# accounts on its actor allowlist, so a note that names the token dispatches
# work merely by being posted -- which is how a "park" became a kick (#3706
# review). Never inline the token here, at source or by concatenation: what
# matters is the RENDERED body.
KICK_SIGNAL_REF = "the manual kick signal documented in `docs/sprint-autopilot.md`"


def _issue_list(numbers: list[int]) -> str:
    return ", ".join(f"#{n}" for n in numbers)


def _park_headline(event_phrase: str, sprint_title: str, park_state: dict[str, Any]) -> list[str]:
    """The state-specific opening of the park note (#3709).

    Owner definition (2026-08-10, #3709): "The sprint isn't done until all
    agentic work is complete AND in For Release status." Only
    PARK_SPRINT_COMPLETE may call the sprint done; the agentic-work-complete
    state says exactly that and names what is awaiting the owner's pass.
    Promotion to For Release stays owner-only -- agents never self-promote.
    """
    state = park_state.get("state") or ""
    lines: list[str] = []

    if state == PARK_WORK_IN_FLIGHT:
        by_status = park_state.get("open_by_status") or {}
        breakdown = ", ".join(f"{s or 'no status'}: {c}" for s, c in sorted(by_status.items()))
        lines.append(
            f"⏸️ **Sprint Autopilot — parked, work still in flight**: {event_phrase}. "
            f'**"{sprint_title}" has no open Backlog issues**, but '
            f"{park_state.get('open_total', 0)} issue(s) are still open ({breakdown}) — "
            f"so this is **not** sprint completion."
        )
        lines.append("")
        lines.append(
            "Autopilot has nothing eligible to dispatch (dispatch draws from Backlog), "
            "so it parks here. The in-flight issues finish through their own runs; each "
            "merge kicks this loop again, and the parked-issue scan below resumes any "
            "issue whose dependency gates have since closed."
        )
        return lines

    if state == PARK_AWAITING_ACCEPTANCE:
        awaiting = [int(n) for n in (park_state.get("awaiting_acceptance") or [])]
        lines.append(
            f"🏁 **Sprint Autopilot — agentic work complete; awaiting owner acceptance**: "
            f'{event_phrase}. Every issue in **"{sprint_title}"** is closed and no '
            f'in-flight PRs or runs remain. **This is not "sprint complete."**'
        )
        lines.append("")
        lines.append(
            f"**Awaiting the owner's acceptance pass ({len(awaiting)}):** "
            f"{_issue_list(awaiting)}"
        )
        lines.append("")
        lines.append(
            "The sprint is done only once every item has been accepted and moved to "
            "**For Release** (owner definition, 2026-08-10, #3709). That promotion is "
            "owner-only (the acceptance sweep / `promote_accepted_features` tooling) — "
            "agents never self-promote items to For Release, and the next sprint's work "
            "does not begin before this point."
        )
        return lines

    if state == PARK_SPRINT_COMPLETE:
        accepted = [int(n) for n in (park_state.get("accepted") or [])]
        lines.append(
            f"✅ **Sprint Autopilot — sprint complete**: {event_phrase}. Every item in "
            f'**"{sprint_title}"** has been accepted by the owner and is in **For '
            f"Release** ({len(accepted)} item(s): {_issue_list(accepted)})."
        )
        lines.append("")
        lines.append(
            "Autopilot stays parked at the sprint boundary: crossing into the next "
            "sprint needs a planning event or a human kick."
        )
        return lines

    if state == PARK_EMPTY:
        lines.append(
            f'⏸️ **Sprint Autopilot — parked**: {event_phrase}. **"{sprint_title}"** has '
            f"no items at all, so there is nothing to work and nothing to accept."
        )
        return lines

    # No park_state supplied (or an unrecognized one): fall back to a
    # verdict-free parked note rather than claiming a completion state the
    # data does not support -- the exact #3709 defect.
    lines.append(
        f"⏸️ **Sprint Autopilot — parked**: {event_phrase}. "
        f'**"{sprint_title}" has no open Backlog issues left**, so autopilot is parked '
        f"at the sprint boundary."
    )
    return lines


def build_rc_publish_lines(rc_publish: dict[str, Any]) -> list[str]:
    """The release-candidate section of the park note (#3729).

    The autopilot cuts a candidate when the sprint reaches
    agentic-work-complete, because that park *is* the start of an acceptance
    round. The note's job here is to answer the owner's only question at
    that moment -- "what do I install to test this round?" -- with a version,
    not a link to a run whose number nobody has read yet.

    Renders nothing at all for a park that publishes no candidate (`{}`),
    which is every other park state.
    """
    if not rc_publish:
        return []

    version = str(rc_publish.get("version") or "")
    branch = str(rc_publish.get("branch") or "")
    runs_url = str(rc_publish.get("runs_url") or "")
    reason = str(rc_publish.get("reason") or "")
    lines: list[str] = []

    if rc_publish.get("dispatched"):
        named = f"`{version}`" if version else "the next candidate (`rcN`, resolved by the run)"
        where = f" from `{branch}`" if branch else ""
        watch = f" ([watch it]({runs_url}))" if runs_url else ""
        lines.append(
            f"📦 **Release candidate for this acceptance round:** {named} is publishing "
            f"now{where} — PyPI plus the Homebrew `-rc` formulas, in one run{watch}."
        )
    elif rc_publish.get("error"):
        why = f" ({reason})" if reason else ""
        lines.append(
            f"⚠️ **Release candidate:** the automatic `rc` publish could not be "
            f"dispatched{why}. Publish one by hand with `nyxgpt release publish "
            f"--channel rc --publish` before starting the acceptance round."
        )
    else:
        named = f"`{version}`" if version else "the last published candidate"
        why = f" ({reason})" if reason else ""
        lines.append(
            f"📦 **Release candidate for this acceptance round:** {named} — no new "
            f"candidate was cut{why}."
        )

    if version:
        lines.append("")
        lines.append("```bash")
        lines.append(f"pip install nyxgpt=={version}")
        lines.append("brew tap dkblinux98/nyxgpt && brew install nyxgpt-api-rc nyxgpt-web-rc")
        lines.append("```")
    lines.append("")
    lines.append(
        "A candidate is **not** a release: `pip install nyxgpt` and `brew install "
        "nyxgpt-api` keep resolving to the latest stable version, and the autopilot "
        "never publishes one (#3729 — a release is cut only by the ceremony)."
    )
    return lines


def build_sprint_park_note(payload: dict[str, Any]) -> str:
    """Renders the loud park note posted on the release tracking issue when
    the active sprint's Backlog pool drains (#3706).

    Payload keys:
      event_phrase     what just happened (e.g. "Issue #123 merged")
      sprint_title     the active sprint that just completed ("" if there is
                       no active iteration at all -- the conservative-stop
                       case)
      release_version  e.g. "v3.0.0" (may be empty)
      by_sprint        {sprint title or "": open Backlog count} for the rest
                       of the release, active sprint included (it is zero by
                       definition when this note is rendered)
      park_state       the sprint_park_state() result (#3709). Omitted or
                       empty renders the pre-#3709 wording, which is only
                       correct for the no-active-sprint conservative stop.
      resume_scan      the parked-issue scan (#3709) as classify_candidates()
                       returns it, plus "selected" -- what was auto-resumed
                       this cycle, and what is still waiting on gates
      rc_publish       what the agentic-work-complete rc dispatch did
                       (#3729), as _autopilot_publish_rc reports it. Empty
                       for every park state that publishes no candidate
    """
    event_phrase = payload.get("event_phrase", "Work completed")
    sprint_title = payload.get("sprint_title") or ""
    release_version = payload.get("release_version") or ""
    park_state: dict[str, Any] = payload.get("park_state") or {}
    resume_scan: dict[str, Any] = payload.get("resume_scan") or {}
    rc_publish: dict[str, Any] = payload.get("rc_publish") or {}
    by_sprint: dict[str, int] = {
        str(k): int(v) for k, v in (payload.get("by_sprint") or {}).items() if int(v) > 0
    }
    # Only exclude the active-sprint bucket when there IS an active sprint.
    # With sprint_title == "" (the conservative-stop case) the `""` key is
    # the *no sprint set* bucket, not the active sprint -- dropping it hid
    # real waiting work from the note and undercounted the total (#3706
    # review).
    remaining_elsewhere = {
        k: v for k, v in by_sprint.items() if not (sprint_title and k == sprint_title)
    }
    total_elsewhere = sum(remaining_elsewhere.values())

    release_label = f"release {release_version}" if release_version else "this release"
    scope_label = "outside the active sprint" if sprint_title else "in a sprint"

    lines: list[str] = []
    if sprint_title:
        lines.extend(_park_headline(event_phrase, sprint_title, park_state))
    else:
        lines.append(
            f"🏁 **Sprint Autopilot — parked**: {event_phrase}. No sprint iteration is "
            f"currently active (no iteration's date window contains today), so there is "
            f"no sprint pool to draw from and autopilot is parked."
        )
    lines.append("")

    # Directly under the headline: at agentic-work-complete the owner's next
    # action is installing the candidate, so it comes before the backlog
    # accounting (#3729).
    rc_lines = build_rc_publish_lines(rc_publish)
    if rc_lines:
        lines.extend(rc_lines)
        lines.append("")

    gate_lines = build_gate_lines(resume_scan, resume_scan.get("selected"))
    if gate_lines:
        lines.append("**Parked-issue scan (auto-resume, #3709):**")
        lines.append("")
        lines.extend(gate_lines)
        lines.append("")

    if total_elsewhere:
        lines.append(f"**Still open in {release_label}, {scope_label}:**")
        lines.append("")
        for title in sorted(remaining_elsewhere, key=_sprint_sort_key):
            label = title if title else "_No sprint set_"
            count = remaining_elsewhere[title]
            lines.append(f"- {label}: {count} open Backlog issue(s)")
        lines.append("")
        lines.append(f"Total waiting {scope_label}: **{total_elsewhere}**.")
    else:
        lines.append(
            f"**Nothing is left in {release_label}'s Backlog either** — the release "
            f"backlog is drained. Merged work is in **Acceptance Testing** for "
            f"stakeholder sign-off. Autopilot resumes when `RELEASE_ISSUE_NUMBER` and "
            f"`RELEASE_BRANCH` point at the next release; it never crosses a release "
            f"boundary on its own."
        )

    lines.append("")
    lines.append(
        "The automatic loop is bound by the **current sprint**, not the release "
        "(owner policy 2026-08-10, #3706): queued work in a future sprint is not "
        "pulled forward without a planning event."
    )
    lines.append("")
    lines.append("**Work resumes when either:**")
    lines.append("")
    if park_state.get("state") == PARK_WORK_IN_FLIGHT:
        # This state is not a boundary stop: the sprint's own open issues
        # drive the loop from here, so say that first (#3709).
        lines.append(
            "- the in-flight issues above finish — every merge kicks this loop again, "
            "and the parked-issue scan resumes anything whose gates have closed, or"
        )
    lines.append(
        "- the next sprint's date window opens (iteration boundaries are evaluated in "
        "`SPRINT_TIMEZONE`, default `America/New_York`) and a kick is posted, or"
    )
    lines.append(
        f"- the owner posts {KICK_SIGNAL_REF} here to pull work forward deliberately "
        f"— a human kick still overrides the sprint boundary."
    )
    lines.append("")
    lines.append(AUTOPILOT_INFO_MARKER)
    return "\n".join(lines)


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
        print(
            "usage: sprint_calc.py "
            "<report|autopilot-decision|sprint-park-state|park-note|huddle-routing>",
            file=sys.stderr,
        )
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
    if cmd == "sprint-park-state":
        payload = json.loads(sys.stdin.read())
        print(json.dumps(sprint_park_state(payload)))
        return 0
    if cmd == "park-note":
        payload = json.loads(sys.stdin.read())
        print(build_sprint_park_note(payload))
        return 0
    if cmd == "huddle-routing":
        disagreement_type, request_changes_count = argv[1], int(argv[2])
        print(huddle_routing_decision(disagreement_type, request_changes_count))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
