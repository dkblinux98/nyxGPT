"""PR lane invariant (#3742): merged/closed PRs never strand in "In Review".

Two halves:

* the behavioural suite (`tests/test_pr_lane_hygiene.sh`), run here because
  `pytest -v` is the gate this repo actually runs -- it drives the real
  merge flow, close handler and sweep against a stateful `gh` stub and
  reads each PR's board Status back afterwards;
* static wiring checks, so the invariant cannot be silently unhooked from
  the flows that are supposed to enforce it (the owner has had to sweep
  stranded PR cards by hand repeatedly: 13 + 3 on 2026-08-10, 10 more on
  2026-08-13).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
MERGE_SCRIPT = ROOT / "scripts" / "agents" / "review_accept_and_merge.sh"
CLOSE_SCRIPT = ROOT / "scripts" / "agents" / "pr_close_project_status.sh"
SWEEP_SCRIPT = ROOT / "scripts" / "agents" / "reconcile_pr_lane.sh"
CLOSE_WORKFLOW = ROOT / ".github" / "workflows" / "pr_project_status_on_close.yml"
SWEEP_WORKFLOW = ROOT / ".github" / "workflows" / "sweep_pr_status.yml"


class TestShellSuite:
    def test_pr_lane_suite_passes(self):
        suite = ROOT / "tests" / "test_pr_lane_hygiene.sh"
        result = subprocess.run(
            ["bash", str(suite)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestLibraryHelpers:
    def test_status_closed_has_a_literal_default(self):
        """No new repo variable is required for the terminal PR lane."""
        assert 'STATUS_CLOSED="${STATUS_CLOSED:-Closed}"' in LIB.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "helper",
        ["pr_project_item_id()", "pr_status()", "set_pr_status()", "close_pr_project_item()"],
    )
    def test_helper_exists(self, helper):
        assert helper in LIB.read_text(encoding="utf-8")

    def test_pr_item_lookup_is_pr_shaped(self):
        """A PR's item must be resolved as a PullRequest, not via the issue path.

        ensure_issue_in_project()'s scan only matches `... on Issue` content,
        so reusing it for a PR silently falls through to its add path on
        every call and resolves the PR's *issue* node id.
        """
        body = LIB.read_text(encoding="utf-8")
        start = body.index("pr_project_item_id()")
        end = body.index("pr_status()", start)
        block = body[start:end]
        assert "pullRequest(number:$num)" in block
        assert "/pulls/${pr_number}" in block


class TestMergeFlowStampsThePr:
    def test_merge_flow_closes_the_pr_card(self):
        body = MERGE_SCRIPT.read_text(encoding="utf-8")
        assert "close_pr_project_item" in body

    def test_stamp_happens_right_after_the_merge(self):
        """Stamped before the issue bookkeeping, so a later failure cannot skip it."""
        body = MERGE_SCRIPT.read_text(encoding="utf-8")
        assert body.index("close_pr_project_item") < body.index('gh issue close "$ISSUE"')

    def test_dry_run_announces_the_stamp(self):
        body = MERGE_SCRIPT.read_text(encoding="utf-8")
        assert "would: set PR #$PR project Status -> '$STATUS_CLOSED'" in body


class TestClosedWithoutMergePath:
    def test_close_handler_guards_open_prs(self):
        body = CLOSE_SCRIPT.read_text(encoding="utf-8")
        assert 'if [[ "$pr_state" == "OPEN" ]]' in body
        assert "close_pr_project_item" in body

    def test_workflow_fires_on_pr_close(self):
        workflow = yaml.safe_load(CLOSE_WORKFLOW.read_text(encoding="utf-8"))
        # PyYAML parses the bare `on:` key as the boolean True.
        triggers = workflow.get("on", workflow.get(True))
        assert triggers["pull_request"]["types"] == ["closed"]

    def test_workflow_covers_merged_and_unmerged(self):
        """No `merged == false` guard: both exits must reach the terminal lane."""
        workflow = yaml.safe_load(CLOSE_WORKFLOW.read_text(encoding="utf-8"))
        job = workflow["jobs"]["stamp-closed-lane"]
        assert "if" not in job
        assert any("pr_close_project_status.sh" in (step.get("run") or "") for step in job["steps"])


class TestSweepBackstop:
    def test_sweep_only_touches_pull_requests(self):
        body = SWEEP_SCRIPT.read_text(encoding="utf-8")
        assert 'content.get("__typename") != "PullRequest"' in body
        assert 'content.get("state") not in ("MERGED", "CLOSED")' in body

    def test_sweep_workflow_runs_the_script(self):
        workflow = yaml.safe_load(SWEEP_WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["sweep"]["steps"]
        assert any("reconcile_pr_lane.sh" in (step.get("run") or "") for step in steps)

    def test_scheduled_run_applies_changes(self):
        """The backfill must not need a human to flip dry_run off."""
        workflow = yaml.safe_load(SWEEP_WORKFLOW.read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True))
        assert "schedule" in triggers
        sweep_step = workflow["jobs"]["sweep"]["steps"][-1]
        assert "github.event_name == 'schedule' && 'false'" in sweep_step["env"]["DRY_RUN"]
