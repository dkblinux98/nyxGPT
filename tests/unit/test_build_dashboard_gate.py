"""Unit tests for build_dashboard.py's review-gate series (#3667).

Covers gate_series(), which recomputes the current month's REQUEST_CHANGES
"rejected" count from data/reviews_final.json — the GitHub-native PR-review
dump that replaced the Gmail month-window search (owner decision 2026-08-08).
Older months predate that dump and must keep their seeded historical values.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
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


def _review(date):
    """A minimal reviews_final.json entry — gate_series only reads `date`."""
    return {"issue": 1, "pr": 2, "module": "api", "date": date}


AUG = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _month(gate, label):
    return next(g for g in gate if g["m"] == label)


def test_current_month_rejected_comes_from_the_review_dump(build_dashboard):
    """August's count is the number of dump rounds dated in August, not the seed."""
    reviews = [
        _review("2026-08-02T09:00:00Z"),
        _review("2026-08-06T11:30:00Z"),
        _review("2026-08-09T23:59:59Z"),
    ]

    gate = build_dashboard.gate_series([], {}, reviews, now=AUG)

    assert _month(gate, "Aug")["rejected"] == 3
    # The seeded constant for August must not survive.
    assert _month(gate, "Aug")["rejected"] != _month(build_dashboard.GATE, "Aug")["rejected"]


def test_other_months_reviews_do_not_leak_into_the_current_month(build_dashboard):
    """Only rounds dated in the current month count; earlier months keep their seed."""
    reviews = [
        _review("2026-07-31T23:00:00Z"),
        _review("2026-01-15T10:00:00Z"),
        _review("2026-08-04T10:00:00Z"),
    ]

    gate = build_dashboard.gate_series([], {}, reviews, now=AUG)

    assert _month(gate, "Aug")["rejected"] == 1
    for label in ("Jan", "Feb", "Mar", "Jul"):
        assert _month(gate, label)["rejected"] == _month(build_dashboard.GATE, label)["rejected"]


def test_current_month_with_no_rounds_is_zero_not_the_seed(build_dashboard):
    """An empty dump means zero rejections this month — it must not fall back to the seed."""
    gate = build_dashboard.gate_series([], {}, [_review("2026-07-04T10:00:00Z")], now=AUG)

    assert _month(gate, "Aug")["rejected"] == 0


def test_month_outside_the_seeded_series_leaves_every_month_untouched(build_dashboard):
    """GATE runs Jan–Aug; a `now` past its end must not index out of range."""
    reviews = [_review("2026-12-01T10:00:00Z")]

    gate = build_dashboard.gate_series([], {}, reviews, now=datetime(2026, 12, 1, tzinfo=UTC))

    assert [g["rejected"] for g in gate] == [g["rejected"] for g in build_dashboard.GATE]


def test_gate_series_does_not_mutate_the_seeded_constant(build_dashboard):
    """gate_series copies GATE; repeated calls must be independent."""
    before = [dict(g) for g in build_dashboard.GATE]

    build_dashboard.gate_series([], {}, [_review("2026-08-01T10:00:00Z")], now=AUG)

    assert before == build_dashboard.GATE


def test_rejected_count_is_independent_of_af_pm_and_merged_inputs(build_dashboard):
    """The dump-derived count coexists with issue causes and pr_times aggregates."""
    issues = [
        {"created": "2026-08-01T10:00:00Z", "cause": "defect"},
        {"created": "2026-08-02T10:00:00Z", "cause": "spec"},
        {"created": "2026-07-02T10:00:00Z", "cause": "pm"},
    ]
    pr_times = {
        "10": ["2026-08-01T00:00:00Z", "2026-08-01T02:00:00Z"],
        "11": ["2026-08-02T00:00:00Z", "2026-08-02T06:00:00Z"],
    }

    gate = build_dashboard.gate_series(issues, pr_times, [_review("2026-08-03T10:00:00Z")], now=AUG)

    aug = _month(gate, "Aug")
    assert aug["rejected"] == 1
    assert aug["af"] == 2
    assert aug["pm"] == 0
    assert _month(gate, "Jul")["pm"] == 1
    assert aug["merged"] == 2
    assert aug["medianHrs"] == 4.0
