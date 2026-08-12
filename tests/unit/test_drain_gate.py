"""Unit tests for scripts/agents/lib/drain_gate.py (#3730).

Covers the three decisions the acceptance drain gate rests on:

  * `summarize` — which issues sit in the Acceptance Testing / Acceptance
    Failed lanes on one page of the project-items query (closed issues
    included: an item awaiting acceptance is normally closed).
  * `decide` — the gate opens only when Acceptance Testing is empty
    EXCEPT the release tracking issue, which is exempt.
  * `bypass` — agent-process issues skip the gate; product acceptance
    work does not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "drain_gate.py"
_spec = importlib.util.spec_from_file_location("drain_gate", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
drain_gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = drain_gate
_spec.loader.exec_module(drain_gate)


def _item(number, status=None, state="CLOSED", typename="Issue"):
    field_values = []
    if status is not None:
        field_values.append(
            {
                "__typename": "ProjectV2ItemFieldSingleSelectValue",
                "field": {"name": "Status"},
                "name": status,
            }
        )
    return {
        "content": {"__typename": typename, "number": number, "state": state},
        "fieldValues": {"nodes": field_values},
    }


def _page(*items):
    return {"data": {"node": {"items": {"nodes": list(items)}}}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "STATUS_FIELD",
        "STATUS_ACCEPTANCE_TESTING",
        "STATUS_ACCEPTANCE_FAILED",
        "RELEASE_ISSUE",
        "DRAIN_GATE_BYPASS_LABELS",
    ):
        monkeypatch.delenv(key, raising=False)


# --- summarize -------------------------------------------------------


def test_summarize_buckets_both_lanes():
    page = _page(
        _item(1, "Acceptance Testing"),
        _item(2, "Acceptance Failed"),
        _item(3, "Backlog"),
        _item(4, "Acceptance Testing", state="OPEN"),
    )
    assert drain_gate.summarize(page) == {
        "acceptance_testing": [1, 4],
        "acceptance_failed": [2],
    }


def test_summarize_counts_closed_issues():
    """An item awaiting acceptance is normally a CLOSED issue -- a drain
    check that only counted open issues would report an empty lane while
    the owner still had a full round to test."""
    page = _page(_item(7, "Acceptance Testing", state="CLOSED"))
    assert drain_gate.summarize(page)["acceptance_testing"] == [7]


def test_summarize_ignores_pull_requests_and_unstatused_items():
    page = _page(
        _item(10, "Acceptance Testing", typename="PullRequest"),
        _item(11, None),
    )
    assert drain_gate.summarize(page) == {"acceptance_testing": [], "acceptance_failed": []}


def test_summarize_honors_renamed_lanes(monkeypatch):
    monkeypatch.setenv("STATUS_ACCEPTANCE_TESTING", "Testing")
    monkeypatch.setenv("STATUS_ACCEPTANCE_FAILED", "Failed")
    page = _page(_item(1, "Testing"), _item(2, "Failed"), _item(3, "Acceptance Testing"))
    assert drain_gate.summarize(page) == {"acceptance_testing": [1], "acceptance_failed": [2]}


# --- decide ----------------------------------------------------------


def test_gate_closed_while_items_remain_in_acceptance_testing(monkeypatch):
    monkeypatch.setenv("RELEASE_ISSUE", "3521")
    state = drain_gate.decide(
        {"acceptance_testing": [3521, 3600, 3601], "acceptance_failed": [3700]}
    )
    assert state["open"] is False
    assert state["blockers"] == [3600, 3601]
    assert state["held"] == [3700]


def test_gate_opens_when_only_the_release_issue_remains(monkeypatch):
    monkeypatch.setenv("RELEASE_ISSUE", "3521")
    state = drain_gate.decide({"acceptance_testing": [3521], "acceptance_failed": [3700, 3701]})
    assert state["open"] is True
    assert state["blockers"] == []
    assert state["held"] == [3700, 3701]
    assert state["release_issue_exempt"] == [3521]


def test_gate_opens_on_a_fully_empty_lane(monkeypatch):
    monkeypatch.setenv("RELEASE_ISSUE", "3521")
    state = drain_gate.decide({"acceptance_testing": [], "acceptance_failed": []})
    assert state["open"] is True
    assert state["held"] == []


def test_release_issue_is_only_exempt_when_configured():
    """Without RELEASE_ISSUE nothing is exempt: the gate must stay closed
    rather than guess which item is the ledger."""
    state = drain_gate.decide({"acceptance_testing": [3521], "acceptance_failed": [3700]})
    assert state["open"] is False
    assert state["blockers"] == [3521]


def test_decide_deduplicates_and_sorts():
    state = drain_gate.decide({"acceptance_testing": [], "acceptance_failed": [9, 3, 9]})
    assert state["held"] == [3, 9]


# --- bypass ----------------------------------------------------------


def test_process_exception_prose_bypasses_the_gate():
    issue = {
        "title": "feat: drain-gated failure processing",
        "body": "## Process exception\nThis issue bypasses the drain gate it implements.",
        "labels": [{"name": "Improvement"}],
    }
    assert drain_gate.bypass(issue) is True


def test_machine_marker_bypasses_the_gate():
    assert drain_gate.bypass({"body": f"filed by automation\n{drain_gate.BYPASS_MARKER}"}) is True


def test_ordinary_acceptance_failure_is_gated():
    issue = {
        "title": "bug: acceptance failure 1 for #3600",
        "body": "Related feature: #3600\n\nThe web UI 500s on upload.",
        "labels": [{"name": "Acceptance Failure"}],
    }
    assert drain_gate.bypass(issue) is False


def test_improvement_filed_during_acceptance_is_gated():
    issue = {
        "title": "Improve upload UX",
        "body": "Related feature: #3600",
        "labels": [{"name": "Improvement"}],
    }
    assert drain_gate.bypass(issue) is False


def test_label_rule_is_off_until_configured():
    issue = {"title": "process work", "body": "no marker", "labels": [{"name": "Process"}]}
    assert drain_gate.bypass(issue) is False


def test_configured_bypass_label_matches_case_insensitively(monkeypatch):
    monkeypatch.setenv("DRAIN_GATE_BYPASS_LABELS", "process, agent-process")
    issue = {"title": "process work", "body": "no marker", "labels": [{"name": "Agent-Process"}]}
    assert drain_gate.bypass(issue) is True


def test_bypass_tolerates_missing_fields():
    assert drain_gate.bypass({}) is False


# --- merge -----------------------------------------------------------


def test_merge_combines_pages_without_duplicates():
    merged = drain_gate._merge(
        [
            {"acceptance_testing": [2, 1], "acceptance_failed": [5]},
            {"acceptance_testing": [1, 3], "acceptance_failed": []},
        ]
    )
    assert merged == {"acceptance_testing": [1, 2, 3], "acceptance_failed": [5]}
