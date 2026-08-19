"""`nyxgpt ops uninstall` -- the wrapped teardown that has to precede removal (#3859).

The defect these cover: after `brew uninstall` + `brew untap` of every nyxgpt
formula, the owner's Mac was still serving :8000 and :3000 from kegs whose
files had just been deleted, `com.nyxgpt.ollama-logs` was still running at PID
58068, and five plists remained in ~/Library/LaunchAgents. No supported
command could stop any of it -- with the tap gone brew could not resolve the
formula names, and `nyxgpt ops down` leaves the machine installed by design.

So the assertions here are about *deregistration*, not stopping: a plist or a
systemd unit left on disk is reloaded at the next login, which is the
difference between "stopped" and "uninstalled".
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from nyxgpt import install_mode, ops


def _macos(monkeypatch, tmp_path: Path) -> Path:
    """Stage a macOS machine whose HOME is `tmp_path`; returns its LaunchAgents dir."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    la_dir = tmp_path / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    return la_dir


def _write_plist(la_dir: Path, label: str, program_args: list[str]) -> Path:
    """Write a launchd plist for `label` invoking `program_args`."""
    path = la_dir / f"{label}.plist"
    with path.open("wb") as handle:
        plistlib.dump({"Label": label, "ProgramArguments": program_args}, handle)
    return path


@pytest.mark.unit
def test_the_log_and_env_agents_are_one_labelled_set(monkeypatch, tmp_path):
    """All three unconditionally-installed agents are removed, not just the followers.

    `DEV_LAUNCHD_LABELS` holds only api/web, so a teardown built on it alone
    would have left `com.nyxgpt.ollama-logs`, `com.nyxgpt.ollama-env` and
    `com.nyxgpt.cassandra-logs` exactly where the owner found them.
    """
    la_dir = _macos(monkeypatch, tmp_path)
    booted: list[str] = []
    monkeypatch.setattr(
        ops, "_stop_launchagent", lambda label: booted.append(label) or [ops.OpsResult(True, "")]
    )
    for label in ops.SUPPORT_LAUNCHD_LABELS.values():
        _write_plist(la_dir, label, ["/bin/bash", "-lc", str(tmp_path / "script.sh")])

    results = ops._remove_support_launchagents()

    assert all(r.ok for r in results)
    assert sorted(booted) == sorted(ops.SUPPORT_LAUNCHD_LABELS.values())
    assert list(la_dir.iterdir()) == []


@pytest.mark.unit
def test_the_labelled_set_covers_the_agents_that_survived_the_owners_uninstall():
    """The three `com.nyxgpt.*` agents no `brew uninstall` could ever reach."""
    assert set(ops.SUPPORT_LAUNCHD_LABELS.values()) == {
        "com.nyxgpt.cassandra-logs",
        "com.nyxgpt.ollama-logs",
        "com.nyxgpt.ollama-env",
    }
    # And the restart/stop targets are a view of it, not a second list that
    # can drift -- the drift is what left two of three agents unreported.
    for name, label in ops._NATIVE_LOG_FOLLOWER_LAUNCHD_LABELS.items():
        assert ops.SUPPORT_LAUNCHD_LABELS[name] is label
    for name, unit in ops._NATIVE_LOG_FOLLOWER_SYSTEMD_UNITS.items():
        assert ops.SUPPORT_SYSTEMD_UNITS[name] is unit


@pytest.mark.unit
def test_the_dev_agents_still_go_through_the_one_removal_path(monkeypatch, tmp_path):
    """`_remove_dev_launchagents` keeps its behavior after the generalization.

    The mode-switch caller (`_reconcile_install_mode`) is unchanged; what
    #3859 widened is the set of triggers, not the function.
    """
    la_dir = _macos(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_stop_launchagent", lambda label: [ops.OpsResult(True, "")])
    for label in install_mode.DEV_LAUNCHD_LABELS.values():
        _write_plist(la_dir, label, ["/bin/bash", str(tmp_path / "wrapper")])

    results = ops._remove_dev_launchagents()

    assert list(la_dir.iterdir()) == []
    assert any("dev-mode LaunchAgent" in r.message for r in results)


@pytest.mark.unit
def test_removal_is_idempotent_on_a_machine_with_nothing_left(monkeypatch, tmp_path):
    """Teardown routinely runs against half-removed states, so absence is success."""
    _macos(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_stop_launchagent", lambda label: [ops.OpsResult(True, "")])

    assert all(r.ok for r in ops._remove_support_launchagents())
    assert all(r.ok for r in ops._remove_dev_launchagents())


@pytest.mark.unit
def test_brew_service_labels_union_disk_and_launchd(monkeypatch, tmp_path):
    """A candidate-channel service is found by its formula-derived label.

    `nyxgpt-api@3.0.0rc` is not in any list this build ships -- matching by
    the `homebrew.mxcl.nyxgpt` prefix is what finds a release line the code
    has never heard of.
    """
    la_dir = _macos(monkeypatch, tmp_path)
    _write_plist(la_dir, "homebrew.mxcl.nyxgpt-web@3.0.0rc", ["/bin/bash", "/gone/nyxgpt-web"])
    (la_dir / "homebrew.mxcl.postgresql@16.plist").write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(
        ops,
        "_loaded_launchd_labels",
        lambda prefix: ["homebrew.mxcl.nyxgpt-api@3.0.0rc"],
    )

    assert ops._brew_service_launchd_labels() == [
        "homebrew.mxcl.nyxgpt-api@3.0.0rc",
        "homebrew.mxcl.nyxgpt-web@3.0.0rc",
    ]


@pytest.mark.unit
def test_an_untapped_brew_service_is_still_stopped_and_deregistered(monkeypatch, tmp_path):
    """The owner's actual state: brew cannot name the formula, launchd still can.

    `brew services stop nyxgpt-api@3.0.0rc` has nothing to act on once the tap
    is gone. Bootout plus the plist unlink do not need brew at all, which is
    why they run unconditionally.
    """
    la_dir = _macos(monkeypatch, tmp_path)
    label = "homebrew.mxcl.nyxgpt-api@3.0.0rc"
    _write_plist(la_dir, label, ["/bin/bash", "/gone/bin/nyxgpt-api"])
    monkeypatch.setattr(ops, "_loaded_launchd_labels", lambda prefix: [label])
    monkeypatch.setattr(ops, "_which", lambda tool: None)  # no brew on PATH at all
    booted: list[str] = []
    monkeypatch.setattr(
        ops, "_stop_launchagent", lambda lbl: booted.append(lbl) or [ops.OpsResult(True, "")]
    )

    results = ops._remove_brew_service_launchd_jobs()

    assert booted == [label]
    assert not (la_dir / f"{label}.plist").exists()
    assert all(r.ok for r in results)


@pytest.mark.unit
def test_brew_is_asked_first_when_it_can_still_resolve_the_formula(monkeypatch, tmp_path):
    """Homebrew's own bookkeeping stays consistent when brew is still there."""
    la_dir = _macos(monkeypatch, tmp_path)
    label = "homebrew.mxcl.nyxgpt-api"
    _write_plist(la_dir, label, ["/bin/bash", "/gone/bin/nyxgpt-api"])
    monkeypatch.setattr(ops, "_loaded_launchd_labels", lambda prefix: [label])
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_stop_launchagent", lambda lbl: [ops.OpsResult(True, "")])
    ran: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        ran.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._remove_brew_service_launchd_jobs()

    assert ["brew", "services", "stop", "nyxgpt-api"] in ran
    assert any(r.message == "Stopped brew service: nyxgpt-api" for r in results)


@pytest.mark.unit
def test_no_brew_services_registered_is_reported_not_failed(monkeypatch, tmp_path):
    _macos(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_loaded_launchd_labels", lambda prefix: [])

    results = ops._remove_brew_service_launchd_jobs()

    assert [r.ok for r in results] == [True]
    assert "No Homebrew-managed nyxgpt services" in results[0].message


@pytest.mark.unit
def test_systemd_units_are_disabled_and_deleted(monkeypatch, tmp_path):
    """Linux twin: `disable --now` plus the unit-file unlink, then daemon-reload.

    A unit file left in ~/.config/systemd/user is the systemd equivalent of a
    plist left in ~/Library/LaunchAgents -- re-enablable, and back at the next
    login. The stray `nyxgpt-legacy.service` proves the glob half: a unit from
    an older nyxGPT that no map in this build names is still removed.
    """
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    monkeypatch.setattr(ops, "_systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    for unit in ("nyxgpt-api", "nyxgpt-ollama-logs", "nyxgpt-legacy"):
        (unit_dir / f"{unit}.service").write_text("[Unit]\n", encoding="utf-8")
    ran: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        ran.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._remove_native_systemd_units()

    assert all(r.ok for r in results)
    assert list(unit_dir.iterdir()) == []
    assert ["systemctl", "--user", "disable", "--now", "nyxgpt-legacy.service"] in ran
    assert ["systemctl", "--user", "daemon-reload"] in ran


@pytest.mark.unit
def test_systemd_teardown_on_a_clean_machine_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    monkeypatch.setattr(ops, "_systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(ops, "_which", lambda tool: None)

    results = ops._remove_native_systemd_units()

    assert [r.ok for r in results] == [True]
    assert "No nyxgpt systemd --user units left installed" in results[0].message


@pytest.mark.unit
def test_uninstall_runs_every_population(monkeypatch, tmp_path, capsys):
    """The command covers all three populations plus the install-mode marker."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    called: list[str] = []

    def record(name, value=None):
        def _step(*args, **kwargs):
            called.append(name)
            return [ops.OpsResult(True, name)]

        return _step

    monkeypatch.setattr(ops, "_down_mark_intentional_stops", record("mark"))
    monkeypatch.setattr(ops, "_stop_native_service", lambda c: called.append(f"stop:{c}") or [])
    monkeypatch.setattr(ops, "_stop_docker_container", lambda n: called.append(f"docker:{n}") or [])
    monkeypatch.setattr(ops, "_remove_dev_launchagents", record("dev-agents"))
    monkeypatch.setattr(ops, "_remove_support_launchagents", record("support-agents"))
    monkeypatch.setattr(ops, "_remove_brew_service_launchd_jobs", record("brew-services"))
    monkeypatch.setattr(ops, "_down_compose_teardown", lambda scope, volumes: [])
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    install_mode.write_install_mode(install_mode.INSTALL_MODE_ARTIFACT, None)

    rc = ops.uninstall(SimpleNamespace(volumes=False, yes_really=False, quiet=False))

    assert rc == 0
    assert called == [
        "mark",
        "stop:api",
        "stop:web",
        "stop:ollama",
        "docker:nyxgpt-cassandra",
        "dev-agents",
        "support-agents",
        "brew-services",
    ]
    assert not install_mode.install_mode_file().exists()
    # The operator is told what to run next -- removing the artifact stays
    # the package manager's job, and the teardown is what makes it safe.
    assert "brew uninstall" in capsys.readouterr().out


@pytest.mark.unit
def test_uninstall_preserves_data_unless_explicitly_told_otherwise(monkeypatch, tmp_path, capsys):
    """`--volumes` without `--yes-really` refuses, same contract as `down`."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))

    rc = ops.uninstall(SimpleNamespace(volumes=True, yes_really=False, quiet=False))

    assert rc == 2
    assert "--yes-really" in capsys.readouterr().err


@pytest.mark.unit
def test_uninstall_passes_the_volume_choice_through(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(ops, "_down_mark_intentional_stops", lambda: [])
    monkeypatch.setattr(ops, "_stop_native_service", lambda c: [])
    monkeypatch.setattr(ops, "_stop_docker_container", lambda n: [])
    monkeypatch.setattr(ops, "_uninstall_native_service_managers", lambda: [])
    monkeypatch.setattr(
        ops, "_down_compose_teardown", lambda scope, volumes: seen.append((scope, volumes)) or []
    )
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)

    rc = ops.uninstall(SimpleNamespace(volumes=True, yes_really=True, quiet=True))

    assert rc == 0
    assert seen == [("all", True)]


@pytest.mark.unit
def test_a_job_pointing_at_a_deleted_keg_is_orphaned(monkeypatch, tmp_path):
    """`argv[0]` is `/bin/bash` for every brew service, so it cannot be the signal."""
    la_dir = _macos(monkeypatch, tmp_path)
    _write_plist(la_dir, "homebrew.mxcl.nyxgpt-api@3.0.0rc", ["/bin/bash", "/gone/bin/nyxgpt-api"])

    assert ops._launchd_job_is_orphaned("homebrew.mxcl.nyxgpt-api@3.0.0rc") is True


@pytest.mark.unit
def test_a_live_service_is_not_reported_as_orphaned(monkeypatch, tmp_path):
    """Otherwise every healthy machine gets the warning, and nobody reads it."""
    la_dir = _macos(monkeypatch, tmp_path)
    wrapper = tmp_path / "bin" / "nyxgpt-api"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/bash\n", encoding="utf-8")
    _write_plist(la_dir, "homebrew.mxcl.nyxgpt-api", ["/bin/bash", str(wrapper)])

    assert ops._launchd_job_is_orphaned("homebrew.mxcl.nyxgpt-api") is False
    # A label with no plist at all is not orphaned either -- it is nothing.
    assert ops._launchd_job_is_orphaned("homebrew.mxcl.nyxgpt-web") is False


@pytest.mark.unit
def test_install_reports_orphans_from_a_previous_install(monkeypatch, tmp_path):
    """The safety net for the uninstall hook Homebrew does not have.

    Reports only: at install time a loaded nyxgpt job is usually the
    operator's own stack, which the install is about to restart normally.
    """
    la_dir = _macos(monkeypatch, tmp_path)
    label = "homebrew.mxcl.nyxgpt-api@3.0.0rc"
    _write_plist(la_dir, label, ["/bin/bash", "/gone/bin/nyxgpt-api"])
    monkeypatch.setattr(ops, "_loaded_launchd_labels", lambda prefix: [label])
    monkeypatch.setattr(ops, "_launchd_agent_loaded", lambda lbl: False)

    results = ops._report_orphaned_launchd_jobs()

    assert [r.ok for r in results] == [True]
    assert label in results[0].details
    assert "nyxgpt ops uninstall" in results[0].details
    # Nothing was removed: this step diagnoses, it does not act.
    assert (la_dir / f"{label}.plist").exists()


@pytest.mark.unit
def test_a_clean_machine_gets_no_orphan_report(monkeypatch, tmp_path):
    _macos(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_loaded_launchd_labels", lambda prefix: [])
    monkeypatch.setattr(ops, "_launchd_agent_loaded", lambda lbl: False)

    assert ops._report_orphaned_launchd_jobs() == []


@pytest.mark.unit
def test_the_orphan_report_never_fails_an_install(monkeypatch, tmp_path):
    """A diagnostic that can red an install is a diagnostic that gets removed."""
    _macos(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_loaded_launchd_labels", lambda prefix: ["homebrew.mxcl.nyxgpt-api"])
    monkeypatch.setattr(ops, "_launchd_agent_loaded", lambda lbl: True)
    monkeypatch.setattr(ops, "_launchd_job_is_orphaned", lambda lbl: True)

    assert all(r.ok for r in ops._report_orphaned_launchd_jobs())


@pytest.mark.unit
def test_the_orphan_report_is_macos_only(monkeypatch, tmp_path):
    """launchd is the only service manager `brew uninstall` can strand a job in."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")

    assert ops._report_orphaned_launchd_jobs() == []
