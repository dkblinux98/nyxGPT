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

Reads changed paths on stdin (one per line, repo-relative, as
`git diff --name-only` prints them) and writes the eslint targets, relative
to `web/`, one per line.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable

#: eslint is run from inside `web/`, so targets are printed relative to it.
PREFIX = "web/"

#: What the web eslint config actually covers.
EXTENSIONS = (".ts", ".tsx")


def web_lint_targets(paths: Iterable[str]) -> list[str]:
    """The `web/` TS/TSX files among `paths`, relative to `web/`.

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
        seen.add(target)
        targets.append(target)
    return targets


def main() -> int:
    for target in web_lint_targets(sys.stdin):
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
