"""No workflow string carrying a `${{ }}` expression may reach 21,000 characters.

GitHub evaluates any YAML string containing an expression as one template, and
refuses one over **21,000 characters** -- not at that step, but by rejecting the
*whole workflow file*. Nothing runs and nothing reports: a pushed branch shows a
`push` run of the workflow (a workflow with no `push` trigger) failed with zero
jobs and "This run likely failed because of a workflow file issue".

That is how #4183's PR #4185 sat with its review requested and no review: the
class-sweep gate it added grew `claude-code-review.yml`'s review prompt from
17,798 to 22,659 characters, so the PR's `review_requested` event had no valid
workflow to run. The YAML parsed, actionlint was clean, every unit test passed
-- the limit is enforced only by GitHub, at run time, on the branch that breaks
it. This test enforces it before a push.

The prompts are the strings that grow: each new gate or rule gets written into
them. Prefer stating a rule once in its runbook (`agents/runbooks/`) and
pointing the prompt at it -- the agents read the checkout -- over restating it
in the prompt. If a prompt genuinely needs more room, render it in an earlier
step and pass it as a step output: the 21,000 limit applies to the template
string in the YAML, not to an output's value.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: GitHub's limit on an expression-bearing string ("Exceeded max expression
#: length"). Strictly below it is accepted.
MAX_EXPRESSION_STRING = 21_000


def _expression_strings(node, path):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _expression_strings(value, (*path, str(key)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _expression_strings(value, (*path, str(index)))
    elif isinstance(node, str) and "${{" in node:
        yield "/".join(path), node


def _oversized(text: str) -> list[tuple[str, int]]:
    return [
        (where, len(value))
        for where, value in _expression_strings(yaml.safe_load(text), ())
        if len(value) >= MAX_EXPRESSION_STRING
    ]


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_no_expression_string_reaches_githubs_limit(workflow: Path) -> None:
    oversized = _oversized(workflow.read_text(encoding="utf-8"))
    assert not oversized, (
        f"{workflow.name}: GitHub rejects the whole workflow when an expression-bearing "
        f"string reaches {MAX_EXPRESSION_STRING} characters -- {oversized}. State the rule "
        "once in its runbook and point the prompt at it, or render the prompt in an earlier "
        "step and pass it as an output."
    )


#: An empty expression -- GitHub fails the whole file with "An expression was
#: expected". The fix for the limit above first reintroduced exactly this, by
#: writing the two-brace syntax into prose inside the prompt (2026-10-09).
EMPTY_EXPRESSION = re.compile(r"\$\{\{\s*\}\}")


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_no_empty_expression(workflow: Path) -> None:
    lines = [
        number
        for number, line in enumerate(workflow.read_text(encoding="utf-8").splitlines(), 1)
        if EMPTY_EXPRESSION.search(line) and not line.lstrip().startswith("#")
    ]
    assert not lines, (
        f"{workflow.name}: empty expression at line(s) {lines} -- GitHub rejects the whole "
        "file ('An expression was expected'). Don't write the two-brace syntax in prose."
    )


def test_the_check_sees_the_shape_that_broke_pr_4185() -> None:
    """The discriminator, pinned: a prompt over the limit is caught, one under is not."""
    over = (
        "jobs:\n  r:\n    steps:\n      - with:\n          prompt: |\n            ${{ github.event.pull_request.number }}\n"
        + ("            " + "x" * 80 + "\n") * 300
    )
    under = over.replace("x" * 80, "x" * 40)
    assert _oversized(over)
    assert not _oversized(under)
