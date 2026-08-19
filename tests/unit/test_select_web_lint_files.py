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
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "select_web_lint_files.py"
_spec = importlib.util.spec_from_file_location("select_web_lint_files", _MODULE)
assert _spec is not None and _spec.loader is not None
sel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sel)


def test_top_level_web_config_files_are_selected():
    """The regression: `web/next.config.ts` is edited in practice — the
    service-worker work touched it — and the pathspec form silently skipped
    it."""
    assert sel.web_lint_targets(["web/next.config.ts"]) == ["next.config.ts"]
    assert sel.web_lint_targets(["web/vitest.config.ts"]) == ["vitest.config.ts"]


def test_nested_sources_and_tests_are_selected():
    assert sel.web_lint_targets(["web/src/app/page.tsx", "web/tests/app/chat.test.tsx"]) == [
        "src/app/page.tsx",
        "tests/app/chat.test.tsx",
    ]


def test_paths_are_relative_to_web_because_eslint_runs_there():
    assert sel.web_lint_targets(["web/src/lib/logger.ts"]) == ["src/lib/logger.ts"]


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
def test_unrelated_paths_are_not_selected(path):
    assert sel.web_lint_targets([path]) == []


def test_duplicates_collapse_and_order_follows_the_diff():
    assert sel.web_lint_targets(["web/b.ts", "web/a.ts", "web/b.ts"]) == ["b.ts", "a.ts"]


def test_blank_and_whitespace_lines_are_ignored():
    assert sel.web_lint_targets(["", "   ", "\n", "web/a.ts\n"]) == ["a.ts"]


def test_a_merge_with_no_web_changes_selects_nothing():
    """Distinct from the failure above only by intent -- which is why the
    positive cases above have to exist."""
    assert sel.web_lint_targets(["README.md", "src/nyxgpt/app.py"]) == []
