#!/usr/bin/env python3
"""Native issue-relationship resolution (#3731).

Owner decision 2026-08-12: the blocking relationship between an issue filed
during acceptance testing (`@acceptance-failure` / `@improvement`) and the
issue it was filed against lives in **GitHub's native blocked-by/blocks
relationships** -- never in body prose (`Related feature: #N`), never in
comment markers. The new issue blocks acceptance of the marked issue, and
transitively anything blocked by that one.

This module holds the *pure* half of that model, mirroring the split used by
`sprint_calc.py`, `retry_budget.py`, `parked_resume.py` and `drain_gate.py`:
`lib/gh_project.sh` calls the dependency REST API
(`/issues/{n}/dependencies/{blocked_by,blocking}`) and hands the resulting
edges here as JSON, so graph resolution and the historical fallback are
unit-testable with no network.

Historical fallback (documented, deliberate): issues filed *before* this
change carry only the retired `Related feature: #N` / `Parent feature: #N`
body line and have no native edges. Every resolver here is native-first and
falls back to that prose ONLY when no native edge exists, so old data keeps
attributing correctly while nothing new is ever written in prose form.
"""

from __future__ import annotations

import json
import re
import sys
from collections import deque
from typing import Any, Iterable, Mapping

# Retired convention, read-only: `Related feature: #123`, and the even older
# `Parent feature: #123` from the sub-issue model (2026-08-01). Matched only
# as a fallback for issues that predate the native relationships.
_PROSE_RELATED_RE = re.compile(r"(?:Parent|Related)\s+feature:\s*#(?P<n>\d+)\b", re.IGNORECASE)


def parse_related_feature_prose(body: str | None) -> int | None:
    """The issue number from the first legacy `Related feature: #N` line.

    Returns None when the body has no such marker -- which is the expected
    case for anything filed after #3731.
    """
    if not body:
        return None
    match = _PROSE_RELATED_RE.search(body)
    return int(match.group("n")) if match else None


def resolve_related_features(
    blocks: Iterable[Any] | None = None, body: str | None = None
) -> list[int]:
    """Issues that this issue blocks: native `blocks` edges, else prose.

    `blocks` is what `/issues/{n}/dependencies/blocking` returned (issue
    numbers, or objects carrying a `number`). When it is empty the retired
    body marker is consulted, so a historical failure issue still resolves to
    its feature.
    """
    native = _as_numbers(blocks)
    if native:
        return native
    prose = parse_related_feature_prose(body)
    return [prose] if prose is not None else []


def resolve_related_feature(
    blocks: Iterable[Any] | None = None, body: str | None = None
) -> int | None:
    """The single feature an acceptance-failure/improvement issue belongs to.

    The handlers create exactly one blocking edge per filed issue, so the
    first resolved relationship is the feature. None when the issue is not
    related to anything (a plain feature issue, or an owner-filed defect).
    """
    related = resolve_related_features(blocks, body)
    return related[0] if related else None


def _as_numbers(items: Iterable[Any] | None) -> list[int]:
    """Issue numbers from a dependency list, de-duplicated, order preserved.

    Accepts both the raw REST shape (dicts with `number`) and a plain list of
    numbers, so callers can pass API output straight through.
    """
    out: list[int] = []
    for item in items or []:
        if isinstance(item, Mapping):
            value = item.get("number")
        else:
            value = item
        if value is None:
            continue
        num = int(value)
        if num not in out:
            out.append(num)
    return out


def transitive_closure(root: int, edges: Mapping[Any, Iterable[Any]]) -> list[int]:
    """Every issue reachable from `root` through `edges`, ascending.

    `edges` maps an issue number to its direct neighbours in ONE direction:
    pass a blocked_by map to get everything transitively blocking `root`,
    or a blocks map to get everything `root` transitively blocks. `root`
    itself is never in the result, and cycles terminate (a mis-entered
    A-blocks-B-blocks-A pair must not hang the promotion sweep).
    """
    normalized: dict[int, list[int]] = {int(k): _as_numbers(v) for k, v in edges.items()}
    seen: set[int] = set()
    queue: deque[int] = deque(normalized.get(int(root), []))
    while queue:
        node = queue.popleft()
        if node == int(root) or node in seen:
            continue
        seen.add(node)
        queue.extend(normalized.get(node, []))
    return sorted(seen)


def transitive_blockers(root: int, blocked_by: Mapping[Any, Iterable[Any]]) -> list[int]:
    """Everything transitively blocking `root` (its full acceptance gate)."""
    return transitive_closure(root, blocked_by)


def transitive_blocked(root: int, blocks: Mapping[Any, Iterable[Any]]) -> list[int]:
    """Everything `root` transitively blocks.

    This is the "and transitively anything blocked by that one" half of the
    owner's rule: filing a failure against issue F holds back not just F but
    every issue whose own acceptance waits on F.
    """
    return transitive_closure(root, blocks)


def feature_blockers(records: Iterable[Mapping[str, Any]]) -> dict[int, list[int]]:
    """feature -> the issues filed against it, from a list of issue records.

    Each record is one candidate blocker (an Acceptance Failure or
    Improvement issue): `number`, optional `blocks` (native edges) and
    optional `body` (historical prose fallback). Result values are sorted and
    de-duplicated so callers get a stable gate list.
    """
    out: dict[int, list[int]] = {}
    for record in records:
        number = int(record["number"])
        for feature in resolve_related_features(record.get("blocks"), record.get("body")):
            if feature == number:
                continue
            out.setdefault(feature, [])
            if number not in out[feature]:
                out[feature].append(number)
    return {feature: sorted(blockers) for feature, blockers in out.items()}


def _main(argv: list[str]) -> int:
    if not argv:
        print(
            "usage: issue_relationships.py "
            "<resolve-related|parse-prose|transitive|feature-blockers> [args]",
            file=sys.stderr,
        )
        return 2

    cmd = argv[0]
    if cmd == "parse-prose":
        # Issue body on stdin, never as argv: bodies carry newlines and shell
        # metacharacters, and quoting them through bash is how injections start.
        related = parse_related_feature_prose(sys.stdin.read())
        if related is not None:
            print(related)
        return 0
    if cmd == "resolve-related":
        payload = json.loads(sys.stdin.read())
        for feature in resolve_related_features(payload.get("blocks"), payload.get("body")):
            print(feature)
        return 0
    if cmd == "transitive":
        if len(argv) < 2:
            print("usage: issue_relationships.py transitive <root>", file=sys.stderr)
            return 2
        edges = json.loads(sys.stdin.read())
        for num in transitive_closure(int(argv[1]), edges):
            print(num)
        return 0
    if cmd == "feature-blockers":
        records = json.loads(sys.stdin.read())
        print(json.dumps(feature_blockers(records)))
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
