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


def test_an_empty_diff_runs_everything() -> None:
    """No information is not the same as no impact."""
    selection = selector.select([])
    assert selection.full and selection.pytest_args == "tests/unit/"


def test_transitive_dependents_are_selected() -> None:
    """One level deep is how a scoped run misses the regression it was for."""
    affected = selector._source_dependents({"nyxgpt.config"})
    assert len(affected) > 1, "config has dependents; the closure returned only itself"
    assert "nyxgpt.config" in affected
