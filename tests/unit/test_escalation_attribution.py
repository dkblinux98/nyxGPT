"""Escalation DMs are signed by the agent that raised them (#3911).

Until #3910 this repo had one Slack identity, so every escalation arrived from
the same bot and the owner had to read the body to learn which agent was
stuck. #3910 filed one user token per agent; `notify_human_escalation` now
spends them, so a self-heal FATAL reads as coming from the developer agent and
a 3-cycle breaker from the review agent.

The *decision* -- which token, which fallback, which record -- is exercised
against stubbed `curl` by `tests/test_gh_project_lib.sh` (Test 17b), which
`assignment-dispatch-smoke.yml` runs on a real runner whenever
`gh_project.sh` changes. Whether Slack will actually honour an agent token's
DM is a property of the workspace and is proved by
`scripts/slack-escalation-smoke.sh` against the live API.

What is left, and what these tests are for, is the *wiring* -- the class of
defect where the mechanism is correct and never fires:

  - a workflow that declares `AGENT_ROLE` but never passes that agent's token,
    so every DM silently takes the bot fallback;
  - a role-owned script that loses its `AGENT_ROLE` default and starts
    attributing its reports to whichever workflow happened to run it;
  - someone reinstating inference from workflow names, which is the one thing
    this design refuses to do.

None of those fails a unit test of the function, and none of them fails
loudly at runtime: they degrade to exactly the behaviour that existed before,
which is why they need a structural guard rather than a smoke test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
GH_PROJECT = REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"

#: The Slack user token secret each role speaks with. Same three names
#: `huddle_channel.py` reads (SPEAKER_TOKEN_ENV) -- one token per agent, one
#: vocabulary for both consumers.
ROLE_TOKEN_ENV = {
    "dev": "SLACK_USER_TOKEN_DEV",
    "review": "SLACK_USER_TOKEN_REVIEW",
    "scrum": "SLACK_USER_TOKEN_SCRUM",
}

#: Every role-owned script that escalates, and the identity its reports carry.
#: `scrummaster_dispatch_next.sh` is the reason this mapping lives in the
#: scripts and not in the workflows: `developer_pull_next_issue.yml` runs it
#: under DEVELOPER_AGENT_TOKEN, but a dispatch-block report is the
#: scrummaster's, and attributing it to the developer agent would name the
#: wrong agent on every queue stall.
ROLE_OWNED_SCRIPTS = {
    "scrummaster_dispatch_next.sh": "scrum",
    "release_ceremony_watch.sh": "scrum",
    "review_head_gate_action.sh": "review",
    "review_ensure_handoff.sh": "review",
    "dispatch_conflict_resolution.sh": "review",
}


def _workflows_with_escalation_secrets() -> list[Path]:
    """Workflow files that wire the escalation DM at all.

    Found by their secrets rather than by a hand-kept list: a new escalating
    workflow should be caught by these tests the day it is added, not the day
    someone remembers to add it here.
    """
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        text = path.read_text()
        if "SLACK_USER_ID: ${{ secrets.SLACK_USER_ID }}" in text:
            found.append(path)
    return found


class TestTheRoleIsNeverInferred:
    """`AGENT_ROLE` and nothing else.

    Workflow names in this repo are not uniform -- "Notify Merge Conflicts"
    and "Claude Code Review" both escalate as the review agent and neither
    says so -- and a wrong guess signs an escalation with the wrong agent's
    name. That is strictly worse than the unattributed bot DM it replaces, so
    an unrecognised role attributes nothing at all.
    """

    def test_resolution_reads_only_agent_role(self):
        source = GH_PROJECT.read_text()
        body = source[source.index("_escalation_role() {") :]
        body = body[: body.index("\n}\n")]
        assert "AGENT_ROLE" in body
        for guess in ("GITHUB_WORKFLOW", "GITHUB_JOB", "GITHUB_ACTOR", "BASH_SOURCE"):
            assert guess not in body, f"the role must not be inferred from {guess}"

    def test_an_unknown_role_resolves_to_nothing(self):
        source = GH_PROJECT.read_text()
        body = source[source.index("_escalation_role() {") :]
        body = body[: body.index("\n}\n")]
        # A catch-all that printed a default would make every unrecognised
        # value claim to be one specific agent.
        assert re.search(r"\*\)\s*printf\s+''", body), "the catch-all must attribute nothing"


class TestAttributionNeverCostsANotification:
    """#3695's delivery guarantee outranks #3911's sender name.

    A user token this workspace will not let open a DM must not turn a
    delivered escalation into a warned no-op. Both identities are tried before
    the function gives up.
    """

    def test_the_bot_is_tried_after_the_agent_identity(self):
        source = GH_PROJECT.read_text()
        body = source[source.index("notify_human_escalation() {") :]
        body = body[: body.index("\n  return 0\n}\n")]
        assert 'sent_as="$role"' in body
        assert 'sent_as="bot"' in body
        # The agent attempt has to come first, or attribution never happens.
        assert body.index('sent_as="$role"') < body.index('sent_as="bot"')

    def test_a_missing_bot_token_alone_no_longer_skips_the_dm(self):
        """The pre-#3911 guard required SLACK_BOT_TOKEN specifically.

        An agent token with no bot token configured is now enough to send --
        otherwise the shared bot stays a hard dependency of a feature whose
        whole point is to stop routing everything through it.
        """
        source = GH_PROJECT.read_text()
        body = source[source.index("notify_human_escalation() {") :]
        body = body[: body.index("\n  if _slack_notify_recent")]
        assert '-z "$user_token" && -z "${SLACK_BOT_TOKEN:-}"' in body


class TestRoleOwnedScriptsCarryTheirOwnIdentity:
    def test_every_escalating_script_declares_its_role(self):
        for name, role in ROLE_OWNED_SCRIPTS.items():
            script = (REPO_ROOT / "scripts" / "agents" / name).read_text()
            assert "notify_human_escalation" in script, (
                f"{name} no longer escalates -- drop it from ROLE_OWNED_SCRIPTS "
                "rather than leaving a guard that pins nothing"
            )
            expected = f'export AGENT_ROLE="${{AGENT_ROLE:-{role}}}"'
            assert expected in script, f"{name} must declare `{expected}`"

    def test_the_default_is_overridable(self):
        """`${AGENT_ROLE:-x}`, never a bare assignment.

        A workflow making an inline call for a different agent has to be able
        to say so, and the tests set it directly.
        """
        for name in ROLE_OWNED_SCRIPTS:
            script = (REPO_ROOT / "scripts" / "agents" / name).read_text()
            assert 'export AGENT_ROLE="${AGENT_ROLE:-' in script


class TestTheWorkflowsActuallyPassTheToken:
    """The silent-no-op class: a role declared, a token never wired.

    Attribution that falls back to the bot on every single run looks exactly
    like attribution that works, because the DM still arrives. Nothing in the
    logs of a healthy pipeline would say otherwise.
    """

    def test_an_inline_agent_role_is_paired_with_its_token(self):
        checked = 0
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text()
            for role in re.findall(r"^\s*AGENT_ROLE:\s*([a-z-]+)\s*$", text, re.MULTILINE):
                assert role in ROLE_TOKEN_ENV, (
                    f"{path.name} sets AGENT_ROLE: {role}, which "
                    f"`_escalation_role` does not resolve -- it would attribute nothing"
                )
                secret = ROLE_TOKEN_ENV[role]
                assert f"{secret}: ${{{{ secrets.{secret} }}}}" in text, (
                    f"{path.name} declares AGENT_ROLE: {role} but never passes "
                    f"{secret}, so every DM would take the bot fallback"
                )
                checked += 1
        assert checked, "no workflow declares AGENT_ROLE -- the wiring guard pins nothing"

    def test_every_escalating_workflow_can_attribute(self):
        """Each one passes at least one agent token, or says why it cannot."""
        for path in _workflows_with_escalation_secrets():
            text = path.read_text()
            assert any(secret in text for secret in ROLE_TOKEN_ENV.values()), (
                f"{path.name} wires the escalation DM but passes no agent token, "
                "so its escalations can only ever come from the shared bot"
            )


class TestTheLiveProofExists:
    """The lesson of this issue's reopening, pinned.

    #3911 first closed claiming executed evidence for behaviour that had never
    run against a real Slack token; the adapter had in fact shipped with two of
    four operations dead. The scoped-in half of that lesson here is that the
    live smoke has to *fail* when the thing is broken -- so it runs with no bot
    token to fall back on, and has a fault-injection half.
    """

    def test_the_smoke_runs_without_a_fallback(self):
        smoke = (REPO_ROOT / "scripts" / "slack-escalation-smoke.sh").read_text()
        assert 'SLACK_BOT_TOKEN=""' in smoke
        assert "--prove-it-fails" in smoke

    def test_the_workflow_withholds_the_bot_token_from_the_default_half(self):
        workflow = (WORKFLOWS / "slack-huddle-smoke.yml").read_text()
        assert "scripts/slack-escalation-smoke.sh" in workflow
        step = workflow[workflow.index("Each agent DMs the owner under its own identity") :]
        step = step[: step.index("scripts/slack-escalation-smoke.sh")]
        assert "SLACK_BOT_TOKEN" not in step, (
            "with a fallback in the environment a refused agent token still "
            "delivers, and the job would pass on the bot's work"
        )

    def test_it_is_dispatch_only(self):
        """Each run DMs the owner for real."""
        workflow = (WORKFLOWS / "slack-huddle-smoke.yml").read_text()
        trigger = workflow[workflow.index("\non:") : workflow.index("\njobs:")]
        assert "workflow_dispatch" in trigger
        assert "push:" not in trigger
        assert "schedule:" not in trigger
