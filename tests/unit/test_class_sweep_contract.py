"""Fixes cover the defect class, and the process asks (#4183).

The mechanism this pins: an issue states a symptom, its criteria are written per
symptom, the developer passes each one, and review checks the criteria -- so
nobody is asked what *class* the defect belongs to or where else that class
lives, and the sweep never happens. `CLAUDE.md` first principle 2 already said a
narrow patch on a general defect "has not finished the job"; nothing enforced
it, which is why #4136 was the third occurrence of one stale-cloud-record
mechanism, #4135 passed every criterion and failed acceptance on the rest of the
same `ops status` output, #4179 added an error class to one of two copies of the
classifier, and #4174 shipped a rule whose guard script no workflow ran.

Owner decision 2026-10-09 (ledger **D-067**) puts four obligations in place, one
per role. Prose and prompts are most of the mechanism, so their absence *is* the
regression -- these tests fail the build if any of them is dropped:

* issues name their class and surfaces (`CLAUDE.md`, the scrummaster surfaces,
  and both acceptance-handler issue-body templates);
* the developer sweeps the class before implementing, and hands the sweep over
  (developer charter, runbook §3i, and all three implement prompts);
* the same decision is consolidated rather than corrected N times;
* review blocks an unswept fix (review charter, runbook §1e, review prompts),
  and out-of-class findings are filed rather than dropped.

`tests/unit/test_class_sweep.py` covers the helper that carries the sweep into
the PR body; this file covers the contract text that asks for it at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
DEVELOPER_CHARTER = REPO_ROOT / "agents" / "charters" / "developer-agent.md"
REVIEW_CHARTER = REPO_ROOT / "agents" / "charters" / "review-agent.md"
SCRUM_CHARTER = REPO_ROOT / "agents" / "charters" / "scrummaster-agent.md"
DEVELOPER_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "developer-runbook.md"
REVIEW_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "review-runbook.md"
SCRUM_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "scrummaster-runbook.md"
DEVELOPER_PROMPT = REPO_ROOT / "agents" / "prompts" / "developer-agent.prompt.md"
REVIEW_PROMPT = REPO_ROOT / "agents" / "prompts" / "review-agent.prompt.md"
SCRUM_PROMPT = REPO_ROOT / "agents" / "prompts" / "scrummaster-agent.prompt.md"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "developer_auto_implement.yml"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-code-review.yml"
ACCEPTANCE_HANDLER = REPO_ROOT / ".github" / "workflows" / "handle_acceptance_failure.yml"
IMPROVEMENT_HANDLER = REPO_ROOT / ".github" / "workflows" / "handle_improvement.yml"
SUBMIT_SCRIPT = REPO_ROOT / "scripts" / "agents" / "developer_submit_for_review.sh"
LEDGER = REPO_ROOT / "agents" / "LEDGER.md"

#: The section every issue body template must offer.
ISSUE_SECTION = "## Defect class and surfaces"

#: The PR-body section the reviewer reads, and the marker it is found by.
PR_SECTION = "## Class sweep"
SWEEP_MARKER = "<!-- nyxgpt-class-sweep -->"

#: Every surface that must keep the issue number, so the rule keeps its "why".
ISSUE_CITERS = (
    CLAUDE_MD,
    DEVELOPER_CHARTER,
    REVIEW_CHARTER,
    SCRUM_CHARTER,
    DEVELOPER_RUNBOOK,
    REVIEW_RUNBOOK,
    SCRUM_RUNBOOK,
    DEVELOPER_PROMPT,
    REVIEW_PROMPT,
    SCRUM_PROMPT,
    IMPLEMENT_WORKFLOW,
    REVIEW_WORKFLOW,
    ACCEPTANCE_HANDLER,
    IMPROVEMENT_HANDLER,
)

#: The two issue-body templates automation writes. They are mirror images of
#: each other by design, and a change applied to one and not the other is how
#: they diverge (#3999), so both are asserted identically.
HANDLERS = (ACCEPTANCE_HANDLER, IMPROVEMENT_HANDLER)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing contract file: {path}"
    return path.read_text(encoding="utf-8")


def _flowed(text: str) -> str:
    """Line-wrap-insensitive view, so reflowing prose cannot fail a test."""
    return " ".join(text.split())


@pytest.fixture(scope="module")
def claude_md() -> str:
    return _read(CLAUDE_MD)


@pytest.fixture(scope="module")
def developer_runbook() -> str:
    return _read(DEVELOPER_RUNBOOK)


@pytest.fixture(scope="module")
def review_runbook() -> str:
    return _read(REVIEW_RUNBOOK)


@pytest.fixture(scope="module")
def implement_workflow() -> str:
    return _read(IMPLEMENT_WORKFLOW)


@pytest.fixture(scope="module")
def review_workflow() -> str:
    return _read(REVIEW_WORKFLOW)


# --------------------------------------------------------------------------
# 1. Issues name their class and surfaces
# --------------------------------------------------------------------------


def test_claude_md_issue_template_requires_the_section(claude_md: str) -> None:
    body = claude_md.split("**Body Structure:**", 1)[1].split("**Required Project Fields:**", 1)[0]
    assert ISSUE_SECTION in body, (
        "CLAUDE.md's issue body structure must carry the "
        f"'{ISSUE_SECTION}' section -- an issue that never names its class is "
        "an issue whose fix will be scoped to one instance (#4183)"
    )
    flowed = _flowed(body)
    assert "stated as a rule" in flowed, (
        "the template must ask for the CLASS as a rule; a location ('line 812 "
        "breaks on apostrophes') is what makes the fix narrow"
    )
    assert "same decision" in flowed, (
        "the surfaces list must include other code paths making the same "
        "decision -- #4136's AWS account was chosen by three of them"
    )


def test_claude_md_defines_the_rule_once_with_all_four_obligations(claude_md: str) -> None:
    assert "### The class, not the instance" in claude_md, (
        "CLAUDE.md must carry the owner decision as a named section; it is the "
        "single source the charters, runbooks and prompts cite"
    )
    section = claude_md.split("### The class, not the instance", 1)[1]
    section = section.split("\n## ", 1)[0]
    flowed = _flowed(section)

    for obligation in (
        "Defect class and surfaces",  # the issue's obligation
        "sweeps before implementing",  # the developer's
        "One source per decision",  # the consolidation rule
        "Medium (blocking)",  # the reviewer's
    ):
        assert obligation in flowed, (
            f"CLAUDE.md's class rule is missing '{obligation}' -- all four "
            "obligations (issue, developer, consolidation, review) are what "
            "make the rule enforceable rather than aspirational"
        )

    assert "Out-of-class findings are filed, not dropped" in flowed, (
        "out-of-class findings must be filed, not silently fixed and not "
        "dropped -- that is the knowledge this mechanism exists to keep"
    )
    # Symmetry, or the gate becomes a tax and gets routed around.
    assert "found nothing satisfies it" in flowed, (
        "the rule must state its own limits: a reported search that found "
        "nothing satisfies the gate"
    )


@pytest.mark.parametrize("path", HANDLERS, ids=lambda p: p.name)
def test_acceptance_handlers_file_the_section_as_a_checklist(path: Path) -> None:
    """The owner reports a symptom; the handler cannot know the class.

    So the filed body carries the section as unchecked checkbox items for the
    developer to complete, not as prose nobody owns.
    """
    text = _read(path)
    body_template = text.split('echo "## Problem / Motivation"', 1)[1]
    body_template = body_template.split("ACTIVE_SPRINT=", 1)[0]

    assert ISSUE_SECTION in body_template, (
        f"{path.name} must write the '{ISSUE_SECTION}' section into the issue "
        "body it files (#4183)"
    )
    flowed = _flowed(body_template)
    assert "- [ ] Class:" in body_template and "- [ ] Surfaces:" in body_template, (
        f"{path.name} must file the class and surfaces as UNCHECKED checklist "
        "items -- the developer completes them before implementing"
    )
    assert "developer completes this before implementing" in flowed, (
        f"{path.name} must say who completes the section, or it reads as a "
        "field the handler failed to fill in"
    )
    assert "Class sweep" in flowed, (
        f"{path.name}'s acceptance criteria must require the PR's class-sweep "
        "record, so the criterion and the review gate agree"
    )


def test_scrummaster_surfaces_own_the_section() -> None:
    for path in (SCRUM_CHARTER, SCRUM_PROMPT, SCRUM_RUNBOOK):
        flowed = _flowed(_read(path))
        assert "Defect class and surfaces" in flowed, (
            f"{path.relative_to(REPO_ROOT)} must require the class/surfaces "
            "section on every issue the scrummaster files or grooms (#4183)"
        )
        assert "Unknown" in flowed, (
            f"{path.relative_to(REPO_ROOT)} must give the escape hatch "
            "('Unknown -- the developer completes this') so an unknown class "
            "cannot become a dropped section"
        )


# --------------------------------------------------------------------------
# 2. The developer sweeps the class
# --------------------------------------------------------------------------


def test_developer_runbook_defines_the_sweep(developer_runbook: str) -> None:
    assert "## 3i) Class sweep" in developer_runbook, (
        "developer-runbook.md must define the class sweep as a procedure; §3's "
        "one-line mention is the statement, not the how"
    )
    section = developer_runbook.split("## 3i)", 1)[1].split("## 4)", 1)[0]
    flowed = _flowed(section)

    assert "before you implement" in flowed.lower(), (
        "the sweep happens BEFORE implementing -- after the fact it is a "
        "justification, not a search"
    )
    assert "One source per decision" in section, (
        "the runbook must require consolidation where the same decision is "
        "made in more than one place, not N corrected copies (#4179)"
    )
    assert "/tmp/class-sweep.md" in flowed, (
        "the runbook must name the hand-off file; the developer does not own "
        "the PR body and cannot write the section directly"
    )
    assert SWEEP_MARKER in section, "the runbook must name the marker the review prompt greps for"
    assert "NOT PROVIDED" in flowed, (
        "the runbook must say what happens when the sweep is skipped -- a "
        "visible receipt, not silence"
    )
    assert "Out-of-class findings are filed" in section, (
        "a different class found during the sweep is filed as its own issue, "
        "neither silently fixed nor dropped"
    )
    assert "#4174" in flowed, (
        "the runbook must name the rules-without-a-guard class: a rule this "
        "project states and does not enforce is itself an instance"
    )


def test_minimality_is_measured_against_the_class(
    developer_runbook: str, implement_workflow: str
) -> None:
    """"Make the minimal change" is what invited the narrow patch.

    Both places that ask for a minimal change must say minimal *against the
    class* -- otherwise the implement instruction contradicts the gate that
    reviews it, and the instruction is the one the developer reads first.
    """
    for name, text in (("developer-runbook.md", developer_runbook), ("implement prompt", implement_workflow)):
        flowed = _flowed(text)
        assert "measured against the class" in flowed, (
            f"{name} asks for a minimal/smallest change without saying it is "
            "measured against the class, not against the single instance the "
            "issue named (#4183)"
        )


def test_developer_charter_and_prompt_carry_the_obligation() -> None:
    for path in (DEVELOPER_CHARTER, DEVELOPER_PROMPT):
        flowed = _flowed(_read(path))
        assert "class" in flowed.lower() and "sweep" in flowed.lower(), (
            f"{path.relative_to(REPO_ROOT)} must carry the class-sweep "
            "obligation (#4183)"
        )
        assert "consolidat" in flowed.lower(), (
            f"{path.relative_to(REPO_ROOT)} must carry the one-source-per-"
            "decision rule: consolidate, do not correct each copy"
        )
        assert "§3i" in flowed or "3i" in flowed, (
            f"{path.relative_to(REPO_ROOT)} must cite developer-runbook §3i "
            "rather than restating the procedure"
        )


@pytest.mark.parametrize(
    "anchor",
    [
        "## Step 1.5: CLASS SWEEP",  # the initial implementation prompt
        "CLASS SWEEP FIRST",  # the acceptance-failure fix prompt
        "Each finding is an INSTANCE",  # the review-fix prompt
    ],
)
def test_every_developer_prompt_asks_for_the_sweep(implement_workflow: str, anchor: str) -> None:
    """All three fix/implement paths, because a class returns through any of them.

    #4135 and #4136 both arrived through the acceptance path, and a review
    finding is itself an instance -- a gate installed on the initial prompt
    alone would miss the two rounds where the class actually recurred.
    """
    assert anchor in implement_workflow, (
        f"the developer prompt block '{anchor}' is missing from "
        "developer_auto_implement.yml -- that path can ship an unswept fix"
    )


def test_implement_prompt_names_the_handoff_and_the_consolidation_rule(
    implement_workflow: str,
) -> None:
    flowed = _flowed(implement_workflow)
    assert "/tmp/class-sweep.md" in flowed, (
        "the implement prompt must name the file the developer writes; "
        "'record it in the PR body' is an instruction it cannot carry out"
    )
    assert "ONE SOURCE PER DECISION" in implement_workflow, (
        "the implement prompt must require consolidation where the sweep finds "
        "the same decision in several places (#4179, ledger D-066)"
    )
    assert "OUT-OF-CLASS" in implement_workflow, (
        "the implement prompt must require out-of-class findings to be filed "
        "and named rather than silently fixed or dropped"
    )


def test_submit_script_carries_or_reports_the_sweep() -> None:
    text = _read(SUBMIT_SCRIPT)
    assert "class_sweep.py" in text, (
        "developer_submit_for_review.sh must put the sweep into the PR body "
        "through the one helper, not with its own copy of the decision"
    )
    assert "NYXGPT_CLASS_SWEEP_FILE" in text, "the hand-off path must be overridable for tests"
    assert "not-provided" in text, (
        "the submit path must report a missing sweep out loud; a silent skip is "
        "the failure mode #4183 exists to close"
    )


def test_existing_pr_path_refreshes_the_sweep(implement_workflow: str) -> None:
    """A review-fix round never calls the submit script.

    Without this, a second round's PR body keeps the first round's sweep (or its
    NOT PROVIDED receipt) while the reviewer is asked to decide on the rewritten
    change -- a stale artifact is worse than none, because it reads as current.
    """
    step = implement_workflow.split("- name: Request review for existing PR", 1)[1]
    step = step.split("- name: Post review requested comment", 1)[0]
    assert "class_sweep.py" in step, (
        "the existing-PR handoff must refresh the class-sweep section from the "
        "same helper the submit path uses (#4183)"
    )


# --------------------------------------------------------------------------
# 3. Review blocks an unswept fix
# --------------------------------------------------------------------------


def test_review_runbook_core_requirements_list_the_gate(review_runbook: str) -> None:
    core = review_runbook.split("### Core Requirements", 1)[1].split("###", 1)[0]
    assert "Class-sweep gate" in core, (
        "the Core Requirements checklist must list the class-sweep gate "
        "alongside the inverse-claims and executed-verification gates"
    )
    assert "Medium (blocking)" in core


def test_review_runbook_generality_gate_reads_the_artifact(review_runbook: str) -> None:
    section = review_runbook.split("## 1e)", 1)[1].split("## 1f)", 1)[0]
    flowed = _flowed(section)

    assert "#4183" in flowed, "the gate must cite the decision that made the sweep an artifact"
    assert SWEEP_MARKER in section, (
        "the reviewer must be told the marker to look for; 'notice the class' "
        "is what the gate did before #4183 and it is why #4136 shipped twice more"
    )
    assert "NOT PROVIDED" in flowed, (
        "the reviewer must be told that a missing sweep is stated explicitly, "
        "so it is never something they have to infer"
    )
    for condition in (
        "missing or incomplete",
        "sibling instance left unfixed",
        "corrected in N copies",
    ):
        assert condition in flowed, (
            f"the gate's blocking conditions must name '{condition}' -- a gate "
            "without stated conditions is applied inconsistently"
        )
    assert "in scope by definition" in flowed, (
        "a consolidation on a class-sweep fix must be declared in scope, or "
        "the reviewer blocks the very fix the gate asked for"
    )
    assert "Medium (blocking)" in flowed


@pytest.mark.parametrize("path", [REVIEW_CHARTER, REVIEW_PROMPT], ids=lambda p: p.name)
def test_review_surfaces_make_an_unswept_fix_blocking(path: Path) -> None:
    flowed = _flowed(_read(path))
    assert "Class sweep" in flowed, (
        f"{path.relative_to(REPO_ROOT)} must name the PR section the reviewer "
        "reads (#4183)"
    )
    assert "Medium (blocking)" in flowed
    assert "§1e" in flowed or "1e" in flowed, (
        f"{path.relative_to(REPO_ROOT)} must cite review-runbook §1e, where "
        "the gate is defined once"
    )


def test_review_prompt_blocks_approve_on_a_missing_sweep(review_workflow: str) -> None:
    mandatory = review_workflow.split("MANDATORY: You MUST REQUEST_CHANGES if:", 1)[1]
    mandatory = mandatory.split("Do not APPROVE until", 1)[0]
    assert "Class sweep" in mandatory, (
        "a missing or incomplete class sweep must appear in the "
        "REQUEST_CHANGES mandatory list, not only in the guidance prose -- "
        "guidance is what the gate was before #4183"
    )


def test_review_prompt_has_the_gate_and_the_report_section(review_workflow: str) -> None:
    assert "## Class-sweep gate (REQUIRED" in review_workflow, (
        "the review prompt must instruct the reviewer to run the class-sweep "
        "gate, the way it already does for §1a and §1c"
    )
    template = review_workflow.split("### Class Sweep", 1)[1].split("### Executed Verification", 1)[0]
    for field in (
        "Class this change belongs to",
        "section present in the PR body",
        "Search you ran yourself",
        "Sibling instances you found still unfixed",
        "Out-of-class findings filed and named",
    ):
        assert field in template, (
            f"the review body template must report '{field}', so a clean "
            "result is distinguishable from a gate that never ran"
        )
    assert "BLOCKS APPROVE" in template


def test_review_body_reports_the_sweep_section(review_runbook: str) -> None:
    section = review_runbook.split("## 4) Review and recommendation", 1)[1]
    section = section.split("## 5)", 1)[0]
    assert "### Class Sweep" in section, (
        "the review's own output must carry a Class Sweep section, for the "
        "same reason the Code Scanning one exists: an unreported check is "
        "indistinguishable from one that never ran"
    )


# --------------------------------------------------------------------------
# 4. Recorded, and citing its why
# --------------------------------------------------------------------------


def test_the_decision_is_in_the_ledger() -> None:
    text = _read(LEDGER)
    assert "**D-067**" in text, (
        "the owner decision of 2026-10-09 must be a ledger decision entry -- a "
        "rule only in prompts is a rule the next session re-derives (#3774)"
    )
    entry = text.split("**D-067**", 1)[1].split("\n- **", 1)[0]
    flowed = _flowed(entry)
    assert "#4183" in flowed, "the entry must cite its issue"
    assert "Source:" in entry, "a decision entry carries its Source line (ledger entry schema)"


@pytest.mark.parametrize("path", ISSUE_CITERS, ids=lambda p: p.name)
def test_every_surface_keeps_the_issue_reference(path: Path) -> None:
    """A rule whose 'why' is gone is a rule the next session deletes as noise."""
    assert "#4183" in _read(path), (
        f"{path.relative_to(REPO_ROOT)} must cite #4183 so the class-sweep "
        "requirement keeps its rationale"
    )
