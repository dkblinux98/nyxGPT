"""An issue carries exactly one assignee and exactly one label (owner rules).

Both rules already existed in pieces and both were violated in practice:

* **One assignee.** `assign_issue_verified` PATCHes the whole list and reads
  it back, so every path that used it was already correct. The paths that
  used GitHub's *add* verbs were not -- `POST /issues/{n}/assignees` and
  `issues.addAssignees` append -- and that is how issues came to show
  `scrummaster + developer` or `review + developer` at once, which makes "who
  owns this?" unanswerable from the board and miscounts every sweep that asks
  who an issue is assigned to.
* **One label.** `developer_submit_for_review.sh` fails outright on an issue
  with two real labels, and project hygiene stamps `Feature` only on an issue
  that has none. Hygiene decided "has none" against a hardcoded list of four
  label names, so a label added to the project later was invisible to it: an
  `Agent`-labeled issue read as unlabeled, got `Feature` stamped on top, and
  deadlocked at submit time -- the exact failure the hygiene comment says it
  exists to prevent (#3390, #3413, #3415).

Both fixes are one-liners. These tests are here because both rules were
already written down somewhere and drifted anyway: what protects them is a
check that runs, not a sentence in a runbook.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / "scripts"
HYGIENE = SCRIPTS / "agents" / "ensure_issue_hygiene.sh"

#: Every shape that APPENDS an assignee rather than setting the list.
#:
#: `gh api .../assignees` with `-f`/`-F` and no explicit method defaults to
#: POST, so matching only `-X POST` left the commonest form invisible -- the
#: gap the review found. `--method POST` and `gh issue edit --add-assignee`
#: are the other two spellings in use.
_REST_ADD = re.compile(
    r"(?:-X\s+POST|--method\s+POST)[^\n]*issues/[^\n]*?/assignees"
    r"|gh api[^\n]*issues/[^\n]*?/assignees[^\n]*?-[fF]\s"
    r"|gh issue edit[^\n]*--add-assignee"
)

#: `gh pr edit --add-assignee` is fine: the rule is about issues, and a PR
#: legitimately carries an author and a reviewer. So is an error message that
#: *names* the manual fix -- text is not a call.
#: A line that merely *starts* with a quote is a continuation of the string
#: above it (a wrapped `echo`), not a command of its own.
_NOT_A_CALL = re.compile(r"^\s*(echo|printf)\b|^\s*\"|::error::|::warning::|Manual fix:")
#: The Octokit/github-script equivalent.
_SCRIPT_ADD = re.compile(r"issues\.addAssignees\s*\(")


def _sources() -> list[Path]:
    files = [p for p in SCRIPTS.rglob("*.sh") if "__pycache__" not in str(p)]
    files += sorted(WORKFLOWS.glob("*.yml"))
    return files


def _uncommented(path: Path) -> str:
    """Drop shell/YAML comment lines: history in a comment is not a call."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith(("#", "//"))
    )


class TestExactlyOneAssignee:
    def test_nothing_appends_an_assignee_to_an_issue(self):
        offenders = []
        for path in _sources():
            for line in _uncommented(path).splitlines():
                if _NOT_A_CALL.search(line):
                    continue
                if _REST_ADD.search(line) or _SCRIPT_ADD.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}: {line.strip()[:70]}")
        assert not offenders, (
            "these append an assignee instead of setting one, which leaves an "
            f"issue with two: {offenders}. Use assign_issue_verified (or PATCH "
            "the issue with the full assignees list) so the result is exactly "
            "one assignee, verified."
        )

    def test_the_sanctioned_helper_replaces_and_verifies(self):
        lib = (SCRIPTS / "agents" / "lib" / "gh_project.sh").read_text(encoding="utf-8")
        assert '-X PATCH "repos/${REPO_OWNER}/${REPO_NAME}/issues/${issue}"' in lib
        # It reads the list back and compares to the single expected login.
        assert 'if [[ "$actual" == "$assignee" ]]; then' in lib

    def test_the_retry_refires_send_a_documented_clear(self):
        """`-F "assignees[]="` looks like a clear and is not: it sends
        {"assignees":[""]}, an assignee literally named "". The clear then
        no-ops, the set that follows is a same-login replace, and a same-login
        replace emits no `assigned` event (#3647) -- so the re-fire re-fires
        nothing. Both auto-retry paths depend on this."""
        for path in (
            WORKFLOWS / "developer_auto_implement.yml",
            WORKFLOWS / "usage_limit_retry.yml",
        ):
            body = _uncommented(path)
            assert "--input - <<<'{\"assignees\":[]}'" in body, f"{path.name}"
            assert '-F "assignees[]="' not in body, f"{path.name} still sends the empty-string form"

    def test_the_closure_rule_sets_the_owner_rather_than_adding_them(self):
        body = HYGIENE.read_text(encoding="utf-8")
        closure = body[body.index("apply_closure_rule() {") :]
        assert 'assign_issue_verified "$ISSUE" "$HUMAN_OWNER"' in closure
        assert "/assignees" not in closure


class TestExactlyOneLabel:
    def test_hygiene_counts_real_labels_instead_of_recognising_four_names(self):
        body = HYGIENE.read_text(encoding="utf-8")
        assert "real_label_names" in body, (
            "hygiene must decide 'is this issue labeled?' by counting real "
            "labels, not by matching a hardcoded list of names -- a label "
            "added to the project later is invisible to a name list, and gets "
            "a second label stamped on top of it"
        )
        assert 'grep -qE "Acceptance Failure|Improvement' not in body

    def test_the_submit_gate_and_hygiene_share_one_definition_of_a_real_label(self):
        """Two different answers to 'does this issue have a label?' is how the
        deadlock happened: hygiene said no and stamped one, the submit script
        said two and refused."""
        submit = (SCRIPTS / "agents" / "developer_submit_for_review.sh").read_text(encoding="utf-8")
        assert "real_label_names" in submit
        assert "real_label_names" in HYGIENE.read_text(encoding="utf-8")
