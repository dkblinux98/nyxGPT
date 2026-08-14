"""Fixes must be demonstrated by execution, not by inspection (#3775).

Review in this pipeline means reading code. Reading a Homebrew formula does not
reveal `ensurepip` exit 1 on stock Homebrew or `platform.mac_ver()` answering
empty (#3753), reading cloud-init does not reveal a missing `npm` (#3761), and
reading a path helper does not reveal that it resolves relative to a repo the
target does not have (#3759). All four passed review and all four were found by
the owner running the install once.

These tests pin the class fix -- the executed-verification gate: the review
contract requires executed evidence on runtime/install/platform claims and
blocks at Medium without it, the developer contract requires producing that
evidence (adding a smoke job when none covers the path), and the exemption for
pure-logic changes covered by unit tests is stated explicitly. Prompt/runbook
prose is the only mechanism here, so its absence is the regression.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
REVIEW_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "review-runbook.md"
DEVELOPER_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "developer-runbook.md"
REVIEW_PROMPT = REPO_ROOT / "agents" / "prompts" / "review-agent.prompt.md"
DEVELOPER_PROMPT = REPO_ROOT / "agents" / "prompts" / "developer-agent.prompt.md"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-code-review.yml"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "developer_auto_implement.yml"

# The smoke workflows the contract cites as evidence templates. A citation is
# worthless if the workflow it names does not exist.
SMOKE_WORKFLOWS = (
    ".github/workflows/macos-brew-smoke.yml",
    ".github/workflows/linux-native-smoke.yml",
    ".github/workflows/terraform-local-smoke.yml",
)

# Files that must cite the motivating defects so the rule keeps its "why".
INCIDENT_CITERS = (
    CLAUDE_MD,
    REVIEW_RUNBOOK,
    DEVELOPER_RUNBOOK,
    REVIEW_PROMPT,
    REVIEW_WORKFLOW,
    IMPLEMENT_WORKFLOW,
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing contract file: {path}"
    return path.read_text(encoding="utf-8")


def _flowed(text: str) -> str:
    """Collapse whitespace so wrapped prose matches regardless of line breaks."""
    return " ".join(text.split())


@pytest.fixture(scope="module")
def claude_md() -> str:
    return _read(CLAUDE_MD)


@pytest.fixture(scope="module")
def review_runbook() -> str:
    return _read(REVIEW_RUNBOOK)


@pytest.fixture(scope="module")
def developer_runbook() -> str:
    return _read(DEVELOPER_RUNBOOK)


@pytest.fixture(scope="module")
def review_prompt() -> str:
    return _read(REVIEW_PROMPT)


@pytest.fixture(scope="module")
def developer_prompt() -> str:
    return _read(DEVELOPER_PROMPT)


@pytest.fixture(scope="module")
def review_workflow() -> str:
    return _read(REVIEW_WORKFLOW)


@pytest.fixture(scope="module")
def implement_workflow() -> str:
    return _read(IMPLEMENT_WORKFLOW)


# --- Definition of Done -------------------------------------------------


def test_definition_of_done_requires_executed_verification(claude_md: str) -> None:
    section = claude_md.split("## Definition of Done", 1)[1]
    section = section.split("## Operational Command Wrapping", 1)[0]
    flowed = _flowed(section)
    assert "Executed verification" in section, (
        "the Definition of Done must state the executed-verification "
        "requirement -- it is where both agents' contracts point"
    )
    assert "Medium (blocking) finding" in flowed, (
        "missing executed evidence must be blocking in the Definition of "
        "Done, consistent with the end-to-end usability rule beside it"
    )
    assert (
        "fully covered by unit tests" in flowed
    ), "the Definition of Done must state the pure-logic/unit-tested exemption"


# --- Review contract ----------------------------------------------------


def test_review_runbook_has_executed_verification_section(review_runbook: str) -> None:
    assert "## 1c) Executed-verification gate" in review_runbook, (
        "review-runbook.md must carry a dedicated executed-verification "
        "section; inspection cannot see install/runtime failures"
    )


def _runbook_gate_section(review_runbook: str) -> str:
    section = review_runbook.split("## 1c)", 1)[1].split("## 2)", 1)[0]
    return _flowed(section)


def test_review_runbook_makes_missing_evidence_blocking(review_runbook: str) -> None:
    section = _runbook_gate_section(review_runbook)
    assert (
        "Medium (blocking)" in section
    ), "missing executed evidence must be a Medium (blocking) finding, not advisory prose"


def test_review_runbook_defines_what_counts_as_evidence(review_runbook: str) -> None:
    section = _runbook_gate_section(review_runbook)
    for workflow in SMOKE_WORKFLOWS:
        name = Path(workflow).name
        assert name in section, (
            f"the gate must name {name} as accepted executed evidence so the "
            "reviewer knows an existing CI job satisfies it"
        )
    assert "workflow_dispatch" in section, "a dispatched workflow run must count as evidence"
    assert (
        "target platform" in section
    ), "the evidence must be required to run on the platform the claim is about"


def test_review_runbook_states_the_exemption(review_runbook: str) -> None:
    section = _runbook_gate_section(review_runbook)
    assert "Pure-logic changes fully covered by unit tests" in section, (
        "pure-logic changes covered by unit tests must be explicitly exempt -- "
        "the gate targets claims unit tests cannot reach"
    )
    assert "live-verification-ci.md" in section, (
        "the gate must defer to the documented not-CI-coverable list rather "
        "than demanding structurally impossible evidence"
    )


def test_review_runbook_requires_fault_injection(review_runbook: str) -> None:
    section = _runbook_gate_section(review_runbook)
    assert "inject" in section.lower(), (
        "when the runner does not reproduce the failure the evidence must "
        "inject the condition (#3753 round two)"
    )


def test_review_runbook_core_requirements_reference_the_gate(review_runbook: str) -> None:
    core = review_runbook.split("### Core Requirements", 1)[1].split("###", 1)[0]
    assert (
        "Executed-verification gate" in core
    ), "the Core Requirements checklist must list the executed-verification gate"


def test_review_prompt_carries_the_gate(review_prompt: str) -> None:
    assert (
        "EXECUTED VERIFICATION" in review_prompt
    ), "the review agent prompt's severity model must carry the executed-verification entry"
    flowed = _flowed(review_prompt)
    assert (
        "Medium (blocking)" in flowed
    ), "the review agent prompt must make missing executed evidence blocking"
    assert "Executed Verification" in review_prompt, (
        "the review agent prompt must require an '### Executed Verification' "
        "section in the posted review"
    )


def test_review_workflow_prompt_runs_the_gate(review_workflow: str) -> None:
    assert (
        "Executed-verification gate (REQUIRED" in review_workflow
    ), "the review prompt must instruct the reviewer to run the executed-verification gate"
    flowed = _flowed(review_workflow)
    assert "Inspection is not evidence" in flowed or "inspection alone is not evidence" in flowed


def test_review_workflow_blocks_approve_without_executed_evidence(review_workflow: str) -> None:
    mandatory = review_workflow.split("MANDATORY: You MUST REQUEST_CHANGES if:", 1)[1]
    mandatory = mandatory.split("Do not APPROVE until", 1)[0]
    flowed = _flowed(mandatory)
    assert "executed evidence" in flowed, (
        "a runtime claim with no executed evidence must appear in the "
        "REQUEST_CHANGES mandatory list, not only in the guidance prose"
    )


def test_review_body_template_reports_the_evidence(review_workflow: str) -> None:
    template = review_workflow.split("### Executed Verification", 1)[1]
    template = template.split("### Findings", 1)[0]
    for field in (
        "Runtime/install/platform claim in this PR",
        "Target platform(s) the claim is about",
        "Executed evidence cited",
        "Failure reproduced without the fix",
    ):
        assert field in template, (
            f"the review body template must report '{field}' so an unexecuted "
            "claim is distinguishable from a check that never ran"
        )
    assert "BLOCKS APPROVE" in template


# --- Developer contract -------------------------------------------------


def test_developer_runbook_has_executed_verification_section(developer_runbook: str) -> None:
    assert "## 4a) Executed verification" in developer_runbook, (
        "the developer runbook must require executed evidence as part of "
        "implementation, right after the validation loop"
    )
    section = developer_runbook.split("## 4a)", 1)[1].split("## 5)", 1)[0]
    flowed = _flowed(section)
    for workflow in SMOKE_WORKFLOWS:
        assert Path(workflow).name in section, (
            f"the developer contract must name {Path(workflow).name} as a "
            "smoke-workflow template"
        )
    assert "If no job covers your path, add one" in flowed, (
        "the developer must add a smoke job when none covers the changed "
        "path -- 'nothing tests this' is the finding, not the excuse"
    )
    assert (
        "fully covered by unit tests" in flowed
    ), "the developer contract must state the pure-logic/unit-tested exemption"


def test_developer_prompt_requires_the_evidence(developer_prompt: str) -> None:
    flowed = _flowed(developer_prompt)
    assert "executed evidence" in flowed, (
        "the developer agent prompt must instruct producing executed evidence "
        "for runtime/install/platform claims"
    )
    assert "#3775" in developer_prompt


def test_implement_prompt_carries_the_gate(implement_workflow: str) -> None:
    assert (
        "EXECUTED VERIFICATION" in implement_workflow
    ), "the developer implement prompt must instruct executed verification"
    flowed = _flowed(implement_workflow)
    for workflow in SMOKE_WORKFLOWS:
        assert Path(workflow).name in implement_workflow, (
            f"the implement prompt must name {Path(workflow).name} as a "
            "template for producing executed evidence"
        )
    assert (
        "Add one in THIS PR" in flowed or "add one in THIS PR" in flowed
    ), "the implement prompt must require adding a smoke job when none covers the path"


def test_fix_prompts_carry_the_gate(implement_workflow: str) -> None:
    # The acceptance-fix and review-fix cycles are exactly where the defect
    # class of #3753/#3759/#3761 is worked, so the gate must appear in every
    # prompt that fixes code, not only the initial implementation one.
    occurrences = implement_workflow.count("EXECUTED VERIFICATION")
    assert occurrences >= 3, (
        "the executed-verification instruction must appear in the initial "
        "implement prompt AND the review-fix and acceptance-fix prompts "
        f"(found {occurrences})"
    )


# --- Shared invariants --------------------------------------------------


@pytest.mark.parametrize("relpath", SMOKE_WORKFLOWS, ids=lambda p: Path(p).name)
def test_cited_smoke_workflows_exist(relpath: str) -> None:
    assert (REPO_ROOT / relpath).is_file(), (
        f"{relpath} is cited by the executed-verification contract as accepted "
        "evidence; the citation must point at a real workflow"
    )


@pytest.mark.parametrize("path", INCIDENT_CITERS, ids=lambda p: p.name)
def test_contract_cites_the_motivating_defects(path: Path) -> None:
    text = _read(path)
    assert "#3753" in text, (
        f"{path.name} must cite the #3753 defect as the motivating example "
        "so the rule keeps its rationale"
    )
