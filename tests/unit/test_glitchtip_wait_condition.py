"""The GlitchTip ingestion wait is a condition, not a deadline (#4192).

`k8s-local-smoke.sh` 7e asserts that an error reported inside the cluster
reaches the in-cluster GlitchTip. Until this change the wait was a 240s
deadline -- raised from 120s by #4182 on the diagnosis "the worker's queue
latency under CPU pressure has no upper bound this script can derive" -- and
its failure read::

    the api accepted the error but GlitchTip never received it -- the DSN
    does not resolve to the in-cluster GlitchTip

Both halves were wrong on run 39679850907, and the run itself says so: the
diagnostics step printed ``api DSN: http://REDACTED@glitchtip:8080/1`` -- the
DSN resolved -- and the worker's log held **400** ``Enqueuing due task`` lines
and **0** ``Processing task`` lines. Measured against a kind cluster running
the same image and the same manifest, a healthy worker logs them 1:1 (133/133,
each task finishing in ~3ms). The worker's scheduler half was up and its
consumer half had never started, so no deadline could have produced the issue,
and the next reader was sent to look at a DSN that was correct.

That is this issue's class twice over: a required gate whose red is not
evidence about the commit, and a report that names a cause it never measured.
So the wait now ends in one of three *states*, and this file runs the real
functions out of the script -- never a copy -- against a stub ``kubectl`` in
each of them:

1. the issue appears while the worker is consuming  -> pass
2. the worker schedules and consumes nothing        -> fail fast, "not the DSN"
3. the worker consumes and the issue never arrives  -> fail, "did not reach"

Direction 2 is the one that matters: it is the state the release branch was
actually in, and the retired form could only reach it by spending its whole
budget and then misnaming it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SMOKE = REPO_ROOT / "scripts" / "k8s-local-smoke.sh"

#: The functions under test, lifted from the shipped script.
LIFTED = (
    "glitchtip_queue_depth",
    "glitchtip_worker_counts",
    "wait_for_glitchtip_issue",
)


def _shell_function(name: str) -> str:
    """The real body of ``name`` out of the smoke script.

    Sliced rather than restated: a copy maintained here would be the thing
    under test drifting away from the thing that ships, which is how the
    retired wait kept its wrong failure message through two rounds.
    """
    text = SMOKE.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{$", text, re.MULTILINE)
    assert match, f"{SMOKE} no longer defines {name}()"
    lines = text[match.start() :].splitlines(keepends=True)
    body = [lines[0]]
    for line in lines[1:]:
        body.append(line)
        if line.rstrip("\n") == "}":
            return "".join(body)
    raise AssertionError(f"{name}() is not closed at column 0")


@pytest.fixture()
def stub_kubectl(tmp_path: Path):
    """A ``kubectl`` on PATH that answers the four calls the wait makes.

    Parameterised through the environment so one stub covers every state:
    ``STUB_SCHEDULED``/``STUB_PROCESSED`` shape the worker log,
    ``STUB_ISSUE_AT_POLL`` decides when (if ever) the issue list starts
    carrying the marker, and the poll counter lives in a file because each
    call is a fresh process.
    """

    def _install(**env: str) -> dict[str, str]:
        counter = tmp_path / "polls"
        counter.write_text("0", encoding="utf-8")
        stub = tmp_path / "kubectl"
        stub.write_text(
            """#!/usr/bin/env bash
args="$*"
case "$args" in
  *"logs deploy/glitchtip-worker"*)
    for _ in $(seq 1 "${STUB_SCHEDULED:-0}"); do echo "Enqueuing due task: uptime-dispatch-checks"; done
    for _ in $(seq 1 "${STUB_PROCESSED:-0}"); do echo "Processing task abc: apps.uptime.tasks.dispatch_checks"; done
    exit 0 ;;
  *"exec deploy/glitchtip-redis"*)
    printf %s "${STUB_QUEUE_DEPTH:-0}"; exit 0 ;;
  *"exec deploy/nyxgpt-api-stable"*)
    echo "api DSN: http://REDACTED@glitchtip:8080/1"; exit 0 ;;
  *"exec deploy/grafana"*)
    n=$(( $(cat "$STUB_COUNTER") + 1 ))
    printf %s "$n" > "$STUB_COUNTER"
    at="${STUB_ISSUE_AT_POLL:-0}"
    if [ "$at" != 0 ] && [ "$n" -ge "$at" ]; then
      echo '[{"title": "'"$STUB_MARKER"'"}]'
    else
      echo '[]'
    fi
    exit 0 ;;
esac
exit 0
""",
            encoding="utf-8",
        )
        stub.chmod(0o755)
        return dict(
            os.environ,
            PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            STUB_COUNTER=str(counter),
            STUB_MARKER=MARKER,
            **env,
        )

    return _install


MARKER = "k8s-smoke-synthetic-error-4242"

HARNESS = """
set -uo pipefail
NAMESPACE=nyxgpt
{functions}
export GLITCHTIP_WAIT_SLEEP=0
export GLITCHTIP_WAIT_POLLS=12
export GLITCHTIP_WORKER_GRACE_POLLS=3
if wait_for_glitchtip_issue "{marker}"; then
  echo "RESULT=pass"
else
  echo "RESULT=fail"
fi
echo "REASON=${{GLITCHTIP_WAIT_REASON}}"
echo "SCHEDULED=${{GLITCHTIP_WORKER_SCHEDULED}}"
echo "PROCESSED=${{GLITCHTIP_WORKER_PROCESSED}}"
echo "POLLS=$(cat "$STUB_COUNTER")"
"""


def _run(env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    script = tmp_path / "harness.sh"
    script.write_text(
        HARNESS.format(
            functions="\n".join(_shell_function(name) for name in LIFTED),
            marker=MARKER,
        ),
        encoding="utf-8",
    )
    done = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env, timeout=120
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return dict(
        line.split("=", 1) for line in done.stdout.splitlines() if line.count("=") >= 1
    )


def test_the_issue_arriving_late_still_passes(stub_kubectl, tmp_path: Path) -> None:
    """A slow but working worker must not be failed: the loop keeps waiting."""
    env = stub_kubectl(STUB_SCHEDULED="40", STUB_PROCESSED="40", STUB_ISSUE_AT_POLL="9")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "pass"
    assert out["REASON"] == "found"
    assert int(out["POLLS"]) == 9, "it stopped the moment the issue appeared"


def test_a_worker_that_consumes_nothing_fails_fast_and_says_so(
    stub_kubectl, tmp_path: Path
) -> None:
    """The release branch's actual state (run 39679850907): 400 scheduled, 0 processed.

    The retired deadline could only reach this by burning 240s and then
    blaming the DSN. The grace window is what keeps a just-started worker from
    reading as a dead one, so the exit is at the grace poll and not before.
    """
    env = stub_kubectl(STUB_SCHEDULED="400", STUB_PROCESSED="0", STUB_ISSUE_AT_POLL="0")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "fail"
    assert out["REASON"] == "worker-idle"
    assert out["SCHEDULED"] == "400"
    assert out["PROCESSED"] == "0"
    assert int(out["POLLS"]) == 3, "it waited out the grace window and then stopped"


def test_a_consuming_worker_with_no_issue_is_a_different_failure(
    stub_kubectl, tmp_path: Path
) -> None:
    """The state the retired message described: the event never arrived."""
    env = stub_kubectl(STUB_SCHEDULED="40", STUB_PROCESSED="40", STUB_ISSUE_AT_POLL="0")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "fail"
    assert out["REASON"] == "not-arrived"
    assert int(out["POLLS"]) == 12, "a consuming worker is waited on to the full budget"


def test_a_just_started_worker_is_not_called_dead(stub_kubectl, tmp_path: Path) -> None:
    """Nothing scheduled yet is not the same as nothing consumed.

    Guards the opposite error: reading an empty log as a dead consumer would
    turn a cold start into a red gate, which is the mistake this change exists
    to stop, pointed the other way.
    """
    env = stub_kubectl(STUB_SCHEDULED="0", STUB_PROCESSED="0", STUB_ISSUE_AT_POLL="11")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "pass"
    assert out["REASON"] == "found"


def test_the_queue_depth_reader_counts_only_lists() -> None:
    """The depth probe is `KEYS` + `TYPE` + `LLEN`, measured on the real image.

    GlitchTip 6.2 runs `vtasks`, not celery: its keys are `{vt}:q:default` and
    `{vt}:q:failed`, alongside several `string` scheduler keys. Naming the
    queue would break on the next rename and `LLEN` of a missing key is 0 --
    which reads as "drained" and is the wrong answer. Verified against
    `redis:7.4.9-alpine` under the shipped manifest.
    """
    body = _shell_function("glitchtip_queue_depth")
    assert "KEYS" in body and "TYPE" in body and "LLEN" in body
    assert "celery" not in body


def test_the_failure_prints_the_dsn_instead_of_blaming_it() -> None:
    """The report must measure the cause it names (#4192).

    The retired message asserted the DSN did not resolve, on a run whose own
    diagnostics printed a correct one. Now the script reads the DSN out of the
    running api Pod and prints it before failing, and the worker-idle arm says
    in words that this is not the DSN.
    """
    text = SMOKE.read_text(encoding="utf-8")
    # Comment lines are stripped first: the retired sentence is QUOTED in the
    # rationale above the new wait, and an assertion satisfied by incidental
    # text is the defect this PR's round 4 was about. What must be gone is the
    # claim being MADE.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "the DSN does not resolve to the in-cluster GlitchTip" not in code
    assert "the DSN the api process actually carries" in code
    assert "NYXGPT_ERROR_TRACKING_DSN" in text
    assert "Not the api's DSN (printed above), and not latency" in text
    # ...and the key never leaves the Pod.
    assert 's#//[^@]*@#//REDACTED@#' in text


def test_the_wait_has_no_hard_coded_deadline_left() -> None:
    """The 48-iteration `for` loop and its "raised from 120s" rationale are gone."""
    text = SMOKE.read_text(encoding="utf-8")
    assert "for _ in $(seq 1 48)" not in text
    assert "GLITCHTIP_WAIT_POLLS" in text
    assert "GLITCHTIP_WORKER_GRACE_POLLS" in text
