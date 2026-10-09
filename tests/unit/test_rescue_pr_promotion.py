"""A promoted rescue PR stops describing itself as a rescue draft (#4184).

`developer_ensure_pr_exists.sh` opens a draft that says, correctly, "⚠️ Rescue
PR -- this work did not complete its checks" / "**This is not a submission for
review**", under a `wip:` title. `developer_auto_implement.yml` promotes that
draft when a continuation run's verification passes -- and used to rewrite only
`Refs #N` into `Closes #N`, leaving both sentences and the `wip:` prefix on a
PR that was then reviewed as a submission and would have merged carrying
`wip:` (review of PR #4191).

A record answering for a state the thing is no longer in is the class #4184 is
about, so the transform lives in one place with these properties pinned:

* the preamble is replaced, not merely deleted -- an operator reading the PR
  cold still learns the branch reached review the long way;
* the Context list survives: it describes the branch, not the draft's state;
* `Refs #N` becomes the closing reference the PR rules require, and only for
  this issue number;
* it is idempotent, because the promotion step now runs on every hand-off
  rather than only on a draft;
* a body that was never a rescue one is left exactly as it was.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "agents" / "lib" / "rescue_pr.py"


def _load():
    spec = importlib.util.spec_from_file_location("rescue_pr", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rescue_pr = _load()

# The body `developer_ensure_pr_exists.sh` writes, trimmed to the parts that
# carry meaning for this transform.
RESCUE_BODY = """<!-- rescue-pr: issue-4184 -->
## ⚠️ Rescue PR — this work did not complete its checks

The developer-agent run for #4184 pushed `feat/4184-x` to `origin` and then
ended before reaching `developer_submit_for_review.sh`.

**This is not a submission for review.** It is deliberately a draft: the run's
verification did not pass, so the work is incomplete by definition.

What to do with it:

- **Continue the work** — reassign the developer agent to #4184.
- **Discard it** — close this PR.

## Context
- Issue: https://github.com/dkblinux98/nyxGPT/issues/4184
- Head branch: `feat/4184-x`

Refs #4184
"""


@pytest.mark.unit
def test_the_promoted_body_no_longer_says_it_is_not_a_submission():
    promoted = rescue_pr.promote_body(RESCUE_BODY, 4184)

    assert promoted is not None
    assert "This is not a submission for review" not in promoted
    assert "⚠️ Rescue PR" not in promoted
    assert "verification did not pass" not in promoted


@pytest.mark.unit
def test_the_promotion_replaces_the_preamble_rather_than_deleting_it():
    """Silence would lose the one fact the preamble carried that is still true:
    this branch reached review the long way, through a rescue draft."""
    promoted = rescue_pr.promote_body(RESCUE_BODY, 4184)

    assert "Promoted from a rescue draft" in promoted
    assert "#3862" in promoted


@pytest.mark.unit
def test_the_context_list_survives_the_promotion():
    """It describes the branch, not the draft's state."""
    promoted = rescue_pr.promote_body(RESCUE_BODY, 4184)

    assert "## Context" in promoted
    assert "Head branch: `feat/4184-x`" in promoted


@pytest.mark.unit
def test_the_reference_becomes_a_closing_one():
    promoted = rescue_pr.promote_body(RESCUE_BODY, 4184)

    assert "Closes #4184" in promoted
    assert "Refs #4184" not in promoted


@pytest.mark.unit
def test_a_longer_issue_number_is_not_rewritten_by_a_prefix_match():
    """`Refs #41840` is a different issue; a substring rewrite would mangle it."""
    body = RESCUE_BODY.replace("Refs #4184\n", "Refs #4184\nSee also Refs #41840\n")

    promoted = rescue_pr.promote_body(body, 4184)

    assert "Refs #41840" in promoted


@pytest.mark.unit
def test_promotion_is_idempotent():
    """The step runs on every hand-off now, not only on a draft, because a PR
    promoted by an earlier round is exactly the one carrying the stale record."""
    once = rescue_pr.promote_body(RESCUE_BODY, 4184)

    assert rescue_pr.promote_body(once, 4184) is None


@pytest.mark.unit
def test_an_ordinary_submission_body_is_left_alone():
    body = "Closes #4184\n\n## Summary\nA change.\n"

    assert rescue_pr.promote_body(body, 4184) is None


@pytest.mark.unit
def test_a_rescue_body_with_no_context_section_still_loses_its_preamble():
    """A hand-edited body is still promoted: leaving the preamble is the
    failure, so a missing section must not make this refuse."""
    body = RESCUE_BODY.split("## Context")[0] + "Refs #4184\n"

    promoted = rescue_pr.promote_body(body, 4184)

    assert "This is not a submission for review" not in promoted
    assert "Promoted from a rescue draft" in promoted


@pytest.mark.unit
def test_the_wip_prefix_is_dropped_from_the_title():
    assert rescue_pr.promote_title("wip: bug: x (#4184)") == "bug: x (#4184)"


@pytest.mark.unit
def test_a_title_that_only_mentions_wip_later_is_untouched():
    """Only the prefix the rescue path writes is removed."""
    title = "feat: record wip: markers (#4184)"

    assert rescue_pr.promote_title(title) == title


@pytest.mark.unit
def test_the_cli_writes_the_body_and_prints_the_title(tmp_path):
    body_file = tmp_path / "body.md"
    body_file.write_text(RESCUE_BODY, encoding="utf-8")

    rc = rescue_pr.main(
        [
            "promote",
            "--body-file",
            str(body_file),
            "--issue",
            "4184",
            "--title",
            "wip: bug: x (#4184)",
        ]
    )

    assert rc == 0
    assert "Closes #4184" in body_file.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_cli_prints_nothing_when_there_is_nothing_to_promote(tmp_path, capsys):
    """The caller's whole "did anything change" test is whether stdout is empty,
    so a no-op must not print a title it would then pointlessly re-apply."""
    body_file = tmp_path / "body.md"
    body_file.write_text("Closes #4184\n\n## Summary\nA change.\n", encoding="utf-8")

    rc = rescue_pr.main(
        ["promote", "--body-file", str(body_file), "--issue", "4184", "--title", "bug: x (#4184)"]
    )

    assert rc == 0
    assert capsys.readouterr().out == ""
