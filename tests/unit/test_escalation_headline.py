"""The escalation headline, composed from evidence rather than looked up (#4176).

The defect this suite exists for: a developer-agent escalation whose own
blast-radius section said *"Is `v3.0.1` red? yes -- `test` failing"* was headed
**"Error type could not be determined. Manual investigation needed."** The
headline was `phase3Diagnosis || phase2Diagnosis || errorExplanations[class]`,
and with both diagnoses empty `unknown` maps to that sentence -- so every
piece of evidence the run had already gathered was ignored (#4166, run
37709148793).

So the acceptance criterion is a statement about EVERY combination of
evidence, which is why these tests enumerate them rather than testing the
happy path: **no headline is ever the bare generic sentence while any evidence
exists.** `test_generic_only_when_nothing_is_known` is the one that fails if
someone reintroduces the lookup.

The other half is the format handshake. `format_verification_detail` writes
the agent error-detail file and `parse_verification_detail` reads it back;
`classify_error` (shell) greps the same first line. The marker is pinned here
because three readers in two languages depend on it, and
tests/test_escalation_evidence.sh pins the shell side against this module's
own output.

The end-to-end behaviour -- Final Verification executed against a planted
failing test, the classifier reading what it wrote, a real stray worktree
blocking a real checkout until it is pruned -- lives in
`tests/test_escalation_evidence.sh`, run below so `pytest -v` covers it too.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from shell_suite import bash4_or_skip

pytestmark = pytest.mark.unit

LIB = Path(__file__).resolve().parents[2] / "scripts" / "agents" / "lib"
sys.path.insert(0, str(LIB))

import escalation_evidence as ee  # noqa: E402

#: The #4166 run, as evidence. Every test below is a subset of this.
BASE_RED = {
    "base_red": True,
    "base_branch": "v3.0.1",
    "base_sha": "a1b2c3d4e5f6",
    "base_checks": ["test"],
}
VERIFICATION = {
    "verification_detail": (
        "Final Verification failed: gate=pytest\n"
        "3 failing test(s):\n"
        "FAILED tests/unit/test_release_candidate.py::test_formula_version\n"
        "FAILED tests/unit/test_release_candidate.py::test_rc_pin\n"
        "FAILED tests/unit/test_release_candidate.py::test_tap\n"
    )
}
PHASE3_CRASHED = {
    "phase3_outcome": "failure",
    "phase3_error": "fatal: 'v3.0.1' is already used by worktree at '/tmp/base-wt'",
}


class TestDetailFormatRoundTrip:
    def test_marker_is_what_classify_error_greps_for(self):
        """Pinned: `classify_error` in gh_project.sh greps this exact string."""
        assert ee.DETAIL_MARKER == "Final Verification failed: gate="
        detail = ee.format_verification_detail("pytest", ["tests/unit/test_a.py::test_x"], 1)
        assert detail.splitlines()[0] == "Final Verification failed: gate=pytest"

    def test_node_ids_and_total_survive_the_round_trip(self):
        nodes = [f"tests/unit/test_m.py::test_{i}" for i in range(5)]
        parsed = ee.parse_verification_detail(
            ee.format_verification_detail("pytest", nodes, len(nodes))
        )
        assert parsed is not None
        assert parsed["gate"] == "pytest"
        assert parsed["failing_tests"] == nodes
        assert parsed["failing_total"] == 5

    def test_long_failure_lists_are_capped_but_counted(self):
        """The owner needs the shape of the failure, not 400 node IDs."""
        nodes = [f"tests/unit/test_m.py::test_{i}" for i in range(50)]
        detail = ee.format_verification_detail("pytest", nodes, 50)
        assert detail.count("FAILED ") == ee.FAILING_TEST_LIMIT
        assert "and 30 more failing test(s)" in detail
        parsed = ee.parse_verification_detail(detail)
        assert parsed is not None
        assert parsed["failing_total"] == 50

    def test_a_gate_without_node_ids_records_its_error_lines(self):
        detail = ee.format_verification_detail(
            "mypy", None, None, ["src/nyxgpt/ops.py:12: error: Name 'x' is not defined"]
        )
        parsed = ee.parse_verification_detail(detail)
        assert parsed is not None
        assert parsed["gate"] == "mypy"
        assert parsed["errors"] == ["src/nyxgpt/ops.py:12: error: Name 'x' is not defined"]

    def test_pytest_failing_with_no_parseable_node_ids_still_says_so(self):
        """A collection error has no `FAILED` line. "0 failures" would be a lie."""
        detail = ee.format_verification_detail("pytest", [], 4)
        assert "4 failing test(s)" in detail
        parsed = ee.parse_verification_detail(detail)
        assert parsed is not None
        assert parsed["failing_total"] == 4

    def test_anything_else_is_not_a_verification_detail(self):
        assert ee.parse_verification_detail("") is None
        assert ee.parse_verification_detail(None) is None
        assert ee.parse_verification_detail("red head is not reviewable") is None


class TestHeadlineCombinations:
    """Every combination of evidence, and what the owner must read in each."""

    def test_generic_only_when_nothing_is_known(self):
        """THE criterion. Evidence anywhere means the generic sentence never prints."""
        assert ee.headline({"error_class": "unknown"}) == ee.GENERIC_HEADLINE
        for evidence in (BASE_RED, VERIFICATION, PHASE3_CRASHED, {"phase3_diagnosis": "x"}):
            ev = {"error_class": "unknown", **evidence}
            assert ee.headline(ev) != ee.GENERIC_HEADLINE, evidence

    def test_phase3_diagnosis_leads(self):
        ev = {"error_class": "unknown", "phase3_diagnosis": "The issue was closed by the owner."}
        assert ee.headline(ev).startswith("The issue was closed by the owner.")

    def test_phase2_diagnosis_is_used_when_phase3_has_none(self):
        ev = {"error_class": "unknown", "phase2_diagnosis": "Rate limit, reset in 40m."}
        assert ee.headline(ev).startswith("Rate limit, reset in 40m.")

    def test_red_base_is_named_with_branch_sha_and_check(self):
        text = ee.headline({"error_class": "unknown", **BASE_RED})
        assert "v3.0.1" in text
        assert "a1b2c3d" in text  # short sha
        assert "`test`" in text
        assert "inherited" in text.lower()

    def test_red_base_without_a_readable_sha_still_names_the_branch(self):
        ev = {"error_class": "unknown", **{**BASE_RED, "base_sha": ""}}
        text = ee.headline(ev)
        assert "`v3.0.1`" in text
        assert "@" not in text

    def test_a_base_whose_state_could_not_be_read_is_not_called_red(self):
        """None is "not checked". Rendering it as either answer invents a fact."""
        for unknown in (None, {}):
            ev = {"error_class": "unknown", "base_red": unknown, "base_branch": "v3.0.1"}
            assert ee.headline(ev) == ee.GENERIC_HEADLINE
        ev = {"error_class": "unknown", "base_red": False, "base_branch": "v3.0.1"}
        assert ee.headline(ev) == ee.GENERIC_HEADLINE

    def test_verification_failure_names_the_gate_and_the_tests(self):
        text = ee.headline({"error_class": "verification_failed:pytest", **VERIFICATION})
        assert "pytest gate" in text
        assert "3 failing test(s)" in text
        assert "test_release_candidate.py::test_formula_version" in text

    def test_a_non_pytest_gate_names_its_first_error(self):
        ev = {
            "error_class": "verification_failed:ruff",
            "verification_detail": (
                "Final Verification failed: gate=ruff\n"
                "src/nyxgpt/api.py:3:1: F401 `os` imported but unused"
            ),
        }
        text = ee.headline(ev)
        assert "ruff linting gate" in text
        assert "F401" in text

    def test_red_base_and_failing_tests_are_both_reported(self):
        """Whose fault AND what broke -- the acceptance run asserts both."""
        text = ee.headline({"error_class": "unknown", **BASE_RED, **VERIFICATION})
        assert "ALREADY RED" in text
        assert "test_release_candidate.py::test_formula_version" in text

    def test_a_crashed_phase3_says_it_did_not_run(self):
        text = ee.headline({"error_class": "unknown", **PHASE3_CRASHED})
        assert "Phase 3 diagnosis did not run" in text
        assert "already used by worktree" in text

    def test_a_crashed_phase3_with_no_error_line_is_honest_about_it(self):
        ev = {"error_class": "unknown", "phase3_outcome": "failure"}
        text = ee.headline(ev)
        assert "Phase 3 diagnosis did not run" in text
        assert "not available mid-run" in text

    def test_a_skipped_phase3_is_not_reported_as_a_crash(self):
        """Skipped and crashed are different facts; only one is Phase 3's fault."""
        for outcome in ("", "skipped", "success"):
            ev = {"error_class": "unknown", "phase3_outcome": outcome, **VERIFICATION}
            assert "did not run" not in ee.headline(ev)

    def test_the_4166_run_reads_as_the_owner_needed_it_to(self):
        """The worked example, end to end."""
        text = ee.headline(
            {
                "error_class": "verification_failed:pytest",
                "failed_step": "Final Verification (Must Pass)",
                **BASE_RED,
                **VERIFICATION,
                **PHASE3_CRASHED,
            }
        )
        assert ee.GENERIC_HEADLINE not in text
        assert "v3.0.1" in text and "`test` is failing" in text
        assert "test_release_candidate.py::test_formula_version" in text
        assert "Phase 3 diagnosis did not run" in text

    def test_the_self_explanatory_classes_keep_their_explanations(self):
        assert ee.headline({"error_class": "fatal:issue_closed"}) == (
            ee.ERROR_EXPLANATIONS["fatal:issue_closed"]
        )
        assert ee.headline({"error_class": "fatal:auth_failure"}).startswith("Authentication")
        # An unrecognised class with no evidence is the generic sentence, not
        # a KeyError and not an invented explanation.
        assert ee.headline({"error_class": "fatal:brand_new"}) == ee.GENERIC_HEADLINE


class TestCauseKey:
    def test_a_red_base_is_one_cause_for_every_issue_it_breaks(self):
        """CLAUDE.md: one systemic cause gets ONE escalation."""
        first = ee.cause_key({"failed_step": "Final Verification (Must Pass)", **BASE_RED})
        second = ee.cause_key({"failed_step": "Submit PR for review", **BASE_RED})
        assert first == second == "base-red:v3.0.1:test"

    def test_the_key_ignores_the_head_sha(self):
        """`test` failing on v3.0.1 is the same fault after the next commit."""
        moved = ee.cause_key({**BASE_RED, "base_sha": "9999999999"})
        assert moved == ee.cause_key(BASE_RED)

    def test_without_a_red_base_the_key_stays_the_failed_step(self):
        ev = {"failed_step": "Submit PR for review"}
        assert ee.cause_key(ev) == "developer-failure:Submit PR for review"
        assert ee.cause_key({}) == "developer-failure:unknown-step"

    def test_a_red_base_with_unnamed_checks_still_keys_on_the_branch(self):
        ev = {"base_red": True, "base_branch": "v3.0.1", "base_checks": []}
        assert ee.cause_key(ev) == "base-red:v3.0.1:unnamed-check"


class TestAnalysisPhase:
    def test_names_where_the_headline_came_from(self):
        assert ee.analysis_phase({"phase3_diagnosis": "x"}) == "Phase 3 (Claude reasoning)"
        assert ee.analysis_phase({"phase2_diagnosis": "x"}) == "Phase 2 (Script analysis)"
        assert ee.analysis_phase(BASE_RED) == "Phase 1 (base-red finding)"
        assert ee.analysis_phase(VERIFICATION) == "Phase 1 (Final Verification's own reason)"
        assert ee.analysis_phase(PHASE3_CRASHED) == "Phase 3 (did not run)"
        assert ee.analysis_phase({}) == "Phase 1 (Error classification)"


class TestCli:
    """The workflow calls this module as a subprocess, so the CLI is an API."""

    SCRIPT = LIB / "escalation_evidence.py"

    def _run(self, args: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_compose_returns_the_four_fields_the_workflow_reads(self):
        ev = {"error_class": "verification_failed:pytest", **BASE_RED, **VERIFICATION}
        out = self._run(["compose"], json.dumps(ev))
        assert out.returncode == 0, out.stderr
        composed = json.loads(out.stdout)
        assert set(composed) == {"headline", "cause", "analysis_phase", "generic"}
        assert composed["cause"] == "base-red:v3.0.1:test"
        assert composed["generic"] is False

    def test_compose_reports_the_generic_case_as_generic(self):
        composed = json.loads(self._run(["compose"], json.dumps({})).stdout)
        assert composed["generic"] is True
        assert composed["headline"] == ee.GENERIC_HEADLINE

    def test_unparseable_stdin_still_yields_a_headline(self):
        """A failure inside the failure reporter must not swallow the report."""
        for stdin in ("", "not json at all", "[]"):
            out = self._run(["compose"], stdin)
            assert out.returncode == 0, out.stderr
            assert json.loads(out.stdout)["headline"] == ee.GENERIC_HEADLINE

    def test_verification_detail_reads_a_short_summary_from_stdin(self):
        summary = "FAILED tests/unit/test_a.py::test_x\nFAILED tests/unit/test_b.py::test_y\n"
        out = self._run(["verification-detail", "pytest", "9"], summary)
        assert out.returncode == 0, out.stderr
        assert out.stdout.splitlines()[0] == "Final Verification failed: gate=pytest"
        assert "9 failing test(s)" in out.stdout
        assert "tests/unit/test_b.py::test_y" in out.stdout

    def test_an_unknown_subcommand_is_a_usage_error(self):
        assert self._run(["diagnose-everything"], "{}").returncode == 2


class TestShellSuite:
    """The executed half: the gate, the classifier and the worktree prune.

    Run from here as well as from
    `.github/workflows/escalation-headline-smoke.yml` so a developer running
    `pytest -v` cannot land a change that passes the pure tests and breaks the
    scripts they describe.
    """

    def test_shell_suite_passes(self):
        if shutil.which("jq") is None:
            pytest.skip("jq is required by the shell suite")
        if shutil.which("git") is None:
            pytest.skip("git is required by the worktree half of the shell suite")
        suite = Path(__file__).resolve().parents[1] / "test_escalation_evidence.sh"
        result = subprocess.run(
            [bash4_or_skip(), str(suite)],
            capture_output=True,
            text=True,
            timeout=300,
            # The suite EXECUTES Final Verification, which runs pytest. Hand it
            # the interpreter pytest is already running under: `python3` from
            # PATH is frequently a system interpreter with no pytest installed,
            # and the gate would then fail for a reason that has nothing to do
            # with what is being tested.
            env={**os.environ, "NYXGPT_TEST_PYTHON": sys.executable},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # A skipped section must not read as a pass here either.
        assert "it records the first failing node ID" in result.stdout, result.stdout
