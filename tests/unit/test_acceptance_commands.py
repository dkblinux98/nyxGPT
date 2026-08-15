"""Structural tests for the two stakeholder-acceptance commands (#3731).

`@acceptance-failure` and `@improvement` are owner-only issue_comment
commands. They must stay mirror images of each other: same gate, same drain
placement, same relationship write — differing only in the label they apply
and the copy they post.

The contract asserted here is the one the owner decided on 2026-08-12:

  * `@improvement` exists and mirrors `@acceptance-failure`
  * BOTH write the native blocked-by relationship (`mark_issue_blocked_by`)
  * NEITHER writes relationship metadata into the issue body or a comment —
    the retired `Related feature: #N` marker is never emitted again
  * both resolve the issue they were filed against with `related_feature_of`
    (native first, retired marker as historical fallback)

These are cheap structural guards, not a workflow runner: a regression here
(a re-introduced prose marker, a dropped relationship write) is silent in
production until the retrospective or the promotion sweep mis-attributes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
FAILURE_WF = WORKFLOWS / "handle_acceptance_failure.yml"
IMPROVEMENT_WF = WORKFLOWS / "handle_improvement.yml"
PROMOTE_SH = REPO_ROOT / "scripts" / "agents" / "promote_accepted_features.sh"


def _load(path):
    return yaml.safe_load(path.read_text())


def _steps(workflow):
    return workflow["jobs"]["handle"]["steps"]


def _run_script(workflow):
    """The shell body of the step that files the issue."""
    return "\n".join(step.get("run", "") for step in _steps(workflow))


def _script_body(workflow):
    """Everything the workflow executes, shell and github-script alike."""
    parts = []
    for step in _steps(workflow):
        parts.append(step.get("run", ""))
        parts.append((step.get("with") or {}).get("script", ""))
    return "\n".join(parts)


@pytest.fixture(scope="module")
def failure_wf():
    return _load(FAILURE_WF)


@pytest.fixture(scope="module")
def improvement_wf():
    return _load(IMPROVEMENT_WF)


# --- both commands exist and are owner-only --------------------------


@pytest.mark.parametrize(
    "path,trigger",
    [(FAILURE_WF, "@acceptance-failure"), (IMPROVEMENT_WF, "@improvement")],
)
def test_command_is_an_owner_only_issue_comment_gate(path, trigger):
    workflow = _load(path)
    # `on:` parses as the boolean True in YAML 1.1 — hence the lookup dance.
    triggers = workflow.get("on") or workflow.get(True)
    assert "issue_comment" in triggers
    # #3790 moved the owner/token test onto the `comment_gate` job, which
    # also runs the anchored "does this comment ISSUE the command?" check
    # before `handle` does anything. The gate contract itself is unchanged.
    condition = workflow["jobs"]["comment_gate"]["if"]
    assert f"'{trigger}'" in condition
    assert "vars.HUMAN_OWNER" in condition
    assert "github.event.issue.pull_request == null" in condition
    assert workflow["jobs"]["handle"]["needs"] == ["comment_gate"]


def test_improvement_mirrors_acceptance_failure_structure(failure_wf, improvement_wf):
    """Same permissions and token; the commands differ only in what they file."""
    failure_job = failure_wf["jobs"]["handle"]
    improvement_job = improvement_wf["jobs"]["handle"]
    assert improvement_job["permissions"] == failure_job["permissions"]
    assert improvement_job["env"] == failure_job["env"]


def test_each_command_applies_its_own_label(failure_wf, improvement_wf):
    assert '--label "Acceptance Failure"' in _run_script(failure_wf)
    assert '--label "Improvement"' in _run_script(improvement_wf)


def test_both_commands_respect_the_drain_gate(failure_wf, improvement_wf):
    """Placement per #3730: gated work is FILED into the holding lane."""
    for workflow in (failure_wf, improvement_wf):
        script = _run_script(workflow)
        assert "STATUS_ACCEPTANCE_FAILED" in script
        assert "drain_gate_hold" in script
        assert "text_bypasses_drain_gate" in script


# --- native relationships are the only storage -----------------------


def test_both_commands_write_the_native_blocking_relationship(failure_wf, improvement_wf):
    for workflow in (failure_wf, improvement_wf):
        assert "mark_issue_blocked_by" in _run_script(workflow)


def test_neither_command_writes_the_retired_prose_marker(failure_wf, improvement_wf):
    """The body-prose convention is retired: nothing may emit it again."""
    for workflow in (failure_wf, improvement_wf):
        body = _script_body(workflow)
        assert 'echo "Related feature: #' not in body
        assert 'echo "Parent feature: #' not in body


def test_neither_command_puts_relationship_metadata_in_a_comment(failure_wf, improvement_wf):
    """Acknowledgement comments describe the flow; they store nothing."""
    for workflow in (failure_wf, improvement_wf):
        for step in _steps(workflow):
            script = (step.get("with") or {}).get("script", "")
            assert "Related feature: #" not in script
            assert "<!--" not in script


def test_both_commands_resolve_the_target_natively(failure_wf, improvement_wf):
    for workflow in (failure_wf, improvement_wf):
        assert "related_feature_of" in _run_script(workflow)


def test_target_resolution_requires_a_handler_label(failure_wf, improvement_wf):
    """A plain feature can natively block a sequenced successor.

    Resolving on a blocking edge alone would misread that as "this issue was
    filed against something", so both handlers also require a handler label.
    """
    for workflow in (failure_wf, improvement_wf):
        script = _run_script(workflow)
        assert '"Acceptance Failure" or . == "Improvement"' in script


# --- the sweep that consumes the relationships ------------------------


def test_promotion_sweep_gates_on_the_transitive_closure():
    body = PROMOTE_SH.read_text()
    assert "transitive_blocked_by_issues" in body
    assert 'issue_relationships.py" feature-blockers' in body


def test_promotion_sweep_sweeps_improvements_too():
    """Both commands record blocking, so both labels must be swept (#3731)."""
    body = PROMOTE_SH.read_text()
    assert "Improvement" in body
    assert "Acceptance%20Failure" in body


def test_promotion_sweep_reads_both_parking_lanes():
    """Owner decision 2026-08-14 (#3780): a feature parked in the holding
    lane is a promotion candidate too, and only a CLOSED one is (an OPEN
    item there is drain-gate-held rework)."""
    body = PROMOTE_SH.read_text()
    assert "STATUS_ACCEPTANCE_FAILED" in body
    assert "_issue_open_state" in body


class TestPromotionSweepBehaviour:
    """Runs the sweep end to end against a stubbed `gh` (no network).

    Wired into pytest because `pytest -v` is the gate this repo actually
    runs; `bash tests/test_promote_accepted_features.sh` still works for
    local debugging.
    """

    def test_shell_suite_passes(self):
        suite = REPO_ROOT / "tests" / "test_promote_accepted_features.sh"
        result = subprocess.run(
            ["bash", str(suite)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, result.stdout + result.stderr
