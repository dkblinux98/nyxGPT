"""Unit tests for scripts/agents/lib/stop_loop_guard.py (#3790).

The guard is the backstop for the 2026-08-15 self-feeding loop: N
stop-without-progress cycles on one issue inside M minutes halt further
automatic retries and post ONE escalation comment instead of an (N+1)th stop
message. Only the repo owner clears the halt.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "stop_loop_guard.py"
)
_spec = importlib.util.spec_from_file_location("stop_loop_guard", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
stop_loop_guard = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = stop_loop_guard
_spec.loader.exec_module(stop_loop_guard)

NOW = datetime(2026, 8, 15, 3, 0, 0, tzinfo=UTC)


def _at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cycle(minutes_ago: float) -> dict:
    return {
        "body": f"⏹️ Stopping -- not In Progress.\n{stop_loop_guard.STOP_CYCLE_MARKER}",
        "created_at": _at(minutes_ago),
        "author_association": "NONE",
    }


def _halt(minutes_ago: float) -> dict:
    return {
        "body": f"🛑 Auto-retry halted.\n{stop_loop_guard.HALT_MARKER}",
        "created_at": _at(minutes_ago),
        "author_association": "NONE",
    }


def _owner(minutes_ago: float, body: str = "looking at this now") -> dict:
    return {"body": body, "created_at": _at(minutes_ago), "author_association": "OWNER"}


class TestEvaluate:
    def test_first_cycle_posts_the_normal_stop_message(self):
        decision = stop_loop_guard.evaluate([], NOW)
        assert decision["action"] == "stop-comment"
        assert decision["cycle_number"] == 1

    def test_second_cycle_still_posts_the_stop_message(self):
        decision = stop_loop_guard.evaluate([_cycle(5)], NOW)
        assert decision["action"] == "stop-comment"
        assert decision["cycle_number"] == 2

    def test_third_cycle_in_window_escalates_instead(self):
        decision = stop_loop_guard.evaluate([_cycle(10), _cycle(5)], NOW)
        assert decision["action"] == "escalate"
        assert decision["cycle_number"] == 3

    def test_escalation_happens_only_once(self):
        decision = stop_loop_guard.evaluate([_cycle(10), _cycle(5), _halt(1)], NOW)
        assert decision["action"] == "silent"
        assert decision["halted"] is True

    def test_cycles_outside_the_window_do_not_count(self):
        old = [_cycle(120), _cycle(90), _cycle(45)]
        decision = stop_loop_guard.evaluate(old, NOW)
        assert decision["action"] == "stop-comment"
        assert decision["prior_cycles"] == 0

    def test_a_halt_outside_the_window_no_longer_silences(self):
        decision = stop_loop_guard.evaluate([_halt(90)], NOW)
        assert decision["halted"] is False
        assert decision["action"] == "stop-comment"

    def test_owner_comment_resets_the_count_and_the_halt(self):
        thread = [_cycle(20), _cycle(15), _halt(12), _owner(10)]
        decision = stop_loop_guard.evaluate(thread, NOW)
        assert decision["halted"] is False
        assert decision["prior_cycles"] == 0
        assert decision["action"] == "stop-comment"

    def test_owner_comment_before_the_cycles_does_not_reset_them(self):
        thread = [_owner(25), _cycle(20), _cycle(15)]
        decision = stop_loop_guard.evaluate(thread, NOW)
        assert decision["action"] == "escalate"

    def test_agent_chatter_does_not_reset_the_count(self):
        chatter = {"body": "🤖 working on it", "created_at": _at(8), "author_association": "NONE"}
        thread = [_cycle(20), chatter, _cycle(15)]
        assert stop_loop_guard.evaluate(thread, NOW)["action"] == "escalate"

    def test_unparseable_timestamps_are_ignored(self):
        broken = {"body": stop_loop_guard.STOP_CYCLE_MARKER, "created_at": "not-a-date"}
        assert stop_loop_guard.evaluate([broken], NOW)["prior_cycles"] == 0

    def test_thresholds_are_configurable(self):
        thread = [_cycle(5)]
        decision = stop_loop_guard.evaluate(thread, NOW, max_cycles=2)
        assert decision["action"] == "escalate"


class TestGate:
    def test_unhalted_issue_proceeds(self):
        assert stop_loop_guard.gate([_cycle(5)], NOW)["proceed"] is True

    def test_halted_issue_blocks_agent_retries(self):
        decision = stop_loop_guard.gate([_cycle(10), _cycle(5), _halt(1)], NOW)
        assert decision["proceed"] is False
        assert "only the repo owner" in decision["reason"]

    def test_owner_can_always_resume_a_halted_issue(self):
        decision = stop_loop_guard.gate(
            [_cycle(10), _cycle(5), _halt(1)], NOW, author_is_owner=True
        )
        assert decision["proceed"] is True

    def test_halt_expires_with_the_window(self):
        assert stop_loop_guard.gate([_halt(90)], NOW)["proceed"] is True


class TestIncidentReplay:
    """#3790: ~250 comments per issue, one stop cycle every ~20 seconds."""

    def test_the_loop_is_cut_off_after_the_third_cycle(self):
        thread: list[dict] = []
        posted = 0
        for i in range(20):
            decision = stop_loop_guard.evaluate(thread, NOW)
            if decision["action"] == "stop-comment":
                thread.append(_cycle(20 - i * 0.3))
                posted += 1
            elif decision["action"] == "escalate":
                thread.append(_halt(20 - i * 0.3))
                posted += 1
            else:
                break
        assert posted == 3, "two stop messages plus one escalation, then silence"
        assert sum(1 for c in thread if stop_loop_guard.HALT_MARKER in c["body"]) == 1


class TestCli:
    def _run(self, args, thread):
        return subprocess.run(
            [sys.executable, str(_MODULE_PATH), *args],
            input=json.dumps(thread),
            capture_output=True,
            text=True,
        )

    def test_evaluate_mode_emits_json(self):
        result = self._run(["evaluate", "--now", _at(0)], [_cycle(10), _cycle(5)])
        assert result.returncode == 0
        assert json.loads(result.stdout)["action"] == "escalate"

    def test_gate_mode_respects_the_author_association(self):
        thread = [_cycle(10), _cycle(5), _halt(1)]
        blocked = self._run(["gate", "--now", _at(0)], thread)
        assert json.loads(blocked.stdout)["proceed"] is False
        allowed = self._run(
            ["gate", "--now", _at(0), "--author-association", "OWNER"],
            thread,
        )
        assert json.loads(allowed.stdout)["proceed"] is True

    def test_malformed_stdin_degrades_to_an_empty_thread(self):
        result = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "evaluate"],
            input="not json",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout)["action"] == "stop-comment"
