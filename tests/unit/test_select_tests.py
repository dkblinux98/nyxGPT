"""Guards for the test selector (`scripts/ci/select_tests.py`, #3896).

A selector is only safe if it is wrong in one direction. These tests pin the
properties that make over-selection the failure mode: the floor always runs,
unknown paths escalate to the full suite, and a change to a module still
selects the file that tests it. The last one is the important one -- a selector
that quietly drops the covering test converts a red build into a green one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "select_tests", REPO_ROOT / "scripts" / "ci" / "select_tests.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Registered before execution: @dataclass resolves its own module out of
    # sys.modules, and an unregistered module makes that lookup return None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


selector = _load()


def _targets(*changed: str) -> list[str]:
    return selector.select(list(changed)).pytest_args.split()


def test_the_floor_runs_for_every_change() -> None:
    """No path rule may switch off the guards that protect the agent process."""
    for changed in (
        "docs/api.md",
        "web/src/app/page.tsx",
        "src/nyxgpt/config.py",
        ".github/workflows/ci-tests.yml",
        "scripts/agents/create_issue.sh",
    ):
        selection = selector.select([changed])
        targets = selection.pytest_args
        assert selection.run_python, f"{changed}: python suite disabled entirely"
        if targets == "tests/unit/":
            continue  # full suite includes the floor by definition
        for floor in selector.FLOOR_TESTS:
            assert floor in targets, f"{changed}: floor test {floor} was dropped"


def test_an_unclassified_path_runs_everything() -> None:
    """New directories are safe by default: unknown escalates, never narrows."""
    selection = selector.select(["docker/Dockerfile"])
    assert selection.full
    assert selection.pytest_args == "tests/unit/"
    assert selection.run_web


@pytest.mark.parametrize("path", sorted(selector.FULL_SUITE_PATHS))
def test_selection_invalidating_files_run_everything(path: str) -> None:
    """conftest and pyproject change what every other test means."""
    selection = selector.select([path])
    assert selection.full, f"{path} must force the full suite"
    assert selection.pytest_args == "tests/unit/"


@pytest.mark.parametrize(
    "module,expected_test",
    [
        ("src/nyxgpt/canary.py", "tests/unit/test_canary.py"),
        ("src/nyxgpt/config.py", "tests/unit/test_config.py"),
        ("src/nyxgpt/ops.py", "tests/unit/test_ops.py"),
        ("src/nyxgpt/sessions.py", "tests/unit/test_sessions.py"),
        ("src/nyxgpt/rag/reranker.py", "tests/unit/test_reranker.py"),
    ],
)
def test_a_changed_module_selects_the_test_that_covers_it(module: str, expected_test: str) -> None:
    """The regression this exists to prevent: dropping the covering test.

    Skips rather than passes when the pair does not exist in the tree, so the
    parametrisation cannot silently rot into a vacuous assertion.
    """
    if not (REPO_ROOT / module).exists() or not (REPO_ROOT / expected_test).exists():
        pytest.skip(f"{module} or {expected_test} not present in this tree")
    assert expected_test in _targets(module)


def test_docs_only_changes_skip_the_application_suite() -> None:
    """The win: prose cannot break Python, so it must not pay for the suite."""
    targets = _targets("docs/api.md", "README.md", "product_management/VISION.md")
    assert set(targets) == set(selector.FLOOR_TESTS)
    assert not selector.select(["docs/api.md"]).run_web


def test_web_only_changes_run_web_and_skip_python_beyond_the_floor() -> None:
    selection = selector.select(["web/src/app/page.tsx"])
    assert selection.run_web
    assert set(selection.pytest_args.split()) == set(selector.FLOOR_TESTS)


def test_process_tests_do_not_run_for_application_changes() -> None:
    """A dashboard-builder test cannot be affected by application code.

    This is the half that buys the reduction; it is also the half that would
    hide a regression if the partition were wrong, so it is asserted directly
    rather than inferred from a count.
    """
    targets = _targets("src/nyxgpt/canary.py")
    assert "tests/unit/test_build_dashboard_spend.py" not in targets
    assert "tests/unit/test_canary.py" in targets


def test_application_tests_do_not_run_for_process_changes() -> None:
    targets = _targets("scripts/agents/create_issue.sh")
    assert "tests/unit/test_rag.py" not in targets
    assert any("dashboard" in t or "ledger" in t or "issue" in t for t in targets)


def test_a_changed_test_file_runs_itself() -> None:
    assert "tests/unit/test_canary.py" in _targets("tests/unit/test_canary.py")


def test_a_changed_test_module_does_not_escalate_to_the_full_suite() -> None:
    """The other direction of the helper rule: a real test module still scopes."""
    selection = selector.select(["tests/unit/test_canary.py"])
    assert not selection.full
    assert selection.pytest_args != "tests/unit/"


def _helpers_under_tests() -> list[str]:
    """Every file under `tests/` that pytest would not collect on its own."""
    return sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests").rglob("*.py")
        if not selector._is_test_module(path.name)
    )


def test_no_test_helper_is_ever_handed_to_pytest_by_name() -> None:
    """The #3871 regression: an explicit argument bypasses `python_files`.

    `tests/gh_stub_issue_hygiene.py` is the shell suite's stateful `gh` stub and
    reads `GH_STUB_DIR` at import, because the suite that runs it always sets
    that. Changing it made the selector pass it to pytest by name, the import
    raised KeyError during collection, and collection errors interrupt the run:
    the gate reported a stub failure having executed no tests at all.

    Asserted over the tree rather than over one path, so the next helper added
    beside the two stubs is covered without anyone remembering to come back.
    """
    helpers = _helpers_under_tests()
    assert helpers, "no helper modules found -- this guard would be vacuous"
    for helper in helpers:
        selection = selector.select([helper])
        assert helper not in selection.pytest_args.split(), (
            f"{helper} is not a test module, but the selector hands it to pytest; "
            "an explicit argument is imported whatever its name"
        )
        assert selection.full, f"{helper}: a helper's blast radius is unknown -- must escalate"


def test_an_empty_diff_runs_everything() -> None:
    """No information is not the same as no impact."""
    selection = selector.select([])
    assert selection.full and selection.pytest_args == "tests/unit/"


def test_transitive_dependents_are_selected() -> None:
    """One level deep is how a scoped run misses the regression it was for."""
    affected = selector._source_dependents({"nyxgpt.config"})
    assert len(affected) > 1, "config has dependents; the closure returned only itself"
    assert "nyxgpt.config" in affected


# --------------------------------------------------------------------------
# Deletions: in the diff, not in pytest's argv (#4209 review)
#
# The web half of this class -- a deleted `.tsx` handed to eslint -- is what
# the review found. This is the sibling the sweep turned up: both consumers
# take repo-relative paths out of a diff and hand them to a tool as argv, and
# a diff is a statement about history while argv is a statement about the
# checkout. `pytest tests/unit/test_gone.py` exits 4 on "file or directory
# not found" having executed nothing, which since #4209 is a RED REQUIRED
# CHECK on the PR that did the deleting.
# --------------------------------------------------------------------------

GONE = "tests/unit/test_this_file_was_deleted_by_the_diff.py"


def test_the_fixture_path_really_is_absent() -> None:
    """Everything below is vacuous if someone ever creates this file."""
    assert not (REPO_ROOT / GONE).exists()


def test_a_deleted_test_file_is_not_handed_to_pytest() -> None:
    """A usage error is not a test result: pytest exits 4 and runs nothing."""
    assert GONE not in _targets(GONE)


def test_deleting_a_test_still_runs_the_floor() -> None:
    """Dropping the path must not drop the run.

    The floor guards the agent process and runs for every change (see the
    first test in this file); a diff that only removes a test is still a
    change.
    """
    targets = _targets(GONE)
    assert targets, "a delete-only diff selected nothing at all"
    assert set(selector.FLOOR_TESTS).issubset(targets)


def test_the_surviving_tests_of_a_mixed_diff_are_still_selected() -> None:
    """The fix must not become "a deletion anywhere empties the selection"."""
    assert "tests/unit/test_canary.py" in _targets(GONE, "tests/unit/test_canary.py")


def test_a_deleted_test_is_reported_rather_than_silently_dropped() -> None:
    """`main()` prints the reasons, so a dropped path is visible in the log.

    Selection failures here are silent by construction -- a narrower pytest
    invocation looks exactly like a correct one -- so the only thing between a
    wrong drop and nobody noticing is that it says so.
    """
    reasons = selector.select([GONE]).reasons
    assert any(GONE in reason and "deleted" in reason for reason in reasons), reasons


def test_the_floor_is_filtered_too_not_just_the_changed_paths(tmp_path: Path) -> None:
    """The floor is hardcoded paths, and a diff can delete one of those.

    Filtering only the changed-test branch would leave the same exit-4 in the
    one list that is appended to every single selection.
    """
    gone, *kept = selector.FLOOR_TESTS
    for floor in kept:
        target = tmp_path / floor
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    targets = selector.select([gone], root=tmp_path).pytest_args.split()
    assert gone not in targets
    assert set(kept) == set(targets), "only the deleted floor test may be dropped"


def test_selection_resolves_against_the_given_root_not_the_cwd(tmp_path: Path) -> None:
    """The predicate is rooted, not cwd-relative.

    CI runs these steps from the repo root, which is exactly the kind of
    assumption that holds until one step adds a `cd`.
    """
    present = tmp_path / "tests" / "unit" / "test_only_here.py"
    present.parent.mkdir(parents=True)
    present.write_text("", encoding="utf-8")
    targets = selector.select(["tests/unit/test_only_here.py"], root=tmp_path).pytest_args.split()
    assert "tests/unit/test_only_here.py" in targets
    assert "tests/unit/test_only_here.py" not in _targets("tests/unit/test_only_here.py")
