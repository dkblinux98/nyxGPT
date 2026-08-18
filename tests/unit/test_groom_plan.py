"""Unit tests for the grooming seed (#3908).

The seed is a draft, not a verdict -- but a draft that reorders work behind a
dependency, or that discards a scrummaster's hand-curated expected-files on
the next regroom, is worse than none. Those two properties are what these
tests hold.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LIB = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
sys.path.insert(0, str(_LIB))

for _name in ("sprint_plan", "groom_plan"):
    _spec = importlib.util.spec_from_file_location(_name, _LIB / f"{_name}.py")
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_name] = _module
    _spec.loader.exec_module(_module)

groom_plan = sys.modules["groom_plan"]
sprint_plan = sys.modules["sprint_plan"]


ITEMS = [
    {"issue": 10, "title": "low", "priority": "P3 - Low", "effort": "S", "body": ""},
    {"issue": 20, "title": "critical", "priority": "P0 - Critical", "effort": "L", "body": ""},
    {"issue": 30, "title": "high small", "priority": "P1 - High", "effort": "XS", "body": ""},
    {"issue": 40, "title": "high large", "priority": "P1 - High", "effort": "L", "body": ""},
]


def numbers(plan):
    return [entry["issue"] for entry in plan["order"]]


def test_seed_order_is_priority_then_effort_then_number():
    plan = groom_plan.build_plan(
        sprint="Sprint 12",
        window={},
        milestone="v3.0.0",
        items=ITEMS,
        blocked_by={},
        blocks={},
    )
    assert numbers(plan) == [20, 30, 40, 10]


def test_an_in_sprint_blocker_is_ordered_before_what_it_blocks():
    plan = groom_plan.build_plan(
        sprint="Sprint 12",
        window={},
        milestone="v3.0.0",
        items=ITEMS,
        blocked_by={20: [10]},
        blocks={10: [20]},
    )
    assert numbers(plan).index(10) < numbers(plan).index(20)


def test_a_relationship_cycle_degrades_to_input_order_rather_than_dropping_work():
    plan = groom_plan.build_plan(
        sprint="Sprint 12",
        window={},
        milestone="v3.0.0",
        items=ITEMS,
        blocked_by={10: [20], 20: [10]},
        blocks={},
    )
    assert sorted(numbers(plan)) == [10, 20, 30, 40]


def test_blockers_outside_the_sprint_do_not_reorder_the_plan():
    plan = groom_plan.build_plan(
        sprint="Sprint 12",
        window={},
        milestone="v3.0.0",
        items=ITEMS,
        blocked_by={20: [9999]},
        blocks={},
    )
    assert numbers(plan)[0] == 20
    assert plan["order"][0]["blocked_by"] == [9999]


def test_expected_files_are_seeded_from_the_issue_body():
    items = [
        {
            "issue": 10,
            "title": "t",
            "priority": "P1 - High",
            "effort": "S",
            "body": "- Files affected: `src/nyxgpt/ops.py`",
        }
    ]
    plan = groom_plan.build_plan(
        sprint="Sprint 12",
        window={},
        milestone="v3.0.0",
        items=items,
        blocked_by={},
        blocks={},
    )
    assert plan["order"][0]["expected_files"] == ["src/nyxgpt/ops.py"]


def test_a_regroom_preserves_curated_files_rationale_deferrals_and_the_log():
    previous = {
        "order": [
            {
                "issue": 10,
                "expected_files": ["src/nyxgpt/hand_curated.py"],
                "why_here": "the owner asked for it first",
            }
        ],
        "order_rationale": "hand written",
        "deferred": [{"issue": 99, "why": "next sprint"}],
        "regroom_log": [{"at": "2026-08-19", "change": "added #40"}],
        "capacity": {"velocity_points": 12},
    }
    plan = groom_plan.build_plan(
        sprint="Sprint 12",
        window={},
        milestone="v3.0.0",
        items=[
            {
                "issue": 10,
                "title": "t",
                "priority": "P1 - High",
                "effort": "S",
                "body": "- Files affected: `src/nyxgpt/reseeded.py`",
            }
        ],
        blocked_by={},
        blocks={},
        previous=previous,
    )
    entry = plan["order"][0]
    assert entry["expected_files"] == ["src/nyxgpt/hand_curated.py"]
    assert entry["why_here"] == "the owner asked for it first"
    assert plan["order_rationale"] == "hand written"
    assert plan["deferred"] == [{"issue": 99, "why": "next sprint"}]
    assert plan["regroom_log"] == [{"at": "2026-08-19", "change": "added #40"}]
    assert plan["capacity"] == {"velocity_points": 12}


def test_the_seed_renders_into_a_plan_the_pull_step_can_read():
    plan = groom_plan.build_plan(
        sprint="Sprint 12",
        window={"start": "2026-08-18", "end": "2026-09-01"},
        milestone="v3.0.0",
        items=ITEMS,
        blocked_by={},
        blocks={},
    )
    assert sprint_plan.plan_order(sprint_plan.parse_plan(sprint_plan.render_plan(plan))) == numbers(
        plan
    )


def test_unknown_field_values_sort_last_rather_than_outranking_a_deliberate_one():
    items = ITEMS + [{"issue": 50, "title": "ungroomed", "priority": "", "effort": "", "body": ""}]
    plan = groom_plan.build_plan(
        sprint="Sprint 12",
        window={},
        milestone="v3.0.0",
        items=items,
        blocked_by={},
        blocks={},
    )
    assert numbers(plan)[-1] == 50
