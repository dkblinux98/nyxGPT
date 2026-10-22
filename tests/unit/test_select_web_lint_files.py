"""Which web files the blocking lint runs on (#3938 review finding).

`ci-tests.yml` blocks on eslint for the files a merge touched, because the
tree carries standing debt (#3940) that would otherwise fail every unrelated
change. Choosing those files is the part that can be wrong *silently* — a
selection that returns nothing looks exactly like a merge with no web changes,
and passes green.

Which is what happened: the first version used the git pathspec
`web/**/*.ts`, a form that requires an intermediate directory and therefore
never matched `web/next.config.ts`. A merge editing only that file printed
"nothing to lint" and passed having linted nothing.

The second way it can be wrong is the opposite one, and #4209's review found
it: a **deleted** web file is in the diff and not in the checkout, and eslint
exits 2 on an argument it cannot find. That is loud rather than silent, but it
is red for something that is not the change — and since #4209 this step blocks
a PR head, so the red lands on an author whose PR is fine. Removing a web file
is routine here: #3803, #3804 and #3805 each deleted an admin screen.

Selection is a fact about a checkout, so these tests build one: every case
materialises its paths under `tmp_path` and selects against that root. A test
asserting against the real tree would start passing for the wrong reason the
day someone added a file named like its fixture.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "select_web_lint_files.py"
_spec = importlib.util.spec_from_file_location("select_web_lint_files", _MODULE)
assert _spec is not None and _spec.loader is not None
sel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sel)


def _targets(
    tmp_path: Path,
    paths: Sequence[str],
    *,
    deleted: Iterable[str] = (),
) -> list[str]:
    """Select `paths` against a checkout holding all of them but `deleted`."""
    gone = set(deleted)
    for raw in paths:
        path = raw.strip()
        if not path or path in gone:
            continue
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    return sel.web_lint_targets(paths, root=tmp_path)


def test_top_level_web_config_files_are_selected(tmp_path: Path) -> None:
    """The regression: `web/next.config.ts` is edited in practice — the
    service-worker work touched it — and the pathspec form silently skipped
    it."""
    assert _targets(tmp_path, ["web/next.config.ts"]) == ["next.config.ts"]
    assert _targets(tmp_path, ["web/vitest.config.ts"]) == ["vitest.config.ts"]


def test_nested_sources_and_tests_are_selected(tmp_path: Path) -> None:
    assert _targets(tmp_path, ["web/src/app/page.tsx", "web/tests/app/chat.test.tsx"]) == [
        "src/app/page.tsx",
        "tests/app/chat.test.tsx",
    ]


def test_paths_are_relative_to_web_because_eslint_runs_there(tmp_path: Path) -> None:
    assert _targets(tmp_path, ["web/src/lib/logger.ts"]) == ["src/lib/logger.ts"]


@pytest.mark.parametrize(
    "path",
    [
        "src/nyxgpt/ops.py",  # outside web/
        "web/package.json",  # not linted by the TS config
        "web/public/sw.js",  # not a TS/TSX file
        "docs/web.md",
        "webhooks/handler.ts",  # prefix collision, not the web/ directory
    ],
)
def test_unrelated_paths_are_not_selected(tmp_path: Path, path: str) -> None:
    assert _targets(tmp_path, [path]) == []


def test_duplicates_collapse_and_order_follows_the_diff(tmp_path: Path) -> None:
    assert _targets(tmp_path, ["web/b.ts", "web/a.ts", "web/b.ts"]) == ["b.ts", "a.ts"]


def test_blank_and_whitespace_lines_are_ignored(tmp_path: Path) -> None:
    assert _targets(tmp_path, ["", "   ", "\n", "web/a.ts\n"]) == ["a.ts"]


def test_a_merge_with_no_web_changes_selects_nothing(tmp_path: Path) -> None:
    """Distinct from the failure above only by intent -- which is why the
    positive cases above have to exist."""
    assert _targets(tmp_path, ["README.md", "src/nyxgpt/app.py"]) == []


# --------------------------------------------------------------------------
# Deletions: in the diff, not in eslint's argv (#4209 review)
# --------------------------------------------------------------------------


def test_a_deleted_web_file_is_not_handed_to_eslint(tmp_path: Path) -> None:
    """`eslint web/src/app/gone.tsx` exits 2 on an unmatched pattern.

    `--error-on-unmatched-pattern` defaults to true, so the blocking step
    would go red on a file the PR *removed* -- a gate failing for a reason
    that is not the change, which is the whole of #4209.
    """
    assert (
        _targets(
            tmp_path,
            ["web/src/app/admin/aws/page.tsx"],
            deleted=["web/src/app/admin/aws/page.tsx"],
        )
        == []
    )


def test_the_surviving_files_of_a_mixed_diff_are_still_linted(tmp_path: Path) -> None:
    """The fix must not become "a deletion anywhere disables the lint".

    A screen removal usually edits its neighbours in the same commit, and
    those edits are exactly what the gate is for.
    """
    assert _targets(
        tmp_path,
        ["web/src/app/admin/aws/page.tsx", "web/src/app/admin/page.tsx"],
        deleted=["web/src/app/admin/aws/page.tsx"],
    ) == ["src/app/admin/page.tsx"]


def test_a_rename_lints_the_destination_and_not_the_source(tmp_path: Path) -> None:
    """`git diff --name-only` reports a rename as two paths, one of them gone."""
    assert _targets(
        tmp_path,
        ["web/src/lib/old.ts", "web/src/lib/new.ts"],
        deleted=["web/src/lib/old.ts"],
    ) == ["src/lib/new.ts"]


def test_a_delete_only_web_diff_selects_nothing_rather_than_erroring(tmp_path: Path) -> None:
    """The workflow prints "nothing to lint" and exits 0 on an empty list.

    Safe here precisely because it is not the #3938 case: the web gates still
    ran. `Scope.changed` keeps the deletion, so `web` is true, so `tsc` and
    `vitest --coverage` both evaluated the tree without the file.
    """
    assert (
        _targets(
            tmp_path,
            ["web/src/app/admin/aws/page.tsx", "web/src/app/admin/portability/page.tsx"],
            deleted=[
                "web/src/app/admin/aws/page.tsx",
                "web/src/app/admin/portability/page.tsx",
            ],
        )
        == []
    )


def test_a_directory_path_is_not_a_lint_target(tmp_path: Path) -> None:
    """Belt and braces: the predicate is `is_file()`, not `exists()`.

    git does not name directories in `--name-only`, but eslint given a
    directory lints it recursively -- which on `web/src` is the whole-tree
    debt the blocking step exists to not block on (#3940).
    """
    (tmp_path / "web" / "src" / "app.ts").mkdir(parents=True)
    assert sel.web_lint_targets(["web/src/app.ts"], root=tmp_path) == []
