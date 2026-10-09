#!/usr/bin/env python3
"""The class sweep reaches the PR body, or the PR body says nobody swept (#4183).

Owner decision 2026-10-09 (ledger **D-067**): a fix is done when the *class* of
defect is gone, not when the instance someone was looking at stops reproducing.
The developer records the sweep (`agents/runbooks/developer-runbook.md` §3i) and
the reviewer blocks on a missing or incomplete one
(`agents/runbooks/review-runbook.md` §1e).

The developer agent cannot write the PR body -- `developer_submit_for_review.sh`
builds it, and the implement prompts forbid `gh pr edit`. So the sweep is handed
over as a file (`/tmp/class-sweep.md`, the same shape the review brief already
uses via `/tmp/review-comments.txt`) and this module puts it in the body.

**Two outcomes, never a third.** Either the sweep is in the body under the
marker the review prompt reads, or a `## Class sweep -- NOT PROVIDED` block is,
naming what is missing and what the reviewer should do. Silence is the defect
#4183 was filed about: #4136 reached its third occurrence through reviews that
had no artifact to miss. It is deliberately **advisory** -- it never refuses a
submission, because whether a class was really swept is the reviewer's
judgement and not a grep's.

The decision lives here and nowhere else, because this runs from two places (a
fresh submission and a re-review of an existing PR) and two copies of a decision
is the very fault this gate exists to catch (#4179, ledger **D-066**).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: The marker the review prompt greps for. A body carrying it has a real sweep.
SECTION_MARKER = "<!-- nyxgpt-class-sweep -->"

#: The marker of the receipt written when no sweep was recorded. Deliberately
#: NOT a superstring of SECTION_MARKER: a receipt must never read as a sweep.
MISSING_MARKER = "<!-- nyxgpt-no-class-sweep -->"

SECTION_HEADING = "## Class sweep"
MISSING_HEADING = "## Class sweep — NOT PROVIDED"

#: Where the developer leaves the sweep. The prompts name this path literally.
DEFAULT_SWEEP_PATH = "/tmp/class-sweep.md"


def read_sweep(path: str | Path) -> str | None:
    """Return the sweep text, or None when there is nothing usable there.

    An unreadable, absent, empty or whitespace-only file all mean the same
    thing -- no sweep was recorded -- and all must produce the receipt rather
    than an empty section that would read as a clean result.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    return text.strip() or None


def render_section(sweep: str | None, sweep_path: str | Path = DEFAULT_SWEEP_PATH) -> str:
    """Render the body section for a recorded sweep, or the receipt for none."""
    if sweep is not None:
        return "\n".join([SECTION_HEADING, SECTION_MARKER, sweep, ""])

    return (
        "\n".join(
            [
                MISSING_HEADING,
                MISSING_MARKER,
                "No class sweep was recorded for this change (expected at "
                f"`{sweep_path}`, see `agents/runbooks/developer-runbook.md` §3i).",
                "",
                "**Reviewer:** this is the #4183 finding, stated rather than left to be",
                'noticed. Read the issue\'s "Defect class and surfaces" section, run the',
                "search yourself, and treat a class that was never swept as a Medium",
                "(blocking) finding (`agents/runbooks/review-runbook.md` §1e). If this",
                "change has no defect behind it, that is the one-line answer the sweep",
                "section was supposed to carry.",
            ]
        )
        + "\n"
    )


def _split_out_existing_section(body: str) -> tuple[str, bool]:
    """Remove any existing class-sweep section; report whether a REAL one went.

    "Real" means it carried `SECTION_MARKER` -- a developer-written sweep, as
    opposed to a receipt from an earlier round. The caller needs to know,
    because a later round with no sweep file must not replace a real sweep with
    a receipt (that would manufacture the finding it is supposed to report).
    """
    lines = body.replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    removed_real = False

    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip() in (SECTION_HEADING, MISSING_HEADING):
            section: list[str] = []
            index += 1
            # A section runs to the next ATX heading of the same or higher
            # level, or to the end of the body.
            while index < len(lines) and not lines[index].startswith("## "):
                section.append(lines[index])
                index += 1
            if any(SECTION_MARKER in entry for entry in section):
                removed_real = True
            # Drop the blank line this section was separated by, so repeated
            # application cannot grow the body one newline at a time.
            while kept and not kept[-1].strip():
                kept.pop()
            continue
        kept.append(line)
        index += 1

    text = "\n".join(kept).rstrip("\n")
    return (text + "\n" if text else ""), removed_real


def apply_to_body(
    body: str, sweep: str | None, sweep_path: str | Path = DEFAULT_SWEEP_PATH
) -> tuple[str, str]:
    """Return `(new_body, outcome)` with exactly one class-sweep section.

    Idempotent by construction: any existing section is removed before the
    fresh one is appended, so a re-submission or a re-review round replaces the
    block instead of stacking a second one.

    `outcome` is one of:

    * ``attached``     -- the recorded sweep is now in the body;
    * ``kept``         -- no sweep was recorded, but the body already carried a
      real one (a hand-written body, or an earlier round's); it is left alone;
    * ``not-provided`` -- no sweep anywhere, so the receipt is in the body.
    """
    stripped, had_real = _split_out_existing_section(body)

    if sweep is None and had_real:
        # Put the real section back untouched rather than downgrading it.
        return body, "kept"

    section = render_section(sweep, sweep_path)
    separator = "\n" if stripped else ""
    return f"{stripped}{separator}{section}", ("attached" if sweep else "not-provided")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    apply_cmd = sub.add_parser(
        "apply",
        help="rewrite a PR-body file so it carries exactly one class-sweep section",
    )
    apply_cmd.add_argument("--body-file", required=True)
    apply_cmd.add_argument("--sweep", default=DEFAULT_SWEEP_PATH)

    render_cmd = sub.add_parser("render", help="print the section for the given sweep file")
    render_cmd.add_argument("--sweep", default=DEFAULT_SWEEP_PATH)

    args = parser.parse_args(argv)
    sweep = read_sweep(args.sweep)

    if args.command == "render":
        sys.stdout.write(render_section(sweep, args.sweep))
        return 0

    body_path = Path(args.body_file)
    try:
        body = body_path.read_text(encoding="utf-8")
    except OSError as exc:
        # Advisory: a body we cannot read is the caller's problem to report,
        # and must never be the reason finished work fails to reach review.
        print(f"[class-sweep] cannot read {body_path}: {exc}", file=sys.stderr)
        print("error")
        return 0

    new_body, outcome = apply_to_body(body, sweep, args.sweep)
    if new_body != body:
        body_path.write_text(new_body, encoding="utf-8")
    print(outcome)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
