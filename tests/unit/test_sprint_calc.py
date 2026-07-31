"""Unit tests for scripts/agents/lib/sprint_calc.py (#3480 sprint autopilot).

Pure computation only -- no gh/GraphQL involved -- covering the pieces the
issue explicitly calls out as needing tests: the autopilot stop condition,
verdict/velocity projection, and reorg proposal generation given mocked
project data.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "sprint_calc.py"
_spec = importlib.util.spec_from_file_location("sprint_calc", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
sprint_calc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sprint_calc
_spec.loader.exec_module(sprint_calc)


class TestAutopilotDecision:
    def test_continues_while_backlog_remains(self):
        assert sprint_calc.autopilot_decision(3) == "continue"
        assert sprint_calc.autopilot_decision(1) == "continue"

    def test_completes_once_backlog_is_empty(self):
        assert sprint_calc.autopilot_decision(0) == "complete"


class TestSprintVelocity:
    def test_normal_case(self):
        assert sprint_calc.sprint_velocity(done_count=6, elapsed_days=3) == 2.0

    def test_guards_against_zero_elapsed_days(self):
        # Day 0 of the sprint: still returns a rate instead of dividing by zero.
        assert sprint_calc.sprint_velocity(done_count=1, elapsed_days=0) == 1.0

    def test_guards_against_negative_elapsed_days(self):
        assert sprint_calc.sprint_velocity(done_count=5, elapsed_days=-2) == 5.0


class TestSprintVerdict:
    def test_no_remaining_work_is_always_on_track(self):
        assert sprint_calc.sprint_verdict(remaining=0, velocity=0, days_left=0) == "on-track"

    def test_unknown_when_no_end_date_configured(self):
        assert sprint_calc.sprint_verdict(remaining=5, velocity=1, days_left=None) == "unknown"

    def test_on_track_when_projection_fits(self):
        # 4 remaining / 2 per day = 2 days needed, 5 days left.
        assert sprint_calc.sprint_verdict(remaining=4, velocity=2, days_left=5) == "on-track"

    def test_at_risk_within_twenty_percent_buffer(self):
        # 6 remaining / 1 per day = 6 days needed, 5 days left (20% over).
        assert sprint_calc.sprint_verdict(remaining=6, velocity=1, days_left=5) == "at-risk"

    def test_off_track_beyond_buffer(self):
        # 10 remaining / 1 per day = 10 days needed, 5 days left (100% over).
        assert sprint_calc.sprint_verdict(remaining=10, velocity=1, days_left=5) == "off-track"

    def test_off_track_when_velocity_is_zero_and_work_remains(self):
        assert sprint_calc.sprint_verdict(remaining=3, velocity=0, days_left=2) == "off-track"


class TestReorgTargetCount:
    def test_no_excess_needs_no_reorg(self):
        assert sprint_calc.reorg_target_count(remaining=2, velocity=2, days_left=5) == 0

    def test_computes_excess_issues_to_move(self):
        # 10 remaining / 1 per day = 10 days needed, 5 left -> 5 excess days
        # * 1/day velocity = 5 issues over budget.
        assert sprint_calc.reorg_target_count(remaining=10, velocity=1, days_left=5) == 5

    def test_zero_velocity_proposes_clearing_everything_remaining(self):
        assert sprint_calc.reorg_target_count(remaining=4, velocity=0, days_left=2) == 4

    def test_none_days_left_proposes_nothing(self):
        assert sprint_calc.reorg_target_count(remaining=10, velocity=1, days_left=None) == 0


class TestSelectReorgCandidates:
    ISSUES = [
        {"number": 101, "priority": "P0 - Critical"},
        {"number": 102, "priority": "P3 - Low"},
        {"number": 103, "priority": "P1 - High"},
        {"number": 200, "priority": "P3 - Low"},
        {"number": 150, "priority": None},
    ]

    def test_lowest_priority_selected_first(self):
        result = sprint_calc.select_reorg_candidates(self.ISSUES, 1)
        # Two P3s tie on priority; the higher (less-likely-started) number wins.
        assert [i["number"] for i in result] == [200]

    def test_missing_priority_ranks_below_everything(self):
        result = sprint_calc.select_reorg_candidates(self.ISSUES, 2)
        assert [i["number"] for i in result] == [200, 102]

    def test_zero_count_returns_nothing(self):
        assert sprint_calc.select_reorg_candidates(self.ISSUES, 0) == []

    def test_count_larger_than_pool_returns_everything_ordered(self):
        result = sprint_calc.select_reorg_candidates(self.ISSUES, 99)
        assert [i["number"] for i in result] == [200, 102, 150, 103, 101]


class TestBuildSprintReport:
    def _payload(self, **overrides):
        payload = {
            "sprint_title": "Sprint 6",
            "start_date": "2026-07-27",
            "end_date": "2026-08-03",
            "today": "2026-07-31",
            "counts": {"backlog": 2, "in_progress": 1, "in_review": 0, "done": 3},
            "backlog_issues": [
                {"number": 10, "priority": "P3 - Low"},
                {"number": 11, "priority": "P0 - Critical"},
            ],
            "blockers": [],
        }
        payload.update(overrides)
        return payload

    def test_on_track_report_has_no_proposal(self):
        # 4 days elapsed, done=3 -> velocity 0.75/day; remaining=3 -> ~4 days
        # needed vs 3 days left (33% over, past the 20% buffer) -- so this
        # particular fixture is actually off-track; use an easy on-track one.
        result = sprint_calc.build_sprint_report(
            self._payload(counts={"backlog": 0, "in_progress": 0, "in_review": 0, "done": 5})
        )
        assert result["verdict"] == "on-track"
        assert result["proposal"] is None
        assert "SPRINT_REORG_PROPOSAL" not in result["markdown"]

    def test_off_track_report_includes_proposal_and_marker(self):
        result = sprint_calc.build_sprint_report(
            self._payload(counts={"backlog": 10, "in_progress": 0, "in_review": 0, "done": 1})
        )
        assert result["verdict"] == "off-track"
        assert result["proposal"] is not None
        assert result["proposal"]["action"] == "move_out"
        assert "SPRINT_REORG_PROPOSAL" in result["markdown"]
        assert "APPROVE_SPRINT_REORG" in result["markdown"]

    def test_off_track_with_no_backlog_issues_has_no_proposal(self):
        result = sprint_calc.build_sprint_report(
            self._payload(
                counts={"backlog": 0, "in_progress": 10, "in_review": 0, "done": 0},
                backlog_issues=[],
            )
        )
        assert result["verdict"] == "off-track"
        assert result["proposal"] is None
        assert "no eligible Backlog issues" in result["markdown"]

    def test_blockers_render_in_markdown(self):
        result = sprint_calc.build_sprint_report(
            self._payload(
                counts={"backlog": 0, "in_progress": 0, "in_review": 1, "done": 5},
                blockers=[{"pr": 42, "issue": 99, "changes_requested": 2}],
            )
        )
        assert "PR #42" in result["markdown"]
        assert "2 change-request cycle(s)" in result["markdown"]

    def test_no_active_backlog_reports_none_detected(self):
        result = sprint_calc.build_sprint_report(
            self._payload(counts={"backlog": 0, "in_progress": 0, "in_review": 0, "done": 5})
        )
        assert "Blockers:** none detected" in result["markdown"]
