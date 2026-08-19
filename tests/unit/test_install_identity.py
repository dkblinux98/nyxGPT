"""The install marker records an identity, not a mode (#3861).

The defect these tests pin: `InstallModeState` had two values -- `mode`, plus
a checkout path for dev mode -- and the owner's Mac had four concurrent
install identities. Installing `nyxgpt-api@3.0.0rc` over an existing
`nyxgpt-api` 2.1.0 wrote `artifact` where `artifact` already stood, so
`_reconcile_install_mode`'s `previous.mode != target` gate had nothing to
compare and stopped nothing; both kegs kept `keep_alive` services registered
on ports 8000/3000 and crash-looped against each other for seven hours.

The claims covered here:

- an identity names the manager, the concrete per-component service name, the
  version and the channel, and `mode` is one field of it;
- reconciliation is a *comparison*, so an unanticipated transition pair
  (stable -> candidate, candidate -> stable, a version bump that renames a
  service) reconciles without anyone having enumerated it;
- an unknown previous identity -- a pre-#3861 marker, a malformed one, or no
  marker -- is a possible mismatch reconciled defensively, never "the same";
- `read_install_mode` still never raises;
- `status`/`doctor` report which build is installed, and `doctor` names
  services registered by an install the marker does not describe.

Conventions follow `test_ops_dev_mode.py`: `_run`/`_which` are mocked,
`platform.system()` is pinned per test, and the marker is redirected into
`tmp_path` by the conftest fixture so nothing touches the developer's real
`~/.nyxGPT`.
"""

import subprocess
from pathlib import Path

import pytest

from nyxgpt import install_mode, ops
from nyxgpt.install_mode import (
    CHANNEL_CANDIDATE,
    CHANNEL_DEV,
    CHANNEL_STABLE,
    INSTALL_MODE_ARTIFACT,
    MANAGER_BREW,
    MANAGER_LAUNCHD,
    MANAGER_SYSTEMD,
    InstallIdentity,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_marker(monkeypatch, tmp_path):
    """Point the native marker at tmp_path for every test in this file."""
    marker = tmp_path / ".nyxGPT" / "install-mode.json"
    monkeypatch.setattr(install_mode, "INSTALL_MODE_FILE", marker)
    return marker


def _identity(**overrides) -> InstallIdentity:
    """A known brew/stable identity for `nyxgpt-api`/`nyxgpt-web` 2.1.0."""
    fields: dict = {
        "mode": INSTALL_MODE_ARTIFACT,
        "manager": MANAGER_BREW,
        "services": {"api": "nyxgpt-api", "web": "nyxgpt-web"},
        "version": "2.1.0",
        "channel": CHANNEL_STABLE,
    }
    fields.update(overrides)
    return InstallIdentity.build(**fields)


# --- the model ---


def test_two_artifact_installs_of_different_builds_are_different_identities():
    """The whole defect in one assertion: `artifact == artifact` was the model."""
    stable = _identity()
    candidate = _identity(
        services={"api": "nyxgpt-api@3.0.0rc", "web": "nyxgpt-web@3.0.0rc"},
        version="3.0.0rc12",
        channel=CHANNEL_CANDIDATE,
    )
    assert stable.mode == candidate.mode == INSTALL_MODE_ARTIFACT
    assert stable != candidate
    diffs = stable.differences(candidate)
    assert any("version" in d for d in diffs)
    assert any("channel" in d for d in diffs)
    assert any("services" in d for d in diffs)


def test_the_same_identity_has_no_differences():
    assert _identity().differences(_identity()) == []


def test_service_order_does_not_make_two_identities_differ():
    forwards = InstallIdentity.build(
        mode=INSTALL_MODE_ARTIFACT,
        manager=MANAGER_BREW,
        services={"api": "nyxgpt-api", "web": "nyxgpt-web"},
        version="2.1.0",
        channel=CHANNEL_STABLE,
    )
    backwards = InstallIdentity.build(
        mode=INSTALL_MODE_ARTIFACT,
        manager=MANAGER_BREW,
        services={"web": "nyxgpt-web", "api": "nyxgpt-api"},
        version="2.1.0",
        channel=CHANNEL_STABLE,
    )
    assert forwards == backwards


def test_an_unknown_identity_never_compares_equal_even_to_another_unknown():
    """Unknown must not silently mean "same" -- that is today's failure."""
    assert InstallIdentity().differences(InstallIdentity()) != []
    assert InstallIdentity().differences(_identity()) != []
    assert _identity().differences(InstallIdentity()) != []


def test_identity_roundtrips_through_the_marker(tmp_path):
    identity = _identity(
        services={"api": "nyxgpt-api@3.0.0rc", "web": "nyxgpt-web@3.0.0rc"},
        version="3.0.0rc12",
        channel=CHANNEL_CANDIDATE,
    )
    install_mode.write_install_mode(INSTALL_MODE_ARTIFACT, None, identity=identity)
    read_back = install_mode.read_install_mode().identity
    assert read_back == identity
    assert read_back.service_map == {"api": "nyxgpt-api@3.0.0rc", "web": "nyxgpt-web@3.0.0rc"}


def test_a_pre_3861_marker_reads_back_as_an_unknown_identity(_isolated_marker):
    """Backward compatibility: two fields is a valid marker, and an unknown identity."""
    _isolated_marker.parent.mkdir(parents=True, exist_ok=True)
    _isolated_marker.write_text('{"mode": "dev", "checkout": "/co"}', encoding="utf-8")
    state = install_mode.read_install_mode()
    # The mode half still reads exactly as it did -- a live dev install must
    # not be downgraded to artifact by an upgrade.
    assert state.is_dev is True
    assert state.checkout == "/co"
    assert state.recorded is True
    assert state.identity.known is False


def test_a_missing_marker_reads_back_as_an_unknown_identity():
    state = install_mode.read_install_mode()
    assert state.identity.known is False
    assert state.mode == INSTALL_MODE_ARTIFACT


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        '{"mode": "artifact", "identity": "nyxgpt-api"}',
        '{"mode": "artifact", "identity": {}}',
        '{"mode": "artifact", "identity": {"manager": "brew"}}',
        '{"mode": "artifact", "identity": {"mode": "artifact", "services": ["api"]}}',
    ],
)
def test_a_malformed_marker_never_raises_and_never_invents_an_identity(_isolated_marker, payload):
    """`read_install_mode`'s never-raises contract, extended to the identity block."""
    _isolated_marker.parent.mkdir(parents=True, exist_ok=True)
    _isolated_marker.write_text(payload, encoding="utf-8")
    state = install_mode.read_install_mode()
    assert state.identity.known is False


def test_an_identity_less_write_leaves_no_identity_block(_isolated_marker):
    """A caller that cannot describe what it installed records no identity."""
    install_mode.write_install_mode(INSTALL_MODE_ARTIFACT, None)
    assert "identity" not in _isolated_marker.read_text(encoding="utf-8")
    assert install_mode.read_install_mode().identity.known is False


# --- what the operator is shown ---


def test_the_label_names_which_build_not_only_that_it_is_an_artifact():
    """`ops status`/`doctor` printed the same sentence for a 2.1.0 and a 3.0.0rc12 keg."""
    stable = install_mode.InstallModeState(recorded=True, identity=_identity())
    candidate = install_mode.InstallModeState(
        recorded=True,
        identity=_identity(
            services={"api": "nyxgpt-api@3.0.0rc", "web": "nyxgpt-web@3.0.0rc"},
            version="3.0.0rc12",
            channel=CHANNEL_CANDIDATE,
        ),
    )
    assert stable.label() != candidate.label()
    assert "2.1.0" in stable.label()
    assert "nyxgpt-api@3.0.0rc" in candidate.label()
    assert CHANNEL_CANDIDATE in candidate.label()


def test_the_label_is_unchanged_when_no_identity_was_recorded():
    """Nothing recorded means nothing asserted -- the old sentence, and only it."""
    assert (
        install_mode.InstallModeState().label()
        == "artifact (published/vendored build -- the repo-less default)"
    )


def test_infra_status_reports_the_identity(monkeypatch):
    """The Infrastructure page's source of "which build is this?"."""
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={"api": "started"}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(ops.self_heal, "compose_probe_available", lambda: True)
    install_mode.write_install_mode(INSTALL_MODE_ARTIFACT, None, identity=_identity())

    reported = ops.infra_status()["install_mode"]

    assert reported["identity"]["known"] is True
    assert reported["identity"]["services"] == {"api": "nyxgpt-api", "web": "nyxgpt-web"}
    assert reported["identity"]["version"] == "2.1.0"
    assert reported["identity"]["channel"] == CHANNEL_STABLE


def test_infra_status_says_unknown_rather_than_implying_an_identity(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={"api": "started"}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(ops.self_heal, "compose_probe_available", lambda: True)

    reported = ops.infra_status()["install_mode"]

    assert reported["mode"] == INSTALL_MODE_ARTIFACT
    assert reported["identity"]["known"] is False


def test_doctor_names_services_the_recorded_install_does_not_own(monkeypatch):
    """The detection half: a second keg pair is nameable instead of invisible."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: Path("/nonexistent-home")))
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-api@3.0.0rc": "started", "ollama": "started"},
    )
    issues = ops._foreign_native_service_issues(_identity())
    assert len(issues) == 1
    assert "nyxgpt-api@3.0.0rc" in issues[0]
    # ollama is the same brew service in every mode and is never foreign.
    assert "ollama" not in issues[0]
    # ...and the service this install owns is not reported against itself.
    assert "nyxgpt-api (brew)" not in issues[0]


def test_doctor_says_so_when_nothing_recorded_an_identity(monkeypatch):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: Path("/nonexistent-home")))
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {"nyxgpt-api": "started"})
    issues = ops._foreign_native_service_issues(InstallIdentity())
    assert len(issues) == 1
    assert "No install identity is recorded" in issues[0]


def test_doctor_is_quiet_when_the_machine_matches_its_marker(monkeypatch):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: Path("/nonexistent-home")))
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-web": "started", "ollama": "started"},
    )
    assert ops._foreign_native_service_issues(_identity()) == []


# --- identity detection (ops) ---


def test_macos_artifact_identity_names_the_candidate_formula(monkeypatch, tmp_path):
    """A candidate install registers `nyxgpt-api@3.0.0rc`, and the identity says so."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    # No checkout -> the published tap -> the channel formula (#3759).
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0rc12")

    identity = ops._native_install_identity(dev=False)

    assert identity.manager == MANAGER_BREW
    assert identity.service_map == {
        "api": "nyxgpt-api@3.0.0rc",
        "web": "nyxgpt-web@3.0.0rc",
    }
    assert identity.version == "3.0.0rc12"
    assert identity.channel == CHANNEL_CANDIDATE


def test_macos_artifact_identity_from_a_checkout_names_the_local_tap_formula(monkeypatch, tmp_path):
    """A checkout builds the local `file://` tap, which carries the plain names."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    (tmp_path / "homebrew").mkdir()
    for name in ("nyxgpt-api", "nyxgpt-web"):
        (tmp_path / "homebrew" / f"{name}.rb").write_text("class X < Formula\nend\n")
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0rc12")

    identity = ops._native_install_identity(dev=False)

    # The version really is a candidate, but the formula the local tap
    # installs is the plain one -- and the identity has to say what will
    # actually be registered, not what the version implies.
    assert identity.service_map == {"api": "nyxgpt-api", "web": "nyxgpt-web"}
    assert identity.channel == CHANNEL_CANDIDATE


def test_macos_dev_identity_names_the_launchagents(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0")
    monkeypatch.setattr(ops, "_dev_checkout_root", lambda: tmp_path / "co")

    identity = ops._native_install_identity(dev=True)

    assert identity.manager == MANAGER_LAUNCHD
    assert identity.service_map == {"api": "com.nyxgpt.api", "web": "com.nyxgpt.web"}
    assert identity.channel == CHANNEL_DEV
    assert identity.checkout == str(tmp_path / "co")


def test_linux_identities_differ_by_version_because_the_units_never_do(monkeypatch):
    """The Linux shape the issue warns against assuming away.

    Both modes drive the same `nyxgpt-api`/`nyxgpt-web` systemd --user units,
    so the manager and the service names carry no signal there at all: the
    version and channel fields are the entire difference between two Linux
    installs, which is why they are part of the identity rather than
    decoration on it.
    """
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0")
    stable = ops._native_install_identity(dev=False)
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.1")
    newer = ops._native_install_identity(dev=False)

    assert stable.manager == newer.manager == MANAGER_SYSTEMD
    assert stable.service_names == newer.service_names
    assert stable.differences(newer) == ["version: 3.0.0 -> 3.0.1"]


# --- reconciliation by comparison ---


def _macos_machine(monkeypatch, tmp_path):
    """A macOS machine whose service managers are all mocked into tmp_path."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    stopped: list[str] = []
    monkeypatch.setattr(
        ops,
        "_stop_brew_service",
        lambda name: stopped.append(name) or [ops.OpsResult(True, f"stopped {name}")],
    )
    monkeypatch.setattr(ops, "_stop_launchagent", lambda label: [ops.OpsResult(True, label)])
    # An empty machine by default. The reconcile always reads what the managers
    # actually have registered (the union in `_retire_previous_identity`), so a
    # test that does not set this is saying "nothing beyond the marker".
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    return stopped


def test_installing_a_candidate_over_a_stable_retires_the_stable_services(monkeypatch, tmp_path):
    """The test the acceptance criteria say cannot even be expressed today.

    Recorded: `nyxgpt-api`/`nyxgpt-web` 2.1.0. Target: the 3.0.0rc12
    candidate, whose kegs register `nyxgpt-api@3.0.0rc`/`nyxgpt-web@3.0.0rc`.
    Both are `artifact` mode, so the pre-#3861 gate saw no change and stopped
    nothing -- which is how two `keep_alive` service sets ended up bound to
    ports 8000/3000 on the same machine.
    """
    stopped = _macos_machine(monkeypatch, tmp_path)
    install_mode.write_install_mode(INSTALL_MODE_ARTIFACT, None, identity=_identity())
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0rc12")

    results = ops._reconcile_install_mode(dev=False)

    assert all(r.ok for r in results), [r.message for r in results]
    assert stopped == ["nyxgpt-api", "nyxgpt-web"]
    recorded = install_mode.read_install_mode().identity
    assert recorded.service_map == {
        "api": "nyxgpt-api@3.0.0rc",
        "web": "nyxgpt-web@3.0.0rc",
    }
    assert recorded.version == "3.0.0rc12"


def test_installing_a_stable_over_a_candidate_retires_the_candidate_services(monkeypatch, tmp_path):
    """The reverse direction, which no pair list contained either (#3853)."""
    stopped = _macos_machine(monkeypatch, tmp_path)
    install_mode.write_install_mode(
        INSTALL_MODE_ARTIFACT,
        None,
        identity=_identity(
            services={"api": "nyxgpt-api@3.0.0rc", "web": "nyxgpt-web@3.0.0rc"},
            version="3.0.0rc12",
            channel=CHANNEL_CANDIDATE,
        ),
    )
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0")

    ops._reconcile_install_mode(dev=False)

    assert stopped == ["nyxgpt-api@3.0.0rc", "nyxgpt-web@3.0.0rc"]


def test_a_third_install_nobody_recorded_is_retired_too(monkeypatch, tmp_path):
    """The marker is not the only thing that can be holding a port.

    The owner's Mac carried four install identities and one marker. Retiring
    only what the marker names would leave every install nothing recorded
    exactly where it was -- so the reconcile takes the union of the recorded
    previous identity and what the managers actually report.
    """
    stopped = _macos_machine(monkeypatch, tmp_path)
    install_mode.write_install_mode(INSTALL_MODE_ARTIFACT, None, identity=_identity())
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0rc12")
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {
            # Neither the recorded previous nor the target: a keg from an
            # install that predates the marker entirely.
            "nyxgpt-api@2.9.0rc": "started",
            # The target's own service, already registered -- never retired.
            "nyxgpt-api@3.0.0rc": "started",
            # Installed but unregistered, and the same brew service in every
            # mode: neither is competing for anything.
            "nyxgpt-web": "none",
            "ollama": "started",
        },
    )

    ops._reconcile_install_mode(dev=False)

    # `nyxgpt-web` appears once, from the marker -- the union deduplicates,
    # and `none` in the snapshot is not a second reason to stop it.
    assert stopped == ["nyxgpt-api", "nyxgpt-web", "nyxgpt-api@2.9.0rc"]


def test_a_version_bump_within_a_channel_keeps_the_services_and_the_venv(monkeypatch, tmp_path):
    """Same names, so nothing is stale -- and the api venv is not rebuilt.

    The venv drop exists because two *modes* share one venv path. An ordinary
    upgrade pip-installs the new tarball into that same venv, so rebuilding it
    would add minutes to every Linux upgrade to remove a race that cannot
    happen.
    """
    stopped = _macos_machine(monkeypatch, tmp_path)
    venv = tmp_path / "opt" / "nyxgpt-api" / "venv"
    venv.mkdir(parents=True)
    install_mode.write_install_mode(
        INSTALL_MODE_ARTIFACT, None, identity=_identity(version="3.0.0")
    )
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.1")

    results = ops._reconcile_install_mode(dev=False)

    assert stopped == []
    assert venv.exists()
    assert any("Install identity changing" in r.message for r in results)
    assert install_mode.read_install_mode().identity.version == "3.0.1"


def test_an_unknown_previous_identity_reconciles_what_the_managers_report(monkeypatch, tmp_path):
    """AC3: unknown is a possible mismatch, never "the same".

    This is the upgrade path every existing machine takes exactly once: a
    marker written before #3861, or none at all, and a second install still
    registered beside the one being installed.
    """
    stopped = _macos_machine(monkeypatch, tmp_path)
    venv = tmp_path / "opt" / "nyxgpt-api" / "venv"
    venv.mkdir(parents=True)
    (tmp_path / ".nyxGPT").mkdir(parents=True, exist_ok=True)
    install_mode.INSTALL_MODE_FILE.write_text('{"mode": "artifact"}', encoding="utf-8")
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0rc12")
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {
            "nyxgpt-api": "started",
            "nyxgpt-web": "started",
            "nyxgpt-api@3.0.0rc": "started",
            "ollama": "started",
        },
    )

    results = ops._reconcile_install_mode(dev=False)

    # The 2.1.0 pair retires; the candidate api the target is about to
    # register does not, and ollama is never touched.
    assert stopped == ["nyxgpt-api", "nyxgpt-web"]
    assert not venv.exists()
    assert any("unknown" in (r.details or "") for r in results)


def test_an_unknown_previous_identity_on_a_clean_machine_retires_nothing(monkeypatch, tmp_path):
    """The defensive path must be free on a fresh install, not merely correct."""
    stopped = _macos_machine(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_native_service_version", lambda: "3.0.0")
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {"ollama": "started"})

    results = ops._reconcile_install_mode(dev=False)

    assert stopped == []
    assert all(r.ok for r in results), [r.message for r in results]
    assert install_mode.read_install_mode().identity.known is True


def test_discovery_finds_a_dev_launchagent_plist_that_is_not_loaded(monkeypatch, tmp_path):
    """An unloaded plist with KeepAlive is still a live claim on the port."""
    _macos_machine(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    la_dir = tmp_path / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    (la_dir / "com.nyxgpt.web.plist").write_text("<plist/>", encoding="utf-8")

    assert ops._discover_native_services() == [(MANAGER_LAUNCHD, "com.nyxgpt.web")]


def test_retiring_a_launchagent_removes_its_plist(monkeypatch, tmp_path):
    _macos_machine(monkeypatch, tmp_path)
    la_dir = tmp_path / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    plist = la_dir / "com.nyxgpt.api.plist"
    plist.write_text("<plist/>", encoding="utf-8")

    results = ops._retire_service(MANAGER_LAUNCHD, "com.nyxgpt.api")

    assert all(r.ok for r in results), [r.message for r in results]
    assert not plist.exists()


def test_retiring_never_uninstalls_the_previous_keg(monkeypatch):
    """Stop and de-register, never uninstall -- removal is a teardown call (#3859).

    A keg left in place is harmless once nothing starts it. A *registered*
    service is not: `brew services` and launchd's `KeepAlive` both restart
    their job at login, which is what kept two apis fighting over port 8000.
    """
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    ran: list[list[str]] = []

    def _record(argv, **kwargs):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", _record)

    ops._retire_service(MANAGER_BREW, "nyxgpt-api")

    assert ran == [["brew", "services", "stop", "nyxgpt-api"]]
