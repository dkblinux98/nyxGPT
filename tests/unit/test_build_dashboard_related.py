"""Unit tests for the retrospective's relationship attribution (#3731).

Owner decision 2026-08-12: failure/improvement attribution follows GitHub's
native blocked-by/blocks relationships, not `Related feature: #N` body prose.
These cover both halves of that data path:

  * `dump_relationships.build_snapshot` — the relationships.json shape the
    dump workflow commits, including the inverted `blocked_by` side
  * `build_dashboard.blocks_map` / `attribute_related` — native first, with
    the corpus's prose-derived `related` used only for historical issues, and
    the `native`/`prose`/`none` split reported so the fallback can be retired
    once it reaches zero
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


@pytest.fixture(scope="module")
def dump_relationships():
    return _load("dump_relationships", RETRO_DIR / "dump_relationships.py")


def _issue(n, labels=None, related=None):
    """A minimal all_issues.json entry; attribution reads n/labels/related."""
    entry = {
        "n": n,
        "title": f"issue {n}",
        "labels": labels or ["Feature"],
        "milestone": "Phase 6",
        "created": "2026-08-12T00:00:00Z",
        "closed": None,
        "state": "open",
    }
    if related is not None:
        entry["related"] = related
    return entry


def _relationships(**blocks):
    return {"issues": {k: {"blocks": v, "blocked_by": []} for k, v in blocks.items()}}


# --- the dump snapshot ------------------------------------------------


def test_snapshot_records_both_directions(dump_relationships):
    snap = dump_relationships.build_snapshot("o/r", [3733], {3733: [3730]})
    assert snap["issues"]["3733"]["blocks"] == [3730]
    assert snap["issues"]["3730"]["blocked_by"] == [3733]
    assert snap["repo"] == "o/r"


def test_snapshot_collects_multiple_blockers(dump_relationships):
    snap = dump_relationships.build_snapshot("o/r", [3733, 3740], {3733: [3730], 3740: [3730]})
    assert snap["issues"]["3730"]["blocked_by"] == [3733, 3740]


def test_snapshot_keeps_unlinked_candidates(dump_relationships):
    """An Improvement with no relationship still appears, with empty edges."""
    snap = dump_relationships.build_snapshot("o/r", [3741], {3741: []})
    assert snap["issues"]["3741"] == {"blocks": [], "blocked_by": []}


def test_snapshot_of_nothing_is_empty(dump_relationships):
    assert dump_relationships.build_snapshot("o/r", [], {})["issues"] == {}


# --- blocks_map -------------------------------------------------------


def test_blocks_map_reads_native_edges(build_dashboard):
    assert build_dashboard.blocks_map(_relationships(**{"3733": [3730]})) == {3733: [3730]}


def test_blocks_map_skips_empty_and_missing(build_dashboard):
    assert build_dashboard.blocks_map(_relationships(**{"3733": []})) == {}
    assert build_dashboard.blocks_map(None) == {}
    assert build_dashboard.blocks_map({}) == {}


# --- attribution ------------------------------------------------------


def test_native_relationship_wins_over_prose(build_dashboard):
    issues = [_issue(3733, ["Acceptance Failure"], related=99)]
    counts = build_dashboard.attribute_related(issues, _relationships(**{"3733": [3730]}))
    assert issues[0]["related"] == 3730
    assert issues[0]["relatedSource"] == "native"
    assert counts == {"native": 1, "prose": 0, "none": 0}


def test_historical_issue_falls_back_to_prose_derived_field(build_dashboard):
    """No native edge: the corpus's `related` keeps old data attributing."""
    issues = [_issue(3000, ["Acceptance Failure"], related=2999)]
    counts = build_dashboard.attribute_related(issues, _relationships())
    assert issues[0]["related"] == 2999
    assert issues[0]["relatedSource"] == "prose"
    assert counts["prose"] == 1


def test_missing_relationships_file_is_not_an_error(build_dashboard):
    issues = [_issue(3000, ["Acceptance Failure"], related=2999)]
    assert build_dashboard.attribute_related(issues, None)["prose"] == 1


def test_unattributed_failure_is_counted_as_a_gap(build_dashboard):
    issues = [_issue(3733, ["Acceptance Failure"])]
    counts = build_dashboard.attribute_related(issues, _relationships())
    assert issues[0]["related"] is None
    assert counts == {"native": 0, "prose": 0, "none": 1}


def test_plain_feature_is_not_counted_as_a_gap(build_dashboard):
    """Only failures/improvements are expected to relate to anything."""
    issues = [_issue(3730, ["Feature"])]
    counts = build_dashboard.attribute_related(issues, _relationships())
    assert counts == {"native": 0, "prose": 0, "none": 0}


def test_improvements_are_attributed_too(build_dashboard):
    """Both commands write the relationship, so both must resolve (#3731)."""
    issues = [_issue(3741, ["Improvement"])]
    counts = build_dashboard.attribute_related(issues, _relationships(**{"3741": [3730]}))
    assert issues[0]["related"] == 3730
    assert counts["native"] == 1


def test_qtotals_carries_the_attribution_split(build_dashboard):
    issues = [
        _issue(3733, ["Acceptance Failure"]),
        _issue(3000, ["Acceptance Failure"], related=2999),
    ]
    qdata = build_dashboard.build_qdata(issues, None, _relationships(**{"3733": [3730]}))
    assert qdata["attribution"] == {"native": 1, "prose": 1, "none": 0}
