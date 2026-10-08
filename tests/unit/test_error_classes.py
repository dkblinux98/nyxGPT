"""One table decides what each error class does (#4179).

The defect: a developer round refused for a red head escalated to the owner as
FATAL. Phase 1 classified it `retriable:ci_red` -- correctly, and that class
exists precisely because #3971's contract is that the round CONTINUES -- and
then Phase 2 classified it again, from scratch, with a `case` that had no
`ci_red` arm and a default that wrote `STATUS=FATAL`. The owner's escalation
read *"Error type: retriable:ci_red ... Diagnosis: Unrecognized error type."*

`tests/test_reviewable_head_gate.sh` already pinned Phase 1's classification.
Nothing tested what Phase 2 did with it, so the two halves disagreed for seven
weeks (#3971 landed 2026-08-20; this was observed on #4138, 2026-10-08).

So the acceptance criterion is a statement about EVERY class rather than about
`ci_red`: no class `classify_error` can emit may be missing from the table, and
no `retriable` class may be left on the default outcome.
`test_every_shell_class_has_a_row` and
`test_no_retriable_class_is_left_on_the_default_outcome` are the two that fail
if someone adds a class in one place and not the other -- which is the shape of
this defect, not an instance of it.

The executed half (`developer_analyze_failure.sh` run against a recorded
red-head refusal, with no log API to harvest from) lives in
`tests/test_red_head_round.sh`, run from `TestShellSuite` below so `pytest -v`
covers it too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from shell_suite import bash4_or_skip

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "agents" / "lib"
sys.path.insert(0, str(LIB))

import error_classes as ec  # noqa: E402
import escalation_evidence as ee  # noqa: E402

GH_PROJECT = LIB / "gh_project.sh"
SUBMIT = ROOT / "scripts" / "agents" / "developer_submit_for_review.sh"


class TestTheTableCoversTheClassifier:
    """The enumeration, read out of `classify_error` rather than restated."""

    def test_the_classifier_is_actually_readable(self):
        """If this breaks, every test below is vacuously green -- so it is first."""
        emitted = ec.shell_emitted_classes(GH_PROJECT)
        assert len(emitted) >= 9, emitted
        assert "retriable:ci_red" in emitted
        assert "unknown" in emitted
        # The shell interpolates the gate into the class name; the reader must
        # fold that onto the family key rather than reporting a literal `${...}`.
        assert ec.VERIFICATION_FAILED in emitted
        assert not any("${" in name for name in emitted), emitted

    def test_every_shell_class_has_a_row(self):
        """A class the shell can emit and the table does not know about.

        That gap IS #4179: Phase 2's `case` was a second, incomplete
        enumeration of the same classes.
        """
        missing = [
            name for name in ec.shell_emitted_classes(GH_PROJECT) if name not in ec.ERROR_CLASSES
        ]
        assert not missing, (
            f"classify_error can emit {missing}, which has no row in "
            "scripts/agents/lib/error_classes.py. Add the row (disposition, "
            "phase2 outcome, explanation) -- a class with no row behaves like "
            "`unknown`, which means an owner page for something the pipeline "
            "may well know how to retry."
        )

    def test_no_retriable_class_is_left_on_the_default_outcome(self):
        """THE #4179 assertion.

        `retriable` means "the pipeline knows how to continue". A retriable
        class whose Phase 2 outcome is the default has no continuation, and the
        old default was `FATAL` -- so the class said "retry" and the pipeline
        escalated.
        """
        stranded = [
            name for name in ec.retriable_classes() if ec.phase2_outcome(name) == ec.PHASE2_DEFER
        ]
        assert not stranded, (
            f"{stranded} are retriable but have no Phase 2 outcome. Give each "
            "one a real outcome (investigate / transient / continue_round) or "
            "stop calling it retriable."
        )

    def test_every_row_names_an_outcome_the_script_can_act_on(self):
        for name, row in ec.ERROR_CLASSES.items():
            assert row["phase2"] in ec.OUTCOME_EXIT, (name, row["phase2"])
            assert row["disposition"] in (ec.RETRIABLE, ec.FATAL, ec.DIAGNOSE), name
            assert str(row["explanation"]).strip(), name

    def test_ci_red_continues_the_round_and_never_exits_fatal(self):
        assert ec.disposition("retriable:ci_red") == ec.RETRIABLE
        assert ec.phase2_outcome("retriable:ci_red") == ec.PHASE2_CONTINUE_ROUND
        # Exit 2, which the workflow maps to TRANSIENT, which re-assigns the
        # developer agent -- and the assignment IS the next round (#3882).
        assert ec.exit_code("retriable:ci_red") == ec.EXIT_TRANSIENT
        assert ec.exit_code("retriable:ci_red") != ec.EXIT_FATAL

    def test_an_unknown_or_absent_class_defers_rather_than_dying(self):
        for value in ("", None, "retriable:brand_new", "nonsense"):
            assert ec.normalize(value) == "unknown"
            assert ec.disposition(value) == ec.DIAGNOSE
            assert ec.exit_code(value) == ec.EXIT_DEFER

    def test_the_verification_failed_family_folds_onto_one_row(self):
        for gate in ("pytest", "mypy", "black", "a-gate-invented-tomorrow"):
            cls = f"verification_failed:{gate}"
            assert ec.normalize(cls) == ec.VERIFICATION_FAILED
            assert ec.disposition(cls) == ec.DIAGNOSE
            assert ec.gate_of(cls) == gate
        assert ec.gate_of("retriable:ci_red") == ""


class TestTheShellPredicatesReadTheTable:
    """Phase 1's `is_retriable_error` / `is_fatal_error`, executed."""

    def _disposition(self, error_class: str) -> str:
        script = (
            f"source {GH_PROJECT!s}\n"
            f'if is_retriable_error "{error_class}"; then echo retriable\n'
            f'elif is_fatal_error "{error_class}"; then echo fatal\n'
            "else echo diagnose\nfi\n"
        )
        out = subprocess.run(
            [bash4_or_skip(), "-c", script], capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, out.stderr
        return out.stdout.strip().splitlines()[-1]

    @pytest.mark.parametrize("error_class", sorted(ec.ERROR_CLASSES))
    def test_shell_and_table_agree_on_every_class(self, error_class):
        expected = ec.disposition(error_class)
        # The family key is not what the shell emits -- ask it the real thing.
        if error_class == ec.VERIFICATION_FAILED:
            error_class = "verification_failed:pytest"
        assert self._disposition(error_class) == expected

    def test_the_predicates_still_answer_when_the_table_cannot_be_read(self):
        """The fallback, which is the one that must not wedge a failing run.

        `error_class_disposition` runs on the failure path. A missing
        interpreter there must not be the reason a run cannot classify itself,
        so the class prefix is the documented fallback -- exactly what the
        predicates read before the table existed.
        """
        script = (
            "python3() { return 127; }\n"
            f"source {GH_PROJECT!s}\n"
            'error_class_disposition "retriable:ci_red"\n'
            'error_class_disposition "fatal:auth_failure"\n'
            'error_class_disposition "unknown"\n'
        )
        out = subprocess.run(
            [bash4_or_skip(), "-c", script], capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.split() == ["retriable", "fatal", "diagnose"], out.stdout


class TestTheRedHeadDetailHandshake:
    """Shell writes the `red-head-check:` lines; this module reads them back."""

    def test_the_prefix_is_pinned_on_both_sides(self):
        """Two languages, one string -- the drift that produced #4176's defect 1.

        The shell writes the line (`red_head_check_lines` in gh_project.sh) and
        `parse_red_head_detail` reads it. Neither can see the other, so the
        literal is pinned here.
        """
        assert ee.RED_HEAD_CHECK_PREFIX == "red-head-check: "
        shell = GH_PROJECT.read_text(encoding="utf-8")
        assert "printf 'red-head-check: %s%s\\n'" in shell
        # And the refusal sentence classify_error greps for is still printed by
        # the script that is supposed to print it (#3971).
        assert ee.RED_HEAD_MARKER in SUBMIT.read_text(encoding="utf-8")

    def test_the_checks_and_their_urls_survive_the_round_trip(self):
        detail = (
            "[error] Required checks FAILED on head abc1234: k3s-cloud-smoke,security-scan\n"
            "[error] Refusing to submit: a red head is not reviewable (#3971).\n"
            "red-head-check: k3s-cloud-smoke https://github.com/o/r/actions/runs/1/job/2\n"
            "red-head-check: security-scan\n"
            "[error] This is the developer round's work, not the reviewer's.\n"
        )
        parsed = ee.parse_red_head_detail(detail)
        assert parsed is not None
        assert parsed["head_sha"] == "abc1234"
        assert [c["name"] for c in parsed["checks"]] == ["k3s-cloud-smoke", "security-scan"]
        assert parsed["checks"][0]["url"].endswith("/job/2")
        assert parsed["checks"][1]["url"] == ""

    def test_a_pre_4179_refusal_still_yields_its_check_names(self):
        """Refusals recorded before the structured lines existed.

        The prose sentence is the fallback, for the same reason
        `parse_verification_detail` is tolerant: a reader that only accepts
        today's rendering turns real evidence back into the generic sentence.
        """
        detail = (
            "[error] Required checks FAILED on head deadbee: k3s-cloud-smoke\n"
            "[error] Refusing to submit: a red head is not reviewable (#3971).\n"
        )
        assert [c["name"] for c in ee.red_head_checks(detail)] == ["k3s-cloud-smoke"]

    def test_text_that_is_not_a_refusal_parses_to_nothing(self):
        assert ee.parse_red_head_detail("Submit PR for review") is None
        assert ee.parse_red_head_detail("") is None
        assert ee.red_head_checks("Final Verification failed: gate=pytest") == []

    def test_a_refusal_that_named_no_checks_is_still_a_refusal(self):
        parsed = ee.parse_red_head_detail("Refusing to submit: a red head is not reviewable.")
        assert parsed == {"checks": [], "head_sha": ""}


class TestTheHeadlineAndCauseNameTheCheck:
    """A ci_red escalation can only be a spent budget -- so it names the check."""

    REFUSAL = (
        "[error] Required checks FAILED on head abc1234: k3s-cloud-smoke\n"
        "[error] Refusing to submit: a red head is not reviewable (#3971).\n"
        "red-head-check: k3s-cloud-smoke https://github.com/o/r/actions/runs/1/job/2\n"
    )

    def test_the_headline_names_the_failing_check_not_the_generic_sentence(self):
        text = ee.headline({"error_class": "retriable:ci_red", "verification_detail": self.REFUSAL})
        assert "k3s-cloud-smoke" in text
        assert ee.GENERIC_HEADLINE not in text

    def test_the_cause_key_is_the_check_not_the_step_that_noticed(self):
        """#4138's key was `developer-failure:unknown`.

        Every red-head refusal fails the same step ("Submit PR for review"),
        so keying on the step collapsed unrelated broken checks into one cause
        and split one broken check across every issue that hit it.
        """
        ev = {
            "error_class": "retriable:ci_red",
            "failed_step": "Submit PR for review",
            "verification_detail": self.REFUSAL,
        }
        assert ee.cause_key(ev) == "head-red:k3s-cloud-smoke"

    def test_a_red_base_still_outranks_a_red_head(self):
        """One systemic cause gets ONE escalation (CLAUDE.md).

        A red base breaks every issue built on it, so it keeps the key even
        when this branch's head is red too -- the head is a symptom.
        """
        ev = {
            "base_red": True,
            "base_branch": "v9.9.9",
            "base_checks": ["test"],
            "verification_detail": self.REFUSAL,
        }
        assert ee.cause_key(ev) == "base-red:v9.9.9:test"

    def test_without_a_refusal_the_key_is_unchanged(self):
        ev = {"failed_step": "Final Verification (Must Pass)"}
        assert ee.cause_key(ev) == "developer-failure:Final Verification (Must Pass)"

    def test_the_analysis_phase_says_where_the_evidence_came_from(self):
        assert (
            ee.analysis_phase({"verification_detail": self.REFUSAL})
            == "Phase 1 (the refused red head)"
        )

    def test_every_class_has_a_non_generic_explanation_except_unknown(self):
        """`retriable:ci_red` had no explanation entry either (#4179).

        The headline's last-resort layer was a second hand-maintained dict, so
        a ci_red escalation with no other evidence printed "Error type could
        not be determined" -- the exact sentence #4176 removed.
        """
        for name in ec.ERROR_CLASSES:
            text = ee.ERROR_EXPLANATIONS[name]
            if name == "unknown":
                assert text == ee.GENERIC_HEADLINE
            else:
                assert text != ee.GENERIC_HEADLINE, name


class TestShellSuite:
    """The executed half: Phase 2 run over a recorded refusal, with no log API."""

    def test_shell_suite_passes(self):
        if shutil.which("jq") is None:
            pytest.skip("jq is required by the shell suite")
        if shutil.which("git") is None:
            pytest.skip("git is required by the shell suite")
        suite = ROOT / "tests" / "test_red_head_round.sh"
        result = subprocess.run(
            [bash4_or_skip(), str(suite)],
            capture_output=True,
            text=True,
            timeout=300,
            env={**os.environ, "NYXGPT_TEST_PYTHON": sys.executable},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # A skipped section must not read as a pass here either.
        assert "a red head continues the round" in result.stdout, result.stdout
