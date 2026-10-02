"""The `ops port-forward --status` predicate every k8s smoke branches on (#3986).

Round 1 of #3986 taught five smoke steps to ask "is a managed background
forward up?" like this::

    if nyxgpt ops port-forward --status | grep -qi 'running'; then

That predicate is true in BOTH states. The negative answer is "No managed
background port-forward is running." -- it contains the word the grep looks
for -- so the branch was taken unconditionally:

* ``k8s-observability-smoke`` failed a run in which all six host ports were
  published and all four SRE UIs answered 200, reporting "a managed background
  forward is running" about a cluster that had none;
* and far worse, in ``scripts/k8s-local-smoke.sh`` the same idiom guards the
  ClusterIP fault injections for steps 7-9. There a false positive takes the
  bring-your-own branch and **skips** them with an ``[OK]``, so #3986's own
  executed evidence (#3775) was being quietly short-circuited rather than
  failing. A gate that cannot fail is worse than a red one.

The product side now builds both messages from named constants, and this file
is the guard. It asserts, by rendering the real CLI output, that the sentinel
separates the two states -- and then reads every consumer and asserts none has
gone back to a match that cannot. Pinning only the constants would not help:
the defect lived in the callers' regex, not in the message.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from nyxgpt import ops

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every place that branches on `--status` output. Listed explicitly rather than
# globbed: a new consumer should have to add itself here, which is the moment
# to notice it needs the sentinel too.
STATUS_CONSUMERS = (
    "scripts/k8s-local-smoke.sh",
    "scripts/k8s-artifact-smoke.sh",
    ".github/workflows/k8s-observability-smoke.yml",
)

# A `grep`/`grep -q`/... whose pattern is the first single- or double-quoted
# argument. Good enough for the shell these files are written in, and the
# assertion below is about which patterns appear at all.
_GREP_PATTERN = re.compile(r"""grep\s+(?:-\w+\s+)*(['"])(?P<pattern>.+?)\1""")


def _status_args(**overrides):
    """An argparse Namespace shaped like the `ops port-forward` parser."""
    args = argparse.Namespace(
        target="web",
        port=None,
        background=False,
        status=True,
        stop=False,
        supervise=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _status_output(monkeypatch, capsys, *, running: bool) -> str:
    """The real `--status` stdout for one of the two states."""
    monkeypatch.setattr(
        ops,
        "port_forward_status",
        lambda: {
            "running": running,
            "pid": 4242 if running else 0,
            "targets": ["web"] if running else [],
            "urls": ["http://127.0.0.1:3000"] if running else [],
        },
    )
    # `--status` answers from the recorded state alone; no cluster, no kubectl.
    monkeypatch.setattr(
        ops, "_which", lambda _p: pytest.fail("--status must not require kubectl on PATH")
    )
    assert ops.port_forward(_status_args()) == 0
    return capsys.readouterr().out


def test_the_sentinel_separates_the_two_status_answers(monkeypatch, capsys):
    """The sentinel appears when a forward runs, and NOT when none does.

    The second half is the one round 1 got wrong, and it is the reason the
    constant is a prefix of the running sentence rather than a word inside it.
    """
    running = _status_output(monkeypatch, capsys, running=True)
    assert ops.PORT_FORWARD_STATUS_RUNNING_SENTINEL in running
    assert "4242" in running

    idle = _status_output(monkeypatch, capsys, running=False)
    assert ops.PORT_FORWARD_STATUS_IDLE_MESSAGE in idle
    assert ops.PORT_FORWARD_STATUS_RUNNING_SENTINEL not in idle


def test_the_idle_message_still_contains_the_word_that_caused_the_defect():
    """Why a loose match is unsafe, asserted rather than left as a comment.

    If the idle message is ever reworded to drop "running", the old
    `grep -qi 'running'` would start working by accident and this file would
    stop explaining anything. Keeping the trap visible keeps the guard honest.
    """
    assert "running" in ops.PORT_FORWARD_STATUS_IDLE_MESSAGE
    assert ops.PORT_FORWARD_STATUS_RUNNING_SENTINEL not in ops.PORT_FORWARD_STATUS_IDLE_MESSAGE


@pytest.mark.parametrize("relative", STATUS_CONSUMERS)
def test_every_status_consumer_matches_the_sentinel_and_nothing_looser(relative):
    """No caller may decide "is a forward up?" from a looser pattern.

    Scoped to greps on the same line as `port-forward --status` (the shell
    pipelines that form the predicate), so unrelated greps in these files are
    none of this test's business.
    """
    path = REPO_ROOT / relative
    assert path.exists(), f"{relative} is listed as a --status consumer but does not exist"

    predicates = [
        line for line in path.read_text(encoding="utf-8").splitlines() if "--status" in line
    ]
    assert (
        predicates
    ), f"{relative} no longer reads `port-forward --status` -- drop it from the list"

    matched_sentinel = False
    for line in predicates:
        for match in _GREP_PATTERN.finditer(line):
            pattern = match.group("pattern")
            assert pattern == ops.PORT_FORWARD_STATUS_RUNNING_SENTINEL, (
                f"{relative} greps `--status` output for {pattern!r}. Only "
                f"{ops.PORT_FORWARD_STATUS_RUNNING_SENTINEL!r} distinguishes the two states -- "
                "the idle message contains the word 'running' too (#3986)."
            )
            matched_sentinel = True
    assert matched_sentinel, (
        f"{relative} reads `port-forward --status` but never greps it for "
        f"{ops.PORT_FORWARD_STATUS_RUNNING_SENTINEL!r}"
    )
