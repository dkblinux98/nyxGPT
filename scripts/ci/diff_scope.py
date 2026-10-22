#!/usr/bin/env python3
"""What this CI run is allowed to look at, decided once (#4209).

`ci-tests.yml` asks "what did this change touch" three times -- to pick the
pytest suites, to decide whether the web gates apply, and to pick the files
eslint blocks on -- and every copy read the answer out of
`github.event.before`. That field exists on a `push` and is empty on a
`pull_request`, which is one of the two halves of #4209: the workflow could
not be pointed at a PR head without each copy independently learning about a
second event shape. Three copies of one decision is three places for the next
divergence (CLAUDE.md, "one source per decision"), so the decision lives here,
once, with tests.

The other half of #4209 is that the jobs were gated on
`github.ref == RELEASE_BRANCH` with no `pull_request` trigger at all, so a
blocking gate was only ever evaluated AFTER the merge that broke it. That is
fixed in the workflow; what this module adds is the part that could be
silently wrong -- resolving a base commit for two different event shapes --
which is exactly the kind of logic `select_web_lint_files.py` was extracted
for after a git pathspec quietly matched nothing (#3938).

Two properties hold it together, both inherited from `select_tests.py`:

* **Unidentified means everything.** A base commit that is absent, unparseable
  or the all-zeros SHA of a branch-creation push selects every gate. The
  failure mode is wasted runner minutes, never a gate that silently did not
  run.
* **The answer is a fact about the diff, not a judgment.** Callers get the
  base SHA and the changed paths; the mapping from paths to gates is the small
  explicit table below.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Where a repo-relative diff path is resolved from. The CI steps all run from
#: the repo root, but resolving against the cwd is the kind of assumption that
#: is silently wrong exactly once.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The events this module understands, and where each one keeps its base.
#: `pull_request_target` is included because it reports against the PR the same
#: way (#4167) -- a gate reachable on it is a gate a review can be racing.
PULL_REQUEST_EVENTS = ("pull_request", "pull_request_target")

#: A `push` that creates a branch sends an all-zeros `before`. It is a valid
#: event with no identifiable predecessor, not an error.
NULL_SHA = "0" * 40

#: Paths that mean the web gates (tsc / eslint / vitest --coverage) apply.
WEB_PREFIXES = ("web/",)

#: Paths that can change Python behavior, and therefore what the pytest gates
#: and the determinism guard are measuring. `tests/` counts: the determinism
#: guard exists because a *test* wrote under the real `$HOME` (#4020).
PYTHON_PREFIXES = ("src/", "tests/", "scripts/", "pyproject.toml")

#: Paths for which the pytest selection in `select_tests.py` is skipped and the
#: whole unit suite runs. Product code and the root conftest can reach any
#: suite, so narrowing on them would be a guess.
FULL_SUITE_PREFIXES = ("src/", "web/", "pyproject.toml", "tests/conftest.py")

#: `git` runner signature: argv (without the leading "git") -> stdout.
GitRunner = Callable[[Sequence[str]], str]


def _git(args: Sequence[str]) -> str:
    """Run `git` and return stdout, raising `CalledProcessError` on failure."""
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@dataclass(frozen=True)
class Scope:
    """What a CI run may scope itself to.

    `identified` is False when no base commit could be resolved. Every flag is
    True in that case -- see the module docstring.
    """

    base: str | None
    changed: tuple[str, ...]
    identified: bool

    @property
    def web(self) -> bool:
        """Do the web gates apply to this change?"""
        return not self.identified or _touches(self.changed, WEB_PREFIXES)

    @property
    def python(self) -> bool:
        """Can this change alter what the Python gates measure?"""
        return not self.identified or _touches(self.changed, PYTHON_PREFIXES)

    @property
    def full_suite(self) -> bool:
        """Must the whole unit suite run, rather than a selected subset?"""
        return not self.identified or _touches(self.changed, FULL_SUITE_PREFIXES)


def _touches(paths: Iterable[str], prefixes: Sequence[str]) -> bool:
    return any(path.startswith(prefixes) for path in paths)


def commit_exists(sha: str | None, git: GitRunner = _git) -> bool:
    """Is `sha` a commit this checkout can diff against?

    The all-zeros SHA is rejected without asking git, because `git rev-parse`
    reports it as a valid object name in some configurations and the answer
    "there is no predecessor" is the same either way.
    """
    if not sha or sha == NULL_SHA:
        return False
    try:
        git(["rev-parse", "--quiet", "--verify", f"{sha}^{{commit}}"])
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


def base_sha(
    event_name: str,
    *,
    push_before: str | None,
    pr_base: str | None,
    head: str | None = None,
    git: GitRunner = _git,
) -> str | None:
    """The commit this run's diff is measured from, or None.

    On a `push` that is `github.event.before`. On a pull request it is the
    merge base of the PR's base commit and this head -- not `base.sha` itself,
    which is the base branch tip as of the last synchronize and therefore
    carries every commit the base branch gained since, attributing other
    people's merges to this PR's diff.

    That property depends on `head` being the PR head rather than the merge
    commit GitHub puts in `GITHUB_SHA`; see `head_sha`.
    """
    if event_name in PULL_REQUEST_EVENTS:
        if not commit_exists(pr_base, git):
            return None
        assert pr_base is not None  # narrowed by commit_exists
        if not head:
            return pr_base
        try:
            merged = git(["merge-base", pr_base, head]).strip()
        except (subprocess.CalledProcessError, OSError):
            # A shallow clone can leave the two histories unconnected. The base
            # tip is a worse answer than the merge base and a far better one
            # than "run everything".
            return pr_base
        return merged or pr_base
    if event_name == "push":
        return push_before if commit_exists(push_before, git) else None
    # workflow_dispatch, schedule, issue_comment, ... -- no predecessor is
    # implied by the event, so nothing is scoped away.
    return None


def head_sha(
    event_name: str,
    *,
    pr_head: str | None,
    github_sha: str,
    git: GitRunner = _git,
) -> str:
    """The commit whose content this run is measuring.

    On a `push` that is `GITHUB_SHA`. On a pull request `GITHUB_SHA` is the
    *merge* commit GitHub synthesises for the event, whose first parent is the
    base branch tip -- so `base.sha` is an ancestor of it and
    `git merge-base base.sha $GITHUB_SHA` returns `base.sha` itself, every
    time. The merge-base property `base_sha` documents then holds against a
    fake git and never in production. Measuring from the PR head instead makes
    the documented behavior the real one: the diff is this PR's own commits and
    nobody else's merges.

    Falls back to `GITHUB_SHA` when the head commit is not in the checkout --
    a shallow or single-branch fetch can leave it unreachable, and the merge
    commit is a worse subject than the head and a much better one than nothing.
    """
    if event_name in PULL_REQUEST_EVENTS and commit_exists(pr_head, git):
        assert pr_head is not None  # narrowed by commit_exists
        return pr_head
    return github_sha


def still_exists(path: str, *, root: Path = REPO_ROOT) -> bool:
    """Does this diff path still name a file in the checkout?

    A diff is a statement about history, and a **deletion is part of it**:
    removing a `web/` page moves coverage and behavior, so `Scope.changed`
    keeps deleted paths and the `web` / `python` / `full_suite` decisions must
    keep counting them. What cannot survive the deletion is handing the path to
    a tool as an *argument*:

    * `eslint web/src/app/gone.tsx` exits 2 -- "No files matching the pattern
      ... were found" (`--error-on-unmatched-pattern` defaults to true);
    * `pytest tests/unit/test_gone.py` exits 4 -- "file or directory not
      found", having run nothing.

    Either way the gate is red for something that is not the change, which is
    the precise defect #4209 exists to remove -- and now that these gates block
    a PR head, that red is handed back to an author whose PR does not have it.
    Deleting files is routine here: #3803, #3804 and #3805 each removed an
    admin screen.

    So deletions stay in the diff, and every consumer that turns a diff path
    into an argv entry filters through this one predicate. Two consumers
    correcting it separately would be two places for the next divergence
    (CLAUDE.md, "one source per decision").

    `is_file()` and not `exists()`: git never names a directory in
    `--name-only`, but eslint handed one lints it recursively, which on
    `web/src` is the standing debt the blocking step exists to not block on.
    """
    return (root / path).is_file()


def existing(paths: Iterable[str], *, root: Path = REPO_ROOT) -> list[str]:
    """`paths`, in order, minus the ones this checkout no longer has."""
    return [path for path in paths if still_exists(path, root=root)]


def changed_paths(base: str, head: str, git: GitRunner = _git) -> tuple[str, ...]:
    """Repo-relative paths that differ between `base` and `head`."""
    out = git(["diff", "--name-only", base, head])
    return tuple(line.strip() for line in out.splitlines() if line.strip())


def resolve(
    event_name: str,
    *,
    push_before: str | None,
    pr_base: str | None,
    head: str,
    git: GitRunner = _git,
) -> Scope:
    """The whole decision: base commit plus the paths it reaches."""
    base = base_sha(event_name, push_before=push_before, pr_base=pr_base, head=head, git=git)
    if base is None:
        return Scope(base=None, changed=(), identified=False)
    try:
        changed = changed_paths(base, head, git)
    except (subprocess.CalledProcessError, OSError):
        return Scope(base=None, changed=(), identified=False)
    return Scope(base=base, changed=changed, identified=True)


def github_output(scope: Scope) -> str:
    """`$GITHUB_OUTPUT` lines for `scope`.

    `changed` uses the heredoc form because a diff is multi-line, and the
    delimiter is one git cannot produce in a path.
    """
    lines = [
        f"base={scope.base or ''}",
        f"identified={str(scope.identified).lower()}",
        f"web={str(scope.web).lower()}",
        f"python={str(scope.python).lower()}",
        f"full_suite={str(scope.full_suite).lower()}",
        "changed<<NYXGPT_DIFF_SCOPE_EOF",
        *scope.changed,
        "NYXGPT_DIFF_SCOPE_EOF",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="print $GITHUB_OUTPUT lines instead of a human summary",
    )
    args = parser.parse_args(argv)

    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    head = head_sha(
        event_name,
        pr_head=os.environ.get("PR_HEAD_SHA"),
        github_sha=os.environ.get("GITHUB_SHA", "HEAD"),
    )
    scope = resolve(
        event_name,
        push_before=os.environ.get("PUSH_BEFORE_SHA"),
        pr_base=os.environ.get("PR_BASE_SHA"),
        head=head,
    )

    if args.github_output:
        sys.stdout.write(github_output(scope))
        # The log needs the reasoning too: a run that scoped a gate away must
        # say so on stderr, or the next person reads a green check and cannot
        # tell whether the gate ran.
        if scope.identified:
            print(
                f"diff_scope: {event_name} {scope.base[:12] if scope.base else '-'}"
                f"..{head[:12]} -- {len(scope.changed)} path(s); "
                f"web={scope.web} python={scope.python} full_suite={scope.full_suite}",
                file=sys.stderr,
            )
        else:
            print(
                f"diff_scope: {event_name} has no identifiable base commit -- " "every gate runs",
                file=sys.stderr,
            )
        return 0

    print(f"event:      {event_name}")
    print(f"base:       {scope.base or '(unidentified)'}")
    print(f"head:       {head}")
    print(f"web:        {scope.web}")
    print(f"python:     {scope.python}")
    print(f"full_suite: {scope.full_suite}")
    for path in scope.changed:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
