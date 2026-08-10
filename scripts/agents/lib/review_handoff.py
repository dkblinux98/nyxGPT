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
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

_SPRINT_CALC_PATH = Path(__file__).resolve().parent / "sprint_calc.py"
_spec = importlib.util.spec_from_file_location("_review_handoff_sprint_calc", _SPRINT_CALC_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive
    raise ImportError(f"cannot load sprint_calc from {_SPRINT_CALC_PATH}")
sprint_calc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sprint_calc
_spec.loader.exec_module(sprint_calc)

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
    """
    for comment in comments:
        created = str(comment.get("created_at") or "")
        if not created or created <= since:
            continue
        body = str(comment.get("body") or "")
        if any(marker in body for marker in HANDOFF_MARKERS):
            return True
    return False


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
    """
    structured = [
        str(c.get("body") or "")
        for c in comments
        if "nyxgpt-structured-review" in str(c.get("body") or "")
    ]
    for body in reversed(structured):
        match = _DTYPE_STRUCTURED_RE.search(body)
        if match:
            return match.group(1)

    match = _DTYPE_BODY_RE.search(review_body or "")
    if match:
        return match.group(1)

    return "a"


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
    """
    verdict = latest_verdict(reviews, review_agent)
    if verdict is None:
        return {"action": "none", "reason": "no-verdict"}

    if str(verdict.get("state", "")).upper() != "CHANGES_REQUESTED":
        return {"action": "none", "reason": "latest-verdict-approved"}

    submitted_at = str(verdict.get("submitted_at") or "")
    if handoff_recorded(comments, submitted_at):
        return {"action": "none", "reason": "handoff-already-recorded"}

    count = request_changes_count(reviews, review_agent)
    dtype = disagreement_type(comments, str(verdict.get("body") or ""))
    route = huddle_routing_decision(dtype, count)

    plan: dict[str, Any] = {
        "disagreement_type": dtype,
        "request_changes_count": count,
        "loop_number": count + 1,
        "route": route,
    }

    if route == "escalate_spec_ambiguity":
        plan.update(action="escalate", escalate_reason="spec_ambiguity")
    elif route == "escalate_cycle_limit":
        plan.update(action="escalate", escalate_reason="cycle_limit")
    elif route == "huddle":
        plan.update(action="huddle")
    else:
        plan.update(action="return_to_developer")

    return plan


def _shell_lines(plan: dict[str, Any]) -> str:
    """Render a plan as `key=value` lines for `eval` in the bash caller.

    Values are constrained by construction (enum-ish strings and ints), so
    no quoting/escaping is needed or done.
    """
    keys = (
        "action",
        "reason",
        "route",
        "escalate_reason",
        "disagreement_type",
        "request_changes_count",
        "loop_number",
    )
    return "\n".join(f"{key}={plan.get(key, '')}" for key in keys)


def _main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "plan":
        print("usage: review_handoff.py plan <review_agent>  # PR JSON on stdin", file=sys.stderr)
        return 2

    payload = json.loads(sys.stdin.read())
    plan = plan_handoff(
        payload.get("reviews") or [],
        payload.get("comments") or [],
        argv[1],
    )
    print(_shell_lines(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
