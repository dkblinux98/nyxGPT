"""The candidate channel's versioned brew services are seen by the probe (#3853).

What these guard, and why the fixture below is the whole point of the file:

`brew services list` on a machine that installed a release candidate carries
`nyxgpt-api@3.0.0rc`, not `nyxgpt-api` -- Homebrew names a service after its
formula and the rc channel publishes a separately-named formula so the two
channels can coexist in one tap. Until this issue, `ops.py` and `self_heal.py`
each kept a hard-coded `{"api": "nyxgpt-api", ...}` map and looked the service
up by that literal name, so on **every** rc install:

* `nyxgpt up` -> `_wait_for_stack_healthy` -> `self_heal.list_component_status`
  found no entry for either component, waited the full 180s and exited 2 on a
  stack whose API, web UI and Ollama were all serving;
* `nyxgpt ops status` printed `native api: none` beside a `started` service.

The rc channel is the acceptance-testing path (docs/homebrew.md#candidate-channel),
so the one install flow used to accept a release was the one where `nyxgpt up`
structurally could not return 0.

`_OWNERS_MACHINE` is the exact `brew services list` the owner captured on
2026-08-17: both the versioned candidate (started) and a prior release's
unversioned service (error) registered for the same component. It fails against
the pre-#3853 code, which is what makes it evidence rather than decoration.
"""

import subprocess

import pytest

from nyxgpt import brew_services, ops, self_heal

pytestmark = pytest.mark.unit

# Verbatim shape of the owner's `nyxgpt ops status` capture on 2026-08-17: two
# service sets registered, the candidate started and the leftover in `error`.
_OWNERS_MACHINE = """\
Name               Status  User      File
nyxgpt-api         error 3  darlabaker ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api.plist
nyxgpt-api@3.0.0rc started  darlabaker ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api@3.0.0rc.plist
nyxgpt-web         error 1  darlabaker ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-web.plist
nyxgpt-web@3.0.0rc started  darlabaker ~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-web@3.0.0rc.plist
ollama             none     darlabaker
"""

# The owner's second capture, after stopping the leftovers: nothing else for the
# probe to read, and the candidate services still started. This is the sample
# that removed "the probe is reading a stale service" as an alternative
# explanation.
_CANDIDATE_ONLY = """\
Name               Status
nyxgpt-api         none
nyxgpt-api@3.0.0rc started
nyxgpt-web         none
nyxgpt-web@3.0.0rc started
ollama             none
"""

_STABLE_ONLY = """\
Name        Status
nyxgpt-api  started
nyxgpt-web  started
ollama      started
"""


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["brew"], returncode, stdout=stdout, stderr=stderr)


# --- brew_services.resolve ---------------------------------------------------


def test_the_candidate_service_is_what_a_component_resolves_to():
    snapshot = brew_services.parse_services_list(_CANDIDATE_ONLY)

    assert brew_services.resolve("api", "nyxgpt-api", snapshot) == "nyxgpt-api@3.0.0rc"
    assert brew_services.resolve("web", "nyxgpt-web", snapshot) == "nyxgpt-web@3.0.0rc"


def test_a_started_versioned_service_beats_a_registered_unversioned_one():
    """The acceptance criterion: several match, prefer the one that is started."""
    snapshot = brew_services.parse_services_list(_OWNERS_MACHINE)

    assert brew_services.resolve("api", "nyxgpt-api", snapshot) == "nyxgpt-api@3.0.0rc"
    assert brew_services.resolve("web", "nyxgpt-web", snapshot) == "nyxgpt-web@3.0.0rc"


def test_a_started_stable_service_beats_a_stopped_candidate():
    """The preference is on *running*, not on the `@` -- both directions."""
    snapshot = {"nyxgpt-api": "started", "nyxgpt-api@3.0.0rc": "stopped"}

    assert brew_services.resolve("api", "nyxgpt-api", snapshot) == "nyxgpt-api"


def test_a_stable_only_machine_is_unchanged():
    snapshot = brew_services.parse_services_list(_STABLE_ONLY)

    assert brew_services.resolve("api", "nyxgpt-api", snapshot) == "nyxgpt-api"
    assert brew_services.resolve("ollama", "ollama", snapshot) == "ollama"


def test_nothing_registered_falls_back_to_the_stable_name():
    """Preserves "absent from the snapshot means out of scope, not down"."""
    assert brew_services.resolve("api", "nyxgpt-api", {}) == "nyxgpt-api"


def test_the_newest_release_line_wins_among_stopped_candidates():
    snapshot = {"nyxgpt-api@3.0.0rc": "stopped", "nyxgpt-api@3.1.0rc": "stopped"}

    assert brew_services.resolve("api", "nyxgpt-api", snapshot) == "nyxgpt-api@3.1.0rc"


def test_ollama_never_resolves_to_a_versioned_formula():
    """`ollama` is upstream Homebrew's formula, not one this project publishes.

    An unrelated `ollama@0.1.0` keg on the machine is not nyxGPT's service, and
    adopting it would be this defect pointing the other way.
    """
    snapshot = {"ollama": "none", "ollama@0.1.0": "started"}

    assert brew_services.resolve("ollama", "ollama", snapshot) == "ollama"


def test_a_similarly_named_formula_is_not_a_variant():
    assert not brew_services.is_variant_of("nyxgpt-api-canary", "nyxgpt-api")
    assert not brew_services.is_variant_of("nyxgpt-apiary", "nyxgpt-api")
    assert brew_services.is_variant_of("nyxgpt-api@3.0.0rc", "nyxgpt-api")


def test_superseded_names_every_registered_variant_but_the_kept_one():
    snapshot = brew_services.parse_services_list(_OWNERS_MACHINE)

    assert brew_services.superseded("nyxgpt-api", snapshot, keep="nyxgpt-api@3.0.0rc") == [
        "nyxgpt-api"
    ]
    # `none` is a formula brew knows with no service registered -- nothing to stop.
    assert (
        brew_services.superseded(
            "nyxgpt-api",
            brew_services.parse_services_list(_CANDIDATE_ONLY),
            keep="nyxgpt-api@3.0.0rc",
        )
        == []
    )


def test_the_two_modules_share_one_service_name_map():
    """Not two copies that agree by convention -- that is what shipped #3853.

    Same rule D-022 settled for `k8s_pod_state.py`: the modules that must
    agree about what is installed read one definition, so they cannot drift.
    """
    assert ops.NATIVE_BREW_SERVICES is brew_services.NATIVE_BREW_SERVICES
    assert self_heal.NATIVE_BREW_SERVICES is brew_services.NATIVE_BREW_SERVICES


# --- self_heal: the probe `nyxgpt up` gates on ------------------------------


@pytest.mark.parametrize("fixture", [_OWNERS_MACHINE, _CANDIDATE_ONLY])
def test_the_health_probe_reports_a_candidate_install_healthy(monkeypatch, fixture):
    """The regression `nyxgpt up` exited 2 on. Fails against the pre-#3853 code.

    `list_component_status` is what `ops._wait_for_stack_healthy` polls, so
    "api and web report healthy here" is the same statement as "`nyxgpt up`
    can return 0 on a candidate install".
    """
    monkeypatch.setattr(self_heal.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: _cp(stdout=""))
    monkeypatch.setattr(self_heal, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(
        self_heal,
        "_brew_services_snapshot",
        lambda: brew_services.parse_services_list(fixture),
    )
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")

    by_service = {s.service: s for s in self_heal.list_component_status()}

    assert by_service["api"].healthy is True
    assert by_service["api"].container == "nyxgpt-api@3.0.0rc"
    assert by_service["web"].healthy is True
    assert by_service["web"].container == "nyxgpt-web@3.0.0rc"


def test_healing_a_candidate_component_restarts_the_versioned_service(monkeypatch):
    """The heal has to act on the service the probe found, not the stable name.

    Restarting `nyxgpt-api` on the owner's machine would have started the
    leftover 2.1.0 keg onto the port the candidate was serving on.
    """
    monkeypatch.setattr(self_heal.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(self_heal, "_dev_mode_active", lambda: False)
    monkeypatch.setattr(
        self_heal,
        "_brew_services_snapshot",
        lambda: brew_services.parse_services_list(_OWNERS_MACHINE),
    )
    restarted: list[str] = []
    monkeypatch.setattr(
        self_heal,
        "_restart_brew_service",
        lambda name: restarted.append(name) or self_heal.HealResult(True, name),
    )

    self_heal.restart_native_component("api")

    assert restarted == ["nyxgpt-api@3.0.0rc"]


# --- ops: the read-out the operator gets ------------------------------------


def test_ops_status_reports_the_candidate_service_under_its_component(monkeypatch):
    """AC2: a fix in self_heal.py alone leaves `ops status` reporting `none`.

    This is the same misreport the install-time caveat used to describe --
    which an operator running `status` days later never saw.
    """
    monkeypatch.setattr(ops, "_is_macos", lambda: True)
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda *a, **k: _cp(stdout=_OWNERS_MACHINE),
    )
    monkeypatch.setattr(ops, "read_install_mode", lambda: ops.InstallModeState("artifact"))

    assert ops._native_services_snapshot() == {
        "api": "started",
        "web": "started",
        "ollama": "none",
    }


def test_restarting_a_component_acts_on_the_installed_formulas_service(monkeypatch):
    monkeypatch.setattr(ops, "_is_macos", lambda: True)
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(ops, "_dev_launchd_label", lambda component: None)
    monkeypatch.setattr(
        ops, "_brew_services_snapshot", lambda: brew_services.parse_services_list(_OWNERS_MACHINE)
    )
    restarted: list[str] = []
    monkeypatch.setattr(
        ops,
        "_restart_brew_service",
        lambda name: restarted.append(name) or [ops.OpsResult(True, "")],
    )

    ops._restart_native_service("web")

    assert restarted == ["nyxgpt-web@3.0.0rc"]


def test_stopping_a_component_clears_every_registered_variant(monkeypatch):
    """`nyxgpt down` has to leave the port free.

    Both channels' formulas declare `keep_alive true`, so a superseded service
    left registered is handed :8000 back by launchd the moment the one this
    install owns stops.
    """
    monkeypatch.setattr(ops, "_is_macos", lambda: True)
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(ops, "_dev_launchd_label", lambda component: None)
    monkeypatch.setattr(
        ops, "_brew_services_snapshot", lambda: brew_services.parse_services_list(_OWNERS_MACHINE)
    )
    stopped: list[str] = []
    monkeypatch.setattr(
        ops, "_stop_brew_service", lambda name: stopped.append(name) or [ops.OpsResult(True, "")]
    )

    ops._stop_native_service("api")

    assert stopped == ["nyxgpt-api@3.0.0rc", "nyxgpt-api"]


# --- ops: the competing-install reconcile -----------------------------------


def test_the_install_stops_a_previous_formulas_service(monkeypatch):
    """AC3: a candidate install must not leave a competing service behind.

    Cause 2 of the issue. `_reconcile_install_mode` cleans up only on a *mode*
    change (dev <-> artifact); the owner's marker already read `artifact`, so
    nothing ran and the 2.1.0 keg's `keep_alive` service kept losing the :8000
    race and being relaunched, filing `[Errno 48] address already in use` into
    GlitchTip every few seconds.
    """
    monkeypatch.setattr(ops, "_is_macos", lambda: True)
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops, "read_install_mode", lambda: ops.InstallModeState("artifact"))
    monkeypatch.setattr(
        ops, "_brew_services_snapshot", lambda: brew_services.parse_services_list(_OWNERS_MACHINE)
    )
    monkeypatch.setattr(ops, "_target_brew_formula", lambda name: f"{name}@3.0.0rc")
    stopped: list[str] = []
    monkeypatch.setattr(
        ops, "_stop_brew_service", lambda name: stopped.append(name) or [ops.OpsResult(True, "")]
    )

    results = ops._stop_superseded_brew_services()

    assert stopped == ["nyxgpt-api", "nyxgpt-web"]
    assert all(r.ok for r in results)
    assert any("#3853" in r.details for r in results)


def test_the_install_never_stops_the_service_it_is_about_to_start(monkeypatch):
    """The failure mode of getting `_target_brew_formula` wrong.

    A stable install on a stable-only machine has nothing superseded -- if this
    ever stopped `nyxgpt-api`, every plain `nyxgpt up` would kill its own API.
    """
    monkeypatch.setattr(ops, "_is_macos", lambda: True)
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops, "read_install_mode", lambda: ops.InstallModeState("artifact"))
    monkeypatch.setattr(
        ops, "_brew_services_snapshot", lambda: brew_services.parse_services_list(_STABLE_ONLY)
    )
    monkeypatch.setattr(ops, "_target_brew_formula", lambda name: name)
    monkeypatch.setattr(
        ops,
        "_stop_brew_service",
        lambda name: pytest.fail(f"stopped {name}, which this install owns"),
    )

    assert ops._stop_superseded_brew_services() == []


def test_dev_mode_leaves_the_superseded_reconcile_alone(monkeypatch):
    """Dev mode stops *every* api/web brew service on the switch instead.

    There is no "the one this install owns" among them, and treating the dev
    LaunchAgent's port-mate as superseded here would fight that path (#3789).
    """
    monkeypatch.setattr(ops, "_is_macos", lambda: True)
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops, "read_install_mode", lambda: ops.InstallModeState("dev"))
    monkeypatch.setattr(
        ops,
        "_stop_brew_service",
        lambda name: pytest.fail("dev mode must not run the superseded reconcile"),
    )

    assert ops._stop_superseded_brew_services() == []


def test_the_target_formula_follows_the_installers_own_branch(monkeypatch, tmp_path):
    """`_target_brew_formula` must answer what the installer will really register.

    A wrong answer here stops the service the install is about to start, so it
    is derived from the same condition `_install_homebrew_api` branches on
    rather than guessed.
    """
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0rc12")
    # No checkout: the artifact path installs the published channel formula.
    assert ops._target_brew_formula("nyxgpt-api") == "nyxgpt-api@3.0.0rc"

    (tmp_path / "homebrew").mkdir()
    (tmp_path / "homebrew" / "nyxgpt-api.rb").write_text("class NyxgptApi < Formula\nend\n")
    # A checkout builds the local file:// tap, which carries the stable name.
    assert ops._target_brew_formula("nyxgpt-api") == "nyxgpt-api"


def test_the_superseded_reconcile_runs_before_the_api_and_web_installs():
    """Ordering is the whole point: the port has to be free before the start.

    Asserted against the step list rather than by comment, because a step
    reordered into the wrong place still passes every test above while doing
    nothing useful.
    """
    source = ops.install.__doc__ or ""
    del source  # the docstring is not the contract -- the list below is

    import inspect

    body = inspect.getsource(ops.install)
    superseded_at = body.index('("superseded brew services"')
    mode_at = body.index('("install mode"')
    api_at = body.index('("native api service"')
    web_at = body.index('("native web service"')

    assert mode_at < superseded_at < api_at < web_at


# --- ANSI-coloured state text (#3861, review round 3) ------------------------

# What a real runner captured. `brew services list`'s Status column is
# colourised and the escapes survive a pipe: `macos-brew-smoke.yml` run
# 32228088507 shows `nyxgpt-api -> \x1b[31merror` after a pipe through `awk`,
# and `selenium-server \x1b[39mnone\x1b[0m` in the same log. Those two tokens
# are reproduced verbatim below; the rest of the row shape is
# `_OWNERS_MACHINE`'s.
#
# Every state nyxGPT compares against a literal (`LIVE_STATES`, `state ==
# "started"`, `state != "none"`) is read out of this, so if the escapes are not
# stripped at the parser a running service reads as down and a de-registered
# one reads as live -- in opposite directions, on the same machine.
_COLOURED = (
    "Name               Status  User   File\n"
    "nyxgpt-api         \x1b[31merror 3\x1b[0m  runner "
    "~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api.plist\n"
    "nyxgpt-api@3.0.0rc \x1b[32mstarted\x1b[0m  runner "
    "~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api@3.0.0rc.plist\n"
    "nyxgpt-web         \x1b[39mnone\x1b[0m\n"
    "nyxgpt-web@3.0.0rc \x1b[32mstarted\x1b[0m  runner "
    "~/Library/LaunchAgents/homebrew.mxcl.nyxgpt-web@3.0.0rc.plist\n"
    "selenium-server    \x1b[39mnone\x1b[0m\n"
)


def test_coloured_states_parse_to_the_bare_state():
    """The chokepoint: no state reaches a comparison wearing an escape."""
    snapshot = brew_services.parse_services_list(_COLOURED)

    assert snapshot["nyxgpt-api"] == "error"
    assert snapshot["nyxgpt-api@3.0.0rc"] == "started"
    assert snapshot["nyxgpt-web"] == "none"
    assert snapshot["selenium-server"] == "none"
    assert not [state for state in snapshot.values() if "\x1b" in state]


def test_a_coloured_started_still_wins_the_resolve():
    """`_rank` prefers a live service by `LIVE_STATES` membership.

    A coloured `started` is in no frozenset, so an unstripped snapshot ranks a
    serving service level with a dead one and falls through to the
    versioned-over-unversioned tie-break -- which is why this fixture puts the
    running service on the *stable* name: that tie-break then resolves the
    component to the candidate keg that is not running, and `nyxgpt ops
    restart api` acts on it.
    """
    snapshot = brew_services.parse_services_list(
        "Name               Status\n"
        "nyxgpt-api         \x1b[32mstarted\x1b[0m\n"
        "nyxgpt-api@3.0.0rc \x1b[39mnone\x1b[0m\n"
    )

    assert brew_services.resolve("api", "nyxgpt-api", snapshot) == "nyxgpt-api"


def test_a_coloured_none_is_still_dropped_by_registered_only():
    """`superseded`'s filter is `!= "none"` -- the other direction of the same bug.

    Unstripped, a formula brew reports as having no service registered looks
    like a registration and `nyxgpt ops install` stops a service that does not
    exist.
    """
    snapshot = brew_services.parse_services_list(_COLOURED)

    assert brew_services.superseded("nyxgpt-web", snapshot, keep="nyxgpt-web@3.0.0rc") == []


def test_the_health_probe_is_not_inverted_by_a_coloured_status_column(monkeypatch):
    """End to end through the real snapshot reader, not a stubbed one.

    `healthy = state == "started"` in `list_component_status` is what
    `_wait_for_stack_healthy` -- `nyxgpt up`'s exit gate -- rides on, so a
    coloured column would make `nyxgpt up` burn its whole timeout and exit 2
    on a healthy machine. That is #3853's symptom arriving by a second route,
    which is why this goes through `_brew_services_snapshot` rather than
    around it.
    """
    monkeypatch.setattr(self_heal.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(self_heal, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: _cp(stdout=_COLOURED if "services" in cmd else ""),
    )
    monkeypatch.setattr(self_heal, "_dev_mode_active", lambda: False)
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")

    by_service = {s.service: s for s in self_heal.list_component_status()}

    assert by_service["api"].healthy is True
    assert by_service["api"].container == "nyxgpt-api@3.0.0rc"
    assert by_service["web"].healthy is True


def test_the_registration_read_strips_colour_and_keeps_a_spaced_path(monkeypatch):
    """`_brew_service_registration` parses brew's rows itself, so it strips too.

    And the File column is matched as "the rest of the line from its leading
    `/` or `~`" rather than as the last whitespace-separated field, so a home
    directory with a space in it still yields the whole plist path instead of
    its last word.
    """
    row = (
        "Name       Status  User   File\n"
        "nyxgpt-api \x1b[31merror 3\x1b[0m  runner "
        "/Users/dar la/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api.plist\n"
    )
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops, "_run", lambda *a, **k: _cp(stdout=row))

    state, plist = ops._brew_service_registration("nyxgpt-api")

    assert state == "error"
    assert plist is not None
    assert str(plist) == "/Users/dar la/Library/LaunchAgents/homebrew.mxcl.nyxgpt-api.plist"
