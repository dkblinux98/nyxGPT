"""Unit tests for the pull decision (#3883).

The point of the pull is that conflicts are avoided by *scheduling*, not
resolved afterwards, and that WIP is read from state handed in rather than
remembered. Both are properties of this function, so this is where they are
pinned -- including the case the old push model got wrong on 2026-08-18: two
issues whose expected-files overlap must never be in flight together.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
_spec = importlib.util.spec_from_file_location("pull_next_issue", _LIB / "pull_next_issue.py")
assert _spec is not None and _spec.loader is not None
pull = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pull)

ROLES = {"owner": "dkblinux98", "scrum": "myGPT-scrummaster-agent", "dev": "myGPT-developer-agent"}


def state(**overrides):
    base = {
        "plan": {
            "order": [
                {"issue": 200, "expected_files": ["src/nyxgpt/ops.py"]},
                {"issue": 100, "expected_files": ["web/src/app/page.tsx"]},
                {"issue": 300, "expected_files": ["src/nyxgpt/rag/store.py"]},
            ]
        },
        "candidates": [
            {"issue": 100, "status": "Backlog", "assignees": ["myGPT-scrummaster-agent"]},
            {"issue": 200, "status": "Backlog", "assignees": []},
            {"issue": 300, "status": "Backlog", "assignees": ["myGPT-scrummaster-agent"]},
        ],
        "in_flight": [],
        "blocked_by": {},
        "wip_limit": 2,
        "status_backlog": "Backlog",
        "roles": ROLES,
    }
    base.update(overrides)
    return base


def test_pull_order_is_the_plans_order_not_the_lowest_issue_number():
    decision = pull.select(state())
    assert decision["issue"] == 200
    assert "position 1 in the sprint plan" in decision["reason"]


def test_an_issue_blocked_by_an_unmerged_issue_is_ineligible():
    decision = pull.select(state(blocked_by={200: [4242]}))
    assert decision["issue"] == 100
    assert {
        "issue": 200,
        "skipped": "blocked",
        "detail": "blocked by #4242 (unmerged)",
    } in decision["considered"]


def test_overlapping_expected_files_yield_the_next_candidate_never_a_parallel_pull():
    decision = pull.select(
        state(in_flight=[{"issue": 900, "files": ["src/nyxgpt/ops.py", "docs/ops.md"]}])
    )
    assert decision["issue"] == 100
    skipped = {entry["issue"]: entry for entry in decision["considered"]}
    assert skipped[200]["skipped"] == "file_overlap"
    assert "src/nyxgpt/ops.py" in skipped[200]["detail"]


def test_a_directory_entry_in_the_plan_covers_files_beneath_it():
    decision = pull.select(
        state(
            plan={"order": [{"issue": 200, "expected_files": ["scripts/agents/"]}]},
            candidates=[{"issue": 200, "status": "Backlog", "assignees": []}],
            in_flight=[{"issue": 900, "files": ["scripts/agents/groom_sprint.sh"]}],
        )
    )
    assert decision["issue"] is None
    assert decision["considered"][0]["skipped"] == "file_overlap"


def test_wip_limit_is_read_from_the_state_handed_in_not_remembered():
    decision = pull.select(
        state(in_flight=[{"issue": 900, "files": []}, {"issue": 901, "files": []}])
    )
    assert decision["issue"] is None
    assert "WIP limit 2 reached" in decision["reason"]
    assert decision["wip"] == 2


def test_the_owner_holding_an_issue_is_never_reassigned():
    decision = pull.select(
        state(
            candidates=[
                {"issue": 200, "status": "Backlog", "assignees": ["dkblinux98"]},
                {"issue": 100, "status": "Backlog", "assignees": []},
            ]
        )
    )
    assert decision["issue"] == 100
    assert decision["considered"][0]["skipped"] == "owner_held"


def test_an_anomalous_assignee_is_reported_not_silently_taken():
    decision = pull.select(
        state(
            candidates=[{"issue": 200, "status": "Backlog", "assignees": ["some-other-human"]}],
        )
    )
    assert decision["issue"] is None
    assert decision["considered"][0]["skipped"] == "anomalous_assignee"


def test_issues_absent_from_the_plan_are_pulled_after_every_planned_one():
    decision = pull.select(
        state(
            plan={"order": [{"issue": 300, "expected_files": []}]},
            candidates=[
                {"issue": 100, "status": "Backlog", "assignees": []},
                {"issue": 300, "status": "Backlog", "assignees": []},
            ],
        )
    )
    assert decision["issue"] == 300
    decision = pull.select(
        state(
            plan={"order": []},
            candidates=[
                {"issue": 300, "status": "Backlog", "assignees": []},
                {"issue": 100, "status": "Backlog", "assignees": []},
            ],
        )
    )
    assert decision["issue"] == 300
    assert "not in the sprint plan" in decision["reason"]


def test_excluded_candidates_from_an_earlier_attempt_are_skipped():
    decision = pull.select(state(exclude=[200, 100]))
    assert decision["issue"] == 300


def test_a_candidate_with_no_expected_files_is_pulled_but_says_it_could_not_check():
    decision = pull.select(
        state(
            plan={"order": [{"issue": 200, "expected_files": []}]},
            candidates=[{"issue": 200, "status": "Backlog", "assignees": []}],
            in_flight=[{"issue": 900, "files": ["src/nyxgpt/ops.py"]}],
        )
    )
    assert decision["issue"] == 200
    assert "overlap could not be checked" in decision["reason"]


def test_nothing_eligible_reports_why_rather_than_returning_bare_none():
    decision = pull.select(state(blocked_by={100: [1], 200: [2], 300: [3]}))
    assert decision["issue"] is None
    assert "blocked, held, or" in decision["reason"]
    assert len(decision["considered"]) == 3


def test_paths_overlap_is_exact_or_directory_prefixed_and_nothing_looser():
    assert pull.paths_overlap("src/a.py", "src/a.py")
    assert pull.paths_overlap("src/", "src/a.py")
    assert pull.paths_overlap("src/a.py", "src/")
    assert not pull.paths_overlap("src/a.py", "src/ab.py")
    assert not pull.paths_overlap("src/a.py", "tests/a.py")
    assert not pull.paths_overlap("", "src/a.py")
