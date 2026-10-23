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
2. the worker's probe says its consumer stopped     -> fail fast, "not the DSN"
3. the worker is consuming and no issue arrives     -> fail, "did not reach"

Direction 2 is the one that matters: it is the state the release branch was
actually in, and the retired form could only reach it by spending its whole
budget and then misnaming it.

Who answers "is the worker consuming?" changed when #4208 landed on the
release branch, and this round's merge carries the wait into it rather than
keeping its own answer. #4208 established the mechanism -- vtasks' ``runworker``
awaits neither its scheduler nor its consumer task, so a dead consumer leaves
the process up -- and shipped ``k8s/observability/glitchtip-worker-ready.py``,
which asks the consumer itself through the ``vtasks_*`` metrics and is what the
manifest's readiness and liveness probes run. The wait now asks that same
probe, in the same spelling. The log-line count it replaces was a second copy
of one decision (D-066) and the weaker one: it read a log format, and it could
not tell "the consumer loop has never run" from "it ran and stopped" -- the
distinction the probe's two questions are.

A fourth direction exists because of that merge, and it is a direction the
counting form could not have: the probe may be *unaskable* (the Pod is not
there yet, the api-server hiccups). "I could not ask" must not be recorded as
"the answer is no" -- that is this issue's class pointed the other way -- so
``unknown`` keeps the wait waiting.
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
    "glitchtip_worker_verdict",
    "wait_for_glitchtip_issue",
)

#: The probe the manifest runs, and the one the wait must ask.
PROBE_PATH = "/etc/nyxgpt/probes/glitchtip-worker-ready.py"
PROBE_SOURCE = REPO_ROOT / "k8s" / "observability" / "glitchtip-worker-ready.py"
MANIFEST = REPO_ROOT / "k8s" / "observability" / "glitchtip.yaml"


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
    ``STUB_PROBE_EXIT`` is the status the worker's readiness probe reports
    (``none`` makes the exec itself fail, i.e. the Pod never answered),
    ``STUB_ISSUE_AT_POLL`` decides when (if ever) the issue list starts
    carrying the marker, and the poll counter lives in a file because each
    call is a fresh process.

    The worker arm reproduces the real shape of the call under test: the
    remote ``sh -c`` prints the probe's own stderr and then ``PROBE_EXIT=$?``,
    which is how the script tells the probe's verdict from kubectl's.
    """

    def _install(**env: str) -> dict[str, str]:
        counter = tmp_path / "polls"
        counter.write_text("0", encoding="utf-8")
        stub = tmp_path / "kubectl"
        stub.write_text(
            """#!/usr/bin/env bash
args="$*"
case "$args" in
  *"exec deploy/glitchtip-worker"*)
    # The probe path must be the one the manifest mounts, or this stub is
    # answering a question the cluster is not being asked.
    case "$args" in
      *"PROBE_PATH"*) ;;
      *) echo "stub kubectl: the wait asked for something other than the probe: $args" >&2
         exit 99 ;;
    esac
    case "${STUB_PROBE_EXIT:-0}" in
      none)
        # The exec itself failed: no PROBE_EXIT line, so no verdict.
        echo 'Error from server (NotFound): deployments.apps "glitchtip-worker" not found' >&2
        exit 1 ;;
      0)
        echo "PROBE_EXIT=0" ;;
      *)
        echo "the worker is serving metrics but has published no vtasks_queue_depth \
sample, so its consumer loop has never run an iteration (#4208)"
        echo "PROBE_EXIT=${STUB_PROBE_EXIT}" ;;
    esac
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
""".replace("PROBE_PATH", PROBE_PATH),
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
echo "VERDICT=${{GLITCHTIP_WORKER_VERDICT}}"
echo "POLLS=$(cat "$STUB_COUNTER")"
echo "PROBE_SAID=${{GLITCHTIP_WORKER_REASON//$'\n'/ }}"
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
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if line.count("=") >= 1)


def test_the_issue_arriving_late_still_passes(stub_kubectl, tmp_path: Path) -> None:
    """A slow but working worker must not be failed: the loop keeps waiting."""
    env = stub_kubectl(STUB_PROBE_EXIT="0", STUB_ISSUE_AT_POLL="9")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "pass"
    assert out["REASON"] == "found"
    assert int(out["POLLS"]) == 9, "it stopped the moment the issue appeared"


def test_a_worker_that_consumes_nothing_fails_fast_and_says_so(
    stub_kubectl, tmp_path: Path
) -> None:
    """The release branch's actual state (run 39679850907): the consumer stopped.

    The retired deadline could only reach this by burning 240s and then
    blaming the DSN. The grace window is what keeps a just-started worker from
    reading as a dead one, so the exit is at the grace poll and not before.

    The verdict is the probe's, not this script's -- and the probe's own
    sentence travels with it, so the failure prints the reason a reader can
    act on instead of a count they then have to interpret.
    """
    env = stub_kubectl(STUB_PROBE_EXIT="1", STUB_ISSUE_AT_POLL="0")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "fail"
    assert out["REASON"] == "worker-idle"
    assert out["VERDICT"] == "not-consuming"
    assert "consumer loop has never run an iteration" in out["PROBE_SAID"]
    assert int(out["POLLS"]) == 3, "it waited out the grace window and then stopped"


def test_a_consuming_worker_with_no_issue_is_a_different_failure(
    stub_kubectl, tmp_path: Path
) -> None:
    """The state the retired message described: the event never arrived."""
    env = stub_kubectl(STUB_PROBE_EXIT="0", STUB_ISSUE_AT_POLL="0")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "fail"
    assert out["REASON"] == "not-arrived"
    assert out["VERDICT"] == "consuming"
    assert int(out["POLLS"]) == 12, "a consuming worker is waited on to the full budget"


def test_a_just_started_worker_is_not_called_dead(stub_kubectl, tmp_path: Path) -> None:
    """A probe that has not failed yet is not a dead consumer.

    Guards the opposite error: reading a cold start as a stopped consumer
    would turn a slow boot into a red gate, which is the mistake this change
    exists to stop, pointed the other way. The probe's own
    ``initialDelaySeconds: 40`` is the same allowance in the manifest.
    """
    env = stub_kubectl(STUB_PROBE_EXIT="0", STUB_ISSUE_AT_POLL="11")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "pass"
    assert out["REASON"] == "found"


def test_an_unaskable_probe_is_not_read_as_a_dead_consumer(stub_kubectl, tmp_path: Path) -> None:
    """``kubectl exec`` failing is not the worker's verdict.

    The Pod may not exist yet, or the api-server may hiccup. Recording that as
    "the consumer is dead" would be a gate naming a cause it never measured --
    this issue's class, in the direction that fails a healthy commit. So the
    verdict is ``unknown``, the wait runs its full budget, and the failure is
    the one that does not claim to know why.
    """
    env = stub_kubectl(STUB_PROBE_EXIT="none", STUB_ISSUE_AT_POLL="0")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "fail"
    assert out["REASON"] == "not-arrived", "an unanswered probe must not end the wait"
    assert out["VERDICT"] == "unknown"
    assert int(out["POLLS"]) == 12


def test_an_unaskable_probe_still_lets_a_healthy_run_pass(stub_kubectl, tmp_path: Path) -> None:
    """And `unknown` is not a failure of its own: the issue arriving still passes."""
    env = stub_kubectl(STUB_PROBE_EXIT="none", STUB_ISSUE_AT_POLL="5")
    out = _run(env, tmp_path)
    assert out["RESULT"] == "pass"
    assert out["REASON"] == "found"


def test_the_verdict_comes_from_the_shipped_probe_and_nothing_else() -> None:
    """One source for "is the worker consuming?" (#4183 / D-066).

    #4208 shipped the probe and the manifest runs it; the wait must ask the
    same file at the same path rather than counting log lines of its own. A
    second answer to one question is how `classify_error` ended up with two
    spellings of one signature a round after a partial fix.
    """
    body = _shell_function("glitchtip_worker_verdict")
    assert PROBE_PATH in body, "the wait must run the probe the manifest mounts"
    assert "PROBE_EXIT=" in body, "the probe's status must be told from kubectl's"
    # The retired second answer is gone, not merely unused. Comments are
    # stripped first: the log lines it counted are QUOTED in the rationale
    # above the wait as the measured evidence for the diagnosis, and an
    # assertion satisfied (or defeated) by incidental text is the defect this
    # PR's round 4 was about. What must be gone is the counting being DONE.
    text = SMOKE.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "glitchtip_worker_counts" not in code
    assert "Enqueuing due task" not in code
    assert "logs deploy/glitchtip-worker" not in code

    assert PROBE_SOURCE.is_file(), f"{PROBE_SOURCE} is the probe this depends on"
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert PROBE_PATH in manifest, "the manifest must mount the probe at the path asked for"


def test_the_queue_depth_reader_counts_only_lists() -> None:
    """The depth probe is `KEYS` + `TYPE` + `LLEN`, measured on the real image.

    GlitchTip 6.2 runs `vtasks`, not celery: its keys are `{vt}:q:ingest` and
    `{vt}:q:failed`, alongside several `string` scheduler keys. Naming the
    queue would break on the next rename and `LLEN` of a missing key is 0 --
    which reads as "drained" and is the wrong answer. Verified against
    `redis:7.4.9-alpine` under the shipped manifest.

    That this docstring said `{vt}:q:default` until #4208 read the key off the
    image, and the assertion below still held, is the property being asserted:
    the reader does not name the queue, so the rename cost it nothing.
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
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "the DSN does not resolve to the in-cluster GlitchTip" not in code
    assert "the DSN the api process actually carries" in code
    assert "NYXGPT_ERROR_TRACKING_DSN" in text
    assert "Not the api's DSN (printed above), and not latency" in text
    # ...and the key never leaves the Pod.
    assert "s#//[^@]*@#//REDACTED@#" in text


def test_the_wait_has_no_hard_coded_deadline_left() -> None:
    """The 48-iteration `for` loop and its "raised from 120s" rationale are gone."""
    text = SMOKE.read_text(encoding="utf-8")
    assert "for _ in $(seq 1 48)" not in text
    assert "GLITCHTIP_WAIT_POLLS" in text
    assert "GLITCHTIP_WORKER_GRACE_POLLS" in text
