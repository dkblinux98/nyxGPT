#!/usr/bin/env python3
"""Blob-level "has this branch's content landed?" check (#3862).

The only question that may authorise deleting a branch is *"is every byte this
branch carries already on the release branch?"*. Every cheaper signal was
disproven against a real three-branch set on 2026-08-18 (issue #3862), and each
one inverts the truth on that set:

===========================  ======================  ======================
signal                       fully-landed branch     stranded branches
===========================  ======================  ======================
commits not on target        3 -- looks most stale   1 each -- look landed
``git branch --merged``      not merged              not merged
merge conflicts              conflicts               would merge cleanly
branch age / "no PR exists"  identical to the others identical
===========================  ======================  ======================

A cleanup keyed on any of them keeps the redundant branch and deletes the two
holding the only copy of 438 lines of test coverage. Content comparison is not
an optimisation on ancestry -- it is the only one of the two that is correct,
because rebase-and-reapply (and squash-merge) land every byte while leaving the
original branch showing unmerged commits forever.

The rule this module implements, for every path the branch touched since it
diverged from the base:

* present on the branch, absent from the base            -> **stranded**
* deleted on the branch, still present on the base       -> **stranded**
  (the branch carries a deletion that never landed)
* present on both, identical blob                        -> landed
* present on both, different blob, base is a **superset**
  (``git diff base branch -- path`` adds zero lines)     -> landed
* present on both, different blob, branch adds lines     -> **stranded**

The superset case is the one every intuitive implementation gets wrong: it is
``agents/LEDGER.md`` on a branch whose work was re-applied elsewhere, where the
base simply moved ahead. It is also why a merge *conflict* proves nothing --
git compares each side to the merge base and never asks whether one side is a
superset of the other.

Anything not positively proven landed is **reported, never deleted**. The
functions here fail toward "not landed" on every error, including a git
invocation that does not run at all.

Pure-ish functions over a git directory, so the classification is unit-testable
against real planted repositories (``tests/unit/test_branch_content.py``); the
CLI at the bottom is what the shell callers use::

    python3 scripts/agents/lib/branch_content.py landed \\
        --base origin/v3.0.0 --branch origin/feat/1234-thing
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field

#: git's null blob/tree marker in `ls-tree`/`diff` output.
MISSING = ""


@dataclass(frozen=True)
class Verdict:
    """The answer, plus the evidence for it.

    `landed` is the only field a caller may branch on for a *deletion*.
    `stranded` exists so the report can name what would have been destroyed --
    a cleanup that refuses is only useful if it says which file made it refuse.
    """

    landed: bool
    reason: str
    stranded: list[str] = field(default_factory=list)


class GitError(RuntimeError):
    """A git invocation this check depends on could not be trusted."""


def _git(repo: str, *args: str, check: bool = True) -> str:
    """Run git in `repo` and return stdout.

    `check=False` is for the probes whose non-zero exit *is* the answer
    (`merge-base --is-ancestor`); everything else raises, because a git command
    that failed tells us nothing and "tells us nothing" must never read as
    "safe to delete".
    """
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # git missing, repo unreadable
        raise GitError(f"git {' '.join(args)} could not run: {exc}") from exc
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip() or 'no stderr'}")
    return result.stdout


def _rev_parse(repo: str, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def _blob_sha(repo: str, ref: str, path: str) -> str:
    """The blob SHA of `path` at `ref`, or MISSING when the tree has no such path."""
    out = _git(repo, "ls-tree", "-z", "--full-tree", ref, "--", path, check=False)
    if not out.strip("\0").strip():
        return MISSING
    entry = out.split("\0")[0]
    # "<mode> <type> <sha>\t<path>"
    meta = entry.split("\t", 1)[0].split()
    if len(meta) < 3 or meta[1] != "blob":
        # A directory (or submodule) at this path: not a file we can compare.
        return MISSING
    return meta[2]


def _changed_paths(repo: str, merge_base: str, branch: str) -> list[str]:
    """Every path the branch changed since diverging from the base.

    `--no-renames` on purpose: a rename must be seen as the delete plus the add
    it is, so the *added* path is checked for presence on the base. Collapsing
    it into one rename entry would let a file that only exists under its new
    name on the branch pass unexamined.
    """
    out = _git(repo, "diff", "--no-renames", "--name-only", "-z", merge_base, branch)
    return [p for p in out.split("\0") if p]


def _added_line_count(repo: str, base: str, branch: str, path: str) -> int | None:
    """Lines `path` gains going base -> branch. None when the diff is binary.

    Zero additions means the base already contains every line the branch's copy
    has, in order -- the base is a superset and the branch adds nothing. That is
    the ``agents/LEDGER.md`` shape on a rebased-but-landed branch.
    """
    out = _git(repo, "diff", "--no-renames", "--numstat", base, branch, "--", path).strip()
    if not out:
        return 0
    added = out.split("\n")[0].split("\t", 1)[0]
    if added == "-":  # binary
        return None
    try:
        return int(added)
    except ValueError:  # pragma: no cover - git always emits a number or "-"
        return None


def branch_content_landed(repo: str, base: str, branch: str) -> Verdict:
    """Is every byte `branch` carries already present on `base`?

    Returns a Verdict; never raises for an ordinary "cannot tell" (bad ref,
    unrelated histories, git unavailable) -- those all resolve to
    ``landed=False`` so the caller reports instead of deleting.
    """
    try:
        base_sha = _rev_parse(repo, base)
        branch_sha = _rev_parse(repo, branch)
    except GitError as exc:
        return Verdict(False, f"cannot resolve refs ({exc}) -- refusing to call it landed")

    if base_sha == branch_sha:
        return Verdict(True, f"{branch} is exactly {base}")

    try:
        # Ancestry is a *sufficient* condition, never a necessary one. Kept
        # first only because it is the cheapest true answer, not because it is
        # the check that matters.
        ancestor = subprocess.run(
            ["git", "-C", repo, "merge-base", "--is-ancestor", branch_sha, base_sha],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return Verdict(False, f"git could not run ({exc}) -- refusing to call it landed")
    if ancestor.returncode == 0:
        return Verdict(True, f"{branch} is an ancestor of {base}")

    try:
        merge_base = _git(repo, "merge-base", base_sha, branch_sha, check=False).strip()
        if not merge_base:
            return Verdict(
                False,
                f"{branch} and {base} have no common ancestor -- nothing to compare against",
            )
        paths = _changed_paths(repo, merge_base, branch_sha)
    except GitError as exc:
        return Verdict(False, f"cannot diff {branch} against {base} ({exc})")

    if not paths:
        return Verdict(True, f"{branch} changes no file relative to its merge base with {base}")

    stranded: list[str] = []
    for path in paths:
        try:
            on_branch = _blob_sha(repo, branch_sha, path)
            on_base = _blob_sha(repo, base_sha, path)
        except GitError as exc:
            stranded.append(f"{path} (cannot read: {exc})")
            continue

        if on_branch == MISSING and on_base == MISSING:
            continue  # deleted on the branch, and gone from the base too
        if on_branch == MISSING:
            stranded.append(f"{path} (deleted on the branch, still present on {base})")
            continue
        if on_base == MISSING:
            stranded.append(f"{path} (present on the branch, absent from {base})")
            continue
        if on_branch == on_base:
            continue

        try:
            added = _added_line_count(repo, base_sha, branch_sha, path)
        except GitError as exc:
            stranded.append(f"{path} (cannot diff: {exc})")
            continue
        if added is None:
            stranded.append(f"{path} (binary, and the two versions differ)")
        elif added > 0:
            stranded.append(f"{path} ({added} line(s) exist only on the branch)")

    if stranded:
        return Verdict(
            False,
            f"{len(stranded)} path(s) carry content that is not on {base}",
            stranded,
        )
    return Verdict(
        True,
        f"every one of the {len(paths)} path(s) {branch} touches is already on {base}",
    )


def _cmd_landed(args: argparse.Namespace) -> int:
    verdict = branch_content_landed(args.repo, args.base, args.branch)
    print("LANDED" if verdict.landed else "UNLANDED")
    print(verdict.reason, file=sys.stderr)
    for item in verdict.stranded:
        print(f"  - {item}", file=sys.stderr)
    return 0 if verdict.landed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decide whether a branch's content is provably on a base branch."
    )
    parser.add_argument("--repo", default=".", help="git working directory (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_landed = sub.add_parser(
        "landed",
        help="print LANDED/UNLANDED; exit 0 only when the branch is provably landed",
    )
    p_landed.add_argument("--base", required=True, help="base ref, e.g. origin/v3.0.0")
    p_landed.add_argument("--branch", required=True, help="branch ref, e.g. origin/feat/1-x")
    p_landed.set_defaults(func=_cmd_landed)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
