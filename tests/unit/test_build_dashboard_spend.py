"""Unit tests for build_dashboard.py's spend-telemetry aggregation (#3696).

Covers spend_by_sprint(), which turns dump_spend.py's per-issue
data/spend.json into a per-sprint rollup (totals + per-issue distribution)
using the Sprint field from data/project_fields.json.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
RETRO_DIR = REPO_ROOT / "scripts" / "retrospective"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build_dashboard():
    return _load("build_dashboard", RETRO_DIR / "build_dashboard.py")


PROJECT_FIELDS = {
    "sprints": [
        {"title": "Sprint 7", "startDate": "2026-07-27", "duration": 7},
        {"title": "Sprint 8", "startDate": "2026-08-03", "duration": 7},
    ],
    "items": [
        {"type": "Issue", "number": 3696, "fields": [{"field": "Sprint", "value": "Sprint 8"}]},
        {"type": "Issue", "number": 3667, "fields": [{"field": "Sprint", "value": "Sprint 7"}]},
        {"type": "Issue", "number": 42, "fields": []},
    ],
}


def make_spend(**issues):
    unattributed = {"claude_steps": 5, "runs": 6, "runner_minutes": 12.0, "retry_cycles": 1}
    return {
        "generated_at": "2026-08-09T23:00:00+00:00",
        "issues": issues,
        "unattributed": unattributed,
    }


class TestSpendBySprint:
    def test_returns_none_without_spend_data(self, build_dashboard):
        assert build_dashboard.spend_by_sprint(None, PROJECT_FIELDS) is None

    def test_groups_issues_into_their_sprint(self, build_dashboard):
        spend = make_spend(
            **{
                "3696": {
                    "claude_steps": 20,
                    "runs": 25,
                    "runner_minutes": 40.1,
                    "retry_cycles": 3,
                    "workflows": {},
                },
                "3667": {
                    "claude_steps": 4,
                    "runs": 5,
                    "runner_minutes": 8.0,
                    "retry_cycles": 0,
                    "workflows": {},
                },
            }
        )
        out = build_dashboard.spend_by_sprint(spend, PROJECT_FIELDS)
        sprints = {s["sprint"]: s for s in out["bySprint"]}
        assert sprints["Sprint 8"]["issues"] == 1
        assert sprints["Sprint 8"]["runner_minutes"] == pytest.approx(40.1)
        assert sprints["Sprint 7"]["issues"] == 1
        # sprint order follows the project's real sprint calendar
        assert [s["sprint"] for s in out["bySprint"]] == ["Sprint 7", "Sprint 8"]

    def test_issue_without_sprint_field_falls_back_to_no_sprint_bucket(self, build_dashboard):
        spend = make_spend(
            **{
                "42": {
                    "claude_steps": 1,
                    "runs": 1,
                    "runner_minutes": 1.0,
                    "retry_cycles": 0,
                    "workflows": {},
                }
            }
        )
        out = build_dashboard.spend_by_sprint(spend, PROJECT_FIELDS)
        assert out["bySprint"][-1]["sprint"] == "(no sprint)"
        assert out["bySprint"][-1]["issues"] == 1

    def test_totals_include_unattributed_bucket(self, build_dashboard):
        spend = make_spend(
            **{
                "3696": {
                    "claude_steps": 20,
                    "runs": 25,
                    "runner_minutes": 40.1,
                    "retry_cycles": 3,
                    "workflows": {},
                }
            }
        )
        out = build_dashboard.spend_by_sprint(spend, PROJECT_FIELDS)
        assert out["totals"]["runs"] == 25 + 6
        assert out["totals"]["claude_steps"] == 20 + 5
        assert out["totals"]["runner_minutes"] == pytest.approx(40.1 + 12.0)
        assert out["unattributed"]["runs"] == 6

    def test_outliers_sorted_descending_by_runner_minutes(self, build_dashboard):
        spend = make_spend(
            **{
                "1": {
                    "claude_steps": 1,
                    "runs": 1,
                    "runner_minutes": 5.0,
                    "retry_cycles": 0,
                    "workflows": {},
                },
                "2": {
                    "claude_steps": 1,
                    "runs": 1,
                    "runner_minutes": 50.0,
                    "retry_cycles": 0,
                    "workflows": {},
                },
                "3": {
                    "claude_steps": 1,
                    "runs": 1,
                    "runner_minutes": 20.0,
                    "retry_cycles": 0,
                    "workflows": {},
                },
            }
        )
        out = build_dashboard.spend_by_sprint(spend, {})
        assert [o["issue"] for o in out["outliers"]] == [2, 3, 1]

    def test_works_without_a_project_fields_snapshot(self, build_dashboard):
        spend = make_spend(
            **{
                "1": {
                    "claude_steps": 1,
                    "runs": 1,
                    "runner_minutes": 5.0,
                    "retry_cycles": 0,
                    "workflows": {},
                }
            }
        )
        out = build_dashboard.spend_by_sprint(spend, None)
        assert out["bySprint"][0]["sprint"] == "(no sprint)"
