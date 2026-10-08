"""The wiring that decides whether a red-head round continues (#4179).

`tests/test_red_head_round.sh` proves `developer_analyze_failure.sh` is right
when it runs. This file pins the part that decides what the workflow DOES with
its answer -- which is where #4138 was actually lost: Phase 2 exited 1, the
workflow mapped exit 1 to `fix_status=FATAL`, and `escalate_fatal` fired for a
class the pipeline itself calls retriable.

The two `if:` expressions that decide it are EVALUATED here, not read, over the
step outputs a ci_red round actually produces. A test that greps for the string
`DEFER` in the condition would pass on a condition that contains it in a
comment; the question is "would the escalation step have run?", so that is the
question asked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_WF_PATH = REPO_ROOT / ".github" / "workflows" / "developer_auto_implement.yml"
DEV_WF = yaml.safe_load(DEV_WF_PATH.read_text())
DEV_STEPS: list[dict] = DEV_WF["jobs"]["implement"]["steps"]

PHASE1 = "Classify error (Phase 1)"
PHASE2 = "Attempt intelligent fix (Phase 2)"
AUTO_RETRY = "Auto-retry on failure (Phase 2+3 - retriable errors)"
ESCALATE = "Escalate fatal error (Phase 1+2+3 - no retry)"
BRIEF = "Save review comments to file (if review issues found)"


def _step(name: str) -> dict:
    for step in DEV_STEPS:
        if step.get("name") == name:
            return step
    raise AssertionError(f"No step named {name!r} in developer_auto_implement.yml")


# ---------------------------------------------------------------------------
# A very small GitHub-expression evaluator, for the two conditions below
# ---------------------------------------------------------------------------

_CONTEXT_REF = re.compile(r"steps\.[A-Za-z0-9_]+\.(?:outputs\.[A-Za-z0-9_]+|outcome|conclusion)")


def _evaluate(expression: str, ctx: dict[str, str], *, failed: bool = True) -> bool:
    """Evaluate a GitHub `if:` expression over `ctx`.

    Only the forms these conditions use: `failure()`, `success()`, `always()`,
    `startsWith(a, b)`, `&&`, `||`, `!=`, `==`, parentheses and single-quoted
    literals. An unset step output is the empty string, which is what GitHub
    does for a step that was skipped -- and getting that wrong is how a
    condition reads as true for a step that never ran.
    """
    expr = " ".join(expression.split())

    def ref(match: re.Match[str]) -> str:
        return repr(ctx.get(match.group(0), ""))

    expr = _CONTEXT_REF.sub(ref, expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = expr.replace("failure()", repr(failed)).replace("success()", repr(not failed))
    expr = expr.replace("always()", "True")
    expr = re.sub(r"startsWith\(", "_starts_with(", expr)

    leftovers = re.findall(r"\b(?:steps|github|vars|needs)\.[A-Za-z0-9_.]+", expr)
    assert not leftovers, f"the evaluator does not understand {leftovers} in: {expression}"

    return bool(
        eval(  # noqa: S307 - test-only, over text from this repository's own workflow
            expr,
            {"__builtins__": {}},
            {"_starts_with": lambda a, b: str(a).startswith(str(b)), "True": True, "False": False},
        )
    )


#: What a red-head round's steps report by the time the decision is made:
#: Phase 1 classified it retriable, Phase 2 continued the round, Phase 3 never
#: ran (so every one of its outputs is empty).
CI_RED_OUTCOME = {
    "steps.classify_error.outputs.error_class": "retriable:ci_red",
    "steps.classify_error.outputs.retriable": "true",
    "steps.classify_error.outputs.failed_step": "Submit PR for review",
    "steps.intelligent_fix.outputs.fix_status": "TRANSIENT",
    "steps.cross_issue_anomaly.outputs.matched": "false",
}

#: The same round, if Phase 2 had nothing to add (the #4138 empty harvest).
DEFER_OUTCOME = {**CI_RED_OUTCOME, "steps.intelligent_fix.outputs.fix_status": "DEFER"}

#: A genuinely fatal round, so the assertions below cannot pass by the
#: escalation having been disabled outright.
FATAL_OUTCOME = {
    "steps.classify_error.outputs.error_class": "fatal:auth_failure",
    "steps.classify_error.outputs.retriable": "false",
    "steps.intelligent_fix.outputs.fix_status": "",
    "steps.cross_issue_anomaly.outputs.matched": "false",
}


class TestTheDecision:
    @pytest.mark.parametrize("outcome", [CI_RED_OUTCOME, DEFER_OUTCOME])
    def test_the_round_continues(self, outcome):
        assert _evaluate(_step(AUTO_RETRY)["if"], outcome) is True

    @pytest.mark.parametrize("outcome", [CI_RED_OUTCOME, DEFER_OUTCOME])
    def test_and_no_escalation_is_posted(self, outcome):
        """THE #4179 assertion, as the owner experienced it.

        On #4138 this evaluated true for `retriable:ci_red` because Phase 2
        reported FATAL, and the owner was DM'd about a failure whose whole
        contract is that the developer round continues (#3971).
        """
        assert _evaluate(_step(ESCALATE)["if"], outcome) is False

    def test_a_fatal_round_still_escalates_and_does_not_retry(self):
        """The other direction: an escalation path that never fires is worse."""
        assert _evaluate(_step(ESCALATE)["if"], FATAL_OUTCOME) is True
        assert _evaluate(_step(AUTO_RETRY)["if"], FATAL_OUTCOME) is False

    def test_a_successful_run_decides_nothing(self):
        for name in (AUTO_RETRY, ESCALATE):
            assert _evaluate(_step(name)["if"], CI_RED_OUTCOME, failed=False) is False

    def test_a_cross_issue_anomaly_still_suppresses_both(self):
        held = {**CI_RED_OUTCOME, "steps.cross_issue_anomaly.outputs.matched": "true"}
        assert _evaluate(_step(AUTO_RETRY)["if"], held) is False
        assert _evaluate(_step(ESCALATE)["if"], held) is False


class TestPhase2Inputs:
    def test_phase_1_hands_over_the_text_it_classified(self):
        run = _step(PHASE1)["run"]
        assert "/tmp/phase1_error_text.txt" in run
        assert "printf '%s\\n' \"$ERROR_LOG\" > /tmp/phase1_error_text.txt" in run

    def test_phase_2_is_given_phase_1s_class_and_text(self):
        step = _step(PHASE2)
        env = step.get("env", {})
        assert env.get("ERROR_CLASS") == "${{ steps.classify_error.outputs.error_class }}"
        assert env.get("NYXGPT_PHASE1_ERROR_FILE") == "/tmp/phase1_error_text.txt"
        # Passed as the script's third argument, which is the class input.
        assert '"$ERROR_CLASS"' in step["run"]

    def test_phase_2_never_reaches_for_the_run_log_archive(self):
        """The harvest that cannot answer mid-run, and used to be step one.

        `gh run view --log` downloads a zip that exists only after the run
        completes; mid-run it is a 403. An empty harvest wrote
        `STATUS=UNKNOWN` and exited 1 -> `fix_status=FATAL` -> escalation.
        """
        script = (REPO_ROOT / "scripts" / "agents" / "developer_analyze_failure.sh").read_text()
        # Comment lines are excluded deliberately: the script's header
        # DESCRIBES the call it no longer makes, which is the explanation the
        # next reader needs. What must not come back is the call.
        code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
        offenders = [ln for ln in code if "gh run view" in ln or "--log" in ln]
        assert not offenders, offenders

    def test_exit_3_is_defer_and_exit_1_is_still_fatal(self):
        run = _step(PHASE2)["run"]
        assert 'echo "fix_status=DEFER"' in run
        assert 'echo "fix_status=FATAL"' in run
        # And the failing check travels to the retry comment.
        assert "ci_red_checks=" in run
        assert (
            _step(AUTO_RETRY)["env"].get("CI_RED_CHECKS")
            == "${{ steps.intelligent_fix.outputs.ci_red_checks }}"
        )


class TestTheNextRoundSeesTheCause:
    def test_the_brief_names_the_red_check_on_the_pr_head(self):
        """A retry that loses the cause just burns the budget.

        The continued round takes the Review Fix path (the refusal left no PR,
        so the rescue draft is what it finds), and its brief is this file. It
        used to say only "finish the work on that branch".
        """
        run = _step(BRIEF)["run"]
        assert "required_check_state" in run
        assert "red_head_check_lines" in run
        assert "A required check is RED on this branch's head" in run
        # Derived from the PR's own head, live -- not relayed through a comment
        # that may describe a check which has since gone green.
        assert ".head.sha" in run

    def test_the_brief_does_not_tell_the_round_to_override_the_gate(self):
        run = _step(BRIEF)["run"]
        assert "--ci-override" not in run
        assert "Do NOT submit the PR yourself" in run
