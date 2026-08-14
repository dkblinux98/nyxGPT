"""Unit tests for build_dashboard.py's churn-cost view (#3776).

Covers churn_view(), which turns dump_churn.py's data/churn.json into the
dashboard's churn-cost section (totals, per-kind split, the issues paying the
most onboarding tax, and the stale-context incident tally), labelling issues
with the Sprint field from data/project_fields.json.
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
    "sprints": [{"title": "Sprint 8", "startDate": "2026-08-03", "duration": 7}],
    "items": [
        {"type": "Issue", "number": 3753, "fields": [{"field": "Sprint", "value": "Sprint 8"}]},
        {"type": "Issue", "number": 42, "fields": []},
    ],
}


def _issue(n, rounds, tokens, ctx, prod, repeat=0, cost=None):
    return {
        "issue": n,
        "rounds": rounds,
        "kinds": {"implement": 1},
        "tokens": {
            "input": tokens,
            "output": 0,
            "cache_creation": 0,
            "cache_read": 0,
            "total": tokens,
        },
        "context_tokens": ctx,
        "production_tokens": prod,
        "repeat_context_tokens": repeat,
        "unknown_split_rounds": 0,
        "cost_usd": cost,
        "context_pct": round(ctx / (ctx + prod) * 100, 1) if ctx + prod else None,
        "first_round_at": "2026-08-10T00:00:00Z",
        "last_round_at": "2026-08-11T00:00:00Z",
    }


CHURN = {
    "generated_at": "2026-08-14T15:00:00+00:00",
    "methodology": {"split": "turn-ratio approximation"},
    "rounds": [
        {
            "issue": 3753,
            "kind": "implement",
            "tokens": {"total": 1000},
            "split": {"context_tokens": 400, "production_tokens": 600},
        },
        {
            "issue": 3753,
            "kind": "review-fix",
            "tokens": {"total": 2000},
            "split": {"context_tokens": 1400, "production_tokens": 600},
        },
        {
            "issue": 42,
            "kind": "implement",
            "tokens": {"total": 500},
            "split": {"context_tokens": None, "production_tokens": None},
        },
    ],
    "issues": {
        "3753": _issue(3753, 2, 3000, 1800, 1200, repeat=1400, cost=1.5),
        "42": _issue(42, 1, 500, 0, 0),
    },
    "totals": {
        "rounds": 3,
        "issues": 2,
        "tokens": {"total": 3500},
        "context_tokens": 1800,
        "production_tokens": 1200,
        "repeat_context_tokens": 1400,
        "context_pct": 60.0,
        "multi_round_issues": 1,
        "unknown_split_rounds": 1,
        "cost_usd": 1.5,
        "priced": True,
    },
    "staleContextIncidents": {
        "total": 3,
        "byKind": {"stale-artifact-claim": 1, "stale-lane-state": 1, "stale-run-state": 1},
        "byMonth": {"2026-08": 3},
        "incidents": [{"id": "x", "date": "2026-08-14", "kind": "stale-lane-state"}],
    },
}


class TestChurnView:
    def test_returns_none_without_churn_data(self, build_dashboard):
        assert build_dashboard.churn_view(None, PROJECT_FIELDS) is None

    def test_totals_incidents_and_methodology_pass_through(self, build_dashboard):
        view = build_dashboard.churn_view(CHURN, PROJECT_FIELDS)
        assert view["totals"]["context_pct"] == 60.0
        assert view["totals"]["priced"] is True
        assert view["incidents"]["total"] == 3
        assert view["methodology"]["split"] == "turn-ratio approximation"
        assert view["generatedAt"] == "2026-08-14T15:00:00+00:00"

    def test_issues_are_sprint_labelled_and_ranked_by_tokens(self, build_dashboard):
        view = build_dashboard.churn_view(CHURN, PROJECT_FIELDS)
        assert [i["issue"] for i in view["topIssues"]] == [3753, 42]
        assert view["topIssues"][0]["sprint"] == "Sprint 8"
        assert view["topIssues"][1]["sprint"] == "(no sprint)"

    def test_multi_round_list_holds_the_issues_paying_repeat_onboarding(self, build_dashboard):
        view = build_dashboard.churn_view(CHURN, PROJECT_FIELDS)
        assert [i["issue"] for i in view["multiRound"]] == [3753]
        assert view["multiRound"][0]["repeat_context_tokens"] == 1400

    def test_by_kind_aggregates_rounds_and_tolerates_unknown_splits(self, build_dashboard):
        view = build_dashboard.churn_view(CHURN, PROJECT_FIELDS)
        by_kind = {k["kind"]: k for k in view["byKind"]}
        assert by_kind["review-fix"] == {
            "kind": "review-fix",
            "rounds": 1,
            "tokens": 2000,
            "context_tokens": 1400,
        }
        # the unknown-split round still contributes tokens, and contributes 0 context
        assert by_kind["implement"]["rounds"] == 2
        assert by_kind["implement"]["tokens"] == 1500
        assert by_kind["implement"]["context_tokens"] == 400
        # ranked by token spend
        assert [k["kind"] for k in view["byKind"]] == ["review-fix", "implement"]

    def test_works_without_a_project_snapshot(self, build_dashboard):
        view = build_dashboard.churn_view(CHURN, None)
        assert {i["sprint"] for i in view["topIssues"]} == {"(no sprint)"}
