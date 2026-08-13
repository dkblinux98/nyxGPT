"""Unit tests for scripts/agents/lib/review_handoff.py (#3704).

Pure computation only -- no gh calls -- covering the decision half of the
dispatch-mode REQUEST_CHANGES handoff backstop: which verdict is current,
whether the normal event chain already handled it, the a/b/c classification,
and the resulting route.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "review_handoff.py"
)
_spec = importlib.util.spec_from_file_location("review_handoff", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
review_handoff = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = review_handoff
_spec.loader.exec_module(review_handoff)

AGENT = "myGPT-review-agent"


def _review(state, submitted_at, login=AGENT, body=""):
    return {
        "user": {"login": login},
        "state": state,
        "submitted_at": submitted_at,
        "body": body,
    }


def _comment(created_at, body, comment_id=0):
    return {"created_at": created_at, "body": body, "id": comment_id}


def _structured(created_at, dtype="a", comment_id=0):
    body = f'<!-- nyxgpt-structured-review: {{"disagreement_type":"{dtype}"}} -->'
    return _comment(created_at, body, comment_id)


class TestLatestVerdict:
    def test_ignores_commented_reviews(self):
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-09T05:33:00Z"),
            _review("COMMENTED", "2026-08-09T06:00:00Z"),
        ]
        verdict = review_handoff.latest_verdict(reviews, AGENT)
        assert verdict is not None
        assert verdict["submitted_at"] == "2026-08-09T05:33:00Z"

    def test_ignores_other_users(self):
        reviews = [_review("APPROVED", "2026-08-09T05:33:00Z", login="someone-else")]
        assert review_handoff.latest_verdict(reviews, AGENT) is None

    def test_sorts_by_submitted_at_not_api_order(self):
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-09T19:39:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-09T05:33:00Z"),
        ]
        verdict = review_handoff.latest_verdict(reviews, AGENT)
        assert verdict is not None
        assert verdict["submitted_at"] == "2026-08-09T19:39:00Z"


class TestRequestChangesCount:
    def test_counts_only_agent_changes_requested(self):
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-09T05:33:00Z"),
            _review("APPROVED", "2026-08-09T06:00:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-09T19:39:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-09T20:00:00Z", login="human"),
        ]
        assert review_handoff.request_changes_count(reviews, AGENT) == 2


class TestHandoffRecorded:
    @pytest.mark.parametrize(
        "body",
        [
            "🔄 **Review Agent**: Changes requested (review loop 2/3)",
            "🤝 Huddle triggered\n\nHUDDLE_TRIGGERED",
            "⚠️ **Review Agent**: Escalated after 3 review cycles",
            "⚠️ Escalated immediately — spec ambiguity (type c)",
        ],
    )
    def test_detects_each_handoff_marker(self, body):
        comments = [_comment("2026-08-09T19:45:00Z", body)]
        assert review_handoff.handoff_recorded(comments, "2026-08-09T19:39:00Z") is True

    def test_ignores_markers_from_an_earlier_cycle(self):
        comments = [_comment("2026-08-09T05:43:00Z", "Changes requested (review loop 1/3)")]
        assert review_handoff.handoff_recorded(comments, "2026-08-09T19:39:00Z") is False

    def test_ignores_unrelated_chatter(self):
        comments = [_comment("2026-08-09T19:45:00Z", "Looks good to me, shipping later")]
        assert review_handoff.handoff_recorded(comments, "2026-08-09T19:39:00Z") is False

    @pytest.mark.parametrize(
        "summary",
        [
            "The change to the review loop counter is off by one.",
            "The acceptance criteria carry a spec ambiguity the PR cannot resolve.",
        ],
    )
    def test_structured_review_comment_is_never_a_footprint(self, summary):
        # The review run persists this comment seconds after its own verdict,
        # so it is always in scan scope -- and its free text routinely
        # discusses the review machinery itself. Counting it would stand the
        # backstop down while no handoff had happened at all.
        payload = json.dumps({"decision": "REQUEST_CHANGES", "summary": summary})
        comments = [
            _comment("2026-08-09T19:39:30Z", f"<!-- nyxgpt-structured-review: {payload} -->")
        ]
        assert review_handoff.handoff_recorded(comments, "2026-08-09T19:39:00Z") is False

    def test_real_handoff_still_counts_alongside_the_structured_comment(self):
        payload = json.dumps({"decision": "REQUEST_CHANGES", "summary": "unrelated prose"})
        comments = [
            _comment("2026-08-09T19:39:30Z", f"<!-- nyxgpt-structured-review: {payload} -->"),
            _comment("2026-08-09T19:41:00Z", "🔄 Changes requested (review loop 2/3)"),
        ]
        assert review_handoff.handoff_recorded(comments, "2026-08-09T19:39:00Z") is True


class TestDisagreementType:
    def test_prefers_latest_structured_comment(self):
        comments = [
            _comment(
                "2026-08-09T05:34:00Z",
                '<!-- nyxgpt-structured-review: {"decision":"REQUEST_CHANGES","disagreement_type":"a"} -->',
            ),
            _comment(
                "2026-08-09T19:40:00Z",
                '<!-- nyxgpt-structured-review: {"decision":"REQUEST_CHANGES","disagreement_type":"b"} -->',
            ),
        ]
        assert review_handoff.disagreement_type(comments) == "b"

    def test_falls_back_to_review_body_when_structured_output_lacks_it(self):
        # A dispatched run executing a pre-#3687 workflow definition posts a
        # structured comment with no disagreement_type at all.
        comments = [
            _comment(
                "2026-08-09T19:40:00Z",
                '<!-- nyxgpt-structured-review: {"decision":"REQUEST_CHANGES"} -->',
            ),
        ]
        body = (
            "### Disagreement Type\n\n**c**: the acceptance criteria are unresolvable as written\n"
        )
        assert review_handoff.disagreement_type(comments, body) == "c"

    def test_accepts_bracketed_body_classification(self):
        body = "**[b]**: the approach itself needs to change"
        assert review_handoff.disagreement_type([], body) == "b"

    def test_defaults_to_a_when_unclassified(self):
        assert review_handoff.disagreement_type([], "no classification here") == "a"

    def test_newest_structured_comment_without_a_type_does_not_inherit_the_previous(self):
        # An earlier cycle classified the round; the current one carries no
        # disagreement_type, so the current review body must win.
        comments = [
            _comment(
                "2026-08-09T05:34:00Z",
                '<!-- nyxgpt-structured-review: {"disagreement_type":"a"} -->',
            ),
            _comment(
                "2026-08-09T19:40:00Z",
                '<!-- nyxgpt-structured-review: {"decision":"REQUEST_CHANGES"} -->',
            ),
        ]
        body = "### Disagreement Type\n\n**b**: the approach itself is the disagreement\n"
        assert review_handoff.disagreement_type(comments, body) == "b"

    def test_structured_comments_are_ordered_by_created_at_not_api_order(self):
        comments = [
            _comment(
                "2026-08-09T19:40:00Z",
                '<!-- nyxgpt-structured-review: {"disagreement_type":"b"} -->',
            ),
            _comment(
                "2026-08-09T05:34:00Z",
                '<!-- nyxgpt-structured-review: {"disagreement_type":"a"} -->',
            ),
        ]
        assert review_handoff.disagreement_type(comments) == "b"

    def test_body_classification_is_read_below_the_heading(self):
        # A bold `**a**:`-shaped line elsewhere in the review must not
        # outrank the real classification under the template's heading.
        body = (
            "### Medium Issues\n\n"
            "**a**: this looks like a classification but is a findings label\n\n"
            "### Disagreement Type\n\n"
            "**c**: the acceptance criteria are unresolvable as written\n"
        )
        assert review_handoff.disagreement_type([], body) == "c"


class TestPlanHandoff:
    def test_no_verdict_is_a_no_op(self):
        plan = review_handoff.plan_handoff([], [], AGENT)
        assert plan == {"action": "none", "reason": "no-verdict"}

    def test_approve_is_left_to_the_merge_path(self):
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-09T05:33:00Z"),
            _review("APPROVED", "2026-08-09T19:39:00Z"),
        ]
        plan = review_handoff.plan_handoff(reviews, [], AGENT)
        assert plan == {"action": "none", "reason": "latest-verdict-approved"}

    def test_stands_down_when_event_chain_already_handled_it(self):
        reviews = [_review("CHANGES_REQUESTED", "2026-08-09T19:39:00Z")]
        comments = [_comment("2026-08-09T19:41:00Z", "Changes requested (review loop 2/3)")]
        plan = review_handoff.plan_handoff(reviews, comments, AGENT)
        assert plan == {"action": "none", "reason": "handoff-already-recorded"}

    def test_first_dropped_cycle_returns_to_developer(self):
        # The regression this issue is about: a dispatched REQUEST_CHANGES
        # with nothing following it must start the dev fix cycle.
        reviews = [_review("CHANGES_REQUESTED", "2026-08-09T05:33:00Z")]
        plan = review_handoff.plan_handoff(reviews, [], AGENT)
        assert plan["action"] == "return_to_developer"
        assert plan["route"] == "normal"
        assert plan["loop_number"] == 2
        assert plan["disagreement_type"] == "a"

    def test_second_verifiable_defect_cycle_huddles(self):
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-09T05:33:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-09T19:39:00Z"),
        ]
        plan = review_handoff.plan_handoff(reviews, [], AGENT)
        assert plan["action"] == "huddle"
        assert plan["request_changes_count"] == 2

    def test_judgment_call_huddles_on_the_first_cycle(self):
        reviews = [_review("CHANGES_REQUESTED", "2026-08-09T19:39:00Z")]
        comments = [
            _comment(
                "2026-08-09T19:40:00Z",
                '<!-- nyxgpt-structured-review: {"disagreement_type":"b"} -->',
            ),
        ]
        plan = review_handoff.plan_handoff(reviews, comments, AGENT)
        assert plan["action"] == "huddle"
        assert plan["disagreement_type"] == "b"

    def test_spec_ambiguity_escalates_at_cycle_zero(self):
        reviews = [_review("CHANGES_REQUESTED", "2026-08-09T19:39:00Z")]
        comments = [
            _comment(
                "2026-08-09T19:40:00Z",
                '<!-- nyxgpt-structured-review: {"disagreement_type":"c"} -->',
            ),
        ]
        plan = review_handoff.plan_handoff(reviews, comments, AGENT)
        assert plan["action"] == "escalate"
        assert plan["escalate_reason"] == "spec_ambiguity"

    def test_third_cycle_escalates_on_the_cycle_limit(self):
        reviews = [_review("CHANGES_REQUESTED", f"2026-08-09T0{n}:00:00Z") for n in range(1, 4)]
        plan = review_handoff.plan_handoff(reviews, [], AGENT)
        assert plan["action"] == "escalate"
        assert plan["escalate_reason"] == "cycle_limit"


class TestHuddleRaceRegressions:
    """The two incident shapes from #3736, as routing decisions.

    (i)  PR #3728, 2026-08-11 -- the huddle trigger fired twice for one
         round, spawning two developer-position runs and two mediations.
    (ii) PR #3733, 2026-08-12 -- a "proceed" huddle decision was followed
         six minutes later by the 3-cycle escalation, which parked the
         issue on the owner and stalled the pipeline.
    """

    def test_double_trigger_in_one_round_yields_a_single_huddle(self):
        # Incident (i): the second run of the same verdict must find the
        # first run's marker and do nothing -- no second HUDDLE_TRIGGERED.
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-11T17:00:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-11T18:05:00Z"),
        ]
        comments = [
            _structured("2026-08-11T18:06:00Z", "a", 100),
            _comment("2026-08-11T18:07:20Z", "🤝 Huddle triggered\n\nHUDDLE_TRIGGERED", 501),
        ]
        plan = review_handoff.plan_round(reviews, comments, AGENT)
        assert plan["action"] == "none"
        assert plan["route"] == "defer_huddle_pending"
        assert plan["huddle_pending"] is True

    def test_first_run_of_that_round_still_triggers_the_huddle(self):
        # Same round, before any trigger exists: the huddle must fire once.
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-11T17:00:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-11T18:05:00Z"),
        ]
        comments = [_structured("2026-08-11T18:06:00Z", "a", 100)]
        assert review_handoff.plan_round(reviews, comments, AGENT)["action"] == "huddle"

    def test_huddle_decision_suppresses_escalation_and_restarts_the_fix_cycle(self):
        # Incident (ii): three REQUEST_CHANGES rounds -- the raw count that
        # tripped the 3-cycle breaker -- but the huddle decided "proceed"
        # after them, so the counter is re-armed and the round hands back to
        # the developer instead of spending an owner turn.
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-12T18:00:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-12T18:30:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-12T18:55:00Z"),
        ]
        comments = [
            _structured("2026-08-12T18:56:00Z", "a", 100),
            _comment("2026-08-12T18:57:00Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T19:04:00Z", "## Huddle Decision\n\nHUDDLE_DECISION: proceed", 601),
        ]
        plan = review_handoff.plan_round(reviews, comments, AGENT)
        assert plan["action"] == "return_to_developer"
        assert plan["route"] == "normal"
        assert plan["request_changes_count"] == 3
        assert plan["effective_count"] == 0
        assert plan["huddle_decision"] == "proceed"

    def test_change_approach_decision_also_rearms_the_counter(self):
        reviews = [_review("CHANGES_REQUESTED", f"2026-08-12T1{n}:00:00Z") for n in range(1, 4)]
        comments = [
            _structured("2026-08-12T14:00:00Z", "a", 100),
            _comment("2026-08-12T14:01:00Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T14:10:00Z", "HUDDLE_DECISION: change-approach", 601),
        ]
        plan = review_handoff.plan_round(reviews, comments, AGENT)
        assert plan["action"] == "return_to_developer"
        assert plan["effective_count"] == 0

    def test_cycles_after_a_decision_count_again_toward_the_breaker(self):
        # Re-arming is not amnesty: three fresh failed rounds after the
        # huddle still escalate.
        reviews = [
            _review("CHANGES_REQUESTED", "2026-08-12T18:00:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-12T20:00:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-12T21:00:00Z"),
            _review("CHANGES_REQUESTED", "2026-08-12T22:00:00Z"),
        ]
        comments = [
            _comment("2026-08-12T18:10:00Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T19:04:00Z", "HUDDLE_DECISION: proceed", 601),
            _structured("2026-08-12T22:01:00Z", "a", 700),
        ]
        plan = review_handoff.plan_round(reviews, comments, AGENT)
        assert plan["action"] == "escalate"
        assert plan["escalate_reason"] == "cycle_limit"
        assert plan["effective_count"] == 3

    def test_a_decided_round_never_huddles_twice(self):
        # Type (b) huddles regardless of count -- but not about a question
        # this round already answered.
        reviews = [_review("CHANGES_REQUESTED", "2026-08-12T18:00:00Z")]
        comments = [
            _structured("2026-08-12T18:01:00Z", "b", 100),
            _comment("2026-08-12T18:02:00Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T18:30:00Z", "HUDDLE_DECISION: descope", 601),
        ]
        plan = review_handoff.plan_round(reviews, comments, AGENT)
        assert plan["action"] == "return_to_developer"

    def test_huddle_escalation_is_not_escalated_again(self):
        # The mediation run performs the escalation itself; the review path
        # must not add a second one.
        reviews = [_review("CHANGES_REQUESTED", f"2026-08-12T0{n}:00:00Z") for n in range(1, 4)]
        comments = [
            _structured("2026-08-12T04:00:00Z", "a", 100),
            _comment("2026-08-12T04:01:00Z", "HUDDLE_TRIGGERED", 501),
            _comment("2026-08-12T04:30:00Z", "HUDDLE_DECISION: escalate", 601),
        ]
        plan = review_handoff.plan_round(reviews, comments, AGENT)
        assert plan["action"] == "none"
        assert plan["route"] == "defer_huddle_escalated"

    def test_a_thread_with_no_huddle_routes_exactly_as_before(self):
        reviews = [_review("CHANGES_REQUESTED", f"2026-08-12T0{n}:00:00Z") for n in range(1, 4)]
        plan = review_handoff.plan_round(reviews, [], AGENT)
        assert plan["action"] == "escalate"
        assert plan["escalate_reason"] == "cycle_limit"
        assert plan["effective_count"] == plan["request_changes_count"] == 3


class TestCli:
    def test_plan_emits_eval_ready_key_value_lines(self, monkeypatch, capsys):
        payload = {
            "reviews": [_review("CHANGES_REQUESTED", "2026-08-09T05:33:00Z")],
            "comments": [],
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        assert review_handoff._main(["plan", AGENT]) == 0

        lines = capsys.readouterr().out.strip().splitlines()
        parsed = dict(line.split("=", 1) for line in lines)
        assert parsed["action"] == "return_to_developer"
        assert parsed["loop_number"] == "2"
        assert parsed["escalate_reason"] == ""
        # Every key must be present even when empty: the bash caller eval's
        # this output and would otherwise carry stale values between polls.
        assert set(parsed) == {
            "action",
            "reason",
            "route",
            "escalate_reason",
            "disagreement_type",
            "request_changes_count",
            "effective_count",
            "loop_number",
            "huddle_decision",
            "huddle_pending",
        }
        # Booleans render as true/false: the primary workflow writes these
        # straight into $GITHUB_OUTPUT, where step conditions compare them.
        assert parsed["huddle_pending"] == "false"

    def test_plan_round_is_callable_from_the_primary_workflow(self, monkeypatch, capsys):
        payload = {
            "reviews": [_review("CHANGES_REQUESTED", "2026-08-12T18:00:00Z")],
            "comments": [
                _structured("2026-08-12T18:01:00Z", "a", 100),
                _comment("2026-08-12T18:02:00Z", "HUDDLE_TRIGGERED", 501),
            ],
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        assert review_handoff._main(["plan-round", AGENT]) == 0

        parsed = dict(line.split("=", 1) for line in capsys.readouterr().out.strip().splitlines())
        assert parsed["action"] == "none"
        assert parsed["huddle_pending"] == "true"

    def test_handoff_recorded_reports_the_huddle_trigger(self, monkeypatch, capsys):
        # The fallback trigger path's dedupe: a huddle trigger IS a handoff
        # footprint, which it used to miss entirely (#3736).
        payload = {
            "reviews": [],
            "comments": [_comment("2026-08-11T18:07:20Z", "HUDDLE_TRIGGERED", 501)],
        }
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

        assert review_handoff._main(["handoff-recorded", "2026-08-11T18:05:00Z"]) == 0
        assert capsys.readouterr().out.strip() == "true"

    def test_bad_usage_exits_two(self, capsys):
        assert review_handoff._main([]) == 2
        assert "usage:" in capsys.readouterr().err
        assert review_handoff._main(["nope", AGENT]) == 2


class TestBackstopScript:
    """Runs the bash half (`review_ensure_handoff.sh`) end-to-end.

    The shell suite stubs `gh` and runs the script in dry-run mode, so this
    exercises the real wait/poll/plan loop without touching GitHub. Wired
    into pytest because `pytest -v` is the gate this repo actually runs;
    the standalone `bash tests/test_review_ensure_handoff.sh` entry point
    still works for local debugging.
    """

    def test_shell_suite_passes(self):
        suite = Path(__file__).resolve().parents[1] / "test_review_ensure_handoff.sh"
        result = subprocess.run(
            ["bash", str(suite)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
