#!/usr/bin/env python3
"""Anchored command-token matching for issue-comment triggers (#3790).

Every comment-driven trigger in this repo used a bare substring test:

    contains(github.event.comment.body, 'RETRY_IMPLEMENTATION')

so a comment that merely *named* the token started a run -- including the
agents' own guidance text. On 2026-08-15 the developer agent's "issue is no
longer In Progress" stop message (which ended "...comment
`RETRY_IMPLEMENTATION` to resume") re-triggered the very workflow that had
just posted it, once every ~20 seconds: ~500 runs and ~500 comments across
#3782 and #3784 in under two hours. #3706 was the same defect on the kick
token.

Two independent guards live here, and both are applied to *every* comment
token (RETRY_IMPLEMENTATION, READY_FOR_NEXT_ISSUE, PAUSE_SPRINT,
@acceptance-failure, @improvement):

1. **Anchored matching.** A token counts as a command only when it *opens a
   line* -- the form a human or an agent uses to issue it. Prose that names
   the token mid-sentence ("comment `RETRY_IMPLEMENTATION` to resume") is a
   mention, never a command. Fenced code blocks and quoted (`>`) lines are
   stripped first, so quoting an earlier comment cannot replay its commands.
2. **The informational marker.** Any agent comment that must name a token
   carries `<!-- nyxgpt-token-mention -->`, which disqualifies the whole
   comment. This is the #3706 pattern (`AUTOPILOT_INFO_MARKER`, recognised
   here too) generalised: a structural guard that holds even if the prose
   later drifts back into naming the token at line start.

Pure functions only, no network -- the workflows hand the comment body in on
stdin (or via an env var) so the decision is unit-testable without mocking
the GitHub API, mirroring retry_budget.py / sprint_calc.py.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

#: Stamped on any agent comment that has to *name* a command token.
MENTION_MARKER = "<!-- nyxgpt-token-mention -->"

#: #3706's autopilot marker means the same thing and is honoured as well.
AUTOPILOT_INFO_MARKER = "<!-- nyxgpt-autopilot-informational -->"

#: Substring forms, so a marker still disqualifies if its comment syntax drifts.
INFORMATIONAL_MARKERS = ("nyxgpt-token-mention", "nyxgpt-autopilot-informational")

#: Every comment token that starts work in this repo.
COMMAND_TOKENS = (
    "RETRY_IMPLEMENTATION",
    "READY_FOR_NEXT_ISSUE",
    "PAUSE_SPRINT",
    "@acceptance-failure",
    "@improvement",
)

_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")

# Optional list bullet / ordered-list number, then optional markdown
# decoration (backticks, bold, italics) -- "- `RETRY_IMPLEMENTATION`" and
# "**@improvement** the button is tiny" open a line just as plainly as the
# bare token does.
_LEAD = r"(?:[-*+]\s+|\d+[.)]\s+)?[`*_]*"


def _significant_lines(body: str) -> list[str]:
    """Body lines with fenced code blocks and quoted lines removed."""
    lines: list[str] = []
    fence: str | None = None
    for raw in body.splitlines():
        match = _FENCE_RE.match(raw)
        if match:
            marker = match.group(1)[:3]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        if raw.lstrip().startswith(">"):
            continue
        lines.append(raw)
    return lines


def is_informational(body: str) -> bool:
    """True when the comment is stamped as naming tokens for information only."""
    return any(marker in (body or "") for marker in INFORMATIONAL_MARKERS)


def is_command(body: str, token: str) -> bool:
    """True when `body` *issues* `token` rather than merely naming it.

    The token must open a line of the comment (after stripping fenced code
    blocks, quoted lines, a list bullet and markdown decoration), and the
    comment must not carry an informational marker.
    """
    if not body or not token:
        return False
    if is_informational(body):
        return False
    pattern = re.compile(rf"^\s*{_LEAD}{re.escape(token)}(?![\w-])")
    return any(pattern.match(line) for line in _significant_lines(body))


def mentions(body: str, token: str) -> bool:
    """The old bare-substring test -- kept to describe what changed, and to
    let callers distinguish "names the token" from "issues the token"."""
    return bool(body) and bool(token) and token in body


def as_mention(body: str) -> str:
    """Stamp a comment body that names a token as informational.

    Agent-authored guidance should avoid naming tokens at all (that is the
    first rule); this marker is the structural backstop for the cases where
    naming one is unavoidable.
    """
    body = body or ""
    if is_informational(body):
        return body
    separator = "" if body.endswith("\n") else "\n"
    return f"{body}{separator}\n{MENTION_MARKER}\n"


def _read_body(args: argparse.Namespace) -> str:
    if args.from_env:
        return os.environ.get(args.from_env, "")
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("is-command", help="exit 0 if the body issues the token")
    check.add_argument("token", help="e.g. RETRY_IMPLEMENTATION")
    check.add_argument(
        "--from-env",
        metavar="VAR",
        help="read the comment body from this environment variable instead of stdin",
    )
    check.add_argument("--quiet", action="store_true", help="suppress the true/false line")

    mark = sub.add_parser("mark", help="append the informational marker to a body")
    mark.add_argument(
        "--from-env",
        metavar="VAR",
        help="read the comment body from this environment variable instead of stdin",
    )

    sub.add_parser("tokens", help="list every command token")

    args = parser.parse_args(argv)

    if args.command == "tokens":
        print("\n".join(COMMAND_TOKENS))
        return 0

    body = _read_body(args)

    if args.command == "mark":
        sys.stdout.write(as_mention(body))
        return 0

    result = is_command(body, args.token)
    if not args.quiet:
        print("true" if result else "false")
    return 0 if result else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
