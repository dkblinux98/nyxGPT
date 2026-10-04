"""The usage-limit retry queue that replaced the `usage-limit-retry` label.

The label is gone (#4134): agents do not create labels, the owner kept
deleting that one, and it broke the one-label invariant on every issue it
touched. The queue it stood for is now marker comments on the release
tracking issue, and the decision "who is due a retry right now?" is this
module -- pure, so it is tested without GitHub.

What the label did implicitly and these tests pin explicitly:

* adding the label == a schedule marker; removing it == a `done` marker;
* "the cron finds nothing" and "the queue is empty" must be the same answer
  only when the queue really is empty -- which is why `queue` is derived from
  the whole thread every time, with no counter to drift;
* a SECOND usage-limit hit on an issue that was already retried re-enters the
  queue (the old label was simply re-added).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LIB = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
sys.path.insert(0, str(LIB))

import usage_limit_retry as ulr  # noqa: E402


class TestSchedule:
    def test_a_scheduled_target_is_queued(self):
        bodies = [ulr.schedule_marker(4134, "issue", 1_000)]
        assert ulr.queue(bodies) == [{"target": 4134, "kind": "issue", "after": 1_000}]

    def test_kind_is_validated(self):
        with pytest.raises(ValueError):
            ulr.schedule_marker(1, "epic", 1)

    def test_issues_and_prs_are_distinguished(self):
        bodies = [ulr.schedule_marker(10, "issue", 1), ulr.schedule_marker(11, "pr", 1)]
        kinds = {entry["target"]: entry["kind"] for entry in ulr.queue(bodies)}
        assert kinds == {10: "issue", 11: "pr"}

    def test_the_marker_survives_surrounding_prose(self):
        body = f"⏳ **Usage-limit retry queued**: issue #7.\n\n{ulr.schedule_marker(7, 'issue', 5)}"
        assert ulr.queue([body])[0]["target"] == 7


class TestDone:
    def test_a_done_marker_retires_the_target(self):
        bodies = [ulr.schedule_marker(10, "issue", 1), ulr.done_marker(10)]
        assert ulr.queue(bodies) == []

    def test_a_done_marker_for_another_target_leaves_this_one_queued(self):
        bodies = [ulr.schedule_marker(10, "issue", 1), ulr.done_marker(11)]
        assert [e["target"] for e in ulr.queue(bodies)] == [10]

    def test_a_reschedule_after_done_re_enters_the_queue(self):
        """A second usage-limit hit on the same issue. The label did this by
        being re-added; the marker does it by being re-posted."""
        bodies = [
            ulr.schedule_marker(10, "issue", 1),
            ulr.done_marker(10),
            ulr.schedule_marker(10, "issue", 900),
        ]
        assert ulr.queue(bodies) == [{"target": 10, "kind": "issue", "after": 900}]

    def test_the_later_marker_wins_within_one_comment(self):
        body = f"{ulr.schedule_marker(10, 'issue', 1)}\n{ulr.done_marker(10)}"
        assert ulr.queue([body]) == []


class TestDue:
    def test_not_due_before_the_deadline(self):
        bodies = [ulr.schedule_marker(10, "issue", 1_000)]
        assert ulr.due(bodies, 999) == []

    def test_due_at_the_deadline(self):
        bodies = [ulr.schedule_marker(10, "issue", 1_000)]
        assert [e["target"] for e in ulr.due(bodies, 1_000)] == [10]

    def test_only_the_due_ones(self):
        bodies = [ulr.schedule_marker(10, "issue", 100), ulr.schedule_marker(11, "pr", 10_000)]
        assert [e["target"] for e in ulr.due(bodies, 500)] == [10]

    def test_an_empty_thread_is_an_empty_queue(self):
        assert ulr.queue([]) == []
        assert ulr.queue(None) == []
        assert ulr.due([], 1) == []

    def test_unrelated_comments_are_ignored(self):
        """Including the per-issue `usage-limit-retry-after` comment, which
        still exists on the issue and is what counts the attempt budget --
        it must not also act as a queue entry on the release thread."""
        bodies = ["<!-- usage-limit-retry-after: 1000 -->", "nothing to see"]
        assert ulr.queue(bodies) == []
