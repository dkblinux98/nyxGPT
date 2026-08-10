"""Unit tests for scripts/agents/lib/parked_resume.py (#3709).

Pure computation only -- no gh/GraphQL involved -- covering the pieces the
issue calls out: the interim prose `Blocked by:` parser, the auto-resume
budget derived from an issue's comment thread, the parked/waiting/active
classification, and the "waiting on gates" report lines.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LIB_DIR = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
sys.path.insert(0, str(_LIB_DIR))
_spec = importlib.util.spec_from_file_location("parked_resume", _LIB_DIR / "parked_resume.py")
assert _spec is not None and _spec.loader is not None
parked_resume = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = parked_resume
_spec.loader.exec_module(parked_resume)


class TestParseBlockedByRefs:
    def test_plain_reference(self):
        assert parked_resume.parse_blocked_by_refs("Blocked by: #3509") == [3509]

    def test_reference_with_phase_label(self):
        body = "## Notes\n\nBlocked by: #3513 (P6-12)\n"
        assert parked_resume.parse_blocked_by_refs(body) == [3513]

    def test_bold_bullet_and_multiple_refs_on_one_line(self):
        body = "- **Blocked by:** #3509, #3510 (P6-11)\n"
        assert parked_resume.parse_blocked_by_refs(body) == [3509, 3510]

    def test_without_colon(self):
        assert parked_resume.parse_blocked_by_refs("Blocked by #42") == [42]

    def test_case_insensitive_and_deduplicated(self):
        body = "blocked by: #7\nBLOCKED BY: #7, #8\n"
        assert parked_resume.parse_blocked_by_refs(body) == [7, 8]

    def test_ignores_refs_outside_blocked_by_lines(self):
        body = "Related to #99 and see #100.\nBlocked by: #7\nAlso mentions #101.\n"
        assert parked_resume.parse_blocked_by_refs(body) == [7]

    def test_empty_and_none_bodies(self):
        assert parked_resume.parse_blocked_by_refs("") == []
        assert parked_resume.parse_blocked_by_refs(None) == []

    def test_blocked_by_line_with_no_refs(self):
        assert parked_resume.parse_blocked_by_refs("Blocked by: the owner's review") == []


class TestResumeBudget:
    def _comment(self, body, association="NONE"):
        return {"body": body, "author_association": association}

    def test_empty_thread_has_full_budget(self):
        budget = parked_resume.resume_budget([])
        assert budget["count"] == 0
        assert budget["exhausted"] is False
        assert budget["next_resume_number"] == 1

    def test_counts_auto_resume_markers(self):
        comments = [
            self._comment("hello"),
            self._comment("resume\n" + parked_resume.render_marker(3510, 1)),
            self._comment("resume\n" + parked_resume.render_marker(3510, 2)),
        ]
        budget = parked_resume.resume_budget(comments)
        assert budget["count"] == 2
        assert budget["exhausted"] is False
        assert budget["next_resume_number"] == 3

    def test_exhausts_at_the_cap(self):
        comments = [
            self._comment(parked_resume.render_marker(3510, n))
            for n in range(1, parked_resume.MAX_AUTO_RESUMES + 1)
        ]
        assert parked_resume.resume_budget(comments)["exhausted"] is True

    def test_owner_comment_resets_the_budget(self):
        comments = [
            self._comment(parked_resume.render_marker(3510, 1)),
            self._comment(parked_resume.render_marker(3510, 2)),
            self._comment(parked_resume.render_marker(3510, 3)),
            self._comment("looked into it, try again", association="OWNER"),
            self._comment(parked_resume.render_marker(3510, 1)),
        ]
        budget = parked_resume.resume_budget(comments)
        assert budget["count"] == 1
        assert budget["exhausted"] is False

    def test_bot_comment_does_not_reset(self):
        """The #3689 lesson: this loop's own comments come from bot accounts
        via PATs, so 'not a bot' would reset on every pass and bound nothing."""
        comments = [
            self._comment(parked_resume.render_marker(3510, 1)),
            self._comment("auto-retry chatter", association="CONTRIBUTOR"),
            self._comment(parked_resume.render_marker(3510, 2)),
        ]
        assert parked_resume.resume_budget(comments)["count"] == 2


class TestClassifyCandidates:
    def test_parked_and_ungated_is_resumable(self):
        scan = parked_resume.classify_candidates(
            [{"issue": 3510, "parked": True, "open_blockers": [], "budget_exhausted": False}]
        )
        assert scan["resumable"] == [{"issue": 3510, "open_blockers": []}]
        assert parked_resume.select_resume(scan) == 3510

    def test_parked_with_open_blockers_waits(self):
        scan = parked_resume.classify_candidates(
            [{"issue": 3513, "parked": True, "open_blockers": [3510], "budget_exhausted": False}]
        )
        assert scan["waiting"] == [{"issue": 3513, "open_blockers": [3510]}]
        assert scan["resumable"] == []
        assert parked_resume.select_resume(scan) is None

    def test_non_parked_issue_is_left_alone(self):
        scan = parked_resume.classify_candidates(
            [{"issue": 3509, "parked": False, "open_blockers": [], "budget_exhausted": False}]
        )
        assert scan["active"] == [3509]
        assert scan["resumable"] == []

    def test_exhausted_budget_is_reported_not_resumed(self):
        scan = parked_resume.classify_candidates(
            [{"issue": 3514, "parked": True, "open_blockers": [], "budget_exhausted": True}]
        )
        assert scan["exhausted"] == [{"issue": 3514, "open_blockers": []}]
        assert parked_resume.select_resume(scan) is None

    def test_selects_lowest_issue_number_first(self):
        scan = parked_resume.classify_candidates(
            [
                {"issue": 3516, "parked": True, "open_blockers": [], "budget_exhausted": False},
                {"issue": 3514, "parked": True, "open_blockers": [], "budget_exhausted": False},
            ]
        )
        assert parked_resume.select_resume(scan) == 3514

    def test_every_candidate_lands_in_exactly_one_bucket(self):
        candidates = [
            {"issue": 1, "parked": False, "open_blockers": [], "budget_exhausted": False},
            {"issue": 2, "parked": True, "open_blockers": [1], "budget_exhausted": False},
            {"issue": 3, "parked": True, "open_blockers": [], "budget_exhausted": True},
            {"issue": 4, "parked": True, "open_blockers": [], "budget_exhausted": False},
        ]
        scan = parked_resume.classify_candidates(candidates)
        bucketed = (
            [c["issue"] for c in scan["resumable"]]
            + [c["issue"] for c in scan["waiting"]]
            + [c["issue"] for c in scan["exhausted"]]
            + scan["active"]
        )
        assert sorted(bucketed) == [1, 2, 3, 4]


class TestBuildGateLines:
    def test_waiting_issues_are_never_silently_dropped(self):
        scan = parked_resume.classify_candidates(
            [
                {"issue": 3513, "parked": True, "open_blockers": [3510], "budget_exhausted": False},
                {
                    "issue": 3516,
                    "parked": True,
                    "open_blockers": [3514, 3515],
                    "budget_exhausted": False,
                },
            ]
        )
        lines = "\n".join(parked_resume.build_gate_lines(scan))
        assert "Waiting on gates" in lines
        assert "#3513 (waiting on #3510)" in lines
        assert "#3516 (waiting on #3514, #3515)" in lines

    def test_reports_the_resumed_issue(self):
        scan = parked_resume.classify_candidates(
            [{"issue": 3510, "parked": True, "open_blockers": [], "budget_exhausted": False}]
        )
        lines = "\n".join(parked_resume.build_gate_lines(scan, parked_resume.select_resume(scan)))
        assert "Auto-resumed:** #3510" in lines

    def test_reports_exhausted_and_active(self):
        scan = parked_resume.classify_candidates(
            [
                {"issue": 3514, "parked": True, "open_blockers": [], "budget_exhausted": True},
                {"issue": 3509, "parked": False, "open_blockers": [], "budget_exhausted": False},
            ]
        )
        lines = "\n".join(parked_resume.build_gate_lines(scan))
        assert "budget exhausted" in lines
        assert "#3514" in lines
        assert "In flight" in lines and "#3509" in lines

    def test_nothing_to_report_renders_nothing(self):
        assert parked_resume.build_gate_lines(parked_resume.classify_candidates([])) == []
