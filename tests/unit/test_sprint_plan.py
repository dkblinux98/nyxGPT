"""Unit tests for the sprint plan doc contract (#3908).

The plan is read by a machine (the pull step, #3883) and by the owner, from
one file. These tests pin the half a machine reads: that it survives a
render/parse round trip, that the pull order and expected-files come back
exactly as written, and that a regroom appends rather than rewrites.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
sys.path.insert(0, str(_LIB))

_spec = importlib.util.spec_from_file_location("sprint_plan", _LIB / "sprint_plan.py")
assert _spec is not None and _spec.loader is not None
sprint_plan = importlib.util.module_from_spec(_spec)
sys.modules["sprint_plan"] = sprint_plan
_spec.loader.exec_module(sprint_plan)


PLAN = {
    "sprint": "Sprint 12",
    "window": {"start": "2026-08-18", "end": "2026-09-01"},
    "milestone": "v3.0.0",
    "order": [
        {
            "issue": 3858,
            "title": "bound every subprocess",
            "priority": "P1 - High",
            "effort": "S",
            "expected_files": ["src/nyxgpt/api/routes.py"],
            "blocked_by": [],
            "blocks": [3521],
            "why_here": "unblocks the release",
        },
        {
            "issue": 3864,
            "title": "RAG collections 500",
            "priority": "P2 - Medium",
            "effort": "M",
            "expected_files": ["src/nyxgpt/rag/store.py"],
            "blocked_by": [3858],
            "blocks": [],
            "why_here": "",
        },
    ],
    "order_rationale": "Dependencies first; #3858 unblocks the release issue.",
    "deferred": [{"issue": 3999, "why": "no reproduction yet"}],
    "capacity": {"velocity_points": 12, "failure_reserve_points": 4, "notes": "n=3 sprints"},
    "regroom_log": [],
}


def test_render_parse_round_trip_preserves_the_machine_readable_half():
    parsed = sprint_plan.parse_plan(sprint_plan.render_plan(PLAN))
    assert parsed == PLAN


def test_pull_order_and_expected_files_are_read_from_the_plan():
    assert sprint_plan.plan_order(PLAN) == [3858, 3864]
    assert sprint_plan.expected_files(PLAN, 3864) == ["src/nyxgpt/rag/store.py"]
    assert sprint_plan.expected_files(PLAN, 4242) == []


def test_prose_reports_the_order_and_its_rationale():
    rendered = sprint_plan.render_plan(PLAN)
    assert "Sprint 12" in rendered
    assert "#3858" in rendered and "#3864" in rendered
    assert rendered.index("#3858") < rendered.index("#3864")
    assert "Dependencies first" in rendered
    assert "no reproduction yet" in rendered


def test_a_plan_with_no_rationale_says_so_rather_than_implying_one():
    rendered = sprint_plan.render_plan({**PLAN, "order_rationale": ""})
    assert "stands unjustified" in rendered


def test_regroom_appends_and_never_rewrites():
    once = sprint_plan.append_regroom(PLAN, "2026-08-20", "#3999 displaced by #4001")
    twice = sprint_plan.append_regroom(once, "2026-08-22", "#4001 accepted")
    assert [e["change"] for e in twice["regroom_log"]] == [
        "#3999 displaced by #4001",
        "#4001 accepted",
    ]
    assert PLAN["regroom_log"] == []
    assert "#3999 displaced by #4001" in sprint_plan.render_plan(twice)


def test_parse_returns_empty_for_a_doc_with_no_plan_block():
    assert sprint_plan.parse_plan("# Notes\n\nnothing machine readable here\n") == {}
    assert sprint_plan.parse_plan("") == {}


def test_parse_survives_a_corrupt_block_instead_of_raising():
    doc = f"{sprint_plan.PLAN_JSON_MARKER}\n```json\n{{not json,\n```\n"
    assert sprint_plan.parse_plan(doc) == {}


class TestExpectedFilesFromBody:
    def test_prefers_the_explicit_files_affected_line(self):
        body = (
            "## Technical Details\n"
            "- Files affected: `src/nyxgpt/ops.py`, `web/src/app/page.tsx`\n"
            "- Mentions `docs/unrelated.md` in prose\n"
        )
        assert sprint_plan.expected_files_from_body(body) == [
            "src/nyxgpt/ops.py",
            "web/src/app/page.tsx",
        ]

    def test_falls_back_to_backticked_paths_when_no_files_line_exists(self):
        body = "The crash is in `src/nyxgpt/cloud.py` and the test in `tests/unit/test_cloud.py`."
        assert sprint_plan.expected_files_from_body(body) == [
            "src/nyxgpt/cloud.py",
            "tests/unit/test_cloud.py",
        ]

    def test_directory_entries_survive_as_prefixes(self):
        assert sprint_plan.expected_files_from_body("- Files: `scripts/agents/*`") == [
            "scripts/agents/"
        ]

    def test_prose_words_are_not_mistaken_for_paths(self):
        body = "This is `important` and `not a path` but `src/a.py` is."
        assert sprint_plan.expected_files_from_body(body) == ["src/a.py"]

    def test_empty_body_yields_no_files_rather_than_raising(self):
        assert sprint_plan.expected_files_from_body(None) == []
        assert sprint_plan.expected_files_from_body("") == []
