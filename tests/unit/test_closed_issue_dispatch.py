"""The dispatch path and the submit guard must agree about closed issues (#3906).

`developer_submit_for_review.sh` has always refused a closed issue. Nothing
refused to *start* work on one, so on #3825 the review agent dispatched a rework
cycle to an issue that had closed 26 minutes earlier when its PR merged; the
developer implemented and validated a correct, mainline-blocking fix; and the
submit guard then refused it -- identically on every retry. The work was
stranded on a branch and had to be re-landed by hand.

The two halves are asserted together here, because the defect was not in either
half: it was in their disagreement.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
SUBMIT = REPO_ROOT / "scripts" / "agents" / "developer_submit_for_review.sh"


def _dispatch_against_state(tmp_path: Path, state: str) -> subprocess.CompletedProcess[str]:
    """Run the real function with `gh` stubbed to report an issue in `state`."""
    stub = tmp_path / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"/issues/4242"* && "$*" == *"state"* ]]; then\n'
        f'  echo "{state}"\n'
        "  exit 0\n"
        "fi\n"
        'echo "{}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    script = f"""
    export PATH="{tmp_path}:$PATH"
    source "{LIB}" >/dev/null 2>&1
    REPO_OWNER=owner REPO_NAME=repo DEV_AGENT=dev-agent
    # `set -e` comes with the library, so a refusal (nonzero return) would
    # otherwise kill this harness before it could report the code.
    rc=0
    assign_and_trigger_developer 4242 || rc=$?
    echo "rc=$rc"
    """
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, cwd=REPO_ROOT)


def test_dispatch_refuses_a_closed_issue(tmp_path: Path) -> None:
    """Starting work that provably cannot be submitted is worse than not starting."""
    result = _dispatch_against_state(tmp_path, "CLOSED")
    combined = result.stdout + result.stderr
    assert "rc=12" in combined, (
        "dispatch to a CLOSED issue must refuse and return 12; otherwise the "
        "developer does real work the submit guard will reject on every retry"
    )
    assert "CLOSED" in combined


def test_dispatch_does_not_reopen_the_issue(tmp_path: Path) -> None:
    """A closed issue in Acceptance Failed is owner signal (D-008), not a mistake."""
    source = LIB.read_text(encoding="utf-8")
    body = source.split("assign_and_trigger_developer()", 1)[1].split("\n}", 1)[0]
    assert "--method PATCH" not in body and '"state":"open"' not in body, (
        "dispatch must never reopen an issue to make it workable -- the placement "
        "is deliberate owner signal"
    )


def test_the_submit_guard_still_refuses_closed_issues() -> None:
    """The other half of the agreement, asserted so a later edit cannot drop it."""
    text = SUBMIT.read_text(encoding="utf-8")
    assert 'if [[ "$issue_state" != "OPEN" ]]; then' in text, (
        "developer_submit_for_review.sh must still refuse a non-open issue; the "
        "dispatch refusal above is calibrated to it"
    )
