"""No agent-script helper may read GraphQL through a pipeline (#3811, V-040).

`graphql()` in `scripts/agents/lib/gh_project.sh` reports a failed API call by
returning non-zero. Written as ``graphql ... | jq ...`` that status is the
*first* segment's, which a pipeline discards -- so a failed read became empty
output and exit 0, indistinguishable from "the value is unset".

That is not a cosmetic difference. `project_field_value` read that way, and a
rate-limited read would have had `ensure_issue_hygiene.sh` write its defaults
over every already-populated field. The same shape sat in `issue_status` (a
swallowed read reads as "this blocker is not accepted", gating promotion), in
`pr_status` (lane reconciliation), twice in `pr_project_item_id` (a swallowed
find sends the PR down the add path), and in `admin_set_fields.sh`'s
`item_id_for_content` (a swallowed read reads as "not on the board", so the
owner's batch reports a clean run having written nothing).

All of them now take the response into a variable first. This test is the
guard that keeps the class closed: it re-runs the sweep that found them, so a
new helper reintroducing the shape fails here rather than in production, where
the failure mode is silence.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
AGENT_SCRIPTS = sorted(
    {
        *(ROOT / "scripts" / "agents").glob("*.sh"),
        *(ROOT / "scripts" / "agents" / "lib").glob("*.sh"),
    }
)

# A call to the wrapper: `graphql "<query>"` or `graphql $VAR`, at the start of
# a command (line start, or after `$(`, `&&`, `||`, `;`, `|`). `gh api graphql`
# is deliberately NOT matched -- that is the wrapper's own single call site,
# which checks its status explicitly.
GRAPHQL_CALL = re.compile(r"(?:^\s*|[|&;(]\s*|\$\(\s*)graphql\s")

# A pipe that is not `||`. The distinction matters: `x="$(graphql ...)" ||
# return 1` is the CORRECT form and must not be flagged.
PIPE = re.compile(r"(?<!\|)\|(?!\|)")

# The shell continues a command past the newline for a trailing backslash and
# for an unterminated quoted string -- and these calls do both, because the
# GraphQL query is usually a multi-line double-quoted heredoc-ish literal with
# the pipe hanging off its far end. Reading only the first line would miss the
# very shape this test exists to catch.
CONTINUES = re.compile(r"\\\s*$")


def _open_quote(text: str) -> bool:
    """True if ``text`` ends inside an unterminated double-quoted string."""
    return len(re.findall(r'(?<!\\)"', text)) % 2 == 1


def _statement_at(lines: list[str], index: int) -> str:
    """The full logical statement beginning at ``lines[index]``."""
    parts = [lines[index]]
    while index + 1 < len(lines) and (CONTINUES.search(parts[-1]) or _open_quote("\n".join(parts))):
        index += 1
        parts.append(lines[index])
    return "\n".join(parts)


def _strip_comment(line: str) -> str:
    """Drop a whole-line comment; prose about pipes is not a pipe."""
    return "" if line.lstrip().startswith("#") else line


@pytest.mark.parametrize("script", AGENT_SCRIPTS, ids=lambda p: p.name)
def test_no_graphql_call_is_piped(script: Path) -> None:
    """`graphql` output is captured, never piped (#3811)."""
    lines = [_strip_comment(line) for line in script.read_text(encoding="utf-8").splitlines()]

    offenders = []
    for i, line in enumerate(lines):
        if not GRAPHQL_CALL.search(line):
            continue
        statement = _statement_at(lines, i)
        # The call's own arguments can legitimately contain `|` inside a jq
        # program or a query string; what matters is a pipe that takes the
        # wrapper's *stdout*, which follows the closing quote of the call.
        if PIPE.search(statement.split("graphql", 1)[1]):
            offenders.append(f"{script.name}:{i + 1}: {line.strip()}")

    assert not offenders, (
        "a piped `graphql` call discards the wrapper's exit status, so a failed "
        "read returns empty output and exit 0 (V-040). Take the response into a "
        "variable first:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_would_catch_the_original_defect(tmp_path: Path) -> None:
    """The check must reject the shape it was written for, not just pass.

    A guard that cannot fail is not a guard: this reproduces the exact line
    `project_field_value` carried before the fix and asserts it is flagged,
    and pairs it with the fixed form to prove the check is not simply matching
    the word `graphql`.
    """
    before = '  graphql "$q" -F item="$item_id" | jq -r --arg f "$field_name" \'.data\''
    after = '  resp="$(graphql "$q" -F item="$item_id")" || return 1'

    offending = tmp_path / "before.sh"
    offending.write_text(before, encoding="utf-8")
    with pytest.raises(AssertionError):
        test_no_graphql_call_is_piped(offending)

    fixed = tmp_path / "after.sh"
    fixed.write_text(after, encoding="utf-8")
    test_no_graphql_call_is_piped(fixed)


def test_admin_set_fields_suite_passes() -> None:
    """Run the behavioural half (#3775 executed verification).

    The static check above proves no call site *has* the shape; it cannot
    prove the replacement behaves. `tests/test_admin_set_fields.sh` runs the
    real `admin_set_fields.sh` against a `gh` stub whose board read fails, and
    fault-injects the piped form to show the defect reproduces on demand --
    the owner's batch reporting "not on the project board / 0 with failures"
    for items it never touched. It runs here because `pytest tests/unit/` is
    the gate this repo actually runs.
    """
    suite = ROOT / "tests" / "test_admin_set_fields.sh"
    result = subprocess.run(
        ["bash", str(suite)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
