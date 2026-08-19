"""Issue hygiene never overwrites a populated field (#3816).

Two halves:

* the behavioural suite (`tests/test_issue_hygiene.sh`), run here because
  `pytest -v` is the gate this repo actually runs -- it drives
  `scripts/agents/ensure_issue_hygiene.sh` against a stateful `gh` stub that
  injects a concurrent deliberate write into the window between hygiene's
  check of a field and its write of that field, and reads the field back
  afterwards;
* static wiring checks, so the guard cannot be silently unhooked -- the
  defect's signature is a *successful* hygiene run that overwrote a good
  value (#3814, 2026-08-16), which no green run reveals.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
HYGIENE = ROOT / "scripts" / "agents" / "ensure_issue_hygiene.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "ensure_project_hygiene.yml"
SMOKE = ROOT / ".github" / "workflows" / "project-hygiene-smoke.yml"

#: Every project field the hygiene job knows how to populate.
FIELDS = ["Status", "Priority", "Effort", "Module", "Sprint"]

#: Project-field setters that write without re-reading first. The fill path may
#: not use any of them (#3816); the closure rule uses the last two on purpose.
UNGUARDED_SETTERS = (
    "set_project_field_value",
    "set_issue_status",
    "set_field_with_retry",
    "clear_project_field_value",
)

#: The closure function's boundaries in the script, used to slice the two rules
#: apart. Assertions about one rule are made against its own slice rather than
#: the whole file -- a file-wide claim about either is an invariant by proxy.
CLOSURE_START = "apply_closure_rule() {"
CLOSURE_END = 'if [[ "$MODE" == "closure" ]]'


def _closure_rule(body: str) -> str:
    """The closure function's body."""
    return body[body.index(CLOSURE_START) : body.index(CLOSURE_END)]


def _fill_path(body: str) -> str:
    """Everything except the closure function's body."""
    return body[: body.index(CLOSURE_START)] + body[body.index(CLOSURE_END) :]


class TestShellSuite:
    def test_issue_hygiene_suite_passes(self):
        suite = ROOT / "tests" / "test_issue_hygiene.sh"
        result = subprocess.run(
            ["bash", str(suite)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestLibraryHelpers:
    def test_guard_helper_exists(self):
        assert "fill_project_field_if_empty()" in LIB.read_text(encoding="utf-8")

    def test_guard_reads_immediately_before_writing(self):
        """The read that decides must be inside the helper, not the caller's.

        A caller-side check is what the pre-#3816 job had: by the time its
        write went out, the value it checked could be several API calls old.
        """
        body = LIB.read_text(encoding="utf-8")
        start = body.index("fill_project_field_if_empty()")
        end = body.index("set_issue_status()", start)
        block = body[start:end]
        assert "project_field_value" in block
        assert block.index("project_field_value") < block.index("set_project_field_value")

    def test_lookup_only_item_finder_exists(self):
        """ "Not on the board" and "on the board, no Status" must be tellable apart."""
        body = LIB.read_text(encoding="utf-8")
        assert "find_issue_project_item()" in body
        # ensure_issue_in_project keeps the add behaviour, built on the finder.
        start = body.index("ensure_issue_in_project()")
        end = body.index("# Project field reads", start)
        block = body[start:end]
        assert "find_issue_project_item" in block
        assert "addProjectV2ItemById" in block


class TestHygieneScript:
    def test_no_unguarded_project_field_write_in_the_fill_path(self):
        """Every field write in the FILL path goes through the guard.

        Scoped to the fill path, and widened to the setters the closure rule
        uses (#3871). Stating this over the whole file would be an invariant
        by proxy: the closure rule writes unguarded on purpose, so a file-wide
        claim is either false or (as the pre-#3871 setter list was) true only
        because it names setters the closure path happens not to use.
        """
        for line in _fill_path(HYGIENE.read_text(encoding="utf-8")).splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for setter in UNGUARDED_SETTERS:
                assert (
                    setter not in stripped
                ), f"{HYGIENE.name}: the fill path writes via {setter}: {stripped}"

    @pytest.mark.parametrize("field", FIELDS)
    def test_every_field_is_guarded(self, field):
        body = HYGIENE.read_text(encoding="utf-8")
        assert f'fill_project_field_if_empty "$ITEM_ID" "{field}"' in body or (
            field == "Status" and 'fill_project_field_if_empty "$ITEM_ID" "$STATUS_FIELD"' in body
        )

    def test_milestone_is_re_read_before_its_edit(self):
        """Milestone is an issue attribute, so it needs the same re-read by hand.

        Scoped to the FILL path: the closure rule also edits a milestone, but
        it does so deliberately and unconditionally (Phase X is the marker it
        exists to apply), so the no-clobber ordering does not apply there.
        """
        body = _fill_path(HYGIENE.read_text(encoding="utf-8"))
        assert "RECHECK_MILESTONE" in body
        assert body.index("RECHECK_MILESTONE") < body.index('gh issue edit "$ISSUE" --milestone')

    def test_labels_are_re_read_before_the_add(self):
        """The two-label deadlock rule (#3390/#3413/#3415) reads labels late."""
        body = HYGIENE.read_text(encoding="utf-8")
        assert body.index("LABELS_JSON=$(gh api") < body.index('gh issue edit "$ISSUE" --add-label')

    def test_settle_wait_is_configurable_and_defaults_to_60s(self):
        body = HYGIENE.read_text(encoding="utf-8")
        assert 'SETTLE_SECONDS="${HYGIENE_SETTLE_SECONDS:-60}"' in body

    def test_no_author_based_exemption(self):
        """The #3517 SCRUM_AGENT skip was a narrow patch on this same race.

        With every field guarded it is redundant, and it also made hygiene
        skip issues that genuinely needed filling.

        Scoped to the fill path (#3871). This used to ban the string
        `SCRUM_AGENT` from the whole file, which stated the invariant by
        proxy: what must not exist is an author-based *exemption* from
        filling. The closure rule names the agent logins for an unrelated
        reason -- it takes a closed, non-completed issue off the agents and
        puts it on the owner -- so the ban now covers the path it is about.
        """
        body = HYGIENE.read_text(encoding="utf-8")
        assert "SCRUM_AGENT" not in _fill_path(body)
        assert "issue.user.login" not in body


class TestWorkflowWiring:
    def test_hygiene_job_runs_the_script(self):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["add-to-project"]["steps"]
        assert any("ensure_issue_hygiene.sh" in (step.get("run") or "") for step in steps)

    def test_no_inline_field_writing_left_in_the_workflow(self):
        """Inline workflow bash cannot be executed by a test."""
        body = WORKFLOW.read_text(encoding="utf-8")
        add_to_project = body[: body.index("  pr-hygiene:")]
        assert "set_project_field_value" not in add_to_project
        assert "set_issue_status" not in add_to_project

    def test_smoke_job_exists_and_runs_the_suite(self):
        workflow = yaml.safe_load(SMOKE.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["no-clobber"]["steps"]
        assert any("tests/test_issue_hygiene.sh" in (step.get("run") or "") for step in steps)

    def test_hygiene_never_fires_on_edited_issues(self):
        """Re-running the fill on every edit would re-open the window on live
        issues. `closed` was added by #3871 for the closure rule, which is a
        different job with a different invariant; `edited` still must not be
        there."""
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True))
        assert set(triggers["issues"]["types"]) == {"opened", "closed"}

    def test_the_fill_job_only_runs_on_opened(self):
        """Adding the `closed` trigger must not put the fill-if-missing job on
        the closure path -- it would stamp defaults onto a closed issue."""
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        condition = str(workflow["jobs"]["add-to-project"]["if"])
        assert "github.event.action == 'opened'" in condition


class TestClosureRule:
    """The non-completed closure rule (#3871): the milestone is the marker.

    An issue closed as anything but a completion ends up carrying the
    `Phase X: Rejected` milestone, with the owner as its sole assignee and
    EVERY project field cleared -- Status, Priority, Effort, Module, Phase,
    Sprint. That is the whole purpose of Phase X (owner rule, 2026-08-19):
    take dead issues out of the picture. A field left behind keeps them in
    somebody's lane, sprint or statistic.

    Deliberately not fill-if-missing -- it overwrites, because a closure that
    was not a completion is a decision the board has to reflect. Status IS
    written here, and only here: the D-001/D-008 rule that lane placement is
    owner signal governs *live* work, and a rejected issue has no lane to be
    signalled about. What it may never do is create the milestone if it is
    absent -- agents do not create project metadata, so that fails loudly.
    """

    def test_the_closure_job_runs_the_script_in_closure_mode(self):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        steps = workflow["jobs"]["closure-hygiene"]["steps"]
        assert any("ensure_issue_hygiene.sh --closure" in (step.get("run") or "") for step in steps)

    def test_the_closure_job_is_gated_on_a_non_completed_closure(self):
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        condition = str(workflow["jobs"]["closure-hygiene"]["if"])
        assert "github.event.action == 'closed'" in condition
        assert "github.event.issue.state_reason != 'completed'" in condition

    def test_the_closure_job_does_not_fire_on_a_null_state_reason(self):
        """The cheap filter has to cover the pipeline's commonest closure.

        `gh issue close` after a merge records `state_reason: null` (live on
        #3917/#3918/#3919), which is not `'completed'` -- so without this
        clause the job would spin a runner and check out the release branch on
        every routine merge, only to exit at the script's null branch. The
        script keeps its own null test for by-hand invocations.
        """
        workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        condition = str(workflow["jobs"]["closure-hygiene"]["if"])
        assert "github.event.issue.state_reason != null" in condition

    def test_the_null_reason_is_also_handled_in_the_script(self):
        body = _closure_rule(HYGIENE.read_text(encoding="utf-8"))
        assert 'if [[ -z "$reason" ]]' in body

    def test_the_smoke_guard_states_an_invariant_the_script_holds(self):
        """The CI guard must be scoped to the fill path, not the whole file.

        Before this review round it asserted "every field write in the hygiene
        script goes through the re-read guard" over the entire file while the
        closure path deliberately writes unguarded -- green, and false, because
        its grep named only setters the closure path happens not to use. The
        fix slices `apply_closure_rule` out and bans the closure rule's own
        setters everywhere else, so passing means what the text says.
        """
        body = SMOKE.read_text(encoding="utf-8")
        assert "apply_closure_rule" in body, "the guard no longer slices the closure rule out"
        for setter in UNGUARDED_SETTERS:
            assert setter in body, f"the guard no longer bans {setter} from the fill path"

    def test_the_reason_test_is_also_in_the_script(self):
        """The workflow `if:` is the cheap filter; the executable test is what
        a suite can run, and what protects a by-hand invocation."""
        body = HYGIENE.read_text(encoding="utf-8")
        assert 'if [[ "$reason" == "completed" ]]' in body

    def test_the_closure_rule_strips_every_project_field(self):
        """Phase X takes a dead issue out of the picture (owner rule,
        2026-08-19): the milestone is the only marker it keeps. A rejected
        issue holding a Status still sits in a lane, one holding a Sprint
        still counts against that sprint's population, and one holding
        Priority/Effort/Module still skews the statistics computed over those
        fields.

        Status is cleared here, and this is the one rule that writes it. The
        D-001/D-008 rule that lane placement is owner signal governs *live*
        work; an issue closed as not-planned has no lane to be signalled
        about, and leaving it in one is the debris this removes.
        """
        closure = _closure_rule(HYGIENE.read_text(encoding="utf-8"))
        assert 'for field in "$STATUS_FIELD" Priority Effort Module Phase Sprint' in closure
        assert "clear_project_field_value" in closure
        assert "set_issue_status" not in closure

    def test_the_marker_is_the_milestone_not_a_project_field(self):
        """Phase X is a GitHub milestone (`Phase X: Rejected`), which is what
        `EXCLUDED_MILESTONES` in the retrospective reads. Setting a project
        `Phase` field instead would leave the issue in the picture it is
        supposed to leave."""
        closure = _closure_rule(HYGIENE.read_text(encoding="utf-8"))
        assert "Phase X: Rejected" in closure
        assert "--milestone" in closure
        assert 'set_field_with_retry "$item_id" "Phase"' not in closure

    def test_the_closure_rule_refuses_a_missing_milestone(self):
        """Agents never create project metadata; a silent skip would leave the
        issue looking handled with no marker on it at all."""
        closure = _closure_rule(HYGIENE.read_text(encoding="utf-8"))
        assert "milestones?state=all" in closure
        assert "does not exist" in closure

    def test_the_closure_rule_uses_the_lookup_only_item_finder(self):
        """A closed issue that was never on the board must not be added to it."""
        closure = _closure_rule(HYGIENE.read_text(encoding="utf-8"))
        assert "find_issue_project_item" in closure
        assert "ensure_issue_in_project" not in closure
