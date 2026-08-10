#!/usr/bin/env python3
"""Pure parked-issue auto-resume calculations for #3709.

No network/gh calls live here on purpose, mirroring sprint_calc.py (#3480)
and retry_budget.py (#3689): lib/gh_project.sh gathers the observable state
(issue bodies, blocker states, open PRs, in-flight developer runs, comment
threads) and hands it to this module as JSON, so the decision math is
unit-testable without mocking the GitHub API.

Background: an In Progress issue can end up *parked* -- no open PR closing
it and no in-flight developer run -- e.g. it refused earlier because a prose
"Blocked by: #N" gate was still open, or its runs died in an incident.
Nothing picked those back up when the blockers merged, so a human posted
RETRY_IMPLEMENTATION at every gate opening (the Sprint 8 cloud chain
#3509 -> #3510 -> #3513 -> #3514/#3515/#3516 was hand-walked that way). The
owner's requirement is no babysitting: the loop drives its own chain.

INTERIM MECHANISM -- prose `Blocked by:` parsing below is a stopgap. It is
superseded by the native issue Relationships work (W1/W2 in
`product_management/AGENTIC_SDLC_DESIGN.md`, deferred to nyxAgent); when
that lands, dependencies are read from the native API and this parser goes
away. It is deliberately minimal: issue-body references only, no comment
scanning, no transitive resolution.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from retry_budget import MAX_RETRIES  # noqa: E402

# The auto-resume budget shares the #3689 retry cap: an issue that has been
# auto-resumed this many times since the last owner comment is not resumed
# again -- it is reported as gate-stuck instead, so a chain that cannot make
# progress escalates to a human rather than looping forever.
MAX_AUTO_RESUMES = MAX_RETRIES

# `Blocked by: #3509`, `**Blocked by:** #3509 (P6-12)`, `- Blocked by #3509,
# #3510`. Matches the label at the start of a line (allowing list bullets,
# blockquote markers and markdown emphasis) and takes every `#N` on the rest
# of that line; parenthetical labels like "(P6-12)" carry no `#` so they are
# ignored for free.
_BLOCKED_BY_LINE_RE = re.compile(
    r"^[ \t>*_\-]*\**\s*blocked\s+by\s*\**\s*:?\s*(?P<refs>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_ISSUE_REF_RE = re.compile(r"#(\d+)\b")

_MARKER_RE = re.compile(r"<!--\s*nyxgpt-autoresume:\s*issue=(?P<issue>\d+)\s+n=(?P<n>\d+)\s*-->")


def parse_blocked_by_refs(body: str | None) -> list[int]:
    """Issue numbers referenced by `Blocked by:` lines in an issue body.

    Returns them de-duplicated, in first-seen order. Anything that is not on
    a `Blocked by` line is ignored, so a body that merely mentions `#1234`
    in prose does not create a phantom gate.
    """
    if not body:
        return []
    seen: list[int] = []
    for line in _BLOCKED_BY_LINE_RE.finditer(body):
        for ref in _ISSUE_REF_RE.finditer(line.group("refs")):
            num = int(ref.group(1))
            if num not in seen:
                seen.append(num)
    return seen


def render_marker(issue: int, resume_number: int) -> str:
    """Machine-readable marker embedded in every auto-resume comment.

    Same shape and purpose as the #3689 retry marker: the budget is
    re-derived from the live comment thread on every check, so there is no
    hidden counter anywhere to drift out of sync with reality.
    """
    return f"<!-- nyxgpt-autoresume: issue={issue} n={resume_number} -->"


def parse_markers(comment_body: str) -> list[dict[str, Any]]:
    """All auto-resume markers in one comment body (normally 0 or 1)."""
    return [
        {"issue": int(m.group("issue")), "n": int(m.group("n"))}
        for m in _MARKER_RE.finditer(comment_body)
    ]


def resume_budget(
    comments: list[dict[str, Any]], max_resumes: int = MAX_AUTO_RESUMES
) -> dict[str, Any]:
    """How many auto-resumes remain for an issue, from its comment thread.

    `comments` is the issue's full thread in chronological (ascending)
    order; each entry needs `body` and `author_association`. Only a comment
    authored by the repo owner (author_association == "OWNER") resets the
    count -- the same reset signal retry_budget.py uses, and for the same
    reason: this loop's own comments are posted by bot accounts via PATs, so
    "not a bot comment" would reset the budget on every pass and bound
    nothing.
    """
    count = 0
    for c in comments:
        if (c.get("author_association") or "").upper() == "OWNER":
            count = 0
            continue
        count += len(parse_markers(c.get("body") or ""))
    return {
        "count": count,
        "exhausted": count >= max_resumes,
        "next_resume_number": count + 1,
        "max_resumes": max_resumes,
    }


def classify_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Splits the scanned In Progress issues into the report's buckets.

    Each candidate is what gh_project.sh could observe for one In Progress
    issue: `issue`, `parked` (no open PR and no in-flight developer run),
    `open_blockers` (blocker issue numbers still open), `budget_exhausted`.
    Non-parked issues are *active* -- something is already working them, so
    the loop must not poke them.

    Returns `resumable` (parked, no open blockers, budget left),
    `waiting` (parked but gated -- reported, never silently dropped),
    `exhausted` (parked and ungated but out of auto-resume budget), and
    `active`.
    """
    resumable: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    exhausted: list[dict[str, Any]] = []
    active: list[int] = []

    for cand in sorted(candidates, key=lambda c: int(c["issue"])):
        issue = int(cand["issue"])
        if not cand.get("parked"):
            active.append(issue)
            continue
        open_blockers = [int(b) for b in (cand.get("open_blockers") or [])]
        entry = {"issue": issue, "open_blockers": open_blockers}
        if open_blockers:
            waiting.append(entry)
        elif cand.get("budget_exhausted"):
            exhausted.append(entry)
        else:
            resumable.append(entry)

    return {
        "resumable": resumable,
        "waiting": waiting,
        "exhausted": exhausted,
        "active": active,
    }


def select_resume(scan: dict[str, Any]) -> int | None:
    """The one issue to auto-resume this kick cycle, or None.

    Lowest issue number first: a sequenced chain (#3509 -> #3510 -> ...) is
    numbered in dependency order, so this walks it front to back. One resume
    per cycle is deliberate -- each merge opens exactly one gate, and the
    next merge kicks the loop again.
    """
    resumable = scan.get("resumable") or []
    return int(resumable[0]["issue"]) if resumable else None


def build_gate_lines(scan: dict[str, Any], resumed: int | None = None) -> list[str]:
    """Markdown lines describing the parked-issue scan for the loud report.

    Every parked issue appears in exactly one line -- resumed, waiting on
    gates, or out of auto-resume budget -- so nothing the scan touched is
    dropped silently.
    """
    lines: list[str] = []
    waiting = scan.get("waiting") or []
    exhausted = scan.get("exhausted") or []
    active = scan.get("active") or []

    if resumed is not None:
        lines.append(
            f"- **Auto-resumed:** #{resumed} — parked with all `Blocked by:` gates "
            f"closed, retry trigger posted."
        )
    if waiting:
        rendered = "; ".join(
            f"#{w['issue']} (waiting on {', '.join('#' + str(b) for b in w['open_blockers'])})"
            for w in waiting
        )
        lines.append(f"- **Waiting on gates:** {rendered}.")
    if exhausted:
        rendered = ", ".join(f"#{e['issue']}" for e in exhausted)
        lines.append(
            f"- **Auto-resume budget exhausted** (needs an owner comment to reset, "
            f"#3689): {rendered}."
        )
    if active:
        rendered = ", ".join(f"#{n}" for n in active)
        lines.append(f"- **In flight** (open PR or running developer job): {rendered}.")
    return lines


def _main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: parked_resume.py <parse-blocked-by|budget|scan|marker|gate-lines>",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]
    if cmd == "parse-blocked-by":
        # Issue body on stdin (never as an argv, so bodies with newlines and
        # shell metacharacters need no quoting gymnastics in bash).
        for num in parse_blocked_by_refs(sys.stdin.read()):
            print(num)
        return 0
    if cmd == "budget":
        comments = json.loads(sys.stdin.read())
        print(json.dumps(resume_budget(comments)))
        return 0
    if cmd == "scan":
        candidates = json.loads(sys.stdin.read())
        scan = classify_candidates(candidates)
        scan["selected"] = select_resume(scan)
        print(json.dumps(scan))
        return 0
    if cmd == "marker":
        if len(argv) < 3:
            print("usage: parked_resume.py marker <issue> <resume_number>", file=sys.stderr)
            return 2
        print(render_marker(int(argv[1]), int(argv[2])))
        return 0
    if cmd == "gate-lines":
        payload = json.loads(sys.stdin.read())
        resumed = payload.get("selected")
        print("\n".join(build_gate_lines(payload, int(resumed) if resumed else None)))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
