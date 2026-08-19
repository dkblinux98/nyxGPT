"""A failed `closingIssuesReferences` read must not look like "no linked issue".

`test_gh_project_pipelines.py` states the rule as a text contract -- no
`graphql` call may be piped, because a pipe discards the wrapper's exit status
(V-043). This file executes the consequence, because the text contract cannot
tell you what the difference *does*.

Since #3862 the two outcomes mean opposite things. `pr_linked_issue` printing
nothing is a legitimate answer -- a machinery PR with no issue behind it merges
and deliberately skips the issue-side bookkeeping. A GraphQL read that FAILED
prints nothing too, and under the piped shape it did so silently and with exit
0, so a transient API failure wore the costume of "this PR closes no issue" and
would leave a real issue open with its PR merged. That is the same class of
defect as #3789/#3815 (a report trusted in place of evidence), which is exactly
what #3862's closure gate exists to stop.

The tests run the real `pr_linked_issue` body sliced out of the shipped
`gh_project.sh` -- so a regression in the script fails them -- against a stubbed
`graphql` that fails the way a rate-limited or network-broken read does. The
pre-fix piped shape is reconstructed alongside and driven through the same
stubs, so the fix is measured by the difference rather than asserted: the old
shape is silent, the shipped shape says so and still falls back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"

# The pre-#3862-fix shape, verbatim: the wrapper's stdout piped straight into
# jq, so `graphql` failing and `graphql` returning an empty edge are the same
# observable event. Kept here so the injection below is a real comparison.
PRE_FIX_HELPER = """
pr_linked_issue() {
  local pr="$1" q num

  q='query($owner:String!, $name:String!, $pr:Int!){
    repository(owner:$owner, name:$name){
      pullRequest(number:$pr){
        closingIssuesReferences(first:5){ nodes { number } }
      }
    }
  }'
  num="$(graphql "$q" -f owner="$REPO_OWNER" -f name="$REPO_NAME" -F pr="$pr" 2>/dev/null \
    | jq -r '.data.repository.pullRequest.closingIssuesReferences.nodes[0].number // empty' 2>/dev/null)"
  if [[ -n "$num" && "$num" != "null" ]]; then
    echo "$num"
    return 0
  fi

  gh api "repos/${REPO_OWNER}/${REPO_NAME}/pulls/${pr}" --jq '.body // ""' 2>/dev/null \
    | grep -oiE 'closes #[0-9]+' | head -1 | grep -oE '[0-9]+' || true
}
"""


def _shipped_helper() -> str:
    """The real `pr_linked_issue` body, sliced out of the shipped library.

    Sourcing the whole of `gh_project.sh` would pull in its project-id
    resolution and command requirements, so the tests execute exactly the
    function under test -- read from the file rather than restated, so a
    regression in the library fails these tests.
    """
    text = LIB.read_text(encoding="utf-8")
    start = text.index("pr_linked_issue() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    body = text[start:end]
    assert "closingIssuesReferences" in body, (
        "scripts/agents/lib/gh_project.sh no longer defines pr_linked_issue over the "
        "native closing link -- this test is slicing the wrong region"
    )
    return body


def _run(helper: str, *, graphql_ok: bool, pr_body: str) -> subprocess.CompletedProcess[str]:
    """Drive `helper` with a graphql that succeeds or fails, and a PR body.

    `graphql_ok=False` is the injected condition: the wrapper returns non-zero
    and writes nothing, which is what a rate-limited, unauthenticated or
    network-broken read looks like from the caller's side.
    """
    if graphql_ok:
        graphql_stub = (
            'graphql() { echo \'{"data":{"repository":{"pullRequest":'
            '{"closingIssuesReferences":{"nodes":[{"number":3825}]}}}}}\'; }'
        )
    else:
        graphql_stub = "graphql() { return 1; }"

    script = f"""
set -uo pipefail
REPO_OWNER=an-owner
REPO_NAME=a-repo
_warn() {{ echo "[warning] $*" >&2; }}
{graphql_stub}
gh() {{ printf '%s\\n' {pr_body!r}; }}
{helper}
pr_linked_issue 3932
"""
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30, cwd=str(REPO_ROOT)
    )


class TestTheNativeLinkIsRead:
    def test_the_closing_edge_is_the_answer_when_it_resolves(self):
        result = _run(_shipped_helper(), graphql_ok=True, pr_body="no prose here")
        assert result.stdout.strip() == "3825"
        assert result.returncode == 0

    def test_a_successful_read_says_nothing_on_stderr(self):
        """The warning must fire on failure only -- a warning on every healthy
        lookup is noise the next reader learns to skip past."""
        result = _run(_shipped_helper(), graphql_ok=True, pr_body="no prose here")
        assert result.stderr.strip() == ""


class TestAFailedReadIsNotSilence:
    def test_it_reports_the_failed_read_and_still_falls_back(self):
        result = _run(_shipped_helper(), graphql_ok=False, pr_body="Closes #3825")
        assert "closingIssuesReferences read failed" in result.stderr
        assert "#3932" in result.stderr, "the warning must name the PR it was reading"
        # The fallback is still taken: a body convention is worse evidence than
        # the native edge, and better than none.
        assert result.stdout.strip() == "3825"

    def test_the_pre_fix_shape_was_silent(self):
        """The fault-injection half. Same stubs, same failure, old shape: the
        piped call swallows the non-zero status, so the caller cannot tell a
        broken read from a PR that closes no issue."""
        result = _run(PRE_FIX_HELPER, graphql_ok=False, pr_body="Closes #3825")
        assert result.stderr.strip() == ""
        assert result.returncode == 0

    def test_a_failed_read_with_no_prose_prints_nothing_but_is_not_quiet(self):
        """The dangerous case: the read failed AND the body says nothing, so
        stdout is identical to a genuine no-issue PR. The stderr line is the
        only thing separating them."""
        result = _run(_shipped_helper(), graphql_ok=False, pr_body="a body with no closing prose")
        assert result.stdout.strip() == ""
        assert "closingIssuesReferences read failed" in result.stderr


class TestNoIssueIsStillALegitimateAnswer:
    def test_a_healthy_read_with_no_link_prints_nothing_and_succeeds(self):
        """#3862: a PR with no issue behind it merges and skips the issue-side
        bookkeeping -- it does not jam the pipeline, and it must not warn."""
        script_helper = _shipped_helper()
        result = _run(script_helper, graphql_ok=True, pr_body="no prose here")
        assert result.returncode == 0
        # ...and with an empty edge rather than a populated one:
        empty = subprocess.run(
            [
                "bash",
                "-c",
                f"""
set -uo pipefail
REPO_OWNER=an-owner
REPO_NAME=a-repo
_warn() {{ echo "[warning] $*" >&2; }}
graphql() {{ echo '{{"data":{{"repository":{{"pullRequest":{{"closingIssuesReferences":{{"nodes":[]}}}}}}}}}}'; }}
gh() {{ printf '%s\\n' 'a body with no closing prose'; }}
{script_helper}
pr_linked_issue 3932
""",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(REPO_ROOT),
        )
        assert empty.returncode == 0
        assert empty.stdout.strip() == ""
        assert empty.stderr.strip() == ""
