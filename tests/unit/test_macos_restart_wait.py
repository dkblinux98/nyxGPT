"""The macOS smoke's restart wait, executed on every PR (#4192).

`macos-brew-smoke.yml`'s "The api restarts itself when the UI asks it to
(#4043)" step failed on 9 of its last 10 PR runs -- and not because #4043
regressed. `POST /api/v1/infra/restart-required` answers
``{"status": "scheduled"}`` by design: `app.infra_restart_required` defers
`_do_restart_required` onto a ``threading.Timer(0.5, ...)`` so the triggering
response can be sent before the process handling it is killed, and
``launchctl kickstart -k`` is one further asynchronous hop. The step then ran
``up 180`` -- which returns *instantly*, because the old process is still
answering ``/health`` -- and read the pid one second later. It reported
``pid=23672 (was 23672)``, i.e. a restart that had not happened yet, as one
that did not happen.

**Why this file exists at all.** That logic is workflow YAML, and the only
thing that executed it was a macOS runner on a path-filtered advisory job. So
the race was invisible to every gate on the tree until it had been red for ten
days on the release branch. This runs the step's real shell -- extracted from
the workflow, never re-typed here -- against a stub `launchctl` whose pid
changes on a delay, on Linux, on every PR.

Both directions, because a test that only runs the fixed form passes on any
machine where the bug cannot reproduce (#3753's lesson, ledger D-006):

* the RETIRED form (``up`` then read the pid) is reconstructed and must read
  the OLD pid against the delayed stub -- the failure, reproduced;
* the shipped ``restarted`` must return the NEW one from the same stub.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "macos-brew-smoke.yml"
STEP_NAME = "The api restarts itself when the UI asks it to (#4043)"

#: How long the stub pretends launchd takes to replace the process. Comfortably
#: longer than one `restarted` poll interval (2s) and than the zero wait the
#: retired form performed, so the two forms must disagree.
STUB_DELAY_SECONDS = 6


def _step_script() -> str:
    """The real `run:` body of the step under test."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if step.get("name") == STEP_NAME:
                return str(step["run"])
    raise AssertionError(f"{WORKFLOW} has no step named {STEP_NAME!r} any more")


def _shell_function(script: str, name: str) -> str:
    """One `name() { ... }` block out of `script`, by its own indentation.

    Read out of the workflow rather than restated, for the reason
    `build_homebrew_artifacts.extract_interpreter_preflight` is: a copy of
    shell this file maintains separately would be the thing under test
    drifting away from the thing that ships.
    """
    match = re.search(rf"^(\s*){re.escape(name)}\(\)\s*\{{", script, re.MULTILINE)
    assert match, f"the step no longer defines {name}()"
    indent = match.group(1)
    rest = script[match.start() :].splitlines(keepends=True)
    body = [rest[0]]
    for line in rest[1:]:
        body.append(line)
        if line.rstrip("\n") == f"{indent}}}":
            return textwrap.dedent("".join(body))
    raise AssertionError(f"{name}() in the step is not closed at its own indentation")


def _harness(tmp_path: Path, *, use_retired_form: bool) -> str:
    """A runnable script: the step's own `api_pid`/`restarted`, plus a driver.

    `api_pid` comes out of the workflow too -- it is what both forms read the
    machine through, so substituting a simpler one here would move the
    measurement off the code that ships.
    """
    script = _step_script()
    pieces = [
        "#!/usr/bin/env bash",
        "set -uo pipefail",
        'KEG_NAME="nyxgpt-api@3.0.0rc"',
        # `label` is the step's, reduced to the one prefix the stub answers
        # for: resolving BOTH of Homebrew's schemes is asserted by the step
        # itself on macOS and is not what this file measures.
        "label() { printf '%s' \"sh.brew.${KEG_NAME}\"; }",
        _shell_function(script, "api_pid"),
    ]
    if use_retired_form:
        # The retired form, reconstructed: `up` (which returns immediately
        # while the OLD process answers -- modelled here as the no-op it was,
        # since /health is not what decides this) and then a bare read.
        pieces.append("restarted() { api_pid; }")
    else:
        pieces.append(_shell_function(script, "restarted"))
    pieces.append('OLD="$1"')
    pieces.append('printf "%s" "$(restarted "$OLD" 60 || true)"')
    harness = tmp_path / "harness.sh"
    harness.write_text("\n".join(pieces) + "\n", encoding="utf-8")
    return str(harness)


def _stub_launchctl(tmp_path: Path) -> Path:
    """A `launchctl` that reports a NEW pid only after `STUB_DELAY_SECONDS`.

    Which is what `kickstart -k` does: the request returns, and the process is
    replaced some time later. The started-at stamp is written on first call so
    the delay is measured from the start of the wait, not from import time.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "launchctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'STAMP="$STUB_STATE/started-at"\n'
        '[ -f "$STAMP" ] || date +%s > "$STAMP"\n'
        'ELAPSED=$(( $(date +%s) - $(cat "$STAMP") ))\n'
        f'if [ "$ELAPSED" -ge {STUB_DELAY_SECONDS} ]; then PID=23999; else PID=23672; fi\n'
        'printf \'{\\n\\t"PID" = %s;\\n}\\n\' "$PID"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def _run(harness: str, bin_dir: Path, state: Path) -> str:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_STATE"] = str(state)
    result = subprocess.run(
        ["bash", harness, "23672"], capture_output=True, text=True, env=env, timeout=180
    )
    return result.stdout.strip()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_shipped_wait_returns_the_pid_of_the_successor(tmp_path: Path) -> None:
    """`restarted` waits out launchd's delay and reports the NEW process."""
    state = tmp_path / "state-new"
    state.mkdir()
    got = _run(_harness(tmp_path, use_retired_form=False), _stub_launchctl(tmp_path), state)
    assert got == "23999", (
        "the step's wait must not return until the pid under the launchd label has "
        f"actually changed; it returned {got!r} against a stub that replaces the "
        f"process after {STUB_DELAY_SECONDS}s"
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_the_retired_form_reads_the_pre_restart_pid(tmp_path: Path) -> None:
    """Fault injection: the failure as it occurred, reproduced.

    Without this half the test above would also pass against the retired code,
    on any machine fast enough to replace the process inside one read -- which
    is precisely the machine this step has never run on.
    """
    state = tmp_path / "state-old"
    state.mkdir()
    got = _run(_harness(tmp_path, use_retired_form=True), _stub_launchctl(tmp_path), state)
    assert got == "23672", (
        "the retired `up` + bare `api_pid` form no longer reads the pre-restart pid, "
        "so the assertion in the other test is not measuring the fix"
    )


def test_the_step_waits_on_the_pid_rather_than_sampling_it() -> None:
    """The wiring: `restarted` is what the step's measurement goes through.

    Asserted on the step text because the two tests above prove the function
    works, not that the step still calls it -- a correct helper nothing uses is
    the shape this whole issue is about.
    """
    script = _step_script()
    assert 'PID_AFTER="$(restarted "$PID_BEFORE"' in script, (
        "half two must obtain PID_AFTER by WAITING for the restart; a bare "
        "`api_pid` after `up` reads the machine before the restart lands"
    )
    assert 'PID_BEFORE="$(restarted "$PID_PRE_INJECT"' in script, (
        "half one must wait for a new process too, or its restart request can reach "
        "a process that still holds the module the injection removed"
    )
    # And the read the defect WAS is not reintroduced beside them.
    #
    # Scoped to `PID_AFTER` on purpose. Half two also reads `PID_BEFORE="$(api
    # _pid)"`, and that one is sound: `start_api` runs immediately above it and
    # has already waited for `/health` and for the launchd job, so there is no
    # asynchronous operation in flight to sample. Forbidding every unwaited
    # `api_pid` read would fail on it and teach the next reader to route a
    # perfectly good read through a wait that measures nothing.
    assert not re.search(r'^\s*PID_AFTER="\$\(api_pid\)"', script, re.MULTILINE), (
        "the post-restart pid is being sampled again rather than waited for -- that "
        "read returns the pre-restart process, which is #4192"
    )
