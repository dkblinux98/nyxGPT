"""Guards for how a failed developer run is classified (#3909).

Two defects on 2026-08-18, both of which reached the owner as a FATAL page for
a failure that needed no human at all:

  * `529 Overloaded` matched no signature, and an unmatched signature defaults
    to fatal;
  * the escalation condition ORs the three analysis phases together, so
    Phase 1's `unknown` -- a *non-answer* from a grep over log lines -- fired
    the alarm even though Phase 3 had already concluded TRANSIENT and written
    the reasoning into the run log.

The second is the one worth pinning hardest: the correct answer existed and was
discarded by a less-informed check.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "agents" / "lib" / "gh_project.sh"
DEV_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "developer_auto_implement.yml"


def _classify(error_text: str) -> str:
    """Run the real shell classifier, not a reimplementation of it."""
    script = f'source "{LIB}" >/dev/null 2>&1; classify_error "$1"'
    result = subprocess.run(
        ["bash", "-c", script, "_", error_text],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    "line",
    [
        "API error: 529 Overloaded",
        "  ⚠ Claude API returned HTTP 529",
        "Error: Overloaded",
        # The API's own error shape, not just its message. `overloaded_error`
        # has no trailing word boundary, so a pattern anchored on both sides
        # would silently stop recognising it (#4192).
        '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
        'status_code=529 body={"type":"overloaded_error"}',
    ],
)
def test_api_overload_is_retriable(line: str) -> None:
    """529 is the API describing its own capacity, not this run's work."""
    assert _classify(line) == "retriable:api_overloaded", (
        "HTTP 529 must classify as retriable -- it says nothing about the change "
        "under test, and the failed run leaves nothing to repair"
    )


def test_a_genuinely_fatal_error_still_escalates() -> None:
    """The widening must not swallow the failures that do need a human."""
    assert _classify("remote: Authentication failed for repository").startswith("fatal:")


def test_unknown_is_still_unknown() -> None:
    """Not every unmatched line should be optimistically retried either."""
    assert _classify("something nobody has ever seen before") == "unknown"


# --- #4192: a signature matches a SIGNAL, never an identifier ---------------
#
# The text `classify_error` reads is a run log. It carries commit SHAs, GitHub
# run and job ids, PR numbers and pytest node ids with line numbers, and the
# signatures are tried in order -- so one signature that can be satisfied by
# that surrounding noise pre-empts the real one for the failure underneath it.
#
# That is what happened: `529|[Oo]verloaded` sat above the red-head signature
# and matched the bare digits `529` anywhere, so the SAME refusal classified
# `retriable:ci_red` with head `0ac51a4c...` and `retriable:api_overloaded`
# with head `3f529aa0...`. A few percent of real red-head refusals were waited
# out and retried against an unchanged red head instead of being handed back to
# the developer (#3971's contract), and `tests/test_reviewable_head_gate.sh`
# flaked on its fixture repository's random SHA (run 38003374998).

#: One real refusal, with the identifiers it genuinely carries. Written as a
#: format string over the head SHA so the SHA is the only thing that varies --
#: which is the whole claim: it must not be able to change the answer.
_RED_HEAD_REFUSAL = (
    "[error] Required checks FAILED on head {sha}: k8s-artifact-smoke\n"
    "[error] Refusing to submit: a red head is not reviewable (#3971).\n"
    "red-head-check: k8s-artifact-smoke "
    "https://github.com/dkblinux98/nyxGPT/actions/runs/{run}/job/{job}\n"
    "[error] This is the developer round's work, not the reviewer's -- fix the\n"
    "[error] failing check(s), push, and submit again.\n"
)


@pytest.mark.parametrize(
    ("sha", "run", "job"),
    [
        # The benign draw, which always passed.
        ("0ac51a4cbb2f41d9f0b7cf7e2b2b4b2a9d1c7e55", "38003375148", "114053035045"),
        # The draws that did not. `529` in the head SHA, in the run id and in
        # the job id -- each on its own was enough to flip the class.
        ("3f529aa0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6", "38003375148", "114053035045"),
        ("0ac51a4cbb2f41d9f0b7cf7e2b2b4b2a9d1c7e55", "38003529148", "114053035045"),
        ("0ac51a4cbb2f41d9f0b7cf7e2b2b4b2a9d1c7e55", "38003375148", "114053035529"),
        # And all three at once, which is the adversarial case the issue asks
        # for explicitly.
        ("529f529a529b529c529d529e529f5290a1b2c3d4", "52952995291", "52905291529"),
    ],
)
def test_a_red_head_refusal_classifies_ci_red_whatever_its_sha(
    sha: str, run: str, job: str
) -> None:
    """The fixture's identifiers must not decide the class (#4192)."""
    text = _RED_HEAD_REFUSAL.format(sha=sha, run=run, job=job)
    assert _classify(text) == "retriable:ci_red", (
        "a red head is the developer round's work (#3971). Classifying it as an API "
        "overload waits two minutes and retries the same unchanged red head, and which "
        f"way it went depended on the digits in {sha!r}/{run}/{job}"
    )


def test_the_retired_overload_pattern_really_did_misclassify_it() -> None:
    """Fault injection: the assertion above has to be able to fail.

    The pre-#4192 signature is restored and run against the same text. If this
    ever stops matching, the parametrized test above is passing for a reason
    other than the fix and the guard is decoration.
    """
    text = _RED_HEAD_REFUSAL.format(
        sha="3f529aa0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6", run="38003375148", job="114053035045"
    )
    retired = subprocess.run(
        ["grep", "-qE", "529|[Oo]verloaded"],
        input=text,
        capture_output=True,
        text=True,
    )
    assert retired.returncode == 0, (
        "the retired `529|[Oo]verloaded` pattern no longer matches a red-head refusal "
        "carrying 529 in its head SHA, so the parametrized test above would pass "
        "against the unfixed classifier too"
    )


def _signature_patterns() -> list[str]:
    """Every pattern `classify_error` tries, read out of the function body.

    Read rather than restated: a list written twice is a list that drifts,
    which is the lesson #4179 paid for. One entry per `grep -q…  "<pattern>"`.
    """
    body = LIB.read_text(encoding="utf-8")
    start = body.index("classify_error() {")
    end = body.index("\n}\n", start)
    found = re.findall(r'grep -q[a-zA-Z]*\s+"([^"]+)"', body[start:end])
    assert found, "no signatures found in classify_error -- this guard reads nothing"
    return found


#: Text that carries NO failure signal at all: only the identifiers a run log
#: prints beside one. Every one of these is a real shape from this repository.
_IDENTIFIER_NOISE = [
    "head=3f529aa0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6",
    "https://github.com/dkblinux98/nyxGPT/actions/runs/38003375148/job/114053035045",
    "claude/issue-4192-20261010-1627",
    "feat/4192-fix-v3-0-1-required-gates-red-flaky-or-unenforced-",
    "PR #4191 (#4184) -> v3.0.1 at f4a357e7",
    "tests/unit/test_failure_classification.py:529",
    "529 529529 5295291",
    "Phase 0 inventories, provisions, then gates",
]


@pytest.mark.parametrize("noise", _IDENTIFIER_NOISE)
def test_no_signature_fires_on_identifier_noise(noise: str) -> None:
    """A signature that matches an identifier classifies the wrong thing (#4192).

    The property, not the one instance: every signature in the table is run
    against text that is nothing but identifiers. A match means that signature
    can pre-empt the real class for any failure whose log happens to print one
    of these -- which is how a red-head refusal became an API overload.
    """
    offenders = []
    for pattern in _signature_patterns():
        matched = subprocess.run(
            ["grep", "-qE", pattern], input=noise, capture_output=True, text=True
        )
        if matched.returncode == 0:
            offenders.append(pattern)
    assert not offenders, (
        f"these signatures match identifier noise with no failure signal in it "
        f"({noise!r}): {offenders}. Anchor each to what the text SAYS the value is "
        "(an HTTP status, a sentence) rather than to the digits or words appearing "
        "anywhere in a run log"
    )


def test_the_whole_classification_of_noise_is_unknown() -> None:
    """And the end-to-end answer for pure noise is `unknown`, not a guess.

    `unknown` routes to Phase 3 for a real diagnosis (ledger: `error_classes`
    DIAGNOSE), which is the honest outcome when nothing in the text names a
    failure. Asserted through the real classifier as well as per-pattern above,
    because ORDER is what turned an incidental match into a wrong answer.
    """
    for noise in _IDENTIFIER_NOISE:
        assert _classify(noise) == "unknown", f"{noise!r} classified as something"


def _escalation_condition() -> str:
    workflow = yaml.safe_load(DEV_WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name", "").startswith("Escalate fatal error"):
                return " ".join(str(step["if"]).split())
    raise AssertionError("the fatal-escalation step is gone")


def test_phase_three_transient_suppresses_the_escalation() -> None:
    """The precedence, asserted on the real condition rather than trusted.

    Phase 1 is a grep; Phase 3 is an analysis that reads the run. When they
    disagree and Phase 3 says TRANSIENT, the alarm must stand down -- the
    auto-retry step is what owns the failure then.
    """
    condition = _escalation_condition()
    assert re.search(r"claude_result\.outputs\.status\s*!=\s*'TRANSIENT'", condition), (
        "the fatal escalation must not fire when Phase 3 concluded TRANSIENT; "
        "without this, Phase 1's `unknown` pages the owner over a capacity blip "
        "while the correct diagnosis sits in the same run log"
    )


def test_the_transient_retry_still_owns_those_failures() -> None:
    """Suppressing the alarm is only safe because something else acts."""
    workflow = yaml.safe_load(DEV_WORKFLOW.read_text(encoding="utf-8"))
    conditions = [
        " ".join(str(step.get("if", "")).split())
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name", "").startswith("Auto-retry on failure")
    ]
    assert conditions, "the auto-retry step is gone -- nothing would handle a TRANSIENT"
    assert any(
        "claude_result.outputs.status == 'TRANSIENT'" in c for c in conditions
    ), "auto-retry must still fire on a Phase 3 TRANSIENT verdict"
