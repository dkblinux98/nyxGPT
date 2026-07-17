from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from interrogate.config import InterrogateConfig
from interrogate.coverage import InterrogateCoverage

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_docstring_coverage_meets_configured_threshold() -> None:
    """`src/nyxgpt` must stay at the docstring coverage configured in
    `[tool.interrogate]` (pyproject.toml). This is the CI-enforced half of
    issue #3225's acceptance criteria: `interrogate` itself isn't wired into
    a GitHub Actions step, so this test is what actually catches a
    regression via the existing `pytest tests/unit/` gate.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    interrogate_cfg = pyproject["tool"]["interrogate"]

    conf = InterrogateConfig(
        fail_under=interrogate_cfg.get("fail-under", 80.0),
        ignore_nested_functions=interrogate_cfg.get("ignore-nested-functions", False),
        ignore_nested_classes=interrogate_cfg.get("ignore-nested-classes", False),
    )
    coverage = InterrogateCoverage(paths=[str(REPO_ROOT / "src" / "nyxgpt")], conf=conf)
    results = coverage.get_coverage()

    missing_paths = sorted(
        f"{file_result.filename} ({file_result.missing} missing)"
        for file_result in results.file_results
        if file_result.missing
    )
    assert results.perc_covered >= conf.fail_under, (
        f"Docstring coverage dropped to {results.perc_covered:.1f}% "
        f"(required: {conf.fail_under}%, missing: {results.missing}/{results.total}).\n"
        f"Files with undocumented items:\n" + "\n".join(missing_paths)
    )
