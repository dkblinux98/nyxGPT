"""The api can restart itself (#4043, acceptance round two).

The pending-restart notice's Restart button stopped the api and nothing
started it again. `brew services restart` is **two** launchd operations with a
`brew` process in between -- boot the job out, then bootstrap it -- and when
the api restarts itself that `brew` is a child of the job being booted out, so
launchd tears it down with the rest of the job's process tree before it
reaches the start half. The owner's rc17 log is the whole mechanism:

    INFO: 127.0.0.1 - "POST /api/v1/infra/restart-required HTTP/1.1" 200
    INFO: Shutting down
    INFO: Finished server process [92792]
    (nothing after this line)

`brew services list` -> `nyxgpt-api@3.0.0rc  none`, `launchctl list` -> no
entry, and the web UI 502ing until `nyxgpt ops restart api` was run from a
shell. The CLI worked on the same machine for one reason only: its `brew` is a
child of the terminal, not of the service being restarted.

The fix hands the restart to launchd as a single operation
(`launchctl kickstart -k gui/<uid>/<label>`), so the actor performing it is
launchd and this process's death cannot interrupt it. What these tests pin:

* the command issued is the single launchd operation, never the stop-then-start
  pair -- the regression that would reintroduce the defect verbatim;
* both of Homebrew's label schemes are tried, because which one a machine
  carries is not knowable from here;
* `brew services restart` survives as the fallback for exactly the case
  launchd cannot answer for (no `launchctl`, or no loaded job), which is also
  the case where it is safe -- a job that is not loaded is not hosting this
  process;
* a refusal is *reported* rather than swallowed, which is what makes "the
  restart could not be launched" recordable at all. Under the old command the
  kill came first, so that answer was structurally unobtainable by the only
  actor that had to report it;
* `web`, `ollama` and `cassandra` stay synchronous and keep clearing their own
  pending flags -- the owner's explicit constraint, and the reason a launchd
  hand-off was chosen over detaching the old command.
"""

from __future__ import annotations

import subprocess

import pytest

from nyxgpt import self_heal

pytestmark = pytest.mark.unit

UID = __import__("os").getuid()
CANDIDATE = "nyxgpt-api@3.0.0rc"


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


class _Recorder:
    """Stands in for `self_heal._run`, answering per command and recording all."""

    def __init__(self, answers=None):
        self.seen: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, cmd, **_kwargs):
        self.seen.append(list(cmd))
        for needle, result in self.answers.items():
            if needle in " ".join(cmd):
                return result
        return _cp()

    @property
    def commands(self) -> list[str]:
        return [" ".join(cmd) for cmd in self.seen]


@pytest.fixture
def macos(monkeypatch):
    """Pin the platform: the restart command differs by it, so the runner must not decide."""
    monkeypatch.setattr(self_heal, "_is_macos", lambda: True)
    monkeypatch.setattr(self_heal, "_is_linux", lambda: False)
    monkeypatch.setattr(self_heal, "_dev_mode_active", lambda: False)
    monkeypatch.setattr(self_heal, "_which", lambda tool: f"/opt/homebrew/bin/{tool}")


@pytest.fixture
def candidate_installed(monkeypatch):
    """An rc install: `api` resolves to the versioned formula, as #3853 made it."""
    monkeypatch.setattr(
        self_heal,
        "_brew_services_snapshot",
        lambda: {CANDIDATE: "started", "nyxgpt-web@3.0.0rc": "started"},
    )


# --- The defect: the restart must not be a stop and a separate start --------


def test_the_restart_is_one_launchd_operation(macos, candidate_installed, monkeypatch):
    """`launchctl kickstart -k`, and no `brew services restart` anywhere."""
    run = _Recorder({"homebrew.mxcl.": _cp(1, stderr="Could not find service")})
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal.restart_native_component("api")

    assert result.ok is True, result.message
    assert f"launchctl kickstart -k gui/{UID}/sh.brew.{CANDIDATE}" in run.commands
    assert not any("services restart" in cmd for cmd in run.commands), (
        "the stop-then-start pair is back -- this is #4043's defect verbatim: " f"{run.commands}"
    )


def test_a_successful_hand_off_names_launchd_in_its_details(
    macos, candidate_installed, monkeypatch
):
    """The operator-facing record says *who* is performing the restart.

    The api cannot report its own completion (the process is gone), so the one
    thing it can leave behind is what it handed off and to whom. The owner's
    diagnosis had to be made from a log whose last line was the shutdown.
    """
    monkeypatch.setattr(self_heal, "_run", _Recorder())

    result = self_heal._restart_brew_service(CANDIDATE)

    assert result.ok is True
    assert result.message == f"Restarted brew service: {CANDIDATE}"
    assert f"gui/{UID}/homebrew.mxcl.{CANDIDATE}" in (result.details or "")


def test_both_homebrew_label_schemes_are_tried(macos, monkeypatch):
    """Current brew writes `sh.brew.<formula>`; older machines carry `homebrew.mxcl.`.

    Asking only one is how a step in `macos-brew-smoke.yml` read a running,
    registered service as unregistered (D-032(d), run 36996645910). Trying
    each costs one refused launchctl call.
    """
    run = _Recorder({"homebrew.mxcl.": _cp(1, stderr="Could not find service")})
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal._restart_brew_service(CANDIDATE)

    assert result.ok is True
    assert run.commands == [
        f"launchctl kickstart -k gui/{UID}/homebrew.mxcl.{CANDIDATE}",
        f"launchctl kickstart -k gui/{UID}/sh.brew.{CANDIDATE}",
    ]


def test_the_first_label_that_answers_wins(macos, monkeypatch):
    """No second call once launchd has accepted one -- a restart is not issued twice."""
    run = _Recorder()
    monkeypatch.setattr(self_heal, "_run", run)

    self_heal._restart_brew_service(CANDIDATE)

    assert run.commands == [f"launchctl kickstart -k gui/{UID}/homebrew.mxcl.{CANDIDATE}"]


# --- The fallback, and why it is safe where it applies ----------------------


def test_an_unloaded_job_falls_back_to_brew_services(macos, monkeypatch):
    """Only `brew services` writes and bootstraps the plist, so it has to stay.

    It is also safe here and nowhere else: launchd knowing no job under either
    label means no job is hosting this process, so there is no process tree
    for a boot-out to take down along with the `brew` issuing it.
    """
    run = _Recorder({"launchctl": _cp(1, stderr="Could not find service")})
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal._restart_brew_service(CANDIDATE)

    assert result.ok is True, result.message
    assert run.commands[-1] == f"brew services restart {CANDIDATE}"


def test_no_launchctl_at_all_falls_back_without_calling_it(macos, monkeypatch):
    """Linuxbrew: `brew services` drives systemd there and there is no gui domain."""
    monkeypatch.setattr(
        self_heal,
        "_which",
        lambda tool: None if tool == "launchctl" else f"/home/linuxbrew/.linuxbrew/bin/{tool}",
    )
    run = _Recorder()
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal._restart_brew_service(CANDIDATE)

    assert result.ok is True, result.message
    assert run.commands == [f"brew services restart {CANDIDATE}"]


def test_a_refusal_that_cannot_act_is_reported_not_swallowed(macos, monkeypatch):
    """ "Could not be launched" has to reach `GET /infra/restart-status`.

    This is the half the old command could not deliver at any price: it killed
    the job before it discovered it could not start it, so the actor obliged
    to report the failure no longer existed. launchd refuses *having killed
    nothing*, so a refusal arrives as a return value -- which
    `app._do_restart_required` records with
    `restart_state.record_attempt_failed`, and the notice ends its poll on.
    """
    monkeypatch.setattr(
        self_heal, "_which", lambda tool: None if tool == "brew" else "/usr/bin/launchctl"
    )
    run = _Recorder({"launchctl": _cp(1, stderr="Could not find service")})
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal._restart_brew_service(CANDIDATE)

    assert result.ok is False
    assert "brew not found" in result.message


def test_launchctl_blowing_up_is_a_failure_not_a_fall_through(macos, monkeypatch):
    """An exception is an answer about this machine, not a reason to try the killer."""

    def explode(cmd, **_kwargs):
        raise OSError("no such domain")

    monkeypatch.setattr(self_heal, "_run", explode)

    result = self_heal._restart_brew_service(CANDIDATE)

    assert result.ok is False
    assert "OSError" in (result.details or "")


# --- Scope: the components whose restart does NOT kill the observer ---------


def test_linux_still_restarts_through_systemd(monkeypatch):
    """Unchanged, and correct for the same reason the fix is.

    `systemctl --user restart` hands the stop and the start to systemd as one
    job; killing the client that asked for it does not cancel the job. There
    is no self-kill to fix on Linux, and inventing one would be a change with
    no defect behind it.
    """
    monkeypatch.setattr(self_heal, "_is_linux", lambda: True)
    monkeypatch.setattr(self_heal, "_is_macos", lambda: False)
    monkeypatch.setattr(self_heal, "_which", lambda tool: f"/usr/bin/{tool}")
    run = _Recorder()
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal.restart_native_component("api")

    assert result.ok is True, result.message
    assert run.commands == ["systemctl --user restart nyxgpt-api.service"]


@pytest.mark.parametrize("component", ["web", "ollama"])
def test_web_and_ollama_stay_synchronous_and_observable(
    macos, candidate_installed, monkeypatch, component
):
    """The owner's constraint, and why a launchd hand-off beat a detached spawn.

    `app._do_restart_required` clears these components' pending flags on the
    strength of the exit code read here, so the restart has to stay something
    this process observes. `launchctl kickstart` is synchronous -- it returns
    launchd's answer -- whereas spawning the old command with
    `start_new_session=True` and not waiting would have discarded that for
    every component in order to fix one.
    """
    run = _Recorder()
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal.restart_native_component(component)

    assert result.ok is True, result.message
    assert run.commands, "nothing was issued"
    assert run.commands[0].startswith("launchctl kickstart -k")


def test_cassandra_is_untouched_by_the_launchd_path(macos, monkeypatch):
    """The one Docker-managed piece of a native install still restarts via docker."""
    monkeypatch.setattr(self_heal, "_cassandra_active_elsewhere", lambda: None)
    run = _Recorder()
    monkeypatch.setattr(self_heal, "_docker_run", run)
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal.restart_native_component("cassandra")

    assert result.ok is True, result.message
    assert run.commands == ["docker restart nyxgpt-cassandra"]


def test_dev_mode_api_is_unchanged(macos, monkeypatch):
    """Dev mode's api was already a kickstart, and that is the corroboration.

    `com.nyxgpt.api` is a LaunchAgent and `_restart_launchagent` has always
    handed it to launchd as one operation -- so the dev-mode self-restart was
    never broken, and only the artifact/brew path was. One path in this module
    already did the right thing.
    """
    monkeypatch.setattr(self_heal, "_dev_mode_active", lambda: True)
    run = _Recorder()
    monkeypatch.setattr(self_heal, "_run", run)

    result = self_heal.restart_native_component("api")

    assert result.ok is True, result.message
    assert run.commands == [f"launchctl kickstart -k gui/{UID}/com.nyxgpt.api"]


# --- The same fault class on the other route into the api process -----------


def test_ops_restart_also_hands_the_api_off_to_launchd(monkeypatch):
    """`POST /api/v1/config/restart` reaches `ops.restart()` from inside the api.

    The sweep for this fault class (runbook 3, "name the fault as a class")
    found a second reachable instance: `app.config_restart` schedules
    `ops.restart(...)` on a timer, which dispatches through
    `ops._restart_native_service` -> `_restart_registered_native_service` --
    so the self-kill was reachable by that route too, and fixing only
    `self_heal` would have left the trap for the next session (first principle
    2). Both call the *same* function rather than keeping a copy each, which is
    the D-045 shape: two implementations of one policy diverged on the answer
    they existed to give identically.

    Driven through the outer `_restart_native_service` deliberately, not the
    inner helper: #4133 inserted its stale-build repair between the route and
    the hand-off, and this asserts the whole route still reaches launchd. The
    unit conftest's autouse stub leaves nothing answering :8000, so the drift
    is "could not tell" and falls through to the registered restart -- which is
    what the owner's healthy machine does too.
    """
    from nyxgpt import ops

    monkeypatch.setattr(ops, "_is_macos", lambda: True)
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(ops, "_dev_launchd_label", lambda component: None)
    monkeypatch.setattr(ops, "_resolved_brew_service", lambda component: CANDIDATE)
    monkeypatch.setattr(self_heal, "_is_macos", lambda: True)
    monkeypatch.setattr(self_heal, "_which", lambda tool: f"/opt/homebrew/bin/{tool}")
    run = _Recorder()
    monkeypatch.setattr(self_heal, "_run", run)
    # If the hand-off were skipped this would be the command, and the
    # assertion below would catch it.
    monkeypatch.setattr(
        ops, "_run", lambda *a, **k: pytest.fail(f"ops ran its own restart command: {a}")
    )

    results = ops._restart_native_service("api")

    assert [r.ok for r in results] == [True], [r.message for r in results]
    assert run.commands == [f"launchctl kickstart -k gui/{UID}/homebrew.mxcl.{CANDIDATE}"]


def test_ops_restart_falls_back_to_brew_when_launchd_knows_no_job(monkeypatch):
    """The fallback is shared too -- an unloaded job still needs `brew services`."""
    from nyxgpt import ops

    monkeypatch.setattr(ops, "_is_macos", lambda: True)
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(ops, "_dev_launchd_label", lambda component: None)
    monkeypatch.setattr(ops, "_resolved_brew_service", lambda component: CANDIDATE)
    monkeypatch.setattr(ops, "_which", lambda tool: f"/opt/homebrew/bin/{tool}")
    monkeypatch.setattr(ops, "_brew_formula_spec", lambda name: name)
    monkeypatch.setattr(self_heal, "_is_macos", lambda: True)
    monkeypatch.setattr(self_heal, "_which", lambda tool: f"/opt/homebrew/bin/{tool}")
    monkeypatch.setattr(
        self_heal, "_run", _Recorder({"launchctl": _cp(113, stderr="Could not find service")})
    )
    ops_run = _Recorder()
    monkeypatch.setattr(ops, "_run", ops_run)

    results = ops._restart_native_service("api")

    assert [r.ok for r in results] == [True], [r.message for r in results]
    # Only the commands that ACT are asserted. `brew --prefix`/`--cellar` are
    # read-only keg lookups the running-build scope check makes before it
    # probes (#4182 moved that read ahead of the probe, which is what makes a
    # host with no native venv skip the probe entirely).
    assert [c for c in ops_run.commands if not c.startswith("brew --")] == [
        f"brew services restart {CANDIDATE}"
    ]


def test_the_install_time_restart_still_rewrites_the_plist(monkeypatch):
    """`_restart_brew_service` itself is untouched, and that is deliberate.

    Its other call sites follow an install or an upgrade, where the point of
    `brew services restart` is that it rewrites and re-bootstraps the plist
    for the keg just built. A kickstart would restart the already-loaded job
    definition, so widening the fix to that helper would trade one defect for
    another.
    """
    from nyxgpt import ops

    monkeypatch.setattr(ops, "_which", lambda tool: f"/opt/homebrew/bin/{tool}")
    monkeypatch.setattr(ops, "_brew_formula_spec", lambda name: name)
    run = _Recorder()
    monkeypatch.setattr(ops, "_run", run)

    results = ops._restart_brew_service(CANDIDATE)

    assert [r.ok for r in results] == [True]
    assert run.commands == [f"brew services restart {CANDIDATE}"]
