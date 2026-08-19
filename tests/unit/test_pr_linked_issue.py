"""A PR's issue is a link, not a sentence (owner rule, 2026-08-19).

GitHub already stores which issue a PR closes -- the "Development" sidebar
link and the `closingIssuesReferences` edge behind it, which is what actually
closes the issue on merge. Every consumer here used to re-derive that by
grepping `Closes #N` out of the PR body: prose standing in for a relationship
the platform stores, the same mistake as driving workflows from comment
tokens (#3882) and as the retired `Related feature: #N` convention (D-002).

The cost was not theoretical. "PR #N body does not contain 'Closes #N'"
hard-failed the merge automation on #3921, #3927, #3929 and #3933 in one
night, each needing a manual override.

These tests pin the shape rather than the transport: one helper, native edge
first, body only as a fallback, and no consumer left grepping on its own.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
SCRIPTS = ROOT / "scripts" / "agents"
WORKFLOWS = ROOT / ".github" / "workflows"

#: A body-grep for the closing keyword, in any of the shapes used here.
_BODY_GREP = re.compile(r"(sed -n 's/\.\*Closes #|grep -oiE 'closes #\[0-9\]\+)")


def _uncommented(path: Path) -> str:
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )


class TestTheHelper:
    def test_it_exists_and_asks_github_for_the_link_first(self):
        body = LIB.read_text(encoding="utf-8")
        assert "pr_linked_issue()" in body
        assert "closingIssuesReferences" in body

    def test_the_body_convention_is_the_fallback_not_the_source(self):
        body = LIB.read_text(encoding="utf-8")
        helper = body[body.index("pr_linked_issue()") :]
        helper = helper[: helper.index("\n}\n") + 3]
        assert helper.index("closingIssuesReferences") < helper.index("closes #[0-9]+")

    def test_no_linked_issue_prints_nothing_rather_than_failing(self):
        """A PR that closes no issue is legitimate -- rare, but real. The
        helper reports the absence; each caller decides what it means."""
        body = LIB.read_text(encoding="utf-8")
        helper = body[body.index("pr_linked_issue()") :]
        helper = helper[: helper.index("\n}\n") + 3]
        assert "|| true" in helper
        assert "exit 1" not in helper


class TestNoConsumerGrepsTheBodyItself:
    def test_every_caller_goes_through_the_helper(self):
        """One transitional exemption: `ensure_project_hygiene.yml`'s
        pr-hygiene job checks out RELEASE_BRANCH rather than the PR head, so
        on the PR that ADDS the helper the sourced library does not have it
        yet. It guards with `declare -F` and keeps the body read as the
        fallback arm; delete that arm once this is on the release branch."""
        offenders = []
        sources = list(SCRIPTS.rglob("*.sh")) + sorted(WORKFLOWS.glob("*.yml"))
        for path in sources:
            if path.name == "gh_project.sh":
                continue  # the helper's own fallback lives here
            if path.name == "ensure_project_hygiene.yml":
                # Exempt only while it guards on `declare -F` -- an unguarded
                # body read here would be the old behavior wearing a comment.
                assert "declare -F pr_linked_issue" in path.read_text(encoding="utf-8")
                continue
            if _BODY_GREP.search(_uncommented(path)):
                offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, (
            "these still derive a PR's issue by grepping its body instead of "
            f"reading the native link through pr_linked_issue: {offenders}"
        )

    @pytest.mark.parametrize(
        "consumer",
        [
            "review_ensure_handoff.sh",
            "dispatch_conflict_resolution.sh",
            "scrummaster_sprint_report.sh",
        ],
    )
    def test_the_shell_consumers_call_it(self, consumer):
        assert "pr_linked_issue" in (SCRIPTS / consumer).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "consumer",
        [
            "review_agent_auto_review.yml",
            "ensure_project_hygiene.yml",
            "huddle_decision_dispatch.yml",
            "link_revert_pr_to_issue.yml",
        ],
    )
    def test_the_workflow_consumers_call_it(self, consumer):
        assert "pr_linked_issue" in (WORKFLOWS / consumer).read_text(encoding="utf-8")


class TestTheErrorNoLongerDemandsTheSentence:
    @pytest.mark.parametrize(
        "path",
        [
            WORKFLOWS / "review_agent_auto_review.yml",
            WORKFLOWS / "huddle_decision_dispatch.yml",
            SCRIPTS / "review_ensure_handoff.sh",
        ],
    )
    def test_it_reports_a_missing_issue_not_missing_prose(self, path):
        body = path.read_text(encoding="utf-8")
        assert "does not contain 'Closes #N'" not in body
        assert "closes no issue" in body
