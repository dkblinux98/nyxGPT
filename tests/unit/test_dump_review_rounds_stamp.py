"""The review-round dump stamps its snapshot with an as-of time (#3807).

`reviews_final.json` is a bare list with nowhere to record when it was dumped,
so `dashboard_data.json` — written by the same run — carries the stamp the
dashboard reports as the review-round data's as-of time.
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


@pytest.fixture(scope="module")
def dump_review_rounds():
    sys.path.insert(0, str(RETRO_DIR))
    spec = importlib.util.spec_from_file_location(
        "dump_review_rounds", RETRO_DIR / "dump_review_rounds.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["dump_review_rounds"] = module
    spec.loader.exec_module(module)
    return module


def _snapshot(module, **kwargs):
    return module.build_dashboard_snapshot(
        [], "2026-08-11T00:00:00Z", "2026-08-18T00:00:00Z", [], set(), **kwargs
    )


def test_snapshot_carries_the_dump_time(dump_review_rounds):
    snapshot = _snapshot(dump_review_rounds, generated_at="2026-08-18T09:00:00+00:00")
    assert snapshot["generated_at"] == "2026-08-18T09:00:00+00:00"


def test_snapshot_stamps_itself_when_no_time_is_passed(dump_review_rounds):
    """A caller that forgets the stamp still gets one, never an unstamped file."""
    snapshot = _snapshot(dump_review_rounds)
    stamped = datetime.fromisoformat(snapshot["generated_at"])
    assert stamped.tzinfo is not None
    assert abs((datetime.now(UTC) - stamped).total_seconds()) < 60


def test_snapshot_keeps_its_existing_shape(dump_review_rounds):
    snapshot = _snapshot(dump_review_rounds)
    assert {"modules", "days", "issues", "cleanPRs", "cleanByModule", "totals"} <= set(snapshot)
