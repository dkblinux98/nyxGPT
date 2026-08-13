#!/usr/bin/env python3
"""Post-review handoff planning for the dispatch-mode backstop (#3704).

`review_agent_auto_review.yml` executes a review verdict only in reaction to
a *downstream* event: the `pull_request_review` submission, or the
`nyxgpt-structured-review` comment as an `issue_comment` fallback. When
`claude-code-review.yml` is invoked via `workflow_dispatch` (the recovery
path for PRs whose automatic review never fired), the review run itself has
no way to observe whether either link actually fired -- and when one drops,
a REQUEST_CHANGES verdict simply goes nowhere. Observed 2026-08-09/10 on
PRs #3684, #3683 and #3606: verdicts posted, zero fix activity for 9+ hours
until a human posted `RETRY_IMPLEMENTATION` by hand.

This module holds the decision half of the backstop:
`scripts/agents/review_ensure_handoff.sh` gathers PR reviews and comments via
`gh` and hands them here as JSON, so "did the handoff already happen, and if
not what should happen" is unit-testable without mocking the GitHub API --
the same split `sprint_calc.py` uses for the sprint math.

Routing itself is NOT re-implemented here: `huddle_routing_decision` is
imported from `sprint_calc.py` so the backstop and the primary workflow can
never disagree about whether a round loops, huddles, or escalates (#3687).

Since #3736 the *primary* workflow calls `plan_round` here too, for the same
reason: the huddle-state deferral (don't escalate a round a huddle already
resolved, don't trigger a second huddle for one round) has to be the same
decision on both paths, or the race it exists to remove simply moves.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


def _load_sibling(name: str) -> Any:
    """Import a sibling lib module by path (these are scripts, not a package)."""
    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_review_handoff_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sprint_calc = _load_sibling("sprint_calc")
huddle_state = _load_sibling("huddle_state")

huddle_routing_decision = sprint_calc.huddle_routing_decision

#: Substrings that only ever appear in a comment posted by the handoff steps
#: of `review_agent_auto_review.yml` (return-to-developer / huddle /
#: escalation). Seeing any of them on the PR after the verdict was submitted
#: means the event chain did its job and the backstop must stay out of it.
HANDOFF_MARKERS = (
    "review loop",  # "Changes requested (review loop N/3)"
    "HUDDLE_TRIGGERED",  # huddle dispatch marker
    "Escalated after 3 review cycles",  # cycle-limit escalation
    "spec ambiguity",  # type-(c) cycle-zero escalation
)

#: Marker of the machine-readable review payload `claude-code-review.yml`
#: persists after every review ("Persist structured review output"). It is
#: the event chain's *trigger*, never its footprint -- and it embeds the
#: review's free text, so its body can contain HANDOFF_MARKERS substrings by
#: coincidence. `handoff_recorded` must therefore skip it; the primary path
#: excludes it the same way (`review_agent_auto_review.yml` scans only
#: comments newer than the structured comment).
STRUCTURED_REVIEW_MARKER = "nyxgpt-structured-review"

#: Review states that count as a verdict. COMMENTED/DISMISSED reviews are
#: chatter -- they neither approve nor request changes, so they must not
#: shadow the real verdict when picking "the latest one".
_VERDICT_STATES = ("APPROVED", "CHANGES_REQUESTED")

# "**a**: justification" / "**[b]**: ..." -- the a/b/c line the review body
# template asks for under "### Disagreement Type". Used only when the
# structured-review comment carries no machine-readable classification
# (e.g. a dispatched run executing an older workflow definition whose
# --json-schema predates #3687).
_DTYPE_BODY_RE = re.compile(r"^\s*\*\*\[?([abc])\]?\*\*\s*:", re.MULTILINE)

# The "### Disagreement Type" heading the review template emits. When it is
# present the classification is read from *below* it, so an unrelated bold
# `**a**:`-shaped line earlier in the body cannot outrank the real one.
_DTYPE_HEADING_RE = re.compile(r"^#{1,6}\s*Disagreement\s+Type\b.*$", re.MULTILINE | re.IGNORECASE)

_DTYPE_STRUCTURED_RE = re.compile(r'"disagreement_type"\s*:\s*"([abc])"')


def _login(entry: dict[str, Any]) -> str:
    user = entry.get("user") or {}
    return str(user.get("login") or "")


def agent_verdicts(reviews: list[dict[str, Any]], review_agent: str) -> list[dict[str, Any]]:
    """Review-agent APPROVED/CHANGES_REQUESTED reviews, oldest first.

    Sorted by `submitted_at` rather than trusting API order, so a paginated
    fetch that arrives out of order still yields the true latest verdict.
    """
    verdicts = [
        r
        for r in reviews
        if _login(r) == review_agent and str(r.get("state", "")).upper() in _VERDICT_STATES
    ]
    return sorted(verdicts, key=lambda r: str(r.get("submitted_at") or ""))


def latest_verdict(reviews: list[dict[str, Any]], review_agent: str) -> dict[str, Any] | None:
    """The review agent's most recent verdict on the PR, or None."""
    verdicts = agent_verdicts(reviews, review_agent)
    return verdicts[-1] if verdicts else None


def request_changes_count(reviews: list[dict[str, Any]], review_agent: str) -> int:
    """Cumulative REQUEST_CHANGES reviews by the review agent on this PR.

    Mirrors the `gh api .../reviews` count in
    `review_agent_auto_review.yml`'s "Count review iterations" step, so the
    backstop reports the same loop number the primary path would have.
    """
    return sum(
        1
        for r in reviews
        if _login(r) == review_agent and str(r.get("state", "")).upper() == "CHANGES_REQUESTED"
    )


def handoff_recorded(comments: list[dict[str, Any]], since: str) -> bool:
    """True if a handoff comment was posted after `since` (ISO-8601).

    `since` is the verdict's `submitted_at`; comments at or before it belong
    to an earlier review cycle and must not suppress this one's handoff.

    The run's own `nyxgpt-structured-review` comment is skipped: it lands
    seconds after the verdict (so it is always in scan scope) and embeds the
    review's free text, where a phrase like "review loop" or "spec
    ambiguity" -- both ordinary words in this repo's review prose -- would
    otherwise be mistaken for a handoff footprint and stand the backstop
    down while nothing had happened. It is the trigger of the event chain,
    never its footprint.
    """
    for comment in comments:
        created = str(comment.get("created_at") or "")
        if not created or created <= since:
            continue
        body = str(comment.get("body") or "")
        if STRUCTURED_REVIEW_MARKER in body:
            continue
        if any(marker in body for marker in HANDOFF_MARKERS):
            return True
    return False


def _disagreement_section(review_body: str) -> str:
    """The review body from its "Disagreement Type" heading onwards.

    Returns the whole body when the heading is absent, so a review that
    states the classification without the template's heading still parses.
    """
    match = _DTYPE_HEADING_RE.search(review_body)
    return review_body[match.end() :] if match else review_body


def disagreement_type(
    comments: list[dict[str, Any]],
    review_body: str = "",
) -> str:
    """The #3687 a/b/c classification for the current REQUEST_CHANGES round.

    Prefers the machine-readable value in the most recent
    `nyxgpt-structured-review` comment (identical to what
    `review_agent_auto_review.yml` reads), then falls back to parsing the
    review body's "Disagreement Type" line, then to "a" -- the pre-#3687
    default, which loops as before rather than over-escalating.

    Only the newest structured comment is consulted: it belongs to the
    round being planned, so when it carries no classification the current
    review body must win rather than an earlier cycle's stale value.
    """
    structured = sorted(
        (c for c in comments if STRUCTURED_REVIEW_MARKER in str(c.get("body") or "")),
        key=lambda c: str(c.get("created_at") or ""),
    )
    if structured:
        match = _DTYPE_STRUCTURED_RE.search(str(structured[-1].get("body") or ""))
        if match:
            return match.group(1)

    match = _DTYPE_BODY_RE.search(_disagreement_section(review_body or ""))
    if match:
        return match.group(1)

    return "a"


def effective_request_changes_count(
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    review_agent: str,
) -> int:
    """REQUEST_CHANGES reviews that count toward the 3-cycle breaker (#3736).

    A huddle decision of proceed / change-approach / descope *re-arms* the
    fix cycle: the disagreement was resolved by the huddle, so the rounds
    that led to it are history, not evidence that the loop is stuck.
    Reviews submitted after the decision are the only ones counted. Without
    this, a huddle that concluded "proceed" was followed straight into the
    cycle-limit escalation it had just argued against (PR #3733).

    With no resumable decision on the PR this is the plain cumulative count,
    identical to `request_changes_count`.
    """
    decision = huddle_state.latest_decision(comments)
    if not decision or decision["decision"] not in huddle_state.RESUMABLE_DECISIONS:
        return request_changes_count(reviews, review_agent)

    since = str(decision["created_at"])
    return sum(
        1
        for r in reviews
        if _login(r) == review_agent
        and str(r.get("state", "")).upper() == "CHANGES_REQUESTED"
        and str(r.get("submitted_at") or "") > since
    )


def plan_round(
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    review_agent: str,
    review_body: str = "",
) -> dict[str, Any]:
    """Route the current REQUEST_CHANGES round, deferring to the huddle (#3736).

    This is `huddle_routing_decision` plus the state of the huddle that the
    counter alone cannot see:

    - **huddle pending** (a `HUDDLE_TRIGGERED` this round with no decision
      yet): nothing happens. Not another trigger, not an escalation -- the
      huddle owns the round until it decides. This is also what deduplicates
      the trigger, since the second run of the same verdict sees the first
      run's marker.
    - **huddle escalated**: the mediation run already handed the issue to
      the owner with the standard primitives; a second escalation here would
      only spam the thread.
    - **huddle resumed** (proceed / change-approach / descope): the cycle
      counter is re-armed (`effective_request_changes_count`) and the round
      routes as an ordinary fix cycle. A round that has already huddled
      never huddles twice -- including type (b), which otherwise huddles
      unconditionally.

    `action` is one of "none" / "return_to_developer" / "huddle" /
    "escalate"; `route` carries the finer-grained reason.
    """
    status = huddle_state.huddle_status(comments)
    count = request_changes_count(reviews, review_agent)
    effective = effective_request_changes_count(reviews, comments, review_agent)
    dtype = disagreement_type(comments, review_body)

    plan: dict[str, Any] = {
        "disagreement_type": dtype,
        "request_changes_count": count,
        "effective_count": effective,
        "loop_number": effective + 1,
        "huddle_decision": status["decision"],
        "huddle_pending": status["pending"],
    }

    if status["pending"]:
        plan.update(action="none", route="defer_huddle_pending", reason="huddle-pending")
        return plan

    if status["escalated"]:
        plan.update(action="none", route="defer_huddle_escalated", reason="huddle-escalated")
        return plan

    route = huddle_routing_decision(dtype, effective)
    if route == "huddle" and status["triggered"]:
        # This round already huddled and got its answer -- run the fix cycle
        # the decision called for instead of huddling about it again.
        route = "normal"

    plan["route"] = route
    if route == "escalate_spec_ambiguity":
        plan.update(action="escalate", escalate_reason="spec_ambiguity")
    elif route == "escalate_cycle_limit":
        plan.update(action="escalate", escalate_reason="cycle_limit")
    elif route == "huddle":
        plan.update(action="huddle")
    else:
        plan.update(action="return_to_developer")

    return plan


def plan_handoff(
    reviews: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    review_agent: str,
) -> dict[str, Any]:
    """What the dispatch-mode backstop should do for this PR.

    Returns a dict with an `action` of:

    - "none": nothing to repair -- no verdict yet, the latest verdict was an
      APPROVE (that path already works), or a handoff comment already
      landed after the verdict. `reason` says which.
    - "return_to_developer": start the dev agent's review-fix cycle.
      `loop_number` is the number the primary path would print in its
      "review loop N/3" comment (`request_changes_count + 1`) -- mirrored
      rather than corrected so both paths label the same cycle identically.
    - "huddle": post the huddle trigger instead of another blind fix cycle.
    - "escalate": hand to the human owner; `escalate_reason` is
      "spec_ambiguity" or "cycle_limit".

    Routing (including the #3736 huddle-state deferral) comes from
    `plan_round`, shared with the primary workflow.
    """
    verdict = latest_verdict(reviews, review_agent)
    if verdict is None:
        return {"action": "none", "reason": "no-verdict"}

    if str(verdict.get("state", "")).upper() != "CHANGES_REQUESTED":
        return {"action": "none", "reason": "latest-verdict-approved"}

    submitted_at = str(verdict.get("submitted_at") or "")
    if handoff_recorded(comments, submitted_at):
        return {"action": "none", "reason": "handoff-already-recorded"}

    return plan_round(reviews, comments, review_agent, str(verdict.get("body") or ""))


def _shell_lines(plan: dict[str, Any]) -> str:
    """Render a plan as `key=value` lines for `eval` in the bash caller.

    Values are constrained by construction (enum-ish strings, ints and
    bools), so no quoting/escaping is needed or done. Bools render as
    true/false: the caller writes these straight into `$GITHUB_OUTPUT`,
    where step conditions compare against those spellings.
    """
    keys = (
        "action",
        "reason",
        "route",
        "escalate_reason",
        "disagreement_type",
        "request_changes_count",
        "effective_count",
        "loop_number",
        "huddle_decision",
        "huddle_pending",
    )
    rendered = []
    for key in keys:
        value = plan.get(key, "")
        if isinstance(value, bool):
            value = "true" if value else "false"
        rendered.append(f"{key}={value}")
    return "\n".join(rendered)


def _main(argv: list[str]) -> int:
    usage = (
        "usage: review_handoff.py <plan|plan-round> <review_agent>  # PR JSON on stdin\n"
        "       review_handoff.py handoff-recorded <since>          # PR JSON on stdin"
    )
    if len(argv) != 2 or argv[0] not in ("plan", "plan-round", "handoff-recorded"):
        print(usage, file=sys.stderr)
        return 2

    payload = json.loads(sys.stdin.read())
    reviews = payload.get("reviews") or []
    comments = payload.get("comments") or []

    if argv[0] == "plan":
        print(_shell_lines(plan_handoff(reviews, comments, argv[1])))
        return 0
    if argv[0] == "plan-round":
        print(_shell_lines(plan_round(reviews, comments, argv[1])))
        return 0
    if argv[0] == "handoff-recorded":
        # Used by review_agent_auto_review.yml's structured-comment fallback
        # to detect that the pull_request_review path already executed this
        # verdict. Sharing HANDOFF_MARKERS with the backstop is the fix for
        # the fallback missing HUDDLE_TRIGGERED entirely and posting a
        # second huddle trigger (#3736, PR #3728).
        print("true" if handoff_recorded(comments, argv[1]) else "false")
        return 0

    print(usage, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
