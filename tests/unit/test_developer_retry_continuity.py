"""A verification retry continues the previous attempt's work (#4038).

`tests/test_reconcile_work_branch.sh` proves the *reconciliation* is right when
it runs, against real git repositories. This file pins the wiring that decides
whether it runs at all, and the prompt text that decides what the agent does
before it gets there -- neither of which that test can see.

THE DEFECT THESE GUARD AGAINST. `anthropics/claude-code-action` mints and checks
out a fresh ``claude/issue-<n>-<timestamp>`` branch on every invocation, cut
from the release branch, and ``developer_auto_implement.yml`` invokes it six
times. Each retry attempt is therefore handed a clean cut of the release branch
that does not contain the work it is being asked to fix. The Attempt 2 and
Attempt 3 prompts nevertheless told the agent "you should already be on the
feature branch from the previous step" -- true when they were written in
2026-02, false from 2026-07-07 when the action's behaviour changed under the
floating ``@v1`` tag. On #4033 that cost sixteen minutes of re-implementation,
two divergent branches, and a rescue draft PR the owner closed by hand.

Every assertion below corresponds to a way an ordinary-looking edit silently
restores that defect: reintroducing the false premise, dropping the explicit
branch name, deriving the branch from the working tree instead of run state,
removing a reconciliation step, or letting a ``uses:`` drift back to a floating
tag.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

DEV_WF_PATH = WORKFLOWS / "developer_auto_implement.yml"
DEV_WF = yaml.safe_load(DEV_WF_PATH.read_text())
DEV_STEPS: list[dict] = DEV_WF["jobs"]["implement"]["steps"]

#: The run-state handle the retries read the work branch through. Deriving it
#: from ``git branch --show-current`` at retry time is wrong by construction --
#: that names the decoy the action minted seconds earlier (D-031).
WORK_BRANCH_REF = "steps.work_branch.outputs.name"

RETRY_STEPS = ("Claude Fix Issues (Attempt 2)", "Claude Fix Issues (Attempt 3)")


def _step(name: str) -> dict:
    for step in DEV_STEPS:
        if step.get("name") == name:
            return step
    raise AssertionError(f"No step named {name!r} in developer_auto_implement.yml")


def _index(name: str) -> int:
    for i, step in enumerate(DEV_STEPS):
        if step.get("name") == name:
            return i
    raise AssertionError(f"No step named {name!r} in developer_auto_implement.yml")


def test_the_work_branch_is_recorded_before_any_retry_boundary() -> None:
    """Frozen into run state at the one point where reading the tree is right.

    Every path that produces work has just positioned the workspace on its
    branch, and nothing has crossed a ``claude-code-action`` boundary yet.
    After this point everything has. If the recording step ever moves below a
    retry, it records the decoy and the whole change inverts.
    """
    record = _index("Record the work branch")
    for retry in RETRY_STEPS:
        assert record < _index(retry), f"{retry} runs before the work branch is recorded"
    assert record < _index("Run Verification (Attempt 1)")


def test_the_retries_take_the_branch_from_run_state_not_the_working_tree() -> None:
    for name in RETRY_STEPS:
        prompt = _step(name)["with"]["prompt"]
        assert WORK_BRANCH_REF in prompt, f"{name} does not name the recorded work branch"
        assert "git branch --show-current" not in prompt, (
            f"{name} derives the branch from the working tree, which on the "
            "failure path names a branch minted seconds ago (D-031)"
        )


def test_the_false_premise_is_gone() -> None:
    """The sentence that made re-implementing the reasonable response.

    It asserted branch continuity across an action boundary and named no
    branch to recover, so an agent that disbelieved it had nothing to act on.
    """
    text = DEV_WF_PATH.read_text()
    assert "You should already be on the feature branch from the previous step" not in text
    assert "check out the branch yourself" not in text


def test_every_recovery_instruction_names_its_branch() -> None:
    """A checkout instruction with no branch in it is not an instruction."""
    for name in (*RETRY_STEPS, "Run Claude Code to fix review issues (Review Fix)"):
        prompt = _step(name)["with"]["prompt"]
        assert "git checkout -B" in prompt, f"{name} does not tell the agent to check anything out"
        checkouts = re.findall(r"git checkout -B \S+", prompt)
        assert checkouts, f"{name}'s checkout instruction names no branch"
        for line in checkouts:
            assert "${{" in line, (
                f"{name} hard-codes a branch in {line!r}; it must interpolate the "
                "branch this run actually recorded"
            )


def test_the_retries_tell_the_agent_not_to_start_over() -> None:
    for name in RETRY_STEPS:
        prompt = _step(name)["with"]["prompt"].lower()
        assert "re-implement" in prompt


def test_a_retry_with_no_known_work_branch_does_not_run() -> None:
    """No branch, no invocation.

    A retry that cannot be told where the previous attempt's commits are has
    exactly one thing it can do -- rebuild the issue on the branch the action
    just minted -- and that is the defect, not a fallback. Spend nothing.
    """
    for name in RETRY_STEPS:
        condition = str(_step(name)["if"])
        assert f"{WORK_BRANCH_REF} != ''" in condition, (
            f"{name} can run without a known work branch, which can only produce "
            "a re-implementation on a stranded branch"
        )


def test_every_claude_step_that_can_strand_work_is_followed_by_a_reconciliation() -> None:
    """The prompt asks; this is what makes it true regardless of the answer.

    The review path has had a deterministic reconciliation since #3145 and the
    implement path had none -- which is why a prompt that merely *asserted*
    branch continuity went six weeks without anyone noticing it was false. An
    instruction the agent may decline, or decline to finish, is not a
    mechanism.
    """
    expected = {
        "Claude Fix Issues (Attempt 2)": "Ensure attempt 2's fixes landed on the work branch",
        "Claude Fix Issues (Attempt 3)": "Ensure attempt 3's fixes landed on the work branch",
        "Run Claude Code to fix review issues (Review Fix)": (
            "Ensure review fixes landed on PR branch"
        ),
    }
    for claude_step, reconcile_step in expected.items():
        assert _index(claude_step) < _index(reconcile_step)
        run = _step(reconcile_step).get("run", "")
        assert "reconcile_work_branch.sh" in run, (
            f"{reconcile_step} no longer calls the shared reconciliation; a "
            "second copy of this logic is how #3145 came to be fixed on one "
            "path and not the other"
        )
        assert "--release" in run


def test_the_reconciliation_runs_before_the_gate_that_judges_its_result() -> None:
    """Attempt 3's fixes must be on the tree Final Verification reads.

    Final Verification has no ``if:`` and no ``continue-on-error``: if the
    reconciliation ran after it, the last attempt's fixes could sit on the
    action's branch while the gate failed the tree they were never applied to.
    """
    assert _index("Ensure attempt 2's fixes landed on the work branch") < _index(
        "Run Verification (Attempt 2)"
    )
    assert _index("Ensure attempt 3's fixes landed on the work branch") < _index(
        "Final Verification (Must Pass)"
    )


def test_the_reconciliation_never_force_pushes() -> None:
    """D-011: forward merge, never a rewrite of a shared branch.

    An action branch cut from the release branch does not contain the target's
    commits, so forcing it over the target destroys them -- which is the
    #4033 shape exactly. ``tests/test_reconcile_work_branch.sh`` case 1b runs
    the retired force-push form and shows it doing so.
    """
    script = (REPO_ROOT / "scripts" / "agents" / "reconcile_work_branch.sh").read_text()
    assert "push --force" not in script
    assert "push -f " not in script
    assert "git rebase" not in script
    assert "git merge --no-edit" in script


def test_the_reconciliation_deletes_only_on_the_blob_level_proof() -> None:
    """D-031: 'the push exited 0' is a report, not evidence."""
    script = (REPO_ROOT / "scripts" / "agents" / "reconcile_work_branch.sh").read_text()
    assert "branch_content.py" in script
    delete_at = script.index("push origin --delete")
    proof_at = script.index("branch_content.py")
    assert proof_at < delete_at, "the delete is not gated on the content proof"


AGENT_WORKFLOWS = (
    "claude-code-review.yml",
    "claude-md-binding-canary.yml",
    "claude.yml",
    "developer_auto_implement.yml",
    "huddle_session.yml",
    "scrummaster_groom_sprint.yml",
)

_USES_ACTION = re.compile(r"uses:\s*anthropics/claude-code-action@(\S+)")


def test_the_action_is_pinned_to_an_exact_commit() -> None:
    """This defect arrived through the floating ``@v1`` tag.

    ``v1`` is a *major* tag: it moves. The action's branch handling changed
    underneath this repository on 2026-07-07 -- it began minting and checking
    out its own branch on every invocation, overriding the branch
    ``developer_create_branch.sh`` had already created -- with no commit here
    to explain it, and six weeks passed before anyone noticed work was being
    stranded. A SHA makes the next such change a deliberate upgrade that reads
    a changelog first, rather than a silent one.
    """
    unpinned: list[str] = []
    for name in AGENT_WORKFLOWS:
        for ref in _USES_ACTION.findall((WORKFLOWS / name).read_text()):
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                unpinned.append(f"{name}: @{ref}")
    assert not unpinned, "claude-code-action is not pinned to a commit SHA in: " + ", ".join(
        unpinned
    )


def test_no_agent_workflow_was_missed() -> None:
    """The list above is the whole fleet, not the files someone remembered.

    A new workflow that invokes the action on the floating tag reopens the
    hole for itself, and the pinning test above would not look at it.
    """
    invoking = {
        path.name
        for path in WORKFLOWS.glob("*.yml")
        if "anthropics/claude-code-action@" in path.read_text()
    }
    assert invoking == set(AGENT_WORKFLOWS), (
        "the set of workflows invoking claude-code-action has changed; add the "
        f"new one to AGENT_WORKFLOWS (found: {sorted(invoking)})"
    )
