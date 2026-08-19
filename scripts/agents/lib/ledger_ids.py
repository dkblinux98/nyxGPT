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


#: An ID as it is *referenced* anywhere in the prose, not just where it is
#: defined. Renumbering an entry has to carry its cross-references with it, or
#: the ledger's `[[D-021]]`-style links start pointing at somebody else's fact.
REFERENCE_RE = re.compile(r"(?<![A-Za-z0-9])([DVPQS]-\d{3})(?![0-9])")


def _ids_introduced(merge_base_text: str, text: str) -> list[str]:
    """IDs present in `text` and absent from `merge_base_text`, in document order.

    This is the exact test for "the branch added this entry", and it is why the
    merge base is required rather than inferred: an ID that already existed at
    the divergence point is a shared entry someone *edited*, and renumbering
    an edit would be a different bug from the one this fixes.
    """
    already = set(parse_ids(merge_base_text))
    seen: set[str] = set()
    introduced = []
    for entry in parse_ids(text):
        if entry not in already and entry not in seen:
            seen.add(entry)
            introduced.append(entry)
    return introduced


def reallocate(
    merge_base_text: str, base_text: str, branch_text: str
) -> tuple[str, dict[str, str]]:
    """Renumber the branch's colliding entries; return the new text and the mapping.

    `agents/LEDGER.md`'s schema says to allocate the next unused number, which
    is only true at the instant of merge. On a branch open for days it is a
    guess, and two branches open at once are each *correctly* told the same
    number by `next` -- the collision is created at merge, by neither branch
    alone (#3862; it happened to `V-016`, `V-024`, `V-025`, and to `D-021`
    through `D-023` before that).

    So the allocation is redone here, at merge time, against what the base
    branch actually holds by then. Only IDs the branch *introduced* that the
    base has *also* introduced since the merge base are touched: a collision
    needs both sides to have invented the same number independently. Entries
    the branch merely edits keep their IDs, and the base is never rewritten --
    the branch yields, so the merge is a fast-forward of the numbering rather
    than a negotiation.

    Deterministic: collisions are resolved in the branch's document order,
    each taking max + 1 across the base, the branch, and every number handed
    out so far. Same inputs, same output, on any machine and any re-run.
    """
    base_new = set(_ids_introduced(merge_base_text, base_text))
    branch_new = _ids_introduced(merge_base_text, branch_text)
    collisions = [entry for entry in branch_new if entry in base_new]
    if not collisions:
        return branch_text, {}

    highest: dict[str, int] = {}
    for text in (base_text, branch_text):
        for kind, num in ENTRY_RE.findall(text):
            if num != PLACEHOLDER_ID:
                highest[kind] = max(highest.get(kind, 0), int(num))

    mapping: dict[str, str] = {}
    for entry in collisions:
        kind = entry[0]
        highest[kind] = highest.get(kind, 0) + 1
        mapping[entry] = f"{kind}-{highest[kind]:03d}"

    # One pass over the original text, so a chain (V-016 -> V-045 while
    # V-045 -> V-046 in the same run) can never rewrite an already-rewritten ID.
    rewritten = REFERENCE_RE.sub(lambda m: mapping.get(m.group(1), m.group(1)), branch_text)
    return rewritten, mapping


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


def merge_base(base: str, branch: str) -> str:
    """The divergence point of `base` and `branch`, or "" when there is none."""
    result = subprocess.run(
        ["git", "merge-base", base, branch],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _cmd_next(args: argparse.Namespace) -> int:
    texts = [Path(args.ledger).read_text(encoding="utf-8")]
    if args.base:
        texts.append(read_git_ref(args.base, args.ledger))
    print(next_id(args.kind, *texts))
    return 0


def _cmd_reallocate(args: argparse.Namespace) -> int:
    """Merge-time reallocation. Exit 0 = nothing to do, 1 = collisions handled."""
    ledger = Path(args.ledger)
    branch_text = ledger.read_text(encoding="utf-8")
    base_text = read_git_ref(args.base, args.ledger)
    point = merge_base(args.base, args.branch)
    if not point:
        raise RuntimeError(
            f"no merge base between {args.base} and {args.branch}; "
            "refusing to guess which entries the branch introduced"
        )
    merge_base_text = read_git_ref(point, args.ledger)

    new_text, mapping = reallocate(merge_base_text, base_text, branch_text)
    if not mapping:
        print(f"no ledger ID collisions between {args.branch} and {args.base}", file=sys.stderr)
        return 0

    for old, new in mapping.items():
        print(f"{old} -> {new}")
    if args.write:
        ledger.write_text(new_text, encoding="utf-8")
        print(f"rewrote {ledger}", file=sys.stderr)
    return 1


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

    p_realloc = sub.add_parser(
        "reallocate",
        help="renumber the branch's colliding entries against the live base branch",
        description="Run at MERGE time. Prints 'old -> new' per collision; exits 1 "
        "when anything was reallocated, 0 when there was nothing to do.",
    )
    p_realloc.add_argument("--base", required=True, help="live base ref, e.g. origin/v3.0.0")
    p_realloc.add_argument("--branch", default="HEAD", help="branch ref (default: %(default)s)")
    p_realloc.add_argument(
        "--write", action="store_true", help="rewrite the ledger in place (default: report only)"
    )
    p_realloc.set_defaults(func=_cmd_reallocate)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
