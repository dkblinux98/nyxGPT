"""Unit tests for the board read behind the pull (#3883).

The filters here are inherited, not invented -- the release wall, the hard
sprint boundary (#3706), the support-report refusal (#3745) and the release
tracking issue that once got handed to the developer agent to implement
(#3521). Each is pinned so the pull cannot quietly reopen a boundary the push
model already learned to respect.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
sys.path.insert(0, str(_LIB))
_spec = importlib.util.spec_from_file_location("board_pull_state", _LIB / "board_pull_state.py")
assert _spec is not None and _spec.loader is not None
board_pull_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(board_pull_state)


def item(
    number, status, *, sprint="Sprint 12", milestone="v3.0.0", state="OPEN", labels=(), assignees=()
):
    return {
        "content": {
            "__typename": "Issue",
            "number": number,
            "state": state,
            "milestone": {"title": milestone},
            "labels": {"nodes": [{"name": name} for name in labels]},
            "assignees": {"nodes": [{"login": login} for login in assignees]},
        },
        "fieldValues": {
            "nodes": [
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "field": {"name": "Status"},
                    "name": status,
                },
                {
                    "__typename": "ProjectV2ItemFieldIterationValue",
                    "field": {"name": "Sprint"},
                    "title": sprint,
                },
            ]
        },
    }


def page(*items):
    return {"data": {"node": {"items": {"nodes": list(items)}}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STATUS_FIELD", "Status")
    monkeypatch.setenv("STATUS_BACKLOG", "Backlog")
    monkeypatch.setenv("STATUS_IN_PROGRESS", "In Progress")
    monkeypatch.setenv("STATUS_IN_REVIEW", "In Review")
    monkeypatch.setenv("SPRINT_FIELD", "Sprint")
    monkeypatch.setenv("SPRINT_SCOPED", "1")
    monkeypatch.setenv("ACTIVE_SPRINT_TITLE", "Sprint 12")
    monkeypatch.setenv("RELEASE_VERSION", "v3.0.0")
    monkeypatch.setenv("RELEASE_ISSUE", "3521")


def test_backlog_issues_become_candidates_carrying_their_assignees():
    state = board_pull_state.board_state([page(item(10, "Backlog", assignees=["scrum"]))])
    assert state["candidates"] == [
        {
            "issue": 10,
            "status": "Backlog",
            "sprint": "Sprint 12",
            "milestone": "v3.0.0",
            "assignees": ["scrum"],
        }
    ]


def test_in_progress_and_in_review_are_in_flight_not_candidates():
    state = board_pull_state.board_state(
        [page(item(10, "In Progress"), item(11, "In Review"), item(12, "Backlog"))]
    )
    assert state["in_flight"] == [10, 11]
    assert [c["issue"] for c in state["candidates"]] == [12]


def test_in_flight_is_counted_across_sprints_because_it_is_a_live_conflict_surface():
    state = board_pull_state.board_state([page(item(10, "In Review", sprint="Sprint 11"))])
    assert state["in_flight"] == [10]


def test_the_sprint_boundary_is_hard_for_candidates():
    state = board_pull_state.board_state(
        [page(item(10, "Backlog", sprint="Sprint 13"), item(11, "Backlog", sprint=None))]
    )
    assert state["candidates"] == []


def test_the_release_wall_excludes_other_milestones_and_unmilestoned_work():
    state = board_pull_state.board_state(
        [page(item(10, "Backlog", milestone="v4.0.0"), item(11, "Backlog", milestone=None))]
    )
    assert state["candidates"] == []


def test_support_reports_and_the_release_tracking_issue_are_never_candidates():
    state = board_pull_state.board_state(
        [page(item(10, "Backlog", labels=["Support"]), item(3521, "Backlog"))]
    )
    assert state["candidates"] == []


def test_closed_issues_are_ignored_entirely():
    state = board_pull_state.board_state([page(item(10, "Backlog", state="CLOSED"))])
    assert state["candidates"] == [] and state["in_flight"] == []


def test_pages_accumulate_and_candidates_come_back_in_board_order(tmp_path):
    pages = tmp_path / "pages.json"
    pages.write_text(
        json.dumps(page(item(30, "Backlog"))) + "\n" + json.dumps(page(item(20, "Backlog"))) + "\n",
        encoding="utf-8",
    )
    with pages.open(encoding="utf-8") as handle:
        loaded = [json.loads(line) for line in handle if line.strip()]
    state = board_pull_state.board_state(loaded)
    assert [c["issue"] for c in state["candidates"]] == [20, 30]
