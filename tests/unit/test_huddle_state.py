"""Unit tests for scripts/agents/lib/huddle_state.py (#3736).

Pure computation only -- no gh calls -- covering the state the huddle
protocol carries in a PR's comment thread: which round a marker belongs to,
which of two racing markers owns the round, what a mediation decided, and
whether an escalation already went out.

The two incidents this module exists to prevent are reproduced end-to-end in
tests/unit/test_review_handoff.py (routing); these tests pin the primitives
those decisions are built from.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "huddle_state.py"
)
_spec = importlib.util.spec_from_file_location("huddle_state", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
huddle_state = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = huddle_state
_spec.loader.exec_module(huddle_state)

STRUCTURED = 'nyxgpt-structured-review {"decision":"REQUEST_CHANGES","disagreement_type":"a"}'


def _comment(created_at, body, comment_id=0):
    return {"created_at": created_at, "body": body, "id": comment_id}


#: Fixed reference clock: the fixtures below are dated, so the
#: pending-huddle deadline must be evaluated against a pinned "now"
#: rather than the wall clock, or these tests would start failing six
#: hours after the dates they describe.
NOW = datetime(2026, 8, 12, 19, 30, tzinfo=UTC)


def _status(comments, now=NOW):
    return huddle_state.huddle_status(comments, now=now)


def _round(structured_at="2026-08-12T18:00:00Z"):
    """A thread whose current round starts at `structured_at`."""
    return [_comment(structured_at, STRUCTURED, 100)]


class TestParseDecision:
    @pytest.mark.parametrize(
        "body,expected",
        [
            ("HUDDLE_DECISION: proceed", "proceed"),
            ("HUDDLE_DECISION: proceed as-is, the fix is right", "proceed"),
            ("HUDDLE_DECISION: change-approach", "change-approach"),
            ("HUDDLE_DECISION: change approach", "change-approach"),
            ("HUDDLE_DECISION: descope", "descope"),
            ("HUDDLE_DECISION: escalate to owner", "escalate"),
            ("HUDDLE_DECISION: **proceed**", "proceed"),
            ("HUDDLE_DECISION: `descope`", "descope"),
            ("HUDDLE_DECISION:proceed", "proceed"),
        ],
    )
    def test_recognizes_the_decision_prose_the_mediation_writes(self, body, expected):
        assert huddle_state.parse_decision(body) == expected

    def test_unfilled_template_is_not_a_decision(self):
        # The mediation prompt's own placeholder line, echoed verbatim: this
        # is not a decision and must never be read as one.
        body = "## Huddle Decision\n\nHUDDLE_DECISION: [proceed|change-approach|descope|escalate]"
        assert huddle_state.parse_decision(body) == ""

    def test_unknown_value_is_not_a_decision(self):
        assert huddle_state.parse_decision("HUDDLE_DECISION: maybe later") == ""

    def test_no_marker_is_not_a_decision(self):
        assert huddle_state.parse_decision("## Huddle Decision\n\nWe should proceed.") == ""

    def test_last_decision_line_wins(self):
        body = "HUDDLE_DECISION: [proceed|descope]\n\nfinal:\n\nHUDDLE_DECISION: descope"
        assert huddle_state.parse_decision(body) == "descope"


class TestRoundScoping:
    def test_round_starts_at_the_newest_structured_review_comment(self):
        comments = [
            _comment("2026-08-12T17:00:00Z", STRUCTURED, 1),
            _comment("2026-08-12T18:00:00Z", STRUCTURED, 2),
        ]
        assert huddle_state.round_start(comments) == "2026-08-12T18:00:00Z"

    def test_no_structured_comment_puts_the_whole_thread_in_scope(self):
        assert huddle_state.round_start([_comment("2026-08-12T17:00:00Z", "hi", 1)]) == ""

    def test_previous_round_markers_do_not_gate_this_round(self):
        comments = [
            _comment("2026-08-12T17:00:00Z", STRUCTURED, 1),
            _comment("2026-08-12T17:05:00Z", "🤝 Huddle triggered\n\nHUDDLE_TRIGGERED", 2),
            _comment("2026-08-12T18:00:00Z", STRUCTURED, 3),
        ]
        status = _status(comments)
        assert status["triggered"] is False
        assert status["pending"] is False

    def test_structured_comment_body_is_never_a_marker(self):
        # A review *of the huddle machinery* quotes these markers in its
        # summary; the structured comment embeds that summary verbatim.
        comments = [
            _comment(
                "2026-08-12T18:00:00Z",
                STRUCTURED + "\nsummary: dedupes HUDDLE_TRIGGERED and HUDDLE_DECISION: proceed",
                1,
            )
        ]
        assert _status(comments)["triggered"] is False


class TestPrimaryMarkerComment:
    def test_first_trigger_of_the_round_is_primary(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T18:09:05Z", "HUDDLE_TRIGGERED", 502),
        ]
        assert huddle_state.is_primary_marker_comment(comments, "HUDDLE_TRIGGERED", 501) is True

    def test_duplicate_trigger_is_not_primary(self):
        # PR #3728: two HUDDLE_TRIGGERED comments 105s apart, from the two
        # trigger paths of the same verdict. Only the first runs a huddle.
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T18:09:05Z", "HUDDLE_TRIGGERED", 502),
        ]
        assert huddle_state.is_primary_marker_comment(comments, "HUDDLE_TRIGGERED", 502) is False

    def test_same_second_ties_break_on_comment_id(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 700),
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 699),
        ]
        assert huddle_state.is_primary_marker_comment(comments, "HUDDLE_TRIGGERED", 699) is True
        assert huddle_state.is_primary_marker_comment(comments, "HUDDLE_TRIGGERED", 700) is False

    def test_unknown_comment_id_stands_down(self):
        comments = _round() + [_comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501)]
        assert huddle_state.is_primary_marker_comment(comments, "HUDDLE_TRIGGERED", 999) is False
        assert huddle_state.is_primary_marker_comment(comments, "HUDDLE_TRIGGERED", "x") is False

    def test_mediation_requests_dedupe_the_same_way(self):
        comments = _round() + [
            _comment("2026-08-12T18:12:00Z", "HUDDLE_MEDIATION_REQUESTED", 601),
            _comment("2026-08-12T18:13:00Z", "HUDDLE_MEDIATION_REQUESTED", 602),
        ]
        marker = huddle_state.HUDDLE_MEDIATION_MARKER
        assert huddle_state.is_primary_marker_comment(comments, marker, 601) is True
        assert huddle_state.is_primary_marker_comment(comments, marker, 602) is False


class TestHuddleStatus:
    def test_triggered_without_a_decision_is_pending(self):
        comments = _round() + [_comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501)]
        status = _status(comments)
        assert status["pending"] is True
        assert status["triggered"] is True
        assert status["decision"] == ""

    def test_counts_a_raced_double_trigger(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T18:09:05Z", "HUDDLE_TRIGGERED", 502),
        ]
        assert _status(comments)["trigger_count"] == 2

    def test_decision_closes_the_pending_state_and_is_resumable(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T19:04:00Z", "## Huddle Decision\n\nHUDDLE_DECISION: proceed", 601),
        ]
        status = _status(comments)
        assert status["pending"] is False
        assert status["decision"] == "proceed"
        assert status["resumable"] is True
        assert status["escalated"] is False
        assert status["dispatched"] is False

    def test_escalate_decision_is_terminal_not_resumable(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T19:04:00Z", "HUDDLE_DECISION: escalate", 601),
        ]
        status = _status(comments)
        assert status["escalated"] is True
        assert status["resumable"] is False

    def test_dispatch_marker_is_recorded(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T19:04:00Z", "HUDDLE_DECISION: change-approach", 601),
            _comment("2026-08-12T19:05:00Z", "🔁 dispatched\n\nHUDDLE_DECISION_DISPATCHED", 602),
        ]
        assert _status(comments)["dispatched"] is True

    def test_an_unparsable_decision_leaves_the_huddle_pending(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T19:04:00Z", "HUDDLE_DECISION: [proceed|descope]", 601),
        ]
        assert _status(comments)["pending"] is True

    def test_an_abandoned_huddle_stops_deferring_after_the_deadline(self):
        # A pending huddle silences the whole review path, so the deferral
        # is bounded: if the position or mediation run never happens, the
        # round must route by cycle count again instead of stalling.
        comments = _round() + [_comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501)]
        now = datetime(2026, 8, 13, 6, 0, tzinfo=UTC)
        status = huddle_state.huddle_status(comments, now=now)
        assert status["stale"] is True
        assert status["pending"] is False

    def test_a_fresh_huddle_is_still_pending(self):
        comments = _round() + [_comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501)]
        now = datetime(2026, 8, 12, 19, 0, tzinfo=UTC)
        status = huddle_state.huddle_status(comments, now=now)
        assert status["pending"] is True
        assert status["stale"] is False

    def test_a_decided_huddle_is_never_stale(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T19:04:00Z", "HUDDLE_DECISION: proceed", 601),
        ]
        now = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
        assert huddle_state.huddle_status(comments, now=now)["stale"] is False

    def test_an_unreadable_timestamp_keeps_the_deferral(self):
        # Guessing "expired" from a timestamp that could not be parsed would
        # drop the deferral on a live huddle.
        comments = _round() + [_comment("not-a-timestamp", "HUDDLE_TRIGGERED", 501)]
        now = datetime(2026, 8, 20, 0, 0, tzinfo=UTC)
        status = huddle_state.huddle_status(comments, now=now)
        assert status["stale"] is False
        assert status["pending"] is True

    def test_decision_from_before_the_trigger_does_not_close_it(self):
        comments = [
            _comment("2026-08-12T17:50:00Z", "HUDDLE_DECISION: proceed", 400),
            _comment("2026-08-12T18:00:00Z", STRUCTURED, 100),
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
        ]
        assert _status(comments)["pending"] is True


class TestEscalationRecorded:
    def test_detects_a_cycle_limit_escalation_in_this_round(self):
        comments = _round() + [
            _comment("2026-08-12T19:10:56Z", "⚠️ Escalated after 3 review cycles", 700),
        ]
        assert huddle_state.escalation_recorded(comments, huddle_state.round_start(comments))

    def test_detects_a_spec_ambiguity_escalation(self):
        comments = _round() + [
            _comment("2026-08-12T19:10:56Z", "Escalated immediately — spec ambiguity", 700),
        ]
        assert huddle_state.escalation_recorded(comments, huddle_state.round_start(comments))

    def test_previous_round_escalation_does_not_block_this_one(self):
        comments = [
            _comment("2026-08-12T17:00:00Z", STRUCTURED, 1),
            _comment("2026-08-12T17:10:00Z", "⚠️ Escalated after 3 review cycles", 2),
            _comment("2026-08-12T18:00:00Z", STRUCTURED, 3),
        ]
        assert not huddle_state.escalation_recorded(comments, huddle_state.round_start(comments))

    def test_clean_round_has_no_escalation(self):
        comments = _round() + [_comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501)]
        assert not huddle_state.escalation_recorded(comments, huddle_state.round_start(comments))

    def test_prose_about_spec_ambiguity_is_not_an_escalation(self):
        # A developer position arguing the issue is ambiguous must not read
        # as "already escalated" -- that would suppress the real escalation
        # and stall the round, the very failure mode this issue is about.
        comments = _round() + [
            _comment(
                "2026-08-12T18:20:00Z",
                "## Developer Position\n\nI think this is a spec ambiguity.\n\nHUDDLE_DEV_POSITION",
                600,
            ),
        ]
        assert not huddle_state.escalation_recorded(comments, huddle_state.round_start(comments))


class TestCli:
    def _run(self, args, stdin):
        return subprocess.run(
            [sys.executable, str(_MODULE_PATH), *args],
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_status_emits_eval_ready_lines(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T19:04:00Z", "HUDDLE_DECISION: proceed", 601),
        ]
        result = self._run(["status"], json.dumps({"comments": comments}))
        assert result.returncode == 0, result.stderr
        lines = dict(line.split("=", 1) for line in result.stdout.strip().splitlines())
        assert lines["triggered"] == "true"
        assert lines["pending"] == "false"
        assert lines["decision"] == "proceed"
        assert lines["round_start"] == "2026-08-12T18:00:00Z"

    def test_guard_reports_the_duplicate(self):
        comments = _round() + [
            _comment("2026-08-12T18:07:20Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T18:09:05Z", "HUDDLE_TRIGGERED", 502),
        ]
        payload = json.dumps({"comments": comments})
        assert self._run(["guard", "HUDDLE_TRIGGERED", "501"], payload).stdout.strip() == "true"
        assert self._run(["guard", "HUDDLE_TRIGGERED", "502"], payload).stdout.strip() == "false"

    def test_escalation_recorded_defaults_to_the_current_round(self):
        comments = _round() + [
            _comment("2026-08-12T19:10:56Z", "Escalated after 3 review cycles", 7)
        ]
        result = self._run(["escalation-recorded"], json.dumps({"comments": comments}))
        assert result.stdout.strip() == "true"

    def test_decision_parses_a_raw_body(self):
        result = self._run(["decision"], "## Huddle Decision\n\nHUDDLE_DECISION: descope\n")
        assert result.stdout.strip() == "descope"

    def test_bad_usage_exits_two(self):
        result = self._run([], "")
        assert result.returncode == 2
        assert "usage" in result.stderr
        assert self._run(["nope"], "{}").returncode == 2
        assert self._run(["guard", "ONLY_ONE_ARG"], "{}").returncode == 2
