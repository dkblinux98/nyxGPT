#!/usr/bin/env python3
"""Pure retry-budget and failure-signature calculations for #3689.

No network/gh calls live here on purpose, mirroring sprint_calc.py (#3480):
developer_auto_implement.yml gathers the issue's comment thread via
`gh api .../comments --paginate` and hands it to this module (as JSON on
stdin) so the retry-cap and same-signature-disproof math is unit-testable
without mocking the GitHub API.

Background (#3689): the previous retry counter in
developer_auto_implement.yml keyed its count to the Phase-3-diagnosis-
invented error-type label shown in the auto-retry comment, and treated any
comment not literally authored by "github-actions[bot]" as a "manual
intervention" that resets the count -- but this workflow's own comments are
posted via a PAT (DEVELOPER_AGENT_TOKEN) under a real bot login
(myGPT-developer-agent), so that check was true for the workflow's own
comments and the budget never actually bound anything. This module replaces
both: the budget is keyed to the stable (issue, failed step) pair via an
unforgeable marker embedded in every auto-retry comment, and only a comment
from the repo owner (author_association == "OWNER" -- the same signal
developer_auto_implement.yml's own trigger gate already uses for
RETRY_IMPLEMENTATION) resets it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from typing import Any

MAX_RETRIES = 3

_MARKER_RE = re.compile(
    r"<!--\s*nyxgpt-retry:\s*step=(?P<step>\S+)\s+sig=(?P<sig>\S+)\s+n=(?P<n>\d+)\s*-->"
)


def slugify_step(step_name: str) -> str:
    """Stable, comment-marker-safe identifier for a failed step name."""
    slug = re.sub(r"[^a-z0-9]+", "_", step_name.strip().lower()).strip("_")
    return slug or "unknown_step"


def compute_signature(step_name: str, error_excerpt: str) -> str:
    """Short, deterministic hash of (failed step, error text).

    This is the "signature" the same-signature-disproof check compares
    across consecutive failed runs: an identical signature on two
    consecutive runs empirically disproves "transient" -- retrying again
    would just reproduce the same deterministic failure.
    """
    normalized_error = "\n".join(line.strip() for line in error_excerpt.strip().splitlines())
    digest = hashlib.sha256(f"{step_name}\n{normalized_error}".encode()).hexdigest()
    return digest[:12]


def render_marker(step_name: str, signature: str, retry_number: int) -> str:
    """The machine-readable marker embedded in every auto-retry comment."""
    return f"<!-- nyxgpt-retry: step={slugify_step(step_name)} sig={signature} n={retry_number} -->"


def parse_markers(comment_body: str) -> list[dict[str, Any]]:
    """All nyxgpt-retry markers found in a single comment body (normally 0 or 1)."""
    return [
        {"step": m.group("step"), "sig": m.group("sig"), "n": int(m.group("n"))}
        for m in _MARKER_RE.finditer(comment_body)
    ]


def retry_decision(
    comments: list[dict[str, Any]],
    step_name: str,
    current_signature: str,
    max_retries: int = MAX_RETRIES,
) -> dict[str, Any]:
    """Decide whether another auto-retry is permitted for (issue, step).

    `comments` is the full issue comment thread in chronological (ascending)
    order, as returned by `gh api .../issues/<n>/comments`; each item needs
    at least `body` and `author_association`. Only nyxgpt-retry markers for
    this step posted after the last OWNER-authored comment count toward the
    budget -- a real human commenting is the only thing that resets it,
    independent of whatever error-type label or STATUS a given cycle's
    diagnosis produced.
    """
    last_human_index = -1
    for i, c in enumerate(comments):
        if c.get("author_association") == "OWNER":
            last_human_index = i

    relevant = comments[last_human_index + 1 :]
    step_slug = slugify_step(step_name)

    markers_for_step = [
        marker
        for c in relevant
        for marker in parse_markers(c.get("body") or "")
        if marker["step"] == step_slug
    ]

    retry_count = len(markers_for_step)
    last_signature = markers_for_step[-1]["sig"] if markers_for_step else None

    return {
        "step": step_slug,
        "retry_count": retry_count,
        "cap_exceeded": retry_count >= max_retries,
        "last_signature": last_signature,
        "current_signature": current_signature,
        "same_signature_as_last": last_signature is not None
        and last_signature == current_signature,
        "next_retry_number": retry_count + 1,
    }


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: retry_budget.py <decide|marker|signature> ...", file=sys.stderr)
        return 2

    cmd = argv[0]
    if cmd == "decide":
        if len(argv) != 3:
            print("usage: retry_budget.py decide <step_name> <signature>", file=sys.stderr)
            return 2
        step_name, signature = argv[1], argv[2]
        comments = json.loads(sys.stdin.read())
        print(json.dumps(retry_decision(comments, step_name, signature)))
        return 0
    if cmd == "marker":
        if len(argv) != 4:
            print(
                "usage: retry_budget.py marker <step_name> <signature> <retry_number>",
                file=sys.stderr,
            )
            return 2
        step_name, signature = argv[1], argv[2]
        retry_number = int(argv[3])
        print(render_marker(step_name, signature, retry_number))
        return 0
    if cmd == "signature":
        if len(argv) != 2:
            print("usage: retry_budget.py signature <step_name>", file=sys.stderr)
            return 2
        step_name = argv[1]
        error_excerpt = sys.stdin.read()
        print(compute_signature(step_name, error_excerpt))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
