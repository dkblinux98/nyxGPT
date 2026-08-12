#!/usr/bin/env python3
"""Dump native issue relationships for the retrospective (#3731).

Owner decision 2026-08-12: the link between an issue filed during acceptance
testing and the issue it was filed against is a GitHub **native**
blocked-by/blocks relationship, not `Related feature: #N` body prose. The
retrospective must therefore read relationships from the API, not by parsing
bodies -- so failure/improvement attribution follows the data.

Writes `data/relationships.json`:

    {
      "generated_at": "2026-08-12T00:00:00Z",
      "issues": {"3733": {"blocks": [3730], "blocked_by": []}, ...}
    }

Only issues that can carry an acceptance relationship are walked -- those
labeled `Acceptance Failure` or `Improvement` -- so this costs one extra API
call per such issue rather than one per issue in the repo. Everything they
block is recorded in the same file (inverted into `blocked_by`), which is all
`build_dashboard.py` needs.

Run via .github/workflows/retro_relationships_dump.yml (workflow_dispatch),
mirroring the other retro dumps. Needs `gh` authenticated.

ENV:
  REPO  owner/name (default: dkblinux98/nyxGPT)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).parent
DEFAULT_REPO = "dkblinux98/nyxGPT"
RELATIONSHIP_LABELS = ("Acceptance Failure", "Improvement")


def _gh_json(path: str, paginate: bool = False) -> object:
    """One `gh api` call returning parsed JSON, or None when it fails.

    Dependency endpoints 404 for issues with no relationships on some
    installations, and a dump that dies on the first such issue is useless --
    so failures degrade to "no data for this issue" rather than aborting.
    """
    cmd = ["gh", "api", path]
    if paginate:
        cmd.append("--paginate")
        cmd.append("--slurp")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    try:
        parsed = json.loads(out.stdout or "null")
    except json.JSONDecodeError:
        return None
    if paginate and isinstance(parsed, list):
        flat: list[object] = []
        for page in parsed:
            if isinstance(page, list):
                flat.extend(page)
        return flat
    return parsed


def candidate_issues(repo: str) -> list[int]:
    """Issue numbers that may carry an acceptance relationship."""
    numbers: list[int] = []
    for label in RELATIONSHIP_LABELS:
        quoted = label.replace(" ", "%20")
        page = _gh_json(
            f"repos/{repo}/issues?labels={quoted}&state=all&per_page=100", paginate=True
        )
        for item in page or []:
            if not isinstance(item, dict) or "pull_request" in item:
                continue
            number = item.get("number")
            if number is not None and int(number) not in numbers:
                numbers.append(int(number))
    return sorted(numbers)


def blocks_of(repo: str, issue: int) -> list[int]:
    """Issue numbers `issue` natively blocks."""
    payload = _gh_json(f"repos/{repo}/issues/{issue}/dependencies/blocking")
    if not isinstance(payload, list):
        return []
    return sorted({int(e["number"]) for e in payload if isinstance(e, dict) and e.get("number")})


def build_snapshot(repo: str, issues: list[int], blocks: dict[int, list[int]]) -> dict:
    """The relationships.json payload, with `blocked_by` inverted from `blocks`."""
    graph: dict[str, dict[str, list[int]]] = {}
    for issue in issues:
        graph.setdefault(str(issue), {"blocks": [], "blocked_by": []})
        for target in blocks.get(issue, []):
            graph[str(issue)]["blocks"].append(target)
            entry = graph.setdefault(str(target), {"blocks": [], "blocked_by": []})
            if issue not in entry["blocked_by"]:
                entry["blocked_by"].append(issue)
    for entry in graph.values():
        entry["blocks"] = sorted(set(entry["blocks"]))
        entry["blocked_by"] = sorted(set(entry["blocked_by"]))
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "repo": repo,
        "issues": graph,
    }


def main(argv: list[str]) -> int:
    repo = os.environ.get("REPO", DEFAULT_REPO)
    out = Path(argv[0]) if argv else HERE / "data" / "relationships.json"

    issues = candidate_issues(repo)
    if not issues:
        print("no Acceptance Failure / Improvement issues found", file=sys.stderr)
    blocks = {issue: blocks_of(repo, issue) for issue in issues}
    snapshot = build_snapshot(repo, issues, blocks)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, indent=1) + "\n")
    linked = sum(1 for e in snapshot["issues"].values() if e["blocks"])
    print(f"wrote {out}: {len(issues)} candidate issue(s), {linked} with a native blocks edge")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
