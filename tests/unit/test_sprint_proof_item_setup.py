"""The sprint-iteration proof must not report a cause it did not measure (#4192).

`Release Prereq Smoke` / *Adding a sprint iteration does not wipe board item
values* is a `[required]` check, and on `3a2162c2` it went red like this::

    [proof] config: {"active":[],"completed":["Sprint 3","Sprint 2","Sprint 1"]}
    [proof] WARN: board still shows 2 item(s), expected 3
    [proof] the three test items never appeared -- cannot prove anything

Two things were wrong with that, and neither is the mutation the proof exists
to test:

1. **The cause was discarded.** `assign_item` ran two `gh api graphql`
   mutations and checked *neither*, in a script that deliberately does not set
   `-e`. A write that failed left the item absent and said nothing; the only
   symptom surfaced 40 seconds later as a count, so the report could name
   neither which write failed nor why.
2. **A deadline stood in for a condition.** The *same commit* passed in the
   `pull_request` run one minute later (run 39692595924 vs the `push` run
   39692591792), so the red said nothing about the head -- which is this
   issue's class exactly.

The repair is not a longer sleep. Each write is checked and reports the API's
reason at the moment it fails, and the wait ends in one of three states it can
tell apart: settled, *index lag* (every write returned an id, so the items
exist and only GitHub's item index is behind -- the mutation under test is not
implicated), or *missing* (fewer ids than expected were ever created).

Both directions are executed here against a stub `gh`: the retired body is
replayed and must swallow a failed write, and the shipped body must name it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PROOF = REPO_ROOT / "scripts" / "sprint-iteration-preservation-proof.sh"


def _shell_function(name: str) -> str:
    """The real body of ``name`` out of the proof script.

    Sliced, never retyped: a copy kept here is the thing under test drifting
    away from the thing that runs in CI.
    """
    text = PROOF.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{.*$", text, re.MULTILINE)
    assert match, f"{PROOF} no longer defines {name}()"
    lines = text[match.start() :].splitlines(keepends=True)
    body = [lines[0]]
    for line in lines[1:]:
        body.append(line)
        if line.rstrip("\n") == "}":
            return "".join(body)
    raise AssertionError(f"{name}() is not closed at column 0")


#: The retired `assign_item`, kept verbatim so the defect can be reproduced
#: rather than described. Both mutations unchecked, exactly as it shipped.
RETIRED_ASSIGN_ITEM = """
assign_item() {
  local item
  item="$(graphql 'addDraft' "{}" '.id')"
  graphql 'setField' "{}"
}
"""

#: The stub `gh`. `DRAFT_FAILS_ON` names the title whose draft-issue mutation
#: fails the way GitHub's API does: a message on stderr and a non-zero exit.
STUB_GH = """#!/usr/bin/env bash
body="$(cat)"
if [[ "$body" == *addDraft* || "$body" == *addProjectV2DraftIssue* ]]; then
  if [[ -n "${DRAFT_FAILS_ON:-}" && "$body" == *"$DRAFT_FAILS_ON"* ]]; then
    echo "gh: Could not resolve to a node with the global id of '...' (HTTP 502)" >&2
    exit 1
  fi
  printf 'PVTI_stub_%s\\n' "$RANDOM"
  exit 0
fi
exit 0
"""


@pytest.fixture()
def stub_gh(tmp_path: Path) -> dict[str, str]:
    stub = tmp_path / "gh"
    stub.write_text(STUB_GH, encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    return env


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


#: The harness every case shares: the proof's own `graphql`, plus the ids and
#: the settle knobs the script sets at top level (dialled down so the test
#: measures the decision rather than waiting out the real window).
def _harness(*functions: str) -> str:
    return (
        "set -uo pipefail\n"
        "PROJECT_ID=P; FIELD_ID=F; CREATED_ITEM_IDS=()\n"
        "SETTLE_POLLS=2; SETTLE_INTERVAL=0\n" + _shell_function("graphql") + "".join(functions)
    )


def test_the_shipped_form_names_the_write_that_failed(stub_gh: dict[str, str]) -> None:
    """Direction 1: the cause is reported where it happens, not inferred later."""
    stub_gh["DRAFT_FAILS_ON"] = "item-in-Sprint-3"
    result = _run(
        _harness(_shell_function("assign_item"))
        + 'assign_item ITER "item-in-Sprint-3"; echo "rc=$?"\n'
        + 'echo "recorded=${#CREATED_ITEM_IDS[@]}"\n',
        stub_gh,
    )
    assert "rc=1" in result.stdout, result.stdout + result.stderr
    assert "recorded=0" in result.stdout, result.stdout
    assert "could not create the draft issue 'item-in-Sprint-3'" in result.stderr, result.stderr
    assert "HTTP 502" in result.stderr, "the API's own reason is not relayed"


def test_the_retired_form_swallowed_it(stub_gh: dict[str, str]) -> None:
    """Direction 2, the failure reproduced: unchecked, it reports success.

    This is what made the red uninformative -- the function returned 0 with
    nothing created and nothing said, and the proof found out 40 seconds later
    by counting.
    """
    stub_gh["DRAFT_FAILS_ON"] = "addDraft"
    result = _run(
        _harness(RETIRED_ASSIGN_ITEM) + 'assign_item ITER "item-in-Sprint-3"; echo "rc=$?"\n',
        stub_gh,
    )
    assert "rc=0" in result.stdout, (
        "the retired body no longer swallows a failed write -- this injection is not "
        "reproducing the defect it claims to"
    )
    assert "could not create" not in result.stderr


def test_a_settled_board_returns_its_snapshot(stub_gh: dict[str, str]) -> None:
    result = _run(
        _harness(_shell_function("snapshot_settled"))
        + 'snapshot() { echo \'[{"item":"a"},{"item":"b"}]\'; }\n'
        + "CREATED_ITEM_IDS=(i1 i2)\n"
        + 'snapshot_settled 2 >/dev/null; echo "rc=$?"\n',
        stub_gh,
    )
    assert "rc=0" in result.stdout, result.stdout + result.stderr
    assert "WARN" not in result.stderr


def test_index_lag_is_named_as_index_lag(stub_gh: dict[str, str]) -> None:
    """Every write returned an id, so the mutation under test is not implicated.

    The state run 39692591792 was actually in, and the one its report got
    wrong.
    """
    result = _run(
        _harness(_shell_function("snapshot_settled"))
        + 'snapshot() { echo \'[{"item":"a"},{"item":"b"}]\'; }\n'
        + "CREATED_ITEM_IDS=(i1 i2 i3)\n"
        + 'snapshot_settled 3 >/dev/null; echo "rc=$?"\n',
        stub_gh,
    )
    assert "rc=1" in result.stdout, result.stdout + result.stderr
    assert "item index has not caught up" in result.stderr, result.stderr
    assert "not implicated" in result.stderr
    assert "i1 i2 i3" in result.stderr, "the ids that prove the writes landed are not cited"


def test_a_missing_write_is_not_reported_as_lag(stub_gh: dict[str, str]) -> None:
    result = _run(
        _harness(_shell_function("snapshot_settled"))
        + 'snapshot() { echo \'[{"item":"a"},{"item":"b"}]\'; }\n'
        + "CREATED_ITEM_IDS=(i1 i2)\n"
        + 'snapshot_settled 3 >/dev/null; echo "rc=$?"\n',
        stub_gh,
    )
    assert "rc=1" in result.stdout, result.stdout + result.stderr
    assert "only 2 of 3 item(s) were ever created" in result.stderr, result.stderr
    assert "NOT index lag" in result.stderr


def test_every_assign_item_call_is_checked() -> None:
    """The fix is only a fix if no call site ignores it.

    `assign_item` returning non-zero is the whole repair; a bare call would
    put the swallowed-write behaviour straight back at that site.
    """
    text = PROOF.read_text(encoding="utf-8")
    calls = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^\s*assign_item ", line) and not line.strip().startswith("#")
    ]
    assert calls, "assign_item is never called -- this guard reads nothing"
    unchecked = [call for call in calls if not call.endswith("\\")]
    assert not unchecked, f"these assign_item calls ignore its status: {unchecked}"
