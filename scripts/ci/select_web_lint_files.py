#!/usr/bin/env python3
"""Which web files a merge should be lint-blocked on (#3940 follow-up).

`ci-tests.yml` blocks on eslint for the files a merge actually touched,
because the tree carries standing debt that would otherwise fail every
unrelated change. Choosing those files is the part that can be wrong
silently, so it lives here with tests rather than inline in the workflow.

The first version used a git pathspec, `-- 'web/**/*.ts' 'web/**/*.tsx'`, and
that form requires an intermediate directory: it does not match
`web/next.config.ts`. A merge editing only that file printed "nothing to
lint" and passed green having linted nothing, while the step promised that
edited web files are clean. Filtering here instead of in the pathspec removes
the class -- there is no pattern language to be subtly wrong in.

The second way it can be wrong is the opposite one, found in #4209's review: a
**deleted** web file is in the diff and not in the checkout, and eslint errors
on an argument it cannot find (`--error-on-unmatched-pattern` defaults to true:
"No files matching the pattern ... were found", exit 2). That is loud rather
than silent, but since #4209 this step blocks a PR head, so the red lands on an
author whose change is fine -- the very thing #4209 removed. The existence
predicate is `diff_scope.still_exists`, shared with the pytest selector because
that one has exactly the same shape; see its docstring.

Reads changed paths on stdin (one per line, repo-relative, as
`git diff --name-only` prints them) and writes the eslint targets, relative
to `web/`, one per line.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

# One source for "is this diff path still in the checkout" (#4209 review).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from diff_scope import REPO_ROOT, still_exists  # noqa: E402

#: eslint is run from inside `web/`, so targets are printed relative to it.
PREFIX = "web/"

#: What the web eslint config actually covers.
EXTENSIONS = (".ts", ".tsx")


def web_lint_targets(paths: Iterable[str], *, root: Path = REPO_ROOT) -> list[str]:
    """The `web/` TS/TSX files among `paths` that `root` still has.

    Order is preserved and duplicates dropped, so the workflow's log reads
    the way the diff does.
    """
    seen: set[str] = set()
    targets: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path.startswith(PREFIX) or not path.endswith(EXTENSIONS):
            continue
        target = path[len(PREFIX) :]
        # A path of exactly "web/" or a directory entry has nothing to lint.
        if not target or target in seen:
            continue
        # A deletion belongs in the diff and not in eslint's argv.
        if not still_exists(path, root=root):
            continue
        seen.add(target)
        targets.append(target)
    return targets


def main() -> int:
    for target in web_lint_targets(sys.stdin):
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
