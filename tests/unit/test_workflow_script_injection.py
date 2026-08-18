"""Regression guard: no ``${{ }}`` interpolation into an executable body.

Motivating defect (#3820): the developer agent's fatal-error escalation step
interpolated ``steps.claude_result.outputs.diagnosis`` -- free-form prose --
into a single-quoted JS literal. ``${{ }}`` substitution happens before the
script is parsed, so an apostrophe terminated the literal and the step died
with ``SyntaxError: Unexpected identifier 'issues'`` (run 31959968196). The
step whose whole job is reporting fatal errors crashed before reporting one.

**Widened in #3837 (CodeQL alert #124, critical).** #3820 named the fault as
"a ``script:`` body is JavaScript, not data" and swept exactly that; a ``run:``
block is *shell* source substituted by the same pre-parse pass, and the same
injection was still live in ``huddle_decision_dispatch.yml`` -- a step output
substituted into a ``run:`` body that then built a nested ``bash -lc "..."``
command string, escaping two shells, in a job holding an agent token.

These tests cover both halves for both body kinds: the construct is gone
tree-wide, the guard demonstrably rejects a seeded instance of each (a scanner
that silently stopped scanning would otherwise pass forever), and the
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
import run_block_injection_probe as run_probe  # noqa: E402
import workflow_script_guard as guard  # noqa: E402

WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"


@pytest.fixture(scope="module")
def escalation_script() -> str:
    return probe.extract_script(REPO_ROOT / probe.DEVELOPER_WORKFLOW)


def _planted(tmp_path: Path, name: str, step_body: str) -> Path:
    """Write a one-step workflow whose step body is ``step_body``."""
    workflow = tmp_path / f"{name}.yml"
    workflow.write_text(
        "name: planted\n"
        "on: push\n"
        "jobs:\n"
        "  planted-job:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Planted step\n" + step_body
    )
    return workflow


def test_no_expression_interpolated_into_any_executable_body() -> None:
    """No workflow may build a script: or run: body by string substitution."""
    violations = guard.find_violations(guard.workflow_files(WORKFLOW_DIR))
    assert violations == [], (
        "GitHub Actions expressions interpolated into an executable body "
        '(pass them via `env:` and read `process.env.NAME` / "$NAME" instead):\n'
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


# --- the run: half (#3837, CodeQL #124) ---------------------------------------


def test_guard_detects_a_planted_run_block_violation(tmp_path: Path) -> None:
    """The guard fails on the exact construct CodeQL #124 was about.

    A step output substituted into a `run:` body, which then builds a nested
    `bash -lc "..."` command string -- two shells parse the value.
    """
    workflow = _planted(
        tmp_path,
        "planted-run",
        "        run: |\n"
        '          DECISION="${{ steps.decide.outputs.decision }}"\n'
        "          bash -lc \"echo '$DECISION'\"\n",
    )
    violations = guard.find_violations([workflow])
    assert len(violations) == 1
    assert violations[0].kind == "run"
    assert violations[0].step == "Planted step"
    assert "decision" in violations[0].text


def test_guard_accepts_the_env_shape_in_a_run_block(tmp_path: Path) -> None:
    """The supported ``env:`` + ``"$NAME"`` shape is not flagged."""
    workflow = _planted(
        tmp_path,
        "clean-run",
        "        env:\n"
        "          DECISION: ${{ steps.decide.outputs.decision }}\n"
        "        run: |\n"
        '          echo "$DECISION"\n',
    )
    assert guard.find_violations([workflow]) == []


def test_guard_allows_repo_controlled_expressions_in_a_run_block(tmp_path: Path) -> None:
    """`vars.*` and generated run identity stay inline -- see SAFE_IN_RUN."""
    workflow = _planted(
        tmp_path,
        "safe-run",
        "        run: |\n"
        '          gh api "repos/${{ github.repository }}/issues/${{ github.event.issue.number }}"\n'
        '          echo "${{ vars.RELEASE_BRANCH }} ${{ github.run_id }} ${{ runner.temp }}"\n',
    )
    assert guard.find_violations([workflow]) == []


@pytest.mark.parametrize(
    "expression",
    [
        # A branch name may legally contain a single quote; GitHub's own
        # canonical `run:` injection example.
        "github.head_ref",
        "github.ref_name",
        # Free-text event fields.
        "github.event.issue.title",
        "github.event.comment.body",
        "github.event.pull_request.head.ref",
        # Dispatch inputs are typed by whoever dispatched the run.
        "inputs.version",
        "github.event.inputs.dry_run",
        # Derived values whose provenance is not visible at the use site --
        # the #3820 lesson: `diagnosis` looked fine here too.
        "steps.x.outputs.y",
        "needs.build.outputs.tag",
        "matrix.pr.number",
        # A secret is substituted as source like anything else, and is the
        # one value that must never reach an xtrace line.
        "secrets.DEVELOPER_AGENT_TOKEN",
        # Not a bare context read: the result shape is not visible here.
        "format('{0}', github.event.issue.title)",
        "github.event.inputs.dry_run || 'false'",
    ],
)
def test_guard_rejects_unconstrained_expressions_in_a_run_block(
    tmp_path: Path, expression: str
) -> None:
    """Anything outside SAFE_IN_RUN must go through ``env:``."""
    assert not guard.is_safe_in_run(expression)
    workflow = _planted(
        tmp_path,
        "planted-" + expression.replace(".", "-").replace("|", "-").replace(" ", ""),
        "        run: |\n" + '          echo "${{ ' + expression + ' }}"\n',
    )
    assert len(guard.find_violations([workflow])) == 1


def test_guard_reports_an_unbalanced_expression_in_a_run_block(tmp_path: Path) -> None:
    """Unclassifiable is not the same as safe.

    A `${{` the line-scanner cannot pair with a `}}` (a multi-line
    expression, or a typo) must be reported rather than silently allowed --
    otherwise the allowlist becomes a way to hide from the guard.
    """
    workflow = _planted(
        tmp_path,
        "unbalanced-run",
        '        run: |\n          echo "${{ vars.A\n          }}"\n',
    )
    assert len(guard.find_violations([workflow])) == 1


def test_every_run_body_is_scanned() -> None:
    """A run: scan that found no bodies would pass forever."""
    files = guard.workflow_files(WORKFLOW_DIR)
    kinds = [s.kind for f in files for s in guard.iter_script_steps(f)]
    assert kinds.count("run") > 100, "no run: bodies found -- the scan is broken"
    assert kinds.count("script") > 10, "no script: bodies found -- the scan is broken"


def test_the_run_sweep_actually_had_something_to_sweep() -> None:
    """The allowlist must not be so wide that it permits everything.

    #3837 fixed 71 sites out of 593 interpolations. If a later edit widened
    SAFE_IN_RUN until nothing could ever be flagged, every test above would
    still pass -- this one would not.
    """
    for expression in ("steps.x.outputs.y", "github.head_ref", "inputs.anything"):
        assert not guard.is_safe_in_run(expression)


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


# --- executed: the real run: body under a hostile step output (#3837, D-006) ---


@pytest.fixture(scope="module")
def dispatch_body() -> str:
    return run_probe.extract_run(REPO_ROOT / run_probe.WORKFLOW)


def test_dispatch_step_reads_its_values_from_the_environment(dispatch_body: str) -> None:
    """The step under CodeQL #124 takes nothing by substitution."""
    assert "${{" not in dispatch_body
    for name in ("$DECISION", "$ISSUE", "$PR"):
        assert name in dispatch_body


def test_nested_login_shell_no_longer_interpolates(dispatch_body: str) -> None:
    """The inner `bash -lc` script is a literal; the issue number is argv.

    A double-quoted inner command string is how the value got parsed by a
    *second* shell -- the half of #124 that made it critical.
    """
    assert 'bash -lc "' not in dispatch_body
    assert "bash -lc '" in dispatch_body


def test_pre_fix_run_body_still_executes_the_injection() -> None:
    """Executed: fault injection. The old construct must still be exploitable.

    Without this half the "fixed" assertion passes on any body that happens
    to avoid the payload -- which is how #3820 shipped a `script:`-only fix
    while the same fault sat in a `run:` block.
    """
    where = run_probe.probe_vulnerable()
    assert "True" in where


def test_current_run_body_is_inert_and_keeps_the_text(dispatch_body: str) -> None:
    """Executed: the real body runs a hostile decision without executing it."""
    posted = run_probe.probe_fixed(dispatch_body)
    assert run_probe.HOSTILE_DECISION in posted
    assert run_probe.HOSTILE_ISSUE in posted


def test_probe_rejects_a_body_that_still_interpolates() -> None:
    """The probe fails a body that never got fixed."""
    with pytest.raises(AssertionError, match="still interpolates"):
        run_probe.probe_fixed('DECISION="${{ steps.decide.outputs.decision }}"\n')


def test_run_probe_cli_proves_both_halves() -> None:
    """The CLI the smoke workflow runs exits 0 on the current tree."""
    result = subprocess.run(
        [sys.executable, "scripts/agents/lib/run_block_injection_probe.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS: both halves demonstrated." in result.stdout
