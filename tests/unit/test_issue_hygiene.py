"""Issue hygiene never overwrites a populated field (#3816).

Two halves:

* the behavioural suite (`tests/test_issue_hygiene.sh`), run here because
  `pytest -v` is the gate this repo actually runs -- it drives
  `scripts/agents/ensure_issue_hygiene.sh` against a stateful `gh` stub that
  injects a concurrent deliberate write into the window between hygiene's
  check of a field and its write of that field, and reads the field back
  afterwards;
* static wiring checks, so the guard cannot be silently unhooked -- the
  defect's signature is a *successful* hygiene run that overwrote a good
  value (#3814, 2026-08-16), which no green run reveals.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
HYGIENE = ROOT / "scripts" / "agents" / "ensure_issue_hygiene.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ensure_project_hygiene.yml"
SMOKE = ROOT / ".github" / "workflows" / "project-hygiene-smoke.yml"

#: Every project field the hygiene job knows how to populate.
FIELDS = ["Status", "Priority", "Effort", "Module", "Sprint"]


class TestShellSuite:
    def test_issue_hygiene_suite_passes(self):
        suite = ROOT / "tests" / "test_issue_hygiene.sh"
        result = subprocess.run(
            ["bash", str(suite)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestLibraryHelpers:
    def test_guard_helper_exists(self):
        assert "fill_project_field_if_empty()" in LIB.read_text(encoding="utf-8")

    def test_guard_reads_immediately_before_writing(self):
        """The read that decides must be inside the helper, not the caller's.

        A caller-side check is what the pre-#3816 job had: by the time its
        write went out, the value it checked could be several API calls old.
        """
        body = LIB.read_text(encoding="utf-8")
        start = body.index("fill_project_field_if_empty()")
        end = body.index("set_issue_status()", start)
        block = body[start:end]
        assert "project_field_value" in block
        assert block.index("project_field_value") < block.index("set_project_field_value")

    def test_lookup_only_item_finder_exists(self):
        """ "Not on the board" and "on the board, no Status" must be tellable apart."""
        body = LIB.read_text(encoding="utf-8")
        assert "find_issue_project_item()" in body
        # ensure_issue_in_project keeps the add behaviour, built on the finder.
        start = body.index("ensure_issue_in_project()")
        end = body.index("# Project field reads", start)
        block = body[start:end]
        assert "find_issue_project_item" in block
        assert "addProjectV2ItemById" in block


class TestHygieneScript:
    def test_no_unguarded_project_field_write(self):
        """Every field write goes through the guard -- no bare setter."""
        for number, line in enumerate(HYGIENE.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for setter in ("set_project_field_value", "set_issue_status"):
                assert setter not in stripped, f"{HYGIENE.name}:{number} writes via {setter}"

    @pytest.mark.parametrize("field", FIELDS)
    def test_every_field_is_guarded(self, field):
        body = HYGIENE.read_text(encoding="utf-8")
        assert f'fill_project_field_if_empty "$ITEM_ID" "{field}"' in body or (
            field == "Status" and 'fill_project_field_if_empty "$ITEM_ID" "$STATUS_FIELD"' in body
        )

    def test_milestone_is_re_read_before_its_edit(self):
        """Milestone is an issue attribute, so it needs the same re-read by hand."""
        body = HYGIENE.read_text(encoding="utf-8")
        assert "RECHECK_MILESTONE" in body
        assert body.index("RECHECK_MILESTONE") < body.index('gh issue edit "$ISSUE" --milestone')

    def test_labels_are_re_read_before_the_add(self):
        """The two-label deadlock rule (#3390/#3413/#3415) reads labels late."""
        body = HYGIENE.read_text(encoding="utf-8")
        assert body.index("LABELS=$(gh api") < body.index('gh issue edit "$ISSUE" --add-label')

    def test_settle_wait_is_configurable_and_defaults_to_60s(self):
        body = HYGIENE.read_text(encoding="utf-8")
        assert 'SETTLE_SECONDS="${HYGIENE_SETTLE_SECONDS:-60}"' in body

    def test_no_author_based_exemption(self):
        """The #3517 SCRUM_AGENT skip was a narrow patch on this same race.

        With every field guarded it is redundant, and it also made hygiene
        skip issues that genuinely needed filling.
        """
        body = HYGIENE.read_text(encoding="utf-8")
        assert "SCRUM_AGENT" not in body
        assert "issue.user.login" not in body


class TestWorkflowWiring:
    def test_hygiene_job_runs_the_script(self):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["add-to-project"]["steps"]
        assert any("ensure_issue_hygiene.sh" in (step.get("run") or "") for step in steps)

    def test_no_inline_field_writing_left_in_the_workflow(self):
        """Inline workflow bash cannot be executed by a test."""
        body = WORKFLOW.read_text(encoding="utf-8")
        add_to_project = body[: body.index("  pr-hygiene:")]
        assert "set_project_field_value" not in add_to_project
        assert "set_issue_status" not in add_to_project

    def test_smoke_job_exists_and_runs_the_suite(self):
        workflow = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["no-clobber"]["steps"]
        assert any("tests/test_issue_hygiene.sh" in (step.get("run") or "") for step in steps)

    def test_hygiene_still_only_fires_on_opened_issues(self):
        """Re-running on every edit would re-open the window on live issues."""
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True))
        assert triggers["issues"]["types"] == ["opened"]
