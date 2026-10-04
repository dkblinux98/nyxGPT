#!/usr/bin/env python3
"""Post-merge branch residue: "a PR exists" is not "the work landed" (#4151).

WHAT WENT WRONG. A developer run that is still in flight when its PR is
merged pushes the rest of its work to a branch whose pull request has already
closed. On 2026-10-04 a `git log` sweep found this on `fix/3986-*`:

    04:24:54Z   PR #4126 merged (merge commit 3629e4f82c, onto the release branch)
    04:43:46Z   b7b3b096 pushed to the SAME branch, 19 minutes later

`b7b3b096` is 579 insertions across 10 files -- a CI job, a 214-line test
module, `src/nyxgpt/ops.py` and a dashboard page -- implementing #3986's
acceptance criterion 4, the one the owner could not test. `git cherry` against
the release branch reports `+` for it: the change is present there in no form
at all. #3986 is closed and sits in
`For Release`, so it reads as accepted.

WHY EVERY EXISTING NET MISSED IT. `developer_ensure_pr_exists.sh` (#3862) asks
"does this branch have a pull request?". This branch HAD one; it was merged. So
the backstop found a PR, concluded the work was accounted for, and skipped. The
review path had nothing to review (the PR was closed), the issue was already
closed so nothing dispatched against it, and the promotion sweep saw an issue
already in `For Release`.

The hole is the shape of the question. **A merged pull request is evidence
about the commits it contained, not about every commit the branch will ever
hold** -- the same distinction CLAUDE.md already draws for the merge itself
("'The merge command exited 0' is a report, not evidence"). The replacement
question is asked of git, not of the PR list: does `origin/<branch>` carry
commits that are not on the release branch? That is one `git rev-list --count`,
and it is what found this.

So the classification below is in two halves, and both are needed:

  `pr_state`     the PR list for one head -> which of four situations this
                 branch is in, with the PR numbers that say so. `open` beats
                 `merged` beats `closed-unmerged` beats `none`, because an open
                 PR means the branch's CURRENT head is already routed for
                 review and opening a second one would duplicate it.

  `disposition`  that state plus the git facts -> what to DO. Only the
                 `merged` state consults git; the other three are decided by
                 the PR list alone, which is why the caller computes the git
                 facts lazily.

The two cases must not mask each other (#4151 acceptance criterion 3).
`no-pr` is #3862's case and keeps #3862's handling (a draft rescue PR, or
deletion when the content is provably on the base). `merged-residue` is this
issue's case: a NEW draft PR for the residue plus a loud comment on the
originating issue naming the branch and the shas, because that issue is closed
and its board lane says "accepted" while part of its implementation is not on
the release branch.

WHY `content_landed` IS AN EXEMPTION, AND WHY ONLY A PROVEN ONE COUNTS. A
branch can be ahead by commits whose *content* is already on the base -- a
squash merge rewrites history, so every commit of a squash-merged branch
reports as unmerged, and a cherry-pick or a rebase-and-reapply does the same
to part of one. Surfacing those would make the guard cry wolf on every
squash-merged branch in the repo, and a guard that always fires is one nobody
reads. So a positive proof from `lib/branch_content.py` (every path the branch
touches is already identical on the base) clears the branch.

It must be a POSITIVE proof. `branch_content.py` fails closed -- a missing
python3, an unreadable ref or any internal error means "not proven landed" --
and this classifier preserves that direction by treating anything other than a
proven `True` as residue. The two guards point opposite ways on purpose: for
DELETION the unproven case must keep the branch, and for REPORTING the unproven
case must surface it. Both choices are "never silently lose the work".
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from typing import Any

# PR-list states, strongest-signal first. `unreadable` is not a state of the
# branch but of our knowledge of it: a transient REST failure must never be
# read as "there are no pull requests" (the mistake that would open a duplicate
# PR on every rate limit -- see developer_ensure_pr_exists.sh's PR-count guard
# and tests/test_ensure_pr_exists.sh case 1).
STATE_UNREADABLE = "unreadable"
STATE_OPEN = "open"
STATE_MERGED = "merged"
STATE_CLOSED_UNMERGED = "closed-unmerged"
STATE_NONE = "none"

# Dispositions -- what the caller should do about the branch.
DISPOSITION_UNKNOWN = "unknown"  # knowledge failure: do nothing, say so
DISPOSITION_OPEN_PR = "open-pr"  # already routed for review
DISPOSITION_MERGED_RESIDUE = "merged-residue"  # #4151: surface it
DISPOSITION_MERGED_CLEAN = "merged-clean"  # merged, nothing of its own left
DISPOSITION_ABANDONED = "abandoned"  # closed without merge: a decision
DISPOSITION_NO_PR = "no-pr"  # #3862: rescue or delete

# `--content-landed` accepts a tri-state rather than a flag, because "we could
# not tell" and "it has not landed" must be distinguishable at the call site
# even though they lead to the same action here.
CONTENT_LANDED_TRUE = "true"
CONTENT_LANDED_FALSE = "false"
CONTENT_LANDED_UNKNOWN = "unknown"


def _flatten(pulls: Any) -> list[dict[str, Any]]:
    """Accept either a flat PR array or `gh --paginate`'s array-of-pages.

    `gh api --paginate` emits one JSON array per page rather than one merged
    array (AGENTS.md), so callers slurp the pages. Flattening one level here
    means a caller that forgot to is not silently classified as "no pull
    requests" -- which is the single most dangerous misreading in this file.
    """
    if pulls is None:
        return []
    if isinstance(pulls, dict):
        return [pulls]
    out: list[dict[str, Any]] = []
    for item in pulls:
        if isinstance(item, list):
            out.extend(x for x in item if isinstance(x, dict))
        elif isinstance(item, dict):
            out.append(item)
    return out


def _numbers(prs: Iterable[dict[str, Any]]) -> list[int]:
    """The PR numbers of `prs`, sorted, skipping anything unnumbered."""
    return sorted({int(pr["number"]) for pr in prs if str(pr.get("number", "")).isdigit()})


def pr_state(pulls: Any, base_ref: str) -> dict[str, Any]:
    """Classify one head branch's pull requests.

    `pulls` is the `repos/{repo}/pulls?head=...&state=all` payload (flat or
    paginated), or None when the call failed. `base_ref` is the release branch.

    Returns `{"state": ..., "open_prs": [...], "merged_prs": [...],
    "closed_unmerged_prs": [...]}`.

    Base filtering differs by state, deliberately:

      * `merged` and `closed-unmerged` are filtered to `base_ref`. A PR merged
        into some other branch says nothing about whether this work reached the
        release branch, and that is the only question being asked.
      * `open` is NOT filtered. Any open PR on this head means a human or the
        review path is already looking at these commits; opening a second PR
        for the same head would duplicate it whatever its base. Conservative in
        the direction that cannot lose work.
    """
    if pulls is None:
        return {
            "state": STATE_UNREADABLE,
            "open_prs": [],
            "merged_prs": [],
            "closed_unmerged_prs": [],
        }

    prs = _flatten(pulls)
    open_prs = [pr for pr in prs if pr.get("state") == "open"]
    merged_prs = [
        pr for pr in prs if pr.get("merged_at") and (pr.get("base") or {}).get("ref") == base_ref
    ]
    closed_unmerged = [
        pr
        for pr in prs
        if pr.get("state") == "closed"
        and not pr.get("merged_at")
        and (pr.get("base") or {}).get("ref") == base_ref
    ]

    if open_prs:
        state = STATE_OPEN
    elif merged_prs:
        state = STATE_MERGED
    elif closed_unmerged:
        state = STATE_CLOSED_UNMERGED
    else:
        state = STATE_NONE

    return {
        "state": state,
        "open_prs": _numbers(open_prs),
        "merged_prs": _numbers(merged_prs),
        "closed_unmerged_prs": _numbers(closed_unmerged),
    }


def disposition(
    state: str,
    unmerged_commits: int | None = None,
    content_landed: str = CONTENT_LANDED_UNKNOWN,
) -> str:
    """What to do about a branch in PR-list `state`.

    `unmerged_commits` (`git rev-list --count origin/<base>..origin/<branch>`)
    and `content_landed` are consulted ONLY in the `merged` state; callers pass
    them lazily so a branch with an open PR costs no git work at all.

    A merged branch with commits the base does not have is `merged-residue`
    unless the content is PROVEN to be on the base already (squash merge,
    cherry-pick, rebase-and-reapply). `unknown` is not a proof -- see the
    module docstring on why this guard and the deletion guard treat the
    unproven case in opposite directions.
    """
    if state == STATE_UNREADABLE:
        return DISPOSITION_UNKNOWN
    if state == STATE_OPEN:
        return DISPOSITION_OPEN_PR
    if state == STATE_CLOSED_UNMERGED:
        return DISPOSITION_ABANDONED
    if state == STATE_NONE:
        return DISPOSITION_NO_PR
    if state != STATE_MERGED:
        # An unrecognised state is a caller bug, and a caller bug must not be
        # answered with an action. Report ignorance instead.
        return DISPOSITION_UNKNOWN

    if unmerged_commits is None:
        # The git half was never computed, so the question was not asked. Do
        # not answer it from the PR list -- that is the whole defect.
        return DISPOSITION_UNKNOWN
    if unmerged_commits <= 0:
        return DISPOSITION_MERGED_CLEAN
    if content_landed == CONTENT_LANDED_TRUE:
        return DISPOSITION_MERGED_CLEAN
    return DISPOSITION_MERGED_RESIDUE


def main(argv: Sequence[str]) -> int:
    """CLI: `pr-state` (stdin: the pulls payload) and `disposition`."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_state = sub.add_parser("pr-state", help="classify one head's pull requests")
    p_state.add_argument("--base", required=True, help="release branch name")

    p_disp = sub.add_parser("disposition", help="state + git facts -> action")
    p_disp.add_argument("--state", required=True)
    p_disp.add_argument(
        "--unmerged-count",
        default="",
        help="git rev-list --count origin/<base>..origin/<branch>; empty = not computed",
    )
    p_disp.add_argument(
        "--content-landed",
        default=CONTENT_LANDED_UNKNOWN,
        choices=[CONTENT_LANDED_TRUE, CONTENT_LANDED_FALSE, CONTENT_LANDED_UNKNOWN],
    )

    args = parser.parse_args(argv)

    if args.cmd == "pr-state":
        raw = sys.stdin.read().strip()
        try:
            pulls = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            # Unparseable is a knowledge failure, not an empty list.
            pulls = None
        print(json.dumps(pr_state(pulls, args.base)))
        return 0

    count: int | None
    text = str(args.unmerged_count).strip()
    if text == "":
        count = None
    elif text.lstrip("-").isdigit():
        count = int(text)
    else:
        count = None
    print(disposition(args.state, count, args.content_landed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
