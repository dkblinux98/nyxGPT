#!/usr/bin/env python3
"""The sprint plan doc: the grooming artifact, and its machine-readable half (#3908).

`product_management/sprint_planning/sprint_<N>/PLAN.md` is written by the
scrummaster when a sprint is groomed, before dispatch begins
(`product_management/AGENTIC_SDLC_DESIGN.md` §4). It is two things at once:

  * prose the owner can veto -- ordered scope with the reasoning for that
    order, deferrals, capacity notes, and a regroom log appended in place; and
  * a machine-readable block the developer's pull step consumes (#3883) for
    pull order and per-issue expected-files.

Keeping both in one file is deliberate. A separate YAML would drift from the
prose within a sprint, and the prose is what makes the order reviewable; the
JSON block is generated from the same structure that renders the tables, so
they cannot disagree.

Expected-files is the field that did not exist before: nothing recorded which
files an issue was likely to touch, so the pull step had nothing to compare
and four PRs collided on `src/nyxgpt/app.py` on 2026-08-18. It is seeded
heuristically from the issue body and improved by hand -- a rough list beats
no list, because the overlap check only needs to be right often enough to
schedule around the obvious collisions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any

# The machine-readable half is fenced and preceded by this marker, so the
# parser never has to guess which code block in the doc is the plan.
PLAN_JSON_MARKER = "<!-- sprint-plan:json -->"

_PLAN_BLOCK_RE = re.compile(
    re.escape(PLAN_JSON_MARKER) + r"\s*\n```json\n(.*?)\n```",
    re.DOTALL,
)

# "Files affected: `a.py`, `b.py`" / "- Files: ..." in an issue body.
_FILES_LINE_RE = re.compile(
    r"^\s*[-*]?\s*\**\s*Files(?:\s+affected)?\s*\**\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_BACKTICKED_RE = re.compile(r"`([^`]+)`")

# A bare path mentioned outside backticks: at least one slash, no spaces, and
# either a file extension or a trailing slash. Deliberately conservative --
# a false positive here makes the pull step skip a candidate for an overlap
# that is not real, which costs throughput.
_BARE_PATH_RE = re.compile(r"(?<![\w`/])((?:[\w.\-]+/)+[\w.\-]*(?:\.\w+|/))")


def _clean_path(raw: str) -> str:
    """Normalize one path token, or return "" if it is not a path at all."""
    token = raw.strip().strip("`\"'").rstrip(",.;")
    token = token.lstrip("./")
    if not token or " " in token:
        return ""
    # Glob-ish entries ("scripts/agents/*") are kept as directory prefixes:
    # the overlap check treats a trailing slash as "anything under here".
    token = re.sub(r"\*+$", "", token)
    if not token:
        return ""
    if "/" not in token and "." not in token:
        return ""
    return token


def expected_files_from_body(body: str | None) -> list[str]:
    """Seed an expected-files list from an issue body.

    Prefers an explicit "Files affected:" line -- the issue template asks for
    one, so when it is there it is the author's own answer. Falls back to
    backticked paths anywhere in the body, which is noisier but is what most
    older issues actually carry.
    """
    if not body:
        return []

    found: list[str] = []
    for match in _FILES_LINE_RE.finditer(body):
        line = match.group(1)
        parts = _BACKTICKED_RE.findall(line) or re.split(r"[,;]", line)
        for part in parts:
            cleaned = _clean_path(part)
            if cleaned:
                found.append(cleaned)

    if not found:
        for raw in _BACKTICKED_RE.findall(body):
            cleaned = _clean_path(raw)
            if cleaned and "/" in cleaned:
                found.append(cleaned)
        for raw in _BARE_PATH_RE.findall(body):
            cleaned = _clean_path(raw)
            if cleaned:
                found.append(cleaned)

    seen: set[str] = set()
    ordered: list[str] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            ordered.append(path)
    return ordered


def parse_plan(text: str) -> dict[str, Any]:
    """Read the machine-readable half back out of a rendered PLAN.md."""
    match = _PLAN_BLOCK_RE.search(text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def plan_order(plan: dict[str, Any]) -> list[int]:
    """The pull order: issue numbers in the order the plan intends."""
    return [
        int(entry["issue"])
        for entry in plan.get("order", [])
        if isinstance(entry, dict) and str(entry.get("issue", "")).isdigit()
    ]


def expected_files(plan: dict[str, Any], issue: int) -> list[str]:
    for entry in plan.get("order", []):
        if isinstance(entry, dict) and int(entry.get("issue", 0)) == int(issue):
            return [str(f) for f in entry.get("expected_files", [])]
    return []


def _table(rows: Iterable[dict[str, Any]]) -> str:
    lines = [
        "| # | Issue | Priority | Effort | Expected files | Relationships |",
        "|---|---|---|---|---|---|",
    ]
    for position, entry in enumerate(rows, start=1):
        files = ", ".join(f"`{f}`" for f in entry.get("expected_files", [])) or "_none recorded_"
        blocked_by = entry.get("blocked_by") or []
        blocks = entry.get("blocks") or []
        rel_parts = []
        if blocked_by:
            rel_parts.append("blocked by " + ", ".join(f"#{n}" for n in blocked_by))
        if blocks:
            rel_parts.append("blocks " + ", ".join(f"#{n}" for n in blocks))
        rel = "; ".join(rel_parts) or "_none_"
        title = str(entry.get("title", "")).replace("|", "\\|")
        lines.append(
            f"| {position} | #{entry['issue']} {title} | {entry.get('priority', '') or '-'} "
            f"| {entry.get('effort', '') or '-'} | {files} | {rel} |"
        )
    return "\n".join(lines)


def render_plan(plan: dict[str, Any]) -> str:
    """Render PLAN.md: the prose the owner vetoes, plus the block #3883 reads."""
    sprint = plan.get("sprint") or "unscheduled"
    window = plan.get("window") or {}
    milestone = plan.get("milestone") or "_none_"
    capacity = plan.get("capacity") or {}
    order = plan.get("order") or []

    parts = [
        f"# Sprint plan -- {sprint}",
        "",
        "Written when the sprint is groomed, before dispatch begins "
        "(`product_management/AGENTIC_SDLC_DESIGN.md` §4). Developers pull from "
        "this order (#3883); the scrummaster does not push work at them.",
        "",
        "## 1. Window",
        "",
        f"- **Sprint:** {sprint}",
        f"- **Window:** {window.get('start', '?')} -> {window.get('end', '?')}",
        f"- **Milestone:** {milestone}",
        "",
        "## 2. Scope, in pull order",
        "",
        _table(order) if order else "_No issues groomed into this sprint._",
        "",
        "### Why this order",
        "",
        (plan.get("order_rationale") or "").strip()
        or "_Ordering rationale not recorded -- the groomer's seed order "
        "(dependencies, then priority, then effort) stands unjustified._",
        "",
    ]

    per_issue = [
        f"- **#{entry['issue']}** -- {str(entry.get('why_here', '')).strip()}"
        for entry in order
        if str(entry.get("why_here", "")).strip()
    ]
    if per_issue:
        parts += ["Per issue:", "", *per_issue, ""]

    parts += [
        "## 3. Capacity",
        "",
        f"- **Recent velocity:** {capacity.get('velocity_points', '?')} effort points/sprint",
        f"- **Failure-arrival reserve:** {capacity.get('failure_reserve_points', '?')} points "
        "held back for acceptance failures",
        f"- **Calibration:** {capacity.get('notes', '') or '_no estimate-vs-actual notes yet_'}",
        "",
        "## 4. Deliberate deferrals",
        "",
    ]
    deferred = plan.get("deferred") or []
    if deferred:
        parts += [
            f"- **#{entry['issue']}** -- {entry.get('why', 'no reason recorded')}"
            for entry in deferred
        ]
    else:
        parts.append("_Nothing considered and left out._")

    parts += [
        "",
        "## 5. Regroom log",
        "",
        "Mid-sprint changes append here; the original plan is never rewritten, so "
        "drift stays visible.",
        "",
    ]
    regroom = plan.get("regroom_log") or []
    if regroom:
        parts += [f"- **{entry.get('at', '?')}** -- {entry.get('change', '')}" for entry in regroom]
    else:
        parts.append("_No regrooms yet._")

    parts += [
        "",
        "## 6. Machine-readable plan",
        "",
        "The developer's pull step (#3883) reads the block below for pull order "
        "and expected-files. Edit the prose above and this block together -- they "
        "are generated from one structure and must not disagree.",
        "",
        PLAN_JSON_MARKER,
        "```json",
        json.dumps(plan, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(parts)


def append_regroom(plan: dict[str, Any], at: str, change: str) -> dict[str, Any]:
    """Append a regroom entry -- in place, never a rewrite (§4.5)."""
    updated = dict(plan)
    log = list(updated.get("regroom_log") or [])
    log.append({"at": at, "change": change})
    updated["regroom_log"] = log
    return updated
