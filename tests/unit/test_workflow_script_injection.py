"""Regression guard: no ``${{ }}`` interpolation into github-script bodies.

Motivating defect (#3820): the developer agent's fatal-error escalation step
interpolated ``steps.claude_result.outputs.diagnosis`` -- free-form prose --
into a single-quoted JS literal. ``${{ }}`` substitution happens before the
script is parsed, so an apostrophe terminated the literal and the step died
with ``SyntaxError: Unexpected identifier 'issues'`` (run 31959968196). The
step whose whole job is reporting fatal errors crashed before reporting one.

These tests cover both halves: the construct is gone tree-wide, and the
escalation script actually survives hostile text when executed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "agents" / "lib"))

import escalation_script_probe as probe  # noqa: E402
import workflow_script_guard as guard  # noqa: E402

WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


@pytest.fixture(scope="module")
def escalation_script() -> str:
    return probe.extract_script(REPO_ROOT / probe.DEVELOPER_WORKFLOW)


def test_no_expression_interpolated_into_any_script_body() -> None:
    """No workflow may build a github-script body by string substitution."""
    violations = guard.find_violations(guard.workflow_files(WORKFLOW_DIR))
    assert violations == [], (
        "GitHub Actions expressions interpolated into a github-script body "
        "(pass them via `env:` and read `process.env.NAME` instead):\n"
        + "\n".join(f"  {v.format()}" for v in violations)
    )


def test_guard_detects_a_planted_violation(tmp_path: Path) -> None:
    """The guard fails on the exact construct #3820 was about."""
    workflow = tmp_path / "planted.yml"
    workflow.write_text(
        "name: planted\n"
        "on: push\n"
        "jobs:\n"
        "  planted-job:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Planted step\n"
        "        uses: actions/github-script@v7\n"
        "        with:\n"
        "          script: |\n"
        "            const d = '${{ steps.x.outputs.diagnosis }}';\n"
    )
    violations = guard.find_violations([workflow])
    assert len(violations) == 1
    assert violations[0].step == "Planted step"
    assert violations[0].job == "planted-job"
    assert "diagnosis" in violations[0].text


def test_guard_accepts_the_env_shape(tmp_path: Path) -> None:
    """The supported ``env:`` + ``process.env`` shape is not flagged."""
    workflow = tmp_path / "clean.yml"
    workflow.write_text(
        "name: clean\n"
        "on: push\n"
        "jobs:\n"
        "  clean-job:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Clean step\n"
        "        uses: actions/github-script@v7\n"
        "        env:\n"
        "          DIAGNOSIS: ${{ steps.x.outputs.diagnosis }}\n"
        "        with:\n"
        "          script: |\n"
        "            const d = process.env.DIAGNOSIS;\n"
    )
    assert guard.find_violations([workflow]) == []


def test_every_workflow_is_scanned() -> None:
    """A guard that silently scans nothing would pass forever."""
    files = guard.workflow_files(WORKFLOW_DIR)
    assert len(files) > 10
    steps = [s for f in files for s in guard.iter_script_steps(f)]
    assert len(steps) > 10, "no github-script bodies found -- the scan is broken"


def test_escalation_step_passes_values_through_env(escalation_script: str) -> None:
    """The escalation script reads its inputs from the environment."""
    for name in ("ERROR_CLASS", "PHASE2_STATUS", "PHASE3_STATUS", "PHASE3_DIAGNOSIS"):
        assert f"process.env.{name}" in escalation_script
    assert "${{" not in escalation_script


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_escalation_survives_hostile_diagnosis(escalation_script: str) -> None:
    """Executed: the real script body escalates with the text intact.

    Covers the acceptance criterion directly -- apostrophe, double quote,
    backtick, newline and backslash all present in one diagnosis.
    """
    body = probe.probe_fixed(escalation_script)
    assert probe.HOSTILE_DIAGNOSIS in body
    assert "Workflow failed with non-retriable error" in body


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_pre_fix_form_still_reproduces_the_crash(escalation_script: str) -> None:
    """Executed: fault injection. The old construct must still die.

    Without this half the "fixed" test passes on any script that happens to
    avoid the payload, which is how the defect shipped in the first place.
    """
    stderr = probe.probe_vulnerable(escalation_script)
    assert "SyntaxError" in stderr


def test_devolve_reproduces_the_original_construct() -> None:
    """The devolved form is the literal-interpolation shape, unescaped."""
    devolved = probe.devolve_to_vulnerable(
        "const d = process.env.PHASE3_DIAGNOSIS || '';", {"PHASE3_DIAGNOSIS": "it's"}
    )
    assert devolved == "const d = 'it's';"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_probe_rejects_a_script_that_drops_the_diagnosis() -> None:
    """The probe fails a script that runs but loses the text."""
    dropping = (
        "await github.rest.issues.createComment({\n"
        "  owner: context.repo.owner,\n"
        "  repo: context.repo.repo,\n"
        "  issue_number: 1,\n"
        "  body: 'no diagnosis here'\n"
        "});\n"
    )
    with pytest.raises(AssertionError, match="intact"):
        probe.probe_fixed(dropping)


def test_guard_cli_reports_clean_tree() -> None:
    """The CLI used by CI exits 0 on the current tree."""
    result = subprocess.run(
        [sys.executable, "scripts/agents/lib/workflow_script_guard.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "OK:" in result.stdout
