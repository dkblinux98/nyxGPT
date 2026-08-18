#!/usr/bin/env python3
"""Ledger entry-ID allocation across concurrent branches (#3806).

`agents/LEDGER.md` IDs are never reused, and
`tests/unit/test_operating_ledger.py::test_ledger_entry_ids_are_unique` is the
gate that holds that line. What it cannot do is help a session *pick* the next
one. Sessions were allocating by eye, and #3806's entries were numbered
`V-034`/`V-035` from a partial scan of the file while `V-034`/`V-035` already
existed further up -- the ledger then defined each twice and the gate failed
for every review on the release branch, not just the branch that caused it.

Two rules this encodes, both of which are easy to get wrong by hand:

**max + 1, never lowest-unused.** The public ledger has deliberate gaps where
entries were relocated to the owner's private annex. Those IDs are taken and
invisible here, so filling a gap silently reuses one (`agents/LEDGER.md`,
"How entries reach the ledger").

**Allocate against the live release branch too, not just the working copy.**
Two PRs open at once each see a base that does not yet contain the other's
entries, so scanning only the branch hands both of them the same number. The
collision is then created at merge, by neither branch alone.

Pure functions over text, so the arithmetic is unit-testable without git
(same shape as `retry_budget.py` / `stop_loop_guard.py`); the CLI at the
bottom is what the runbooks call.

    python3 scripts/agents/lib/ledger_ids.py next V --base origin/v3.0.0
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: An entry opens a line as `- **X-000**`. Mirrors ENTRY_RE in
#: tests/unit/test_operating_ledger.py -- the allocator and the gate must
#: agree on what counts as an entry, or one hands out IDs the other rejects.
ENTRY_RE = re.compile(r"^- \*\*([DVPQS])-(\d{3})\*\*", re.MULTILINE)

#: The schema block's reserved placeholder; never a real entry.
PLACEHOLDER_ID = "000"

#: Entry kinds, in the order the ledger documents them.
KINDS = ("D", "V", "P", "Q", "S")

DEFAULT_LEDGER = "agents/LEDGER.md"


def parse_ids(text: str) -> list[str]:
    """Every real entry ID in `text`, in document order (duplicates kept).

    Duplicates are deliberately not collapsed: a ledger that defines an ID
    twice is the defect this module exists to prevent, and a caller checking
    for it needs to see both.
    """
    return [f"{kind}-{num}" for kind, num in ENTRY_RE.findall(text) if num != PLACEHOLDER_ID]


def next_id(kind: str, *texts: str) -> str:
    """The next free ID of `kind`, allocated as max + 1 across every `text`.

    Pass every tree that could already own a number -- the working copy and
    the live release branch at minimum.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown ledger entry kind {kind!r}; expected one of {', '.join(KINDS)}")
    highest = 0
    for text in texts:
        for entry_kind, num in ENTRY_RE.findall(text):
            if entry_kind == kind and num != PLACEHOLDER_ID:
                highest = max(highest, int(num))
    return f"{kind}-{highest + 1:03d}"


def read_git_ref(ref: str, path: str) -> str:
    """`path` as of git `ref`, or "" when that ref does not carry the file.

    A base branch predating the ledger simply owns no IDs. A *missing ref*,
    though, means the caller asked to allocate against something that is not
    there; returning "" would quietly reproduce the branch-only scan this
    module exists to replace, so that raises instead.
    """
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    stderr = result.stderr.strip()
    if "does not exist" in stderr or "exists on disk, but not in" in stderr:
        return ""
    raise RuntimeError(f"cannot read {path} at {ref}: {stderr or 'git show failed'}")


def _cmd_next(args: argparse.Namespace) -> int:
    texts = [Path(args.ledger).read_text(encoding="utf-8")]
    if args.base:
        texts.append(read_git_ref(args.base, args.ledger))
    print(next_id(args.kind, *texts))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Allocate the next free agents/LEDGER.md entry ID."
    )
    parser.add_argument(
        "--ledger", default=DEFAULT_LEDGER, help="path to the ledger (default: %(default)s)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_next = sub.add_parser("next", help="print the next free ID of a kind")
    p_next.add_argument("kind", choices=KINDS)
    p_next.add_argument(
        "--base",
        help="git ref of the live release branch to also allocate against "
        "(e.g. origin/v3.0.0); fetch it first",
    )
    p_next.set_defaults(func=_cmd_next)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
