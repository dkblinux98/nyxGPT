"""The review contract must ask the *inverse* documentation question (#3744).

The documentation criterion the agents enforce is diff-scoped -- "is
documentation for this change updated?" It cannot see prose the change
*falsifies* somewhere else in the tree. #3727/#3735 shipped repo-less PyPI
publishing while `README.md` still asserted that it had not shipped and a repo
checkout was still required; dev and reviewer both passed the docs criterion
honestly because that section was outside the diff's surfaces (#3743 fixed the
instance).

These tests pin the class fix: the inverse-claims criterion in the review
runbook and review prompt, and the authoring rule against expiry-dated
world-state claims on the developer side. They assert the contract text is
present and blocking -- prompt/runbook prose is the only mechanism here, so its
absence is the regression.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

REVIEW_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "review-runbook.md"
DEVELOPER_RUNBOOK = REPO_ROOT / "agents" / "runbooks" / "developer-runbook.md"
REVIEW_PROMPT = REPO_ROOT / "agents" / "prompts" / "review-agent.prompt.md"
REVIEW_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-code-review.yml"
IMPLEMENT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "developer_auto_implement.yml"

# Files that must cite the motivating incident so the rule keeps its "why".
INCIDENT_CITERS = (
    REVIEW_RUNBOOK,
    DEVELOPER_RUNBOOK,
    REVIEW_PROMPT,
    REVIEW_WORKFLOW,
    IMPLEMENT_WORKFLOW,
)


def _read(path: Path) -> str:
    assert path.is_file(), f"missing contract file: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def review_runbook() -> str:
    return _read(REVIEW_RUNBOOK)


@pytest.fixture(scope="module")
def developer_runbook() -> str:
    return _read(DEVELOPER_RUNBOOK)


@pytest.fixture(scope="module")
def review_workflow() -> str:
    return _read(REVIEW_WORKFLOW)


@pytest.fixture(scope="module")
def implement_workflow() -> str:
    return _read(IMPLEMENT_WORKFLOW)


def test_review_runbook_has_inverse_claims_section(review_runbook: str) -> None:
    assert "## 1a) Inverse-claims check" in review_runbook, (
        "review-runbook.md must carry a dedicated inverse-claims section; the "
        "diff-scoped documentation criterion cannot see falsified prose"
    )
    # The check is worthless if it only looks at the diff.
    flowed = " ".join(review_runbook.split())
    assert (
        "not just the files in the diff" in flowed
    ), "the inverse-claims check must direct the reviewer beyond the diff"


def test_review_runbook_makes_falsified_claims_blocking(review_runbook: str) -> None:
    section = review_runbook.split("## 1a)", 1)[1].split("## 2)", 1)[0]
    section = " ".join(section.split())
    assert "Medium (blocking) finding" in section, (
        "a falsified claim left unfixed must be a Medium (blocking) finding, " "not advisory prose"
    )


def test_review_runbook_core_requirements_reference_the_check(review_runbook: str) -> None:
    core = review_runbook.split("### Core Requirements", 1)[1].split("###", 1)[0]
    assert "Inverse-claims check" in core, (
        "the Core Requirements checklist must list the inverse-claims check "
        "alongside the forward docs criterion"
    )


def test_review_workflow_prompt_runs_the_inverse_check(review_workflow: str) -> None:
    assert (
        "Inverse-claims check (REQUIRED" in review_workflow
    ), "the review prompt must instruct the reviewer to run the inverse-claims check"
    assert "what does this" in review_workflow and "make untrue elsewhere" in review_workflow, (
        "the review prompt must pose the inverse question about the change's "
        "consequences for existing prose"
    )


def test_review_workflow_blocks_approve_on_falsified_claims(review_workflow: str) -> None:
    mandatory = review_workflow.split("MANDATORY: You MUST REQUEST_CHANGES if:", 1)[1]
    mandatory = mandatory.split("Do not APPROVE until", 1)[0]
    assert "falsifies a claim" in mandatory, (
        "an unfixed falsified claim must appear in the REQUEST_CHANGES "
        "mandatory list, not only in the guidance prose"
    )


def test_review_body_template_reports_the_search(review_workflow: str) -> None:
    template = review_workflow.split("### Documentation Status", 1)[1]
    template = template.split("### Findings", 1)[0]
    for field in (
        "Tree searched for claims about it",
        "Claims falsified by this change",
        "New expiry-dated world-state claims introduced",
    ):
        assert field in template, (
            f"the review body template must report '{field}' so an empty "
            "result is distinguishable from a check that never ran"
        )
    assert "BLOCKS APPROVE" in template


def test_developer_runbook_has_inverse_claims_sweep(developer_runbook: str) -> None:
    docs_section = developer_runbook.split("## 5) Documentation", 1)[1]
    docs_section = docs_section.split("## 6)", 1)[0]
    assert "Inverse-claims sweep" in docs_section, (
        "the developer runbook's documentation section must require the "
        "inverse-claims sweep before submitting"
    )
    assert "expiry" in docs_section, (
        "the developer runbook must warn against world-state claims with " "built-in expiry"
    )


def test_implement_prompt_carries_authoring_guidance(implement_workflow: str) -> None:
    assert (
        "INVERSE-CLAIMS SWEEP" in implement_workflow
    ), "the developer implement prompt must instruct the inverse-claims sweep"
    assert (
        "NO EXPIRY-DATED WORLD-STATE CLAIMS" in implement_workflow
    ), "the developer implement prompt must forbid expiry-dated world-state claims"
    assert (
        "portability-matrix.md" in implement_workflow
    ), "authoring guidance must point at living sources instead of expiry-dated prose"


@pytest.mark.parametrize("path", INCIDENT_CITERS, ids=lambda p: p.name)
def test_contract_cites_the_motivating_incident(path: Path) -> None:
    text = _read(path)
    assert "#3743" in text, (
        f"{path.name} must cite the #3743 incident as the motivating example "
        "so the rule keeps its rationale"
    )
