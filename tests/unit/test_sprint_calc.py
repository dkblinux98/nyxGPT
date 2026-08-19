"""Unit tests for scripts/agents/lib/sprint_calc.py (#3480 sprint autopilot).

Pure computation only -- no gh/GraphQL involved -- covering the pieces the
issue explicitly calls out as needing tests: the autopilot stop condition,
verdict/velocity projection, and reorg proposal generation given mocked
project data.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib" / "sprint_calc.py"
_spec = importlib.util.spec_from_file_location("sprint_calc", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
sprint_calc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sprint_calc
_spec.loader.exec_module(sprint_calc)


class TestAutopilotDecision:
    def test_continues_while_backlog_remains(self):
        assert sprint_calc.autopilot_decision(3) == "continue"
        assert sprint_calc.autopilot_decision(1) == "continue"

    def test_completes_once_backlog_is_empty(self):
        assert sprint_calc.autopilot_decision(0) == "complete"


class TestSprintVelocity:
    def test_normal_case(self):
        assert sprint_calc.sprint_velocity(done_count=6, elapsed_days=3) == 2.0

    def test_guards_against_zero_elapsed_days(self):
        # Day 0 of the sprint: still returns a rate instead of dividing by zero.
        assert sprint_calc.sprint_velocity(done_count=1, elapsed_days=0) == 1.0

    def test_guards_against_negative_elapsed_days(self):
        assert sprint_calc.sprint_velocity(done_count=5, elapsed_days=-2) == 5.0


class TestSprintVerdict:
    def test_no_remaining_work_is_always_on_track(self):
        assert sprint_calc.sprint_verdict(remaining=0, velocity=0, days_left=0) == "on-track"

    def test_unknown_when_no_end_date_configured(self):
        assert sprint_calc.sprint_verdict(remaining=5, velocity=1, days_left=None) == "unknown"

    def test_on_track_when_projection_fits(self):
        # 4 remaining / 2 per day = 2 days needed, 5 days left.
        assert sprint_calc.sprint_verdict(remaining=4, velocity=2, days_left=5) == "on-track"

    def test_at_risk_within_twenty_percent_buffer(self):
        # 6 remaining / 1 per day = 6 days needed, 5 days left (20% over).
        assert sprint_calc.sprint_verdict(remaining=6, velocity=1, days_left=5) == "at-risk"

    def test_off_track_beyond_buffer(self):
        # 10 remaining / 1 per day = 10 days needed, 5 days left (100% over).
        assert sprint_calc.sprint_verdict(remaining=10, velocity=1, days_left=5) == "off-track"

    def test_off_track_when_velocity_is_zero_and_work_remains(self):
        assert sprint_calc.sprint_verdict(remaining=3, velocity=0, days_left=2) == "off-track"


class TestReorgTargetCount:
    def test_no_excess_needs_no_reorg(self):
        assert sprint_calc.reorg_target_count(remaining=2, velocity=2, days_left=5) == 0

    def test_computes_excess_issues_to_move(self):
        # 10 remaining / 1 per day = 10 days needed, 5 left -> 5 excess days
        # * 1/day velocity = 5 issues over budget.
        assert sprint_calc.reorg_target_count(remaining=10, velocity=1, days_left=5) == 5

    def test_zero_velocity_proposes_clearing_everything_remaining(self):
        assert sprint_calc.reorg_target_count(remaining=4, velocity=0, days_left=2) == 4

    def test_none_days_left_proposes_nothing(self):
        assert sprint_calc.reorg_target_count(remaining=10, velocity=1, days_left=None) == 0


class TestSelectReorgCandidates:
    ISSUES = [
        {"number": 101, "priority": "P0 - Critical"},
        {"number": 102, "priority": "P3 - Low"},
        {"number": 103, "priority": "P1 - High"},
        {"number": 200, "priority": "P3 - Low"},
        {"number": 150, "priority": None},
    ]

    def test_lowest_priority_selected_first(self):
        result = sprint_calc.select_reorg_candidates(self.ISSUES, 1)
        # Two P3s tie on priority; the higher (less-likely-started) number wins.
        assert [i["number"] for i in result] == [200]

    def test_missing_priority_ranks_below_everything(self):
        result = sprint_calc.select_reorg_candidates(self.ISSUES, 2)
        assert [i["number"] for i in result] == [200, 102]

    def test_zero_count_returns_nothing(self):
        assert sprint_calc.select_reorg_candidates(self.ISSUES, 0) == []

    def test_count_larger_than_pool_returns_everything_ordered(self):
        result = sprint_calc.select_reorg_candidates(self.ISSUES, 99)
        assert [i["number"] for i in result] == [200, 102, 150, 103, 101]


class TestBuildSprintReport:
    def _payload(self, **overrides):
        payload = {
            "sprint_title": "Sprint 6",
            "start_date": "2026-07-27",
            "end_date": "2026-08-03",
            "today": "2026-07-31",
            "counts": {"backlog": 2, "in_progress": 1, "in_review": 0, "done": 3},
            "backlog_issues": [
                {"number": 10, "priority": "P3 - Low"},
                {"number": 11, "priority": "P0 - Critical"},
            ],
            "blockers": [],
        }
        payload.update(overrides)
        return payload

    def test_on_track_report_has_no_proposal(self):
        # 4 days elapsed, done=3 -> velocity 0.75/day; remaining=3 -> ~4 days
        # needed vs 3 days left (33% over, past the 20% buffer) -- so this
        # particular fixture is actually off-track; use an easy on-track one.
        result = sprint_calc.build_sprint_report(
            self._payload(counts={"backlog": 0, "in_progress": 0, "in_review": 0, "done": 5})
        )
        assert result["verdict"] == "on-track"
        assert result["proposal"] is None
        assert "SPRINT_REORG_PROPOSAL" not in result["markdown"]

    def test_off_track_report_includes_proposal_and_marker(self):
        result = sprint_calc.build_sprint_report(
            self._payload(counts={"backlog": 10, "in_progress": 0, "in_review": 0, "done": 1})
        )
        assert result["verdict"] == "off-track"
        assert result["proposal"] is not None
        assert result["proposal"]["action"] == "move_out"
        assert "SPRINT_REORG_PROPOSAL" in result["markdown"]
        assert "APPROVE_SPRINT_REORG" in result["markdown"]

    def test_off_track_with_no_backlog_issues_has_no_proposal(self):
        result = sprint_calc.build_sprint_report(
            self._payload(
                counts={"backlog": 0, "in_progress": 10, "in_review": 0, "done": 0},
                backlog_issues=[],
            )
        )
        assert result["verdict"] == "off-track"
        assert result["proposal"] is None
        assert "no eligible Backlog issues" in result["markdown"]

    def test_blockers_render_in_markdown(self):
        result = sprint_calc.build_sprint_report(
            self._payload(
                counts={"backlog": 0, "in_progress": 0, "in_review": 1, "done": 5},
                blockers=[{"pr": 42, "issue": 99, "changes_requested": 2}],
            )
        )
        assert "PR #42" in result["markdown"]
        assert "2 change-request cycle(s)" in result["markdown"]

    def test_no_active_backlog_reports_none_detected(self):
        result = sprint_calc.build_sprint_report(
            self._payload(counts={"backlog": 0, "in_progress": 0, "in_review": 0, "done": 5})
        )
        assert "Blockers:** none detected" in result["markdown"]


class TestHuddleRoutingDecision:
    """#3687 review huddle protocol: routing from (disagreement_type,
    REQUEST_CHANGES count) to what review_agent_auto_review.yml does next.
    """

    def test_type_c_escalates_immediately_regardless_of_count(self):
        assert sprint_calc.huddle_routing_decision("c", 1) == "escalate_spec_ambiguity"
        # Cycle zero: even a first-ever REQUEST_CHANGES of type (c) escalates,
        # no agent conversation can resolve what only the PM knows.
        assert sprint_calc.huddle_routing_decision("c", 3) == "escalate_spec_ambiguity"

    def test_type_b_huddles_immediately_regardless_of_count(self):
        assert sprint_calc.huddle_routing_decision("b", 1) == "huddle"
        assert sprint_calc.huddle_routing_decision("b", 2) == "huddle"

    def test_type_a_first_cycle_is_normal(self):
        assert sprint_calc.huddle_routing_decision("a", 1) == "normal"

    def test_type_a_second_cycle_huddles_instead_of_a_blind_third_retry(self):
        assert sprint_calc.huddle_routing_decision("a", 2) == "huddle"

    def test_type_a_third_cycle_hits_the_unchanged_outer_breaker(self):
        assert sprint_calc.huddle_routing_decision("a", 3) == "escalate_cycle_limit"
        assert sprint_calc.huddle_routing_decision("a", 4) == "escalate_cycle_limit"

    def test_unrecognized_type_degrades_to_type_a_behavior(self):
        # A missing/malformed classification must never block the existing
        # loop -- it degrades to the pre-#3687 default instead of failing
        # closed (stuck) or open (skipping straight to escalation).
        assert sprint_calc.huddle_routing_decision("", 1) == "normal"
        assert sprint_calc.huddle_routing_decision("unknown", 2) == "huddle"
        assert sprint_calc.huddle_routing_decision("unknown", 3) == "escalate_cycle_limit"

    def test_cli_huddle_routing(self):
        import subprocess

        result = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "huddle-routing", "b", "1"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.strip() == "huddle"


class TestSprintParkNote:
    """#3706: when the active sprint's pool drains the autopilot parks with a
    loud note -- sprint complete, what remains per future sprint, and how
    work resumes."""

    def _note(self, **overrides):
        payload = {
            "event_phrase": "Issue #3706 merged",
            "sprint_title": "Sprint 8",
            "release_version": "v3.0.0",
            "by_sprint": {"Sprint 8": 0, "Sprint 9": 11, "Sprint 10": 2, "": 3},
        }
        payload.update(overrides)
        return sprint_calc.build_sprint_park_note(payload)

    def test_states_the_sprint_is_complete_and_the_event(self):
        note = self._note()
        assert "Issue #3706 merged" in note
        assert "Sprint 8" in note
        assert "no open Backlog issues left" in note
        assert "parked at the sprint boundary" in note

    def test_lists_remaining_release_work_per_future_sprint(self):
        note = self._note()
        assert "- Sprint 9: 11 open Backlog issue(s)" in note
        assert "- Sprint 10: 2 open Backlog issue(s)" in note
        assert "- _No sprint set_: 3 open Backlog issue(s)" in note
        assert "**16**" in note  # 11 + 2 + 3
        # Numeric sprint ordering: "Sprint 9" precedes "Sprint 10".
        assert note.index("Sprint 9:") < note.index("Sprint 10:")
        # The no-sprint bucket sorts last.
        assert note.index("Sprint 10:") < note.index("_No sprint set_")

    def test_active_sprint_is_not_listed_as_waiting_work(self):
        note = self._note(by_sprint={"Sprint 8": 4, "Sprint 9": 1})
        # A nonzero active-sprint bucket is a data race at worst; it must
        # never be reported as work waiting behind the boundary.
        assert "Sprint 8: 4" not in note
        assert "- Sprint 9: 1 open Backlog issue(s)" in note
        assert "**1**" in note

    def test_explains_how_work_resumes_and_the_owner_override(self):
        note = self._note()
        assert "SPRINT_TIMEZONE" in note
        # The owner override is the assignment lever (#3882), not a comment
        # kick: the note must name the actual action and still point at the
        # doc that explains it.
        assert "assigns the developer agent to an issue" in note
        assert "`docs/sprint-autopilot.md`" in note
        assert "#3706" in note

    def test_never_contains_the_dispatch_kick_token(self):
        # The kick token is deleted (#3882) and the kick is a
        # repository_dispatch, so no comment can start work. This assertion
        # stays as the regression pin for #3706, where the park note named
        # the then-live token and a bare substring test in the dispatch
        # trigger turned every "park" into a kick. Cover every rendered
        # variant.
        variants = [
            self._note(),
            self._note(sprint_title=""),
            self._note(by_sprint={"Sprint 8": 0}),
            self._note(release_version="", by_sprint={"Sprint 9": 2}),
        ]
        for note in variants:
            assert "READY_FOR_NEXT_ISSUE" not in note
            # Structural backstop: the marker the shared comment-token gate
            # treats as disqualifying, so the note stays inert for the
            # tokens that ARE still live even if the prose drifts.
            assert sprint_calc.AUTOPILOT_INFO_MARKER in note

    def test_note_without_a_park_state_claims_no_completion(self):
        # #3709: a note rendered without population data must not announce
        # any completion state -- the pre-#3709 note said "sprint complete"
        # off a Backlog-only count, which was the defect.
        note = self._note()
        assert "sprint complete" not in note.lower()
        assert "parked at the sprint boundary" in note

    def test_release_drained_variant_when_nothing_is_waiting(self):
        note = self._note(by_sprint={"Sprint 8": 0})
        assert "release backlog is drained" in note
        assert "RELEASE_ISSUE_NUMBER" in note
        assert "open Backlog issue(s)" not in note

    def test_no_active_sprint_variant(self):
        note = self._note(sprint_title="")
        assert "No sprint iteration is currently active" in note
        assert "- Sprint 9: 11 open Backlog issue(s)" in note
        # With no active sprint the "" key is the *no sprint set* bucket,
        # not the active sprint. Excluding it here hid 3 waiting issues and
        # reported a total of 13 instead of 16 (#3706 review).
        assert "- _No sprint set_: 3 open Backlog issue(s)" in note
        assert "**16**" in note  # 11 + 2 + 3

    def test_missing_release_version_degrades_gracefully(self):
        note = self._note(release_version="", by_sprint={"Sprint 9": 2})
        assert "this release" in note
        assert "- Sprint 9: 2 open Backlog issue(s)" in note


class TestSprintParkState:
    """#3709: the park decision counts the sprint's WHOLE open population,
    and completion is two states -- agentic work complete (awaiting the
    owner's acceptance) and sprint complete (everything in For Release)."""

    def _state(self, open_by_status=None, closed_by_status=None):
        return sprint_calc.sprint_park_state(
            {
                "open": open_by_status or {},
                "closed": closed_by_status or {},
                "status_backlog": "Backlog",
                "status_for_release": "For Release",
            }
        )

    def test_open_backlog_work_continues(self):
        result = self._state({"Backlog": [10, 11], "In Progress": [12]})
        assert result["state"] == sprint_calc.PARK_CONTINUE
        assert result["backlog_open"] == 2

    def test_empty_backlog_with_in_flight_work_is_not_complete(self):
        # The reported defect: Backlog empty but In Progress / In Review
        # work still live got a "sprint complete -- acceptance next" note.
        result = self._state({"In Progress": [3513], "In Review": [3514]})
        assert result["state"] == sprint_calc.PARK_WORK_IN_FLIGHT
        assert result["open_total"] == 2
        assert result["open_by_status"] == {"In Progress": 1, "In Review": 1}

    def test_issue_with_no_status_still_counts_as_open_work(self):
        result = self._state({"": [4242]})
        assert result["state"] == sprint_calc.PARK_WORK_IN_FLIGHT
        assert result["open_total"] == 1

    def test_all_closed_but_not_accepted_is_awaiting_acceptance(self):
        result = self._state(
            closed_by_status={"Acceptance Testing": [3510, 3509], "For Release": [3508]}
        )
        assert result["state"] == sprint_calc.PARK_AWAITING_ACCEPTANCE
        assert result["awaiting_acceptance"] == [3509, 3510]
        assert result["accepted"] == [3508]

    def test_sprint_complete_only_when_every_item_is_for_release(self):
        # Owner definition (2026-08-10): "The sprint isn't done until all
        # agentic work is complete AND in For Release status."
        result = self._state(closed_by_status={"For Release": [3508, 3509]})
        assert result["state"] == sprint_calc.PARK_SPRINT_COMPLETE
        assert result["accepted"] == [3508, 3509]

    def test_open_work_outranks_accepted_items(self):
        result = self._state({"In Progress": [3513]}, {"For Release": [3508]})
        assert result["state"] == sprint_calc.PARK_WORK_IN_FLIGHT

    def test_sprint_with_no_items_at_all(self):
        assert self._state()["state"] == sprint_calc.PARK_EMPTY

    def test_custom_status_names_are_honored(self):
        result = sprint_calc.sprint_park_state(
            {
                "open": {},
                "closed": {"Shipped": [1]},
                "status_backlog": "Backlog",
                "status_for_release": "Shipped",
            }
        )
        assert result["state"] == sprint_calc.PARK_SPRINT_COMPLETE


class TestParkNoteStates:
    """The rendered park note per #3709 state, including the parked-issue
    scan lines that keep gate-stuck work visible."""

    def _note(self, park_state, resume_scan=None, sprint_title="Sprint 8"):
        return sprint_calc.build_sprint_park_note(
            {
                "event_phrase": "Issue #3510 merged",
                "sprint_title": sprint_title,
                "release_version": "v3.0.0",
                "by_sprint": {"Sprint 8": 0},
                "park_state": park_state,
                "resume_scan": resume_scan or {},
            }
        )

    def test_work_in_flight_note_denies_completion(self):
        note = self._note(
            {
                "state": sprint_calc.PARK_WORK_IN_FLIGHT,
                "open_total": 3,
                "open_by_status": {"In Progress": 2, "In Review": 1},
            }
        )
        assert "work still in flight" in note
        assert "3 issue(s) are still open" in note
        assert "In Progress: 2" in note and "In Review: 1" in note
        assert "not** sprint completion" in note
        assert "READY_FOR_NEXT_ISSUE" not in note

    def test_awaiting_acceptance_note_is_not_sprint_complete(self):
        note = self._note(
            {
                "state": sprint_calc.PARK_AWAITING_ACCEPTANCE,
                "awaiting_acceptance": [3509, 3510],
                "accepted": [],
            }
        )
        assert "agentic work complete; awaiting owner acceptance" in note
        assert '**This is not "sprint complete."**' in note
        assert "#3509, #3510" in note
        assert "For Release" in note
        assert "agents never self-promote" in note

    def test_sprint_complete_note_requires_for_release(self):
        note = self._note(
            {
                "state": sprint_calc.PARK_SPRINT_COMPLETE,
                "accepted": [3509, 3510],
                "awaiting_acceptance": [],
            }
        )
        assert "sprint complete" in note
        assert "accepted by the owner" in note
        assert "#3509, #3510" in note

    def test_empty_sprint_note(self):
        note = self._note({"state": sprint_calc.PARK_EMPTY})
        assert "no items at all" in note

    def test_gate_scan_lines_are_included(self):
        note = self._note(
            {
                "state": sprint_calc.PARK_WORK_IN_FLIGHT,
                "open_total": 2,
                "open_by_status": {"In Progress": 2},
            },
            resume_scan={
                "resumable": [],
                "waiting": [{"issue": 3516, "open_blockers": [3514]}],
                "exhausted": [],
                "active": [],
                "selected": 3513,
            },
        )
        assert "Parked-issue scan" in note
        assert "Auto-resumed:** #3513" in note
        assert "#3516 (waiting on #3514)" in note

    def test_every_state_stays_undispatchable(self):
        for state in (
            sprint_calc.PARK_WORK_IN_FLIGHT,
            sprint_calc.PARK_AWAITING_ACCEPTANCE,
            sprint_calc.PARK_SPRINT_COMPLETE,
            sprint_calc.PARK_EMPTY,
        ):
            note = self._note({"state": state, "open_by_status": {}, "open_total": 0})
            assert "READY_FOR_NEXT_ISSUE" not in note
            assert sprint_calc.AUTOPILOT_INFO_MARKER in note

    def test_in_flight_note_explains_the_loop_drives_itself(self):
        note = self._note(
            {
                "state": sprint_calc.PARK_WORK_IN_FLIGHT,
                "open_total": 1,
                "open_by_status": {"In Progress": 1},
            }
        )
        assert "the in-flight issues above finish" in note
        # Boundary states do not claim that -- there is nothing in flight.
        assert "the in-flight issues above finish" not in self._note(
            {"state": sprint_calc.PARK_SPRINT_COMPLETE, "accepted": [1]}
        )


class TestReleaseCandidateSection:
    """The park note's release-candidate section (#3729).

    When the sprint reaches agentic-work-complete the autopilot cuts the
    candidate that acceptance round installs, and the note has to answer the
    owner's only question at that moment -- "what do I install?" -- with a
    version, not a link to a run nobody has read yet.
    """

    def _note(self, rc_publish, state=None):
        return sprint_calc.build_sprint_park_note(
            {
                "event_phrase": "Issue #3510 merged",
                "sprint_title": "Sprint 8",
                "release_version": "v3.0.0",
                "by_sprint": {"Sprint 8": 0},
                "park_state": {
                    "state": state or sprint_calc.PARK_AWAITING_ACCEPTANCE,
                    "awaiting_acceptance": [3510, 3513],
                },
                "resume_scan": {},
                "rc_publish": rc_publish,
            }
        )

    def test_dispatched_candidate_is_named_with_its_install_commands(self):
        note = self._note(
            {
                "dispatched": True,
                "channel": "rc",
                "branch": "v3.0.0",
                "version": "3.0.0rc2",
                "runs_url": "https://github.com/dkblinux98/nyxGPT/actions/x",
            }
        )
        assert "`3.0.0rc2` is publishing now from `v3.0.0`" in note
        assert "pip install nyxgpt==3.0.0rc2" in note
        assert "brew install nyxgpt-api@3.0.0rc nyxgpt-web@3.0.0rc" in note
        # The note is the round's copy-paste install line, so it has to clear
        # Homebrew's third-party tap gate as written (#3752) -- and tolerate a
        # Homebrew that has neither spelling of the trust subcommand
        # (#3770 -- `tap-trust` on some builds, `trust` on others).
        assert "brew tap dkblinux98/nyxgpt" in note
        assert (
            "(brew tap-trust dkblinux98/nyxgpt || brew trust dkblinux98/nyxgpt || true)"
        ) in note
        assert note.index("tap-trust") < note.index("brew install nyxgpt-api@")
        assert "https://github.com/dkblinux98/nyxGPT/actions/x" in note

    def test_a_dispatch_with_no_preflight_version_still_reports_the_publish(self):
        """The preflight is best-effort; the run resolves the number either way."""
        note = self._note({"dispatched": True, "channel": "rc", "branch": "v3.0.0"})
        assert "is publishing now from `v3.0.0`" in note
        assert "resolved by the run" in note
        # No version to pin, so no install block that would name the wrong one.
        assert "pip install nyxgpt==" not in note

    def test_a_noop_names_the_candidate_the_round_should_install(self):
        note = self._note(
            {
                "dispatched": False,
                "noop": True,
                "channel": "rc",
                "branch": "v3.0.0",
                "version": "3.0.0rc1",
                "reason": "v3.0.0 is unchanged since the last published candidate",
            }
        )
        assert "no new candidate was cut" in note
        assert "`3.0.0rc1`" in note
        assert "pip install nyxgpt==3.0.0rc1" in note

    def test_a_failed_dispatch_says_so_and_names_the_manual_command(self):
        note = self._note(
            {
                "dispatched": False,
                "error": True,
                "channel": "rc",
                "reason": "GitHub refused the workflow dispatch",
            }
        )
        assert "could not be dispatched" in note
        assert "GitHub refused the workflow dispatch" in note
        assert "nyxgpt release publish --channel rc --publish" in note

    def test_the_section_is_absent_when_no_candidate_was_published(self):
        """Every park state but agentic-work-complete publishes nothing."""
        note = self._note({}, state=sprint_calc.PARK_WORK_IN_FLIGHT)
        assert "Release candidate" not in note
        assert "pip install" not in note

    def test_the_note_never_calls_a_candidate_a_release(self):
        note = self._note({"dispatched": True, "version": "3.0.0rc2", "branch": "v3.0.0"})
        assert "A candidate is **not** a release" in note
        # ...and it still cannot dispatch work or claim sprint completion.
        assert "READY_FOR_NEXT_ISSUE" not in note
        assert sprint_calc.AUTOPILOT_INFO_MARKER in note

    def test_the_candidate_comes_before_the_backlog_accounting(self):
        """The owner's next action is installing it, so it leads the note."""
        note = self._note({"dispatched": True, "version": "3.0.0rc2", "branch": "v3.0.0"})
        assert note.index("Release candidate") < note.index("Work resumes when either")
