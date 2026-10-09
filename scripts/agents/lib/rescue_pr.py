#!/usr/bin/env python3
"""Turn a rescue draft's PR record into a submission's, in one place (#4184).

`developer_ensure_pr_exists.sh` opens a **draft** for a branch whose run died
before `developer_submit_for_review.sh` (#3862). That draft deliberately says
so, loudly: the title is prefixed `wip:` and the body opens with "⚠️ Rescue PR
-- this work did not complete its checks" / "**This is not a submission for
review.** ... the run's verification did not pass".

When a continuation run's verification *does* pass, the PR stops being a
rescue. `developer_auto_implement.yml` promoted it by rewriting `Refs #N` into
`Closes #N` and taking it out of draft -- and left both of those sentences
standing. So the reviewed PR asserted it was not a submission while being
reviewed as one, and its merge commit would have carried `wip:`
(review of PR #4191).

That is the same defect class as the issue this helper was written for: **a
record that answers for a state the thing is no longer in.** The two paths a
PR can reach review by must not disagree about what it is, so the promotion
lives here and `developer_submit_for_review.sh`'s adoption branch (which
rewrites title and body wholesale from the submission template) stays the
other, equivalent route -- the two never both run on one hand-off, because
`developer_auto_implement.yml` calls submit only when no PR exists yet.

Pure text in, text out: no `gh`, no network, so the transform is unit-testable
and a workflow step is the only thing that needs a token.

Usage:
    rescue_pr.py promote --body-file BODY --issue N --title "wip: x (#N)"

Prints the promoted title on stdout and rewrites BODY in place when there was
something to promote; prints nothing and leaves BODY untouched otherwise, so
the caller's `if [[ -n "$out" ]]` is the whole "did anything change" test and
re-running it is a no-op.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: The rescue body's own heading. Its presence is what makes a body a rescue
#: record; its absence is what makes this helper idempotent (a promoted body
#: has no heading to find, so a second run changes nothing).
RESCUE_HEADING = "## ⚠️ Rescue PR"

#: The heading the rescue body ends its preamble with. Everything from
#: RESCUE_HEADING up to here is the "this is not a submission" explanation --
#: true of a draft, false of the PR under review -- and is what gets replaced.
#: The Context list after it (issue, head, base, originating run) stays: it
#: describes the branch, not the draft's state, and a reviewer wants it.
CONTEXT_HEADING = "## Context"

#: What replaces the preamble. Keeps the fact an operator reading the PR cold
#: still needs -- this branch reached review the long way -- without the two
#: assertions that stopped being true at the moment of promotion.
PROMOTED_PREAMBLE = """## Promoted from a rescue draft

This branch's first developer-agent run ended before reaching
`developer_submit_for_review.sh`, so a draft was opened for it (#3862) rather
than leaving the work stranded on the remote. A later run's verification
passed, which is what promoted it: **this PR is a submission for review** --
out of draft, with the closing reference below. The rescue draft's "not a
submission" preamble is replaced here rather than left standing, because a
record that still describes the state before the change is the defect class
#4184 was filed for."""

WIP_TITLE_PREFIX = "wip: "


def promote_body(body: str, issue: int) -> str | None:
    """The submission form of a rescue `body`, or None if it is not a rescue one.

    Two edits, and only two: the preamble is replaced, and the deliberately
    non-closing `Refs #N` becomes `Closes #N` (the PR rules require the native
    closing link, and the rescue draft withholds it on purpose so that merging
    an unfinished draft cannot retire its issue).
    """
    if RESCUE_HEADING not in body:
        return None
    start = body.index(RESCUE_HEADING)
    end = body.find(CONTEXT_HEADING, start)
    if end == -1:
        # No Context section (a hand-edited body). Replace to the end rather
        # than refusing: leaving the preamble is the failure being fixed.
        end = len(body)
    promoted = body[:start] + PROMOTED_PREAMBLE + "\n\n" + body[end:]
    # Word-boundary anchored so `Refs #41840` is not rewritten for issue 4184.
    return re.sub(rf"\bRefs #{issue}\b", f"Closes #{issue}", promoted)


def promote_title(title: str) -> str:
    """`wip: x (#4184)` -> `x (#4184)`; any other title unchanged.

    Only the prefix `developer_ensure_pr_exists.sh` writes is removed, and only
    from the front: a title that legitimately contains the word elsewhere is
    the author's.
    """
    if title.startswith(WIP_TITLE_PREFIX):
        return title[len(WIP_TITLE_PREFIX) :]
    return title


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    promote = sub.add_parser("promote", help="rewrite a rescue PR record as a submission's")
    promote.add_argument("--body-file", required=True, type=Path)
    promote.add_argument("--issue", required=True, type=int)
    promote.add_argument("--title", required=True)
    args = parser.parse_args(argv)

    body = args.body_file.read_text(encoding="utf-8")
    promoted = promote_body(body, args.issue)
    new_title = promote_title(args.title)
    if promoted is None and new_title == args.title:
        return 0
    if promoted is not None:
        args.body_file.write_text(promoted, encoding="utf-8")
    print(new_title)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
