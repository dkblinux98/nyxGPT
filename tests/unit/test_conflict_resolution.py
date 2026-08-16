"""Merge-conflict routing decisions (#3801).

2026-08-15: four In Review PRs went conflict-stale in one afternoon as nine
merges landed on the release branch, and the handler's only move was to
assign the human owner — for conflicts whose entire content was "the mainline
moved". Owner rule: conflicts go to the developer agent; the owner is reached
only when there is genuinely a decision only they can make.

These tests pin that routing, plus the guards that keep it from misfiring:
the burst cooldown (nine pushes must not produce nine rounds on one PR), the
anchored token match (prose naming the escalation token must not escalate),
the author gate (this repo is public, so a stranger's comment must not be
able to summon the owner, fake round exhaustion, or freeze a PR in cooldown),
and the escalation marker (the owner is interrupted once per unanswered
question, not once per push to the release branch).
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = REPO_ROOT / "scripts" / "agents" / "lib" / "conflict_resolution.py"
_spec = importlib.util.spec_from_file_location("conflict_resolution", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
cr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cr
_spec.loader.exec_module(cr)

NOW = "2026-08-15T18:00:00Z"

#: The pipeline identities, as `dispatch_conflict_resolution.sh` assembles them
#: from DEV_AGENT / REVIEW_AGENT / HUMAN_OWNER.
TRUSTED = ("dev-agent", "review-agent", "owner")


def _comment(
    body: str, created_at: str = "2026-08-15T09:00:00Z", author: str = "review-agent"
) -> dict:
    return {"body": body, "created_at": created_at, "author": author}


def _round(created_at: str = "2026-08-15T09:00:00Z", author: str = "review-agent") -> dict:
    return _comment(f"Automated round dispatched {cr.ROUND_MARKER}", created_at, author)


def _escalation(created_at: str = "2026-08-15T10:00:00Z", author: str = "review-agent") -> dict:
    return _comment(f"Escalated to the owner {cr.ESCALATION_MARKER}", created_at, author)


class TestDefaultIsTheDeveloperAgent:
    def test_fresh_conflict_dispatches(self):
        result = cr.decide("CONFLICTING", [], now=NOW)
        assert result["action"] == cr.ACTION_DISPATCH
        assert result["rounds"] == 0

    def test_conflict_after_an_old_round_dispatches_again(self):
        result = cr.decide("CONFLICTING", [_round("2026-08-14T09:00:00Z")], now=NOW)
        assert result["action"] == cr.ACTION_DISPATCH
        assert result["rounds"] == 1
        assert "round 2" in result["reason"]

    def test_unrelated_comments_do_not_count_as_rounds(self):
        comments = [_comment("LGTM"), _comment("Please fix the typo")]
        assert cr.decide("CONFLICTING", comments, now=NOW)["rounds"] == 0

    def test_legacy_round_phrase_still_counts(self):
        """PRs that already had a round under the pre-#3801 code path must not
        restart their count from zero."""
        legacy = _comment(
            "⚠️ Merge Conflicts Detected — Automated conflict-resolution round dispatched.",
            "2026-08-14T09:00:00Z",
        )
        assert cr.decide("CONFLICTING", [legacy], now=NOW)["rounds"] == 1


class TestNoopStates:
    @pytest.mark.parametrize("state", ["MERGEABLE", "mergeable"])
    def test_clean_pr_is_a_noop(self, state):
        assert cr.decide(state, [], now=NOW)["action"] == cr.ACTION_NOOP

    def test_unknown_mergeability_is_a_noop_not_a_conflict(self):
        result = cr.decide("UNKNOWN", [], now=NOW)
        assert result["action"] == cr.ACTION_NOOP
        assert "UNKNOWN" in result["reason"]

    @pytest.mark.parametrize("pr_state", ["MERGED", "CLOSED"])
    def test_non_open_pr_is_a_noop(self, pr_state):
        assert cr.decide("CONFLICTING", [], pr_state=pr_state, now=NOW)["action"] == cr.ACTION_NOOP

    def test_round_inside_the_cooldown_is_still_in_flight(self):
        """The burst guard: nine merges in an afternoon fire the handler nine
        times per open PR. Only the first may dispatch."""
        result = cr.decide("CONFLICTING", [_round("2026-08-15T17:40:00Z")], now=NOW)
        assert result["action"] == cr.ACTION_NOOP
        assert "still in flight" in result["reason"]

    def test_cooldown_can_be_disabled(self):
        result = cr.decide(
            "CONFLICTING", [_round("2026-08-15T17:40:00Z")], cooldown_minutes=0, now=NOW
        )
        assert result["action"] == cr.ACTION_DISPATCH

    def test_round_without_a_timestamp_does_not_block_forever(self):
        result = cr.decide("CONFLICTING", [{"body": cr.ROUND_MARKER}], now=NOW)
        assert result["action"] == cr.ACTION_DISPATCH


class TestEscalationIsTheExceptionNotTheDefault:
    def test_agent_raised_owner_decision_escalates_with_the_question(self):
        body = (
            "CONFLICT_REQUIRES_OWNER_DECISION\n\n"
            "`ops.py` now starts Cassandra eagerly on the mainline while this PR "
            "makes startup lazy. Both are owner-accepted; which wins?"
        )
        result = cr.decide("CONFLICTING", [_comment(body)], now=NOW)
        assert result["action"] == cr.ACTION_ESCALATE
        assert "which wins?" in result["question"]

    def test_prose_that_merely_names_the_token_does_not_escalate(self):
        """V-011/#3790: a token is a command only where it opens a line."""
        body = (
            "The agent will resolve this. If it cannot, it posts "
            "`CONFLICT_REQUIRES_OWNER_DECISION` with the question."
        )
        assert cr.decide("CONFLICTING", [_comment(body)], now=NOW)["action"] == cr.ACTION_DISPATCH

    def test_a_question_already_followed_by_a_round_is_spent(self):
        comments = [
            _comment(
                "CONFLICT_REQUIRES_OWNER_DECISION\nWhich behavior wins?", "2026-08-14T08:00:00Z"
            ),
            _round("2026-08-14T12:00:00Z"),
        ]
        assert cr.decide("CONFLICTING", comments, now=NOW)["action"] == cr.ACTION_DISPATCH

    def test_exhausted_rounds_escalate(self):
        comments = [_round(f"2026-08-1{day}T09:00:00Z") for day in (1, 2, 3)]
        result = cr.decide("CONFLICTING", comments, max_rounds=3, now=NOW)
        assert result["action"] == cr.ACTION_ESCALATE
        assert "still conflicted" in result["reason"]

    def test_the_round_dispatch_comment_itself_never_escalates(self):
        """The dispatcher's own comment must not read as an owner request —
        that would make every round escalate on the next pass."""
        dispatch_script = (
            REPO_ROOT / "scripts" / "agents" / "dispatch_conflict_resolution.sh"
        ).read_text()
        # The literal token must not appear in the posted round message at all.
        round_msg_start = dispatch_script.index('MSG="🔀')
        round_msg_end = dispatch_script.index('gh pr comment "$PR"', round_msg_start)
        assert cr.OWNER_DECISION_MARKER not in dispatch_script[round_msg_start:round_msg_end]


class TestPublicCommentThreadIsNotATrustedChannel:
    """The comment thread is attacker-writable — this repo is public.

    `conflict_owner_escalation.yml` gates its commenter, but the polling path
    reads the same tokens out of the thread, so without an author gate here
    that workflow gate is bypassable by just waiting for the next sweep.
    """

    def test_owner_token_from_a_stranger_does_not_escalate(self):
        body = "CONFLICT_REQUIRES_OWNER_DECISION\n\nPlease read https://not-a-real-site.example"
        result = cr.decide(
            "CONFLICTING", [_comment(body, author="drive-by")], now=NOW, trusted_authors=TRUSTED
        )
        assert result["action"] == cr.ACTION_DISPATCH
        assert result["question"] == ""

    def test_owner_token_from_the_developer_agent_still_escalates(self):
        body = "CONFLICT_REQUIRES_OWNER_DECISION\n\nEager or lazy startup?"
        result = cr.decide(
            "CONFLICTING", [_comment(body, author="dev-agent")], now=NOW, trusted_authors=TRUSTED
        )
        assert result["action"] == cr.ACTION_ESCALATE
        assert "Eager or lazy startup?" in result["question"]

    def test_forged_round_markers_do_not_fake_exhaustion(self):
        comments = [_round(f"2026-08-1{day}T09:00:00Z", author="drive-by") for day in (1, 2, 3)]
        result = cr.decide("CONFLICTING", comments, max_rounds=3, now=NOW, trusted_authors=TRUSTED)
        assert result["action"] == cr.ACTION_DISPATCH
        assert result["rounds"] == 0

    def test_a_forged_round_marker_cannot_hold_a_pr_in_cooldown(self):
        """Otherwise a fresh marker every <45 min suppresses resolution forever."""
        comments = [_round("2026-08-15T17:58:00Z", author="drive-by")]
        result = cr.decide("CONFLICTING", comments, now=NOW, trusted_authors=TRUSTED)
        assert result["action"] == cr.ACTION_DISPATCH

    def test_a_real_round_marker_still_holds_the_cooldown(self):
        comments = [_round("2026-08-15T17:58:00Z", author="review-agent")]
        result = cr.decide("CONFLICTING", comments, now=NOW, trusted_authors=TRUSTED)
        assert result["action"] == cr.ACTION_NOOP

    def test_unattributed_comments_are_untrusted(self):
        """A missing login is not a pass."""
        body = "CONFLICT_REQUIRES_OWNER_DECISION\nWhich wins?"
        comments = [{"body": body, "created_at": "2026-08-15T09:00:00Z"}]
        assert (
            cr.decide("CONFLICTING", comments, now=NOW, trusted_authors=TRUSTED)["action"]
            == cr.ACTION_DISPATCH
        )

    def test_logins_compare_case_insensitively(self):
        """GitHub logins are case-insensitive; config and API can disagree."""
        body = "CONFLICT_REQUIRES_OWNER_DECISION\nWhich wins?"
        result = cr.decide(
            "CONFLICTING", [_comment(body, author="Dev-Agent")], now=NOW, trusted_authors=TRUSTED
        )
        assert result["action"] == cr.ACTION_ESCALATE

    def test_rest_nested_user_login_shape_is_understood(self):
        comment = {
            "body": "CONFLICT_REQUIRES_OWNER_DECISION\nWhich wins?",
            "created_at": "2026-08-15T09:00:00Z",
            "user": {"login": "dev-agent"},
        }
        result = cr.decide("CONFLICTING", [comment], now=NOW, trusted_authors=TRUSTED)
        assert result["action"] == cr.ACTION_ESCALATE

    def test_dispatcher_projects_the_author_and_passes_the_trusted_set(self):
        """Structural guard: the gate is worthless if the shell drops the login."""
        script = (REPO_ROOT / "scripts" / "agents" / "dispatch_conflict_resolution.sh").read_text()
        assert "author: (.user.login" in script
        assert "trusted_authors: $trusted" in script
        assert "DEV_AGENT:-" in script and "REVIEW_AGENT:-" in script and "HUMAN_OWNER:-" in script


class TestEscalationIsNotRepeated:
    """An escalation the owner has not answered yet must not be re-sent on
    every subsequent push to the release branch."""

    def test_exhausted_rounds_escalate_only_once(self):
        comments = [_round(f"2026-08-1{day}T09:00:00Z") for day in (1, 2, 3)]
        first = cr.decide("CONFLICTING", comments, max_rounds=3, now=NOW)
        assert first["action"] == cr.ACTION_ESCALATE

        second = cr.decide(
            "CONFLICTING", [*comments, _escalation("2026-08-13T10:00:00Z")], max_rounds=3, now=NOW
        )
        assert second["action"] == cr.ACTION_NOOP
        assert "already escalated" in second["reason"]

    def test_an_owner_question_already_escalated_is_not_re_sent(self):
        comments = [
            _comment("CONFLICT_REQUIRES_OWNER_DECISION\nWhich wins?", "2026-08-15T08:00:00Z"),
            _escalation("2026-08-15T08:05:00Z"),
        ]
        result = cr.decide("CONFLICTING", comments, now=NOW)
        assert result["action"] == cr.ACTION_NOOP
        assert "awaiting an answer" in result["reason"]

    def test_a_round_after_the_escalation_reopens_it(self):
        """The owner answered, a round ran, and it is still stuck — escalate again."""
        comments = [
            *[_round(f"2026-08-1{day}T09:00:00Z") for day in (1, 2, 3)],
            _escalation("2026-08-13T10:00:00Z"),
            _round("2026-08-14T09:00:00Z"),
        ]
        result = cr.decide("CONFLICTING", comments, max_rounds=3, now=NOW)
        assert result["action"] == cr.ACTION_ESCALATE

    def test_a_forged_escalation_marker_cannot_suppress_a_real_escalation(self):
        comments = [
            *[_round(f"2026-08-1{day}T09:00:00Z") for day in (1, 2, 3)],
            _escalation("2026-08-13T10:00:00Z", author="drive-by"),
        ]
        result = cr.decide("CONFLICTING", comments, max_rounds=3, now=NOW, trusted_authors=TRUSTED)
        assert result["action"] == cr.ACTION_ESCALATE

    def test_the_dispatcher_stamps_the_marker_on_its_escalation(self):
        script = (REPO_ROOT / "scripts" / "agents" / "dispatch_conflict_resolution.sh").read_text()
        assert f'ESCALATION_MARKER="{cr.ESCALATION_MARKER}"' in script
        escalation_msg = script[script.index('ESCALATION_MSG="🚨') :]
        assert "${ESCALATION_MARKER}" in escalation_msg


class TestQuestionExtraction:
    def test_strips_markers_and_collapses_whitespace(self):
        body = "CONFLICT_REQUIRES_OWNER_DECISION:\n\nWhich  wins?\nA or B?\n<!-- note -->"
        assert cr.extract_owner_question(body) == "Which wins? A or B?"

    def test_absent_marker_yields_empty_string(self):
        assert cr.extract_owner_question("no token here") == ""


class TestCli:
    def _run(self, payload: dict) -> dict:
        proc = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "decide"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def test_decide_reads_json_and_prints_json(self):
        result = self._run(
            {
                "mergeable": "CONFLICTING",
                "comments": [],
                "now": NOW,
                "trusted_authors": list(TRUSTED),
            }
        )
        assert result["action"] == "dispatch"

    @pytest.mark.parametrize("trusted", [None, [], ["", "  "]])
    def test_the_cli_refuses_to_route_without_an_author_gate(self, trusted):
        """Fail closed: the gate is only a gate if production cannot skip it."""
        payload = {"mergeable": "CONFLICTING", "comments": [], "now": NOW}
        if trusted is not None:
            payload["trusted_authors"] = trusted
        proc = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "decide"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 2
        assert "trusted_authors" in proc.stderr
        assert proc.stdout.strip() == ""

    def test_question_subcommand(self):
        proc = subprocess.run(
            [sys.executable, str(_MODULE_PATH), "question"],
            input="CONFLICT_REQUIRES_OWNER_DECISION\nWhich wins?",
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip() == "Which wins?"


class TestNoOwnerAssignmentOnTheRoutinePath:
    """Structural guard on the two files that used to assign the owner."""

    def test_notify_workflow_no_longer_assigns_the_owner(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "notify-merge-conflicts.yml").read_text()
        assert "addAssignees" not in workflow
        assert "dispatch_conflict_resolution.sh" in workflow

    def test_merge_script_delegates_instead_of_assigning_the_owner(self):
        script = (REPO_ROOT / "scripts" / "agents" / "review_accept_and_merge.sh").read_text()
        conflict_block = script[script.index('if [[ "$pr_mergeable" == "CONFLICTING" ]]') :]
        conflict_block = conflict_block[: conflict_block.index("\nfi\n")]
        assert "dispatch_conflict_resolution.sh" in conflict_block
        assert "assign_issue_verified" not in conflict_block


class TestDispatcherShellBehaviour:
    """Runs `tests/test_conflict_resolution_dispatch.sh` (stubbed `gh`, no
    network) under pytest, so `pytest -v` exercises the shell that actually
    moves the issue — not just the decision it consults."""

    def test_shell_suite_passes(self):
        if shutil.which("jq") is None:
            pytest.skip("jq is required by the shell suite")
        suite = REPO_ROOT / "tests" / "test_conflict_resolution_dispatch.sh"
        result = subprocess.run(["bash", str(suite)], capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr
