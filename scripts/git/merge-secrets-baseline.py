#!/usr/bin/env python3
"""Git merge driver for `.secrets.baseline`, whose only conflict is a clock.

WHY THIS EXISTS. `detect-secrets` rewrites `generated_at` every time it
updates the baseline -- which is every time a scanned file's line numbers
move. Two branches that both touched any scanned file therefore always
conflict on that one line, even when their *findings* are identical and
neither introduced a secret. On 2026-08-22 that cost four identical
hand-resolutions in a single evening of parallel branches, each one an
opportunity to fumble a security-relevant file by hand.

The findings themselves merge cleanly by union: a baseline entry is a
per-file list of detected secrets, branches add entries independently, and
losing one would make the hook fail loudly on the next commit rather than
silently admit a secret. So this driver takes the union of `results` and the
LATER `generated_at`, and refuses anything it does not understand.

Deliberately NOT `merge=ours`/`merge=union`: `ours` silently drops the other
branch's new findings, and `union` concatenates JSON lines into a file that
no longer parses.

Registered per clone (git worktrees share the clone's config, so one
registration covers every agent worktree):

    git config merge.secrets-baseline.name "detect-secrets baseline merge"
    git config merge.secrets-baseline.driver \
        "python3 scripts/git/merge-secrets-baseline.py %O %A %B"

`.gitattributes` maps the file to it. A clone that never registers the
driver falls back to git's default and gets the ordinary conflict -- the
previous behaviour, never something worse.

Exit 0 = merged (result written to %A), 1 = conflict left to the human.
"""

from __future__ import annotations

import json
import sys


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: merge-secrets-baseline.py <base> <ours> <theirs>", file=sys.stderr)
        return 1
    base_path, ours_path, theirs_path = argv
    ours, theirs = _load(ours_path), _load(theirs_path)
    if ours is None or theirs is None:
        print("secrets-baseline: unparseable side; leaving the conflict", file=sys.stderr)
        return 1

    # Anything structural that differs is a real disagreement a human should
    # see -- a changed plugin set or filter set changes what the baseline
    # MEANS, and quietly picking a side would weaken the scan.
    for key in ("version", "plugins_used", "filters_used"):
        if ours.get(key) != theirs.get(key):
            print(f"secrets-baseline: {key} differs; leaving the conflict", file=sys.stderr)
            return 1

    merged = dict(ours)
    ours_results = ours.get("results") or {}
    theirs_results = theirs.get("results") or {}
    if not isinstance(ours_results, dict) or not isinstance(theirs_results, dict):
        print("secrets-baseline: unexpected results shape; leaving the conflict", file=sys.stderr)
        return 1

    results: dict = {}
    for filename in sorted(set(ours_results) | set(theirs_results)):
        a = ours_results.get(filename) or []
        b = theirs_results.get(filename) or []
        if a == b:
            results[filename] = a
            continue
        # Union by identity, order-stable: same secret found at the same
        # place by both sides appears once.
        seen: set[str] = set()
        combined: list = []
        for entry in [*a, *b]:
            key = json.dumps(entry, sort_keys=True)
            if key not in seen:
                seen.add(key)
                combined.append(entry)
        results[filename] = combined
    merged["results"] = results

    # The clock that caused the conflict: keep the later stamp, so the file
    # never claims to be older than the scan it describes.
    stamps = [s for s in (ours.get("generated_at"), theirs.get("generated_at")) if isinstance(s, str)]
    if stamps:
        merged["generated_at"] = max(stamps)

    with open(ours_path, "w", encoding="utf-8") as fh:
        json.dump(merged, fh, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
