"""Unit tests for scripts/agents/lib/cross_issue_anomaly.py (#3694).

Pure computation only -- no gh/GraphQL involved -- covering the
cross-issue infrastructure-anomaly collapse: the first issue to hit a
failed step opens a single tracking-record marker on the release issue,
every other issue hitting the same step while it's still open (and
unresolved) links to it instead of repeating the diagnosis.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "cross_issue_anomaly.py"
)
_spec = importlib.util.spec_from_file_location("cross_issue_anomaly", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
cross_issue_anomaly = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cross_issue_anomaly
_spec.loader.exec_module(cross_issue_anomaly)

STEP = "Check if PR already exists"
WINDOW = cross_issue_anomaly.DEFAULT_WINDOW_MINUTES


def _bot_comment(body: str) -> dict:
    return {"body": body, "author_association": "NONE"}


def _owner_comment(body: str) -> dict:
    return {"body": body, "author_association": "OWNER"}


class TestMarkerRoundTrip:
    def test_render_then_parse(self):
        marker = cross_issue_anomaly.render_marker(STEP, 3667, 1000)
        parsed = cross_issue_anomaly.parse_markers(f"some text\n{marker}\nmore text")
        assert parsed == [{"step": "check_if_pr_already_exists", "issue": 3667, "opened": 1000}]

    def test_no_marker_returns_empty(self):
        assert cross_issue_anomaly.parse_markers("just a regular comment") == []

    def test_ignores_malformed_markers(self):
        assert cross_issue_anomaly.parse_markers("<!-- nyxgpt-anomaly: step=foo -->") == []


class TestFindOpenAnomaly:
    def test_no_markers_at_all(self):
        assert cross_issue_anomaly.find_open_anomaly([], STEP, 1500) is None

    def test_open_marker_within_window(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000))]
        result = cross_issue_anomaly.find_open_anomaly(comments, STEP, 1000 + 60)
        assert result == {
            "step": "check_if_pr_already_exists",
            "origin_issue": 3667,
            "opened_epoch": 1000,
        }

    def test_expired_marker_outside_window_is_not_open(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000))]
        now = 1000 + WINDOW * 60 + 1
        assert cross_issue_anomaly.find_open_anomaly(comments, STEP, now) is None

    def test_marker_at_exactly_the_window_boundary_is_still_open(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000))]
        now = 1000 + WINDOW * 60
        assert cross_issue_anomaly.find_open_anomaly(comments, STEP, now) is not None

    def test_different_step_does_not_match(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker("Push branch", 3667, 1000))]
        assert cross_issue_anomaly.find_open_anomaly(comments, STEP, 1500) is None

    def test_owner_resolve_comment_after_marker_closes_it(self):
        comments = [
            _bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000)),
            _owner_comment("RESOLVE_ANOMALY"),
        ]
        assert cross_issue_anomaly.find_open_anomaly(comments, STEP, 1500) is None

    def test_non_owner_resolve_comment_does_not_close_it(self):
        comments = [
            _bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000)),
            _bot_comment("RESOLVE_ANOMALY"),
        ]
        result = cross_issue_anomaly.find_open_anomaly(comments, STEP, 1500)
        assert result is not None
        assert result["origin_issue"] == 3667

    def test_resolve_comment_before_marker_does_not_close_a_later_marker(self):
        comments = [
            _owner_comment("RESOLVE_ANOMALY"),
            _bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000)),
        ]
        assert cross_issue_anomaly.find_open_anomaly(comments, STEP, 1500) is not None

    def test_latest_marker_for_a_step_wins_over_an_earlier_one(self):
        comments = [
            _bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 500)),
            _bot_comment(cross_issue_anomaly.render_marker(STEP, 3689, 1000)),
        ]
        result = cross_issue_anomaly.find_open_anomaly(comments, STEP, 1500)
        assert result["origin_issue"] == 3689


class TestDecide:
    def test_no_existing_anomaly_opens_a_new_one(self):
        result = cross_issue_anomaly.decide([], 3667, STEP, 1000)
        assert result == {
            "action": "open",
            "origin_issue": 3667,
            "step": "check_if_pr_already_exists",
        }

    def test_matching_open_anomaly_from_another_issue_is_skipped(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000))]
        result = cross_issue_anomaly.decide(comments, 3511, STEP, 1500)
        assert result["action"] == "skip"
        assert result["origin_issue"] == 3667

    def test_the_origin_issue_itself_proceeds_rather_than_skipping(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000))]
        result = cross_issue_anomaly.decide(comments, 3667, STEP, 1500)
        assert result["action"] == "proceed"

    def test_expired_anomaly_lets_a_new_one_open(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000))]
        now = 1000 + WINDOW * 60 + 1
        result = cross_issue_anomaly.decide(comments, 3511, STEP, now)
        assert result["action"] == "open"
        assert result["origin_issue"] == 3511

    def test_resolved_anomaly_lets_a_new_one_open(self):
        comments = [
            _bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000)),
            _owner_comment("RESOLVE_ANOMALY"),
        ]
        result = cross_issue_anomaly.decide(comments, 3511, STEP, 1500)
        assert result["action"] == "open"
        assert result["origin_issue"] == 3511


class TestAnyOpenAnomaly:
    def test_false_when_no_markers(self):
        assert cross_issue_anomaly.any_open_anomaly([], 1000) is False

    def test_true_when_an_open_marker_exists(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000))]
        assert cross_issue_anomaly.any_open_anomaly(comments, 1500) is True

    def test_false_when_the_only_marker_is_resolved(self):
        comments = [
            _bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000)),
            _owner_comment("RESOLVE_ANOMALY"),
        ]
        assert cross_issue_anomaly.any_open_anomaly(comments, 1500) is False

    def test_false_when_the_only_marker_has_expired(self):
        comments = [_bot_comment(cross_issue_anomaly.render_marker(STEP, 3667, 1000))]
        now = 1000 + WINDOW * 60 + 1
        assert cross_issue_anomaly.any_open_anomaly(comments, now) is False

    def test_true_if_any_of_several_steps_has_an_open_marker(self):
        comments = [
            _bot_comment(cross_issue_anomaly.render_marker("Push branch", 3667, 1000)),
            _owner_comment("RESOLVE_ANOMALY"),
            _bot_comment(cross_issue_anomaly.render_marker(STEP, 3689, 1200)),
        ]
        assert cross_issue_anomaly.any_open_anomaly(comments, 1500) is True
