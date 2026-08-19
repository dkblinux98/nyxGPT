"""Dispatch is an event and rework is an assignment, not prose (#3882).

`READY_FOR_NEXT_ISSUE` in an issue comment made prose into an API. A job `if:`
can only substring-match, so any comment that *named* the token started work --
which is how a sprint-boundary park note dispatched the next issue (#3706) and
how a stop message restarted itself ~500 times in two hours (#3790). Two layers
of gating were then built to make comments safe to parse, which is effort spent
keeping the wrong mechanism alive.

What the comment was really doing is a cross-identity handoff: the review agent,
finishing a merge, asking the scrummaster to select next. `repository_dispatch`
is that, natively -- and nothing that merely mentions it can trigger it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
KICK_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "developer_pull_next_issue.yml"
LIB = REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
DEV_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "developer_auto_implement.yml"
# Split so this file is not itself a mention of either retired token.
TOKEN = "READY_FOR_NEXT" + "_ISSUE"
RETRY = "RETRY_" + "IMPLEMENTATION"


def _triggers(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads a bare `on:` key as the boolean True.
    return loaded[True] if True in loaded else loaded["on"]


def test_the_kick_is_a_repository_dispatch() -> None:
    triggers = _triggers(KICK_WORKFLOW)
    assert "repository_dispatch" in triggers, "the kick must be an event"
    assert "dispatch-next-issue" in triggers["repository_dispatch"]["types"]


def test_the_kick_is_not_comment_triggered() -> None:
    """The property that closes #3706 and #3790 for this trigger, permanently."""
    triggers = _triggers(KICK_WORKFLOW)
    assert "issue_comment" not in triggers, (
        "the pull workflow must not be comment-triggered; a comment trigger "
        "can be fired by any text that names the token, including its own output"
    )


@pytest.mark.parametrize("token", [TOKEN, RETRY])
def test_no_producer_writes_the_token(token: str) -> None:
    """Deleted, not deprecated: nothing may post it, or it becomes live again."""
    offenders = []
    for path in sorted((REPO_ROOT / "scripts").rglob("*.sh")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # history in a comment is fine; issuing it is not
            if token in stripped:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, f"the retired token {token} is still issued at: {offenders}"


@pytest.mark.parametrize("token", [TOKEN, RETRY])
def test_no_workflow_subscribes_to_the_token(token: str) -> None:
    """A `contains()` on a comment body is a subscription, wherever it sits."""
    offenders = []
    for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "github.event.comment.body" in line and token in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, f"{token} still gates a workflow at: {offenders}"


def test_rework_arrives_as_an_assignment() -> None:
    """The developer picks work up on assignment and claims the status itself
    -- the actor doing the work owns the transition. If this workflow ever
    subscribes to comments again, the #3706/#3790 shape is back."""
    loaded = yaml.safe_load(DEV_WORKFLOW.read_text(encoding="utf-8"))
    triggers = loaded[True] if True in loaded else loaded["on"]
    assert set(triggers) == {"issues"}
    assert triggers["issues"]["types"] == ["assigned"]


def test_the_dispatch_helper_exists_and_names_the_event() -> None:
    """The replacement has to actually send the event the workflow listens for."""
    text = LIB.read_text(encoding="utf-8")
    assert "dispatch_next_issue()" in text, "the dispatch helper is gone"
    assert (
        "event_type=dispatch-next-issue" in text
    ), "the helper must send the event type the pull workflow subscribes to"


def test_both_producers_use_the_helper() -> None:
    """The autopilot continue and the drain-gate opening are the two kicks."""
    text = LIB.read_text(encoding="utf-8")
    calls = [
        line
        for line in text.splitlines()
        if "dispatch_next_issue " in line and not line.strip().startswith("#")
    ]
    assert (
        len(calls) >= 2
    ), f"expected the autopilot and drain-gate producers to dispatch; found {len(calls)}"
