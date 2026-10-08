"""The ceremony's Phase 0 derivations and gate classification (#4166).

Phase 0 is now the only place a release prerequisite is discovered, so its
arithmetic is what stands between a signed-off release and a ceremony that
dies after the tag is public. Every test here is written so that a plausible
wrong implementation FAILS it:

* `next_patch_version` bumping the wrong component;
* a placeholder milestone title Phase 4's own derivation would not match --
  the single thing that would make the placeholder useless;
* `pick_next_line` taking the highest rather than the lowest candidate, or
  accepting a version at or below the release;
* `next_sprint_plan` restarting the numbering, inventing a duration, or
  starting the new sprint in the past;
* `iteration_resubmit` dropping the ids or the completed iterations -- the two
  mutations proven on a throwaway project to WIPE item values
  (`scripts/sprint-iteration-preservation-proof.sh`);
* `decide` stopping at the first gap instead of reporting all of them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "agents" / "lib"
sys.path.insert(0, str(LIB))

from release_prereqs import (  # noqa: E402
    GATE,
    PROVISION,
    decide,
    is_placeholder_title,
    iteration_resubmit,
    next_patch_version,
    next_release_title,
    next_sprint_plan,
    pick_next_line,
    placeholder_milestone_title,
)

# ---------------------------------------------------------------------------
# version / milestone derivations
# ---------------------------------------------------------------------------


class TestNextPatchVersion:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("3.0.1", "3.0.2"),
            ("3.0.0", "3.0.1"),
            ("2.1.9", "2.1.10"),
            ("10.4.0", "10.4.1"),
            ("v3.0.1", "3.0.2"),
        ],
    )
    def test_it_bumps_the_patch_component_only(self, version, expected):
        assert next_patch_version(version) == expected

    @pytest.mark.parametrize("bad", ["3.0", "3.0.1rc1", "", "next", "3.0.1.1"])
    def test_a_non_release_version_is_refused_rather_than_guessed(self, bad):
        with pytest.raises(ValueError):
            next_patch_version(bad)


class TestPlaceholderMilestone:
    def test_the_title_names_the_next_patch_line(self):
        assert placeholder_milestone_title("3.0.1") == "Placeholder — next line (v3.0.2)"

    def test_phase_4s_own_derivation_picks_the_placeholder_up(self):
        """The whole point of the title: if Phase 4 cannot parse it, Phase 0
        has created a milestone that changes nothing and the ceremony still
        dies at the line gate."""
        title = placeholder_milestone_title("3.0.1")
        assert pick_next_line([title], "3.0.1") == ("v3.0.2", title)

    def test_a_placeholder_is_recognisable_as_one(self):
        assert is_placeholder_title(placeholder_milestone_title("3.0.1"))
        assert not is_placeholder_title("Phase 7 — nyxAgent (v3.1.0)")


class TestPickNextLine:
    OPEN = [
        "Phase 6.5 — Post-Release Patches (v3.0.1)",
        "Phase 8 — Later (v3.2.0)",
        "Phase 7 — nyxAgent (v3.1.0)",
        "Backlog (no version)",
    ]

    def test_it_takes_the_lowest_version_above_the_release(self):
        assert pick_next_line(self.OPEN, "3.0.1") == ("v3.1.0", "Phase 7 — nyxAgent (v3.1.0)")

    def test_the_release_s_own_milestone_is_not_the_next_line(self):
        """`> current`, not `>=`: picking v3.0.1 while releasing 3.0.1 would
        make Phase 4 cut the next line from the branch it just froze."""
        assert pick_next_line(["Phase 6.5 — Post-Release Patches (v3.0.1)"], "3.0.1") is None

    def test_a_lower_version_is_not_the_next_line(self):
        assert pick_next_line(["Phase 5 (v2.1.0)"], "3.0.1") is None

    def test_no_candidate_is_reported_as_none_not_guessed(self):
        assert pick_next_line(["Backlog", "Phase X: Rejected"], "3.0.1") is None

    def test_version_components_compare_numerically_not_as_strings(self):
        """'v3.0.10' < 'v3.0.9' as strings, and that would cut the wrong line."""
        branch, _ = pick_next_line(["a (v3.0.10)", "b (v3.0.9)"], "3.0.1")
        assert branch == "v3.0.9"


class TestNextReleaseTitle:
    @pytest.mark.parametrize(
        ("milestone", "expected"),
        [
            ("Phase 7 — nyxAgent (v3.1.0)", "nyxAgent"),
            ("Phase 6.5 — Post-Release Patches (v3.0.1)", "Post-Release Patches"),
            ("Placeholder — next line (v3.0.2)", "Placeholder — next line"),
            ("No version here", "No version here"),
        ],
    )
    def test_it_strips_the_phase_prefix_and_the_version_suffix(self, milestone, expected):
        assert next_release_title(milestone) == expected


# ---------------------------------------------------------------------------
# the sprint iteration -- the mutation with the 1018-item blast radius
# ---------------------------------------------------------------------------
TODAY = date(2026, 10, 8)


def _config(active=(), completed=(), duration=18):
    return {
        "duration": duration,
        "startDay": 1,
        "iterations": list(active),
        "completedIterations": list(completed),
    }


def _iter(ident, title, start, duration=18):
    return {"id": ident, "title": title, "startDate": start, "duration": duration}


class TestNextSprintPlanDecision:
    def test_an_active_iteration_means_nothing_is_created(self):
        plan = next_sprint_plan(_config(active=[_iter("a", "Sprint 10", "2026-10-05")]), TODAY)
        assert plan["needed"] is False
        assert plan["existing"] == ["Sprint 10"]
        assert "iterations" not in plan, "nothing may be resubmitted when nothing is needed"

    def test_only_completed_iterations_means_one_is_created(self):
        plan = next_sprint_plan(_config(completed=[_iter("a", "Sprint 9", "2026-09-01")]), TODAY)
        assert plan["needed"] is True
        assert plan["provisionable"] is True

    def test_a_field_with_no_default_duration_is_gate_only_not_guessed(self):
        """Inventing a cadence would silently reshape the owner's sprints, so
        this is the one sprint case Phase 0 reports instead of provisioning."""
        plan = next_sprint_plan(_config(completed=[_iter("a", "Sprint 9", "2026-09-01")], duration=0), TODAY)
        assert plan["needed"] is True
        assert plan["provisionable"] is False
        assert "new" not in plan


class TestNextSprintPlanDerivations:
    CONFIG = _config(
        completed=[
            _iter("i1", "Sprint 8", "2026-08-09"),
            _iter("i2", "Sprint 9", "2026-08-27"),
            _iter("i3", "Sprint 10", "2026-09-14"),
        ]
    )

    def test_the_title_continues_the_existing_numbering(self):
        assert next_sprint_plan(self.CONFIG, TODAY)["new"]["title"] == "Sprint 11"

    def test_the_numbering_follows_the_highest_number_not_the_count(self):
        """Three iterations numbered 8, 9, 10 must yield 11, not 4 -- a
        count-based implementation passes a fresh project and collides on
        every real one."""
        plan = next_sprint_plan(self.CONFIG, TODAY)
        assert plan["new"]["title"] == "Sprint 11"

    def test_an_unnumbered_history_starts_at_one(self):
        plan = next_sprint_plan(_config(completed=[_iter("i1", "Kickoff", "2026-08-09")]), TODAY)
        assert plan["new"]["title"] == "Sprint 1"

    def test_the_duration_is_the_field_s_own_default(self):
        plan = next_sprint_plan(_config(completed=[_iter("i1", "Sprint 1", "2026-08-09", 7)], duration=7), TODAY)
        assert plan["new"]["duration"] == 7

    def test_it_starts_today_when_the_last_iteration_has_already_ended(self):
        """Sprint 10 ran 2026-09-14 + 18 days = ended 2026-10-02, before
        today. Starting the new one then would create a sprint that is
        already partly over."""
        assert next_sprint_plan(self.CONFIG, TODAY)["new"]["startDate"] == "2026-10-08"

    def test_it_starts_the_day_after_the_last_one_ends_when_that_is_future(self):
        """GitHub lays iterations out contiguously: start + duration is both
        the end of one and the start of the next (verified on the scratch
        project). A future end date must not be overlapped."""
        config = _config(completed=[_iter("i1", "Sprint 1", "2026-10-06", 18)])
        # 2026-10-06 + 18 = 2026-10-24
        assert next_sprint_plan(config, TODAY)["new"]["startDate"] == "2026-10-24"

    def test_the_config_anchor_keeps_the_earliest_existing_start_date(self):
        """Re-anchoring the field would reshape the owner's cadence."""
        assert next_sprint_plan(self.CONFIG, TODAY)["startDate"] == "2026-08-09"


class TestIterationResubmitPayload:
    """The payload that must not wipe the board.

    Proven on a throwaway project 2026-10-08
    (`scripts/sprint-iteration-preservation-proof.sh`): omitting the ids wipes
    every item value, and omitting the completed iterations wipes exactly the
    items in them. These tests fail any implementation that does either.
    """

    CONFIG = _config(
        completed=[_iter("c1", "Sprint 8", "2026-08-09"), _iter("c2", "Sprint 9", "2026-08-27")],
        active=[_iter("a1", "Sprint 10", "2026-10-06")],
    )

    def test_every_existing_iteration_keeps_its_id(self):
        payload = iteration_resubmit(self.CONFIG, [{"title": "Sprint 11"}])
        existing = [it for it in payload if "id" in it]
        assert [it["id"] for it in existing] == ["c1", "c2", "a1"]

    def test_completed_iterations_are_resubmitted_too(self):
        """`configuration { iterations }` does NOT return completed iterations,
        so the obvious implementation -- read, append, resubmit -- drops them
        and clears the Sprint value on every item assigned to one."""
        titles = [it["title"] for it in iteration_resubmit(self.CONFIG, [])]
        assert "Sprint 8" in titles and "Sprint 9" in titles

    def test_the_new_iteration_comes_last_and_carries_no_id(self):
        payload = iteration_resubmit(self.CONFIG, [{"title": "Sprint 11", "startDate": "2026-10-24"}])
        assert payload[-1] == {"title": "Sprint 11", "startDate": "2026-10-24"}
        assert "id" not in payload[-1]

    def test_existing_iterations_are_ordered_by_start_date(self):
        starts = [it["startDate"] for it in iteration_resubmit(self.CONFIG, [])]
        assert starts == sorted(starts)

    def test_an_iteration_with_no_id_is_dropped_rather_than_sent_id_less(self):
        """A half-read config would otherwise be resubmitted as a NEW
        iteration, which is the wipe."""
        config = _config(completed=[{"title": "Sprint 8", "startDate": "2026-08-09", "duration": 18}])
        assert iteration_resubmit(config, []) == []

    def test_the_plan_payload_is_the_full_resubmit_list(self):
        plan = next_sprint_plan(
            _config(completed=[_iter("c1", "Sprint 8", "2026-08-09"), _iter("c2", "Sprint 9", "2026-08-27")]),
            TODAY,
        )
        ids = [it.get("id") for it in plan["iterations"]]
        assert ids == ["c1", "c2", None]


# ---------------------------------------------------------------------------
# gate classification
# ---------------------------------------------------------------------------
def _f(key, kind, ok):
    return {"key": key, "kind": kind, "ok": ok, "detail": f"{key} detail"}


class TestDecide:
    def test_an_unmet_gate_stops_the_run(self):
        d = decide([_f("tag", GATE, False)])
        assert d["ok"] is False
        assert [f["key"] for f in d["gate_failures"]] == ["tag"]

    def test_an_unmet_provisionable_does_not_stop_the_run(self):
        d = decide([_f("milestone", PROVISION, False)])
        assert d["ok"] is True
        assert [f["key"] for f in d["provision"]] == ["milestone"]

    def test_every_gap_is_reported_not_just_the_first(self):
        """The reason the inventory exists: stopping at the first gap means
        the owner fixes one thing, re-dispatches, and finds the next."""
        d = decide(
            [
                _f("a", GATE, False),
                _f("b", PROVISION, False),
                _f("c", GATE, False),
                _f("d", GATE, True),
                _f("e", PROVISION, False),
            ]
        )
        assert [f["key"] for f in d["gate_failures"]] == ["a", "c"]
        assert [f["key"] for f in d["provision"]] == ["b", "e"]
        assert d["checked"] == 5

    def test_an_all_clear_inventory_proceeds_with_nothing_to_provision(self):
        d = decide([_f("a", GATE, True), _f("b", PROVISION, True)])
        assert d["ok"] is True
        assert d["provision"] == []

    def test_an_unknown_kind_is_an_error_not_a_silent_pass(self):
        """A typo'd kind would otherwise be neither gated nor provisioned --
        a prerequisite that is checked and then ignored."""
        with pytest.raises(ValueError, match="gate"):
            decide([_f("a", "advisory", False)])


# ---------------------------------------------------------------------------
# the CLI contract release_ceremony.sh depends on
# ---------------------------------------------------------------------------
def _run(args, stdin=""):
    return subprocess.run(
        [sys.executable, str(LIB / "release_prereqs.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


class TestCli:
    def test_next_patch_prints_the_bare_version(self):
        assert _run(["next-patch", "3.0.1"]).stdout.strip() == "3.0.2"

    def test_placeholder_title_prints_the_title(self):
        assert _run(["placeholder-title", "3.0.1"]).stdout.strip() == "Placeholder — next line (v3.0.2)"

    def test_next_line_prints_branch_tab_title(self):
        out = _run(["next-line", "3.0.1"], json.dumps(["Phase 7 — nyxAgent (v3.1.0)"])).stdout
        assert out.strip().split("\t") == ["v3.1.0", "Phase 7 — nyxAgent (v3.1.0)"]

    def test_next_line_prints_nothing_when_there_is_no_candidate(self):
        """Phase 0 tests for empty output; a "None" or a traceback would be
        read as a milestone title."""
        res = _run(["next-line", "3.0.1"], json.dumps(["Backlog"]))
        assert res.stdout.strip() == ""
        assert res.returncode == 0

    def test_sprint_plan_round_trips_json_on_stdin(self):
        config = _config(completed=[_iter("c1", "Sprint 9", "2026-08-01")])
        plan = json.loads(_run(["sprint-plan", "--today", "2026-10-08"], json.dumps(config)).stdout)
        assert plan["needed"] is True
        assert plan["new"]["title"] == "Sprint 10"

    def test_report_exits_zero_when_only_provisionables_are_missing(self):
        res = _run(["report"], json.dumps([_f("a", GATE, True), _f("b", PROVISION, False)]))
        assert res.returncode == 0
        assert "PROVISION [b]" in res.stdout

    def test_report_exits_one_and_lists_every_gate_failure(self):
        res = _run(["report"], json.dumps([_f("a", GATE, False), _f("c", GATE, False)]))
        assert res.returncode == 1
        assert "GATE FAIL [a]" in res.stdout and "GATE FAIL [c]" in res.stdout
        assert "2 gate failure(s)" in res.stdout


# ---------------------------------------------------------------------------
# the ceremony script wires it up the way the above assumes
# ---------------------------------------------------------------------------
CEREMONY = ROOT / "scripts" / "release_ceremony.sh"
PROOF = ROOT / "scripts" / "sprint-iteration-preservation-proof.sh"


class TestCeremonyWiring:
    """Cheap structural guards on the Phase 0 contract.

    They exist because the expensive half of this change -- the ordering and
    the resubmit payload -- is unobservable in a unit test and irreversible in
    production.
    """

    def test_provisioning_happens_before_the_agent_flags_are_paused(self):
        """Owner requirement (#4166): nothing irreversible-adjacent happens
        until the provisionable prerequisites are in place."""
        body = CEREMONY.read_text(encoding="utf-8")
        phase0 = body[body.index("Phase 0: prerequisite inventory") :]
        assert phase0.index("\nprovision_line\n") < phase0.index("\npause_agent_flags\n"), (
            "pause_agent_flags must come after provisioning in Phase 0"
        )

    def test_the_gate_decision_is_not_fatal_before_provisioning(self):
        """A gate failure must leave the provisioned objects in place, so the
        report's exit status is captured, not acted on immediately."""
        body = CEREMONY.read_text(encoding="utf-8")
        assert "GATE_OK=1\nprereq_report || GATE_OK=0" in body

    def test_the_resume_path_also_inventories_the_line_prerequisites(self):
        """--phase4-only skips Phase 0, so without this a resume reaches the
        Phase 4 line gate with the milestone/sprint still missing."""
        body = CEREMONY.read_text(encoding="utf-8")
        phase4_only = body.index("Phases 0-3 already ran")
        full_phase0 = body.index("Phase 0: prerequisite inventory")
        resume_block = body[phase4_only:full_phase0]
        assert "inventory_line" in resume_block
        assert "provision_line" in resume_block

    def test_the_sprint_query_asks_for_the_completed_iterations(self):
        """Dropping them from the resubmit list wipes their items' values, so
        the read has to include them in the first place."""
        body = CEREMONY.read_text(encoding="utf-8")
        query = body[body.index("read_sprint_field()") : body.index("inventory_line()")]
        assert "completedIterations" in query
        assert re.search(r"iterations\s*\{\s*id title startDate duration", query)

    def test_only_the_ceremony_creates_a_milestone(self):
        """CLAUDE.md §Tooling forbids agents creating milestones; #4166 is the
        owner's permission for the CEREMONY and nothing else. If a second
        caller appears, that scoped exception has drifted into a general
        licence -- which is exactly how the label rule drifted three times
        before #4134 put a check behind it."""
        callers = set()
        sources = [p for p in (ROOT / "scripts").rglob("*.sh") if "__pycache__" not in str(p)]
        sources += [p for p in (ROOT / "scripts").rglob("*.py") if "__pycache__" not in str(p)]
        sources += sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        for path in sources:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"(?:-X|--method)\s+POST[^\n]*?/milestones\b", stripped):
                    callers.add(str(path.relative_to(ROOT)))
        assert callers == {"scripts/release_ceremony.sh"}, (
            "milestone creation is sanctioned for the release ceremony alone (#4166), "
            f"but these create one: {sorted(callers)}"
        )

    def test_the_proof_script_is_referenced_where_the_hazard_is_encoded(self):
        """The evidence has to be findable from the code it licenses."""
        assert PROOF.exists()
        assert "sprint-iteration-preservation-proof.sh" in (LIB / "release_prereqs.py").read_text(
            encoding="utf-8"
        )
