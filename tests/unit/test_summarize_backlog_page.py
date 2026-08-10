"""Unit tests for scripts/agents/lib/summarize_backlog_page.py (#3480).

Covers the sprint-scoped selection guard: with SPRINT_SCOPED unset/"0" the
behavior must be byte-for-byte identical to the original (pre-#3480)
scrummaster_next_issue.sh inline summarizer -- no regression for the
existing manual-kick flow. With SPRINT_SCOPED=1, only issues whose Sprint
iteration field matches ACTIVE_SPRINT_TITLE are eligible.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "summarize_backlog_page.py"
)
_spec = importlib.util.spec_from_file_location("summarize_backlog_page", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
summarize_backlog_page = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = summarize_backlog_page
_spec.loader.exec_module(summarize_backlog_page)


def _item(number, state="OPEN", status=None, sprint=None, milestone=None, typename="Issue"):
    field_values = []
    if status is not None:
        field_values.append(
            {
                "__typename": "ProjectV2ItemFieldSingleSelectValue",
                "field": {"name": "Status"},
                "name": status,
            }
        )
    if sprint is not None:
        field_values.append(
            {
                "__typename": "ProjectV2ItemFieldIterationValue",
                "field": {"name": "Sprint"},
                "title": sprint,
            }
        )
    content = {"__typename": typename}
    if typename == "Issue":
        content.update({"number": number, "state": state})
        if milestone is not None:
            content["milestone"] = {"title": milestone}
    return {"content": content, "fieldValues": {"nodes": field_values}}


def _page(items):
    return {"data": {"node": {"items": {"nodes": items}}}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "STATUS_FIELD",
        "STATUS_BACKLOG",
        "SPRINT_FIELD",
        "SPRINT_SCOPED",
        "ACTIVE_SPRINT_TITLE",
        "RELEASE_VERSION",
        "RELEASE_ISSUE",
        "EXCLUDE_ISSUES",
    ):
        monkeypatch.delenv(key, raising=False)


class TestUnscopedBehaviorUnchanged:
    """SPRINT_SCOPED unset must reproduce the pre-#3480 selection exactly."""

    def test_picks_lowest_phase_then_lowest_issue_number(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        items = [
            _item(50, status="Backlog", milestone="Phase 3: Foo"),
            _item(10, status="Backlog", milestone="Phase 5: Bar"),
            _item(30, status="Backlog", milestone="Phase 3: Foo"),
            _item(99, status="In Progress", milestone="Phase 1: Baz"),
        ]
        result = summarize_backlog_page.summarize(_page(items))
        assert result["best_issue"] == 30
        assert result["backlog_open"] == 3
        assert result["issue_items"] == 4
        assert result["open_issues"] == 4

    def test_non_backlog_and_closed_issues_are_ignored(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        items = [
            _item(1, state="CLOSED", status="Backlog"),
            _item(2, status="In Review"),
            _item(3, status="Backlog"),
        ]
        result = summarize_backlog_page.summarize(_page(items))
        assert result["best_issue"] == 3
        assert result["backlog_open"] == 1

    def test_ignores_sprint_field_entirely_when_not_scoped(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        items = [
            _item(1, status="Backlog", sprint="Sprint 6"),
            _item(2, status="Backlog", sprint="Sprint 5 (closed)"),
            _item(3, status="Backlog", sprint=None),
        ]
        result = summarize_backlog_page.summarize(_page(items))
        # All three eligible regardless of Sprint value -- unscoped selection
        # doesn't look at Sprint at all.
        assert result["backlog_open"] == 3
        assert result["best_issue"] == 1

    def test_no_candidates_returns_null_best_issue(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        items = [_item(1, status="In Progress")]
        result = summarize_backlog_page.summarize(_page(items))
        assert result["best_issue"] is None
        assert result["backlog_open"] == 0


class TestSprintScopedGuard:
    def test_only_active_sprint_issues_are_eligible(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("SPRINT_FIELD", "Sprint")
        monkeypatch.setenv("SPRINT_SCOPED", "1")
        monkeypatch.setenv("ACTIVE_SPRINT_TITLE", "Sprint 6")
        items = [
            _item(1, status="Backlog", sprint="Sprint 5"),
            _item(2, status="Backlog", sprint="Sprint 6"),
            _item(3, status="Backlog", sprint=None),
        ]
        result = summarize_backlog_page.summarize(_page(items))
        assert result["best_issue"] == 2
        assert result["backlog_open"] == 1

    def test_no_matches_in_active_sprint_yields_no_candidate(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("SPRINT_FIELD", "Sprint")
        monkeypatch.setenv("SPRINT_SCOPED", "1")
        monkeypatch.setenv("ACTIVE_SPRINT_TITLE", "Sprint 6")
        items = [
            _item(1, status="Backlog", sprint="Sprint 5"),
            _item(2, status="Backlog", sprint="Sprint 4"),
        ]
        result = summarize_backlog_page.summarize(_page(items))
        assert result["best_issue"] is None
        assert result["backlog_open"] == 0

    def test_lowest_phase_still_wins_within_the_active_sprint(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("SPRINT_FIELD", "Sprint")
        monkeypatch.setenv("SPRINT_SCOPED", "1")
        monkeypatch.setenv("ACTIVE_SPRINT_TITLE", "Sprint 6")
        items = [
            _item(50, status="Backlog", sprint="Sprint 6", milestone="Phase 5"),
            _item(10, status="Backlog", sprint="Sprint 6", milestone="Phase 2"),
            _item(5, status="Backlog", sprint="Sprint 9", milestone="Phase 1"),
        ]
        result = summarize_backlog_page.summarize(_page(items))
        assert result["best_issue"] == 10


class TestReleaseWall:
    """RELEASE_VERSION filters eligibility to the current release's
    milestones (owner decision 2026-07-31): the autopilot/selector must
    never cross a release boundary on their own, and sprint dates cannot
    be the gate because they drift and future sprints exist on the board
    before their release starts."""

    def test_unset_release_version_changes_nothing(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        items = [
            _item(10, status="Backlog", milestone="Phase 6 — Enterprise (v3.0.0)"),
            _item(20, status="Backlog", milestone="Phase 5.5: Fixes (v2.0.0)"),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 2
        assert out["best_issue"] == 20  # lowest phase number wins as before

    def test_wall_excludes_other_release_and_prefers_in_release(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("RELEASE_VERSION", "v2.0.0")
        items = [
            # Lower number but belongs to the NEXT release: must be invisible.
            _item(10, status="Backlog", milestone="Phase 6 — Enterprise (v3.0.0)"),
            _item(20, status="Backlog", milestone="Phase 5.5: Fixes (v2.0.0)"),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 1
        assert out["best_issue"] == 20

    def test_wall_excludes_unmilestoned_issues(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("RELEASE_VERSION", "v2.0.0")
        items = [_item(30, status="Backlog", milestone=None)]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 0
        assert out["best_issue"] is None

    def test_wall_drained_release_yields_no_candidates(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("RELEASE_VERSION", "v2.0.0")
        items = [
            _item(10, status="Backlog", milestone="Phase 6 — Enterprise (v3.0.0)"),
            _item(
                20, status="Backlog", sprint="Sprint 8", milestone="Phase 6 — Enterprise (v3.0.0)"
            ),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 0
        assert out["best_issue"] is None

    def test_wall_composes_with_sprint_scoping(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("RELEASE_VERSION", "v3.0.0")
        monkeypatch.setenv("SPRINT_SCOPED", "1")
        monkeypatch.setenv("ACTIVE_SPRINT_TITLE", "Sprint 7")
        items = [
            # In release, wrong sprint: excluded by sprint preference.
            _item(
                40, status="Backlog", sprint="Sprint 8", milestone="Phase 6 — Enterprise (v3.0.0)"
            ),
            # In sprint, wrong release: excluded by the wall.
            _item(41, status="Backlog", sprint="Sprint 7", milestone="Phase 5.5: Fixes (v2.0.0)"),
            # In release AND in sprint: the one eligible candidate.
            _item(
                42, status="Backlog", sprint="Sprint 7", milestone="Phase 6 — Enterprise (v3.0.0)"
            ),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 1
        assert out["best_issue"] == 42


class TestExcludeIssues:
    """EXCLUDE_ISSUES (#3665) lets scrummaster_dispatch_next.sh's
    fall-through loop retry selection within one run after an earlier
    candidate turned out to be unclaimable, without re-selecting the same
    blocked issue forever."""

    def test_excluded_issue_is_skipped_in_favor_of_the_next_candidate(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("EXCLUDE_ISSUES", "10")
        items = [
            _item(10, status="Backlog"),
            _item(20, status="Backlog"),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 1
        assert out["best_issue"] == 20

    def test_multiple_excluded_issues(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("EXCLUDE_ISSUES", "10,20")
        items = [
            _item(10, status="Backlog"),
            _item(20, status="Backlog"),
            _item(30, status="Backlog"),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 1
        assert out["best_issue"] == 30

    def test_all_candidates_excluded_yields_no_candidate(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("EXCLUDE_ISSUES", "10,20")
        items = [
            _item(10, status="Backlog"),
            _item(20, status="Backlog"),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 0
        assert out["best_issue"] is None

    def test_unset_exclude_issues_changes_nothing(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        items = [_item(10, status="Backlog")]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 1
        assert out["best_issue"] == 10


class TestReleaseIssueGuard:
    """The release tracking issue is a ledger, never selectable work --
    project hygiene stamping it Backlog + the current milestone once made
    the selector hand it to the developer agent (#3521, 2026-07-31)."""

    def test_release_issue_is_never_a_candidate(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("RELEASE_VERSION", "v2.0.0")
        monkeypatch.setenv("RELEASE_ISSUE", "2759")
        items = [
            _item(2759, status="Backlog", milestone="Phase 5.5: Fixes (v2.0.0)"),
            _item(3464, status="Backlog", milestone="Phase 5.5: Fixes (v2.0.0)"),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 1
        assert out["best_issue"] == 3464

    def test_release_issue_alone_counts_as_drained(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("RELEASE_VERSION", "v2.0.0")
        monkeypatch.setenv("RELEASE_ISSUE", "2759")
        items = [_item(2759, status="Backlog", milestone="Phase 5.5: Fixes (v2.0.0)")]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 0
        assert out["best_issue"] is None

    def test_unset_release_issue_changes_nothing(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        items = [_item(2759, status="Backlog", milestone="Phase 5.5: Fixes (v2.0.0)")]
        out = summarize_backlog_page.summarize(_page(items))
        assert out["backlog_open"] == 1
        assert out["best_issue"] == 2759


class TestSprintCountsBreakdown:
    """#3706: the summarizer reports open Backlog issues bucketed by Sprint
    so a sprint-bounded caller can say what it is NOT dispatching."""

    def test_buckets_eligible_backlog_by_sprint(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("SPRINT_FIELD", "Sprint")
        monkeypatch.setenv("SPRINT_SCOPED", "1")
        monkeypatch.setenv("ACTIVE_SPRINT_TITLE", "Sprint 8")
        items = [
            _item(1, status="Backlog", sprint="Sprint 8"),
            _item(2, status="Backlog", sprint="Sprint 9"),
            _item(3, status="Backlog", sprint="Sprint 9"),
            _item(4, status="Backlog", sprint=None),
            _item(5, status="In Progress", sprint="Sprint 9"),
            _item(6, state="CLOSED", status="Backlog", sprint="Sprint 9"),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        # Only the active sprint's issue is selectable...
        assert out["backlog_open"] == 1
        assert out["best_issue"] == 1
        # ...but the breakdown still reports what was skipped, with the
        # no-sprint bucket keyed by the empty string.
        assert out["sprint_counts"] == {"Sprint 8": 1, "Sprint 9": 2, "": 1}

    def test_breakdown_respects_release_wall_and_exclusions(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        monkeypatch.setenv("RELEASE_VERSION", "v3.0.0")
        monkeypatch.setenv("RELEASE_ISSUE", "3521")
        monkeypatch.setenv("EXCLUDE_ISSUES", "77")
        items = [
            _item(11, status="Backlog", sprint="Sprint 9", milestone="Phase 6 (v3.0.0)"),
            _item(77, status="Backlog", sprint="Sprint 9", milestone="Phase 6 (v3.0.0)"),
            _item(3521, status="Backlog", sprint="Sprint 9", milestone="Phase 6 (v3.0.0)"),
            _item(12, status="Backlog", sprint="Sprint 9", milestone="Phase 7 (v4.0.0)"),
        ]
        out = summarize_backlog_page.summarize(_page(items))
        # Excluded candidate, the release tracking issue, and out-of-release
        # work are all absent from the breakdown too.
        assert out["sprint_counts"] == {"Sprint 9": 1}

    def test_empty_page_has_empty_breakdown(self, monkeypatch):
        monkeypatch.setenv("STATUS_FIELD", "Status")
        monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
        out = summarize_backlog_page.summarize(_page([]))
        assert out["sprint_counts"] == {}
