"""Unit tests for the dev install mode (#3789).

Dev mode (`nyxgpt up --dev`) installs the api/web services from the current
checkout -- an editable venv plus the Next dev server -- instead of building
or downloading artifacts. These tests cover the four claims the issue makes:

- dev mode installs from the checkout (editable pip, dev-server wrapper) and
  never touches a keg, a tap or a dist tarball;
- the artifact path is untouched and remains the default;
- switching modes reconciles the other mode's leftovers rather than leaving
  two things fighting for ports 8000/3000;
- `status`/`doctor` say which mode is running, so a dev pass can't be
  mistaken for an artifact-path pass.

Conventions follow test_ops.py / test_ops_systemd.py: `_run`/`_which`/
subprocess are mocked, `platform.system()` is pinned per test, and the
install-mode marker is redirected into `tmp_path` so nothing reads or writes
the developer's real ~/.nyxGPT.
"""

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import install_mode, ops, self_heal

pytestmark = pytest.mark.unit


class Args:
    """Minimal CLI-args stand-in (attribute bag, like argparse.Namespace)."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _isolated_mode_marker(monkeypatch, tmp_path):
    """Point the install-mode marker at tmp_path for every test in this file."""
    marker = tmp_path / ".nyxGPT" / "install-mode.json"
    monkeypatch.setattr(install_mode, "INSTALL_MODE_FILE", marker)
    return marker


@pytest.fixture
def checkout(tmp_path):
    """A directory shaped like a source checkout, as `_dev_checkout_root` sees one."""
    root = tmp_path / "src-checkout"
    (root / "src" / "nyxgpt").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='nyxgpt'\n", encoding="utf-8")
    (root / "web" / "node_modules").mkdir(parents=True)
    (root / "web" / "package.json").write_text("{}", encoding="utf-8")
    return root


# --- the marker itself ---


def test_install_mode_defaults_to_artifact_when_nothing_recorded():
    state = install_mode.read_install_mode()
    assert state.mode == install_mode.INSTALL_MODE_ARTIFACT
    assert state.is_dev is False
    assert "artifact" in state.label()


def test_install_mode_roundtrips_dev_with_its_checkout(tmp_path):
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")
    state = install_mode.read_install_mode()
    assert state.is_dev is True
    assert state.checkout == str(tmp_path / "co")
    assert str(tmp_path / "co") in state.label()


def test_unreadable_marker_reads_back_as_artifact(_isolated_mode_marker):
    _isolated_mode_marker.parent.mkdir(parents=True, exist_ok=True)
    _isolated_mode_marker.write_text("{not json", encoding="utf-8")
    # Artifact is the safe default: it drives brew services on macOS, which
    # is what any machine without a recorded dev install is running.
    assert install_mode.read_install_mode().is_dev is False


# --- _dev_checkout_root ---


def test_dev_checkout_root_is_none_without_a_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    assert ops._dev_checkout_root() is None


def test_dev_checkout_root_returns_repo_root_in_a_checkout(monkeypatch, checkout):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    assert ops._dev_checkout_root() == checkout


# --- api dev install ---


def test_dev_api_install_is_editable_and_never_builds_an_artifact(monkeypatch, checkout, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    (tmp_path / "opt" / "nyxgpt-api").mkdir(parents=True)

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(
        ops,
        "_install_and_activate_native_systemd_unit",
        lambda service: [ops.OpsResult(True, f"started {service}")],
    )
    with patch.object(ops, "_service_source_tarball") as tarball:
        results = ops._install_native_api_dev()

    assert all(r.ok for r in results), [r.message for r in results]
    # The editable install points at the checkout...
    assert any(c[-3:] == ["install", "-e", str(checkout)] for c in calls), calls
    # ...and nothing vendored/downloaded a dist tarball to install instead.
    tarball.assert_not_called()

    wrapper = (tmp_path / "opt" / "nyxgpt-api" / "bin" / "nyxgpt-api").read_text(encoding="utf-8")
    assert str(tmp_path / "opt" / "nyxgpt-api" / "venv") in wrapper
    assert "dev mode: editable venv" in wrapper


def test_dev_api_install_refuses_without_a_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    results = ops._install_native_api_dev()
    assert [r.ok for r in results] == [False]
    assert "needs a source checkout" in results[0].message


def test_dev_api_install_reports_a_failing_pip(monkeypatch, checkout, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)

    def fake_run(cmd, *a, **kw):
        if "-e" in cmd:
            return _cp(returncode=1, stderr="ERROR: no matching distribution")
        return _cp()

    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_output_excerpt", lambda cp: cp.stderr)
    results = ops._install_native_api_dev()
    assert [r.ok for r in results] == [True, False]
    assert "no matching distribution" in results[-1].details


def test_dev_api_install_builds_the_venv_on_a_qualifying_interpreter(
    monkeypatch, checkout, tmp_path
):
    """`pip install -e` honours `requires-python` too, so dev mode must not
    assume bare `python3` clears the floor either (#3782 applied to #3789)."""
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(ops, "_which", lambda name: None)
    monkeypatch.setattr(ops, "_running_interpreter", lambda: ("/opt/py/bin/python3.12", (3, 12)))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(
        ops,
        "_install_and_activate_native_systemd_unit",
        lambda service: [ops.OpsResult(True, f"started {service}")],
    )
    results = ops._install_native_api_dev()

    assert all(r.ok for r in results), [r.message for r in results]
    venv_dir = str(tmp_path / "opt" / "nyxgpt-api" / "venv")
    assert ["/opt/py/bin/python3.12", "-m", "venv", venv_dir] in calls, calls
    assert not any(c[:3] == ["python3", "-m", "venv"] for c in calls), calls


def test_dev_api_install_stops_when_no_interpreter_meets_the_floor(monkeypatch, checkout, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(ops, "_which", lambda name: None)
    monkeypatch.setattr(ops, "_running_interpreter", lambda: ("/usr/bin/python3.9", (3, 9)))

    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return _cp()

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._install_native_api_dev()

    assert [r.ok for r in results] == [False]
    assert "No Python >= 3.11" in results[0].message
    # Nothing was attempted with the too-old interpreter.
    assert calls == []


# --- web dev install ---


def test_dev_web_install_points_the_wrapper_at_the_checkout(monkeypatch, checkout, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(
        ops,
        "_install_and_activate_native_systemd_unit",
        lambda service: [ops.OpsResult(True, f"started {service}")],
    )
    with (
        patch.object(ops, "_create_dist_tarball") as tarball,
        patch.object(ops.subprocess, "run") as sub,
    ):
        results = ops._install_native_web_dev()

    assert all(r.ok for r in results), [r.message for r in results]
    # No vendoring and no `npm ci`/`npm run build`: dev mode serves the tree.
    tarball.assert_not_called()
    sub.assert_not_called()

    wrapper = (tmp_path / "opt" / "nyxgpt-web" / "bin" / "nyxgpt-web").read_text(encoding="utf-8")
    assert f'cd "{checkout / "web"}"' in wrapper
    assert "npm run dev" in wrapper
    assert "npm run start" not in wrapper


def test_dev_web_install_reports_missing_node_modules(monkeypatch, checkout, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    (checkout / "web" / "node_modules").rmdir()
    results = ops._install_native_web_dev()
    assert [r.ok for r in results] == [False]
    assert "node_modules" in results[0].details


# --- the artifact path stays the default and stays artifact-shaped ---


def test_artifact_wrapper_still_runs_the_built_bundle(tmp_path):
    ops._write_native_web_wrapper(tmp_path, tmp_path / "build" / "nyxgpt-web-1.2.3", dev=False)
    wrapper = (tmp_path / "bin" / "nyxgpt-web").read_text(encoding="utf-8")
    assert "exec npm run start" in wrapper
    assert "npm run dev" not in wrapper
    assert "self-contained build" in wrapper


def test_both_web_start_commands_bind_the_configured_host(tmp_path):
    """Every mode's start command must carry `--hostname`/`--port` explicitly.

    Neither `next dev` nor `next start` reads the `HOST` env var the wrapper
    exports (Next reads `HOSTNAME`; `next start`'s documented control is
    `-H/--hostname`), so a command without the flag falls back to Next's own
    default of `0.0.0.0`.

    This is parametrised over BOTH modes on purpose. The flags were originally
    passed only on the dev command -- and the comment beside it named the
    hazard while fixing one caller -- so the artifact path, which is the
    repo-less default and therefore every real install, read `[web] host` from
    config, exported it, and then bound every interface anyway. That
    contradicted `DECISION_PRIVATE_ACCESS_MECHANISM.md` ("Nothing is ever
    listening on a non-loopback address on the deployments") and left the
    security group as the only thing in front of an auth-disabled-by-default
    web UI on cloud, and nothing at all in front of it on a local install.

    A per-mode assertion here is what stops the next mode added to this
    function from inheriting the same gap silently.
    """
    for dev, expected_runner in ((True, "npm run dev"), (False, "npm run start")):
        root = tmp_path / ("dev" if dev else "artifact")
        ops._write_native_web_wrapper(root, root / "web", dev=dev)
        wrapper = (root / "bin" / "nyxgpt-web").read_text(encoding="utf-8")
        mode = "dev" if dev else "artifact"

        assert expected_runner in wrapper, f"{mode} mode should run {expected_runner}"
        assert '--hostname "$HOST"' in wrapper, (
            f"{mode} mode's start command does not pass --hostname, so Next binds "
            f"0.0.0.0 and [web] host is silently ignored"
        )
        assert '--port "$PORT"' in wrapper, (
            f"{mode} mode's start command does not pass --port, so [web] port is silently ignored"
        )


@pytest.mark.parametrize(
    "system_value,expected",
    [("Darwin", "_install_homebrew_api"), ("Linux", "_install_native_api_systemd")],
)
def test_install_native_api_without_dev_uses_the_artifact_path(monkeypatch, system_value, expected):
    monkeypatch.setattr(ops.platform, "system", lambda: system_value)
    with (
        patch.object(ops, expected, return_value=[ops.OpsResult(True, "artifact")]) as artifact,
        patch.object(ops, "_install_native_api_dev") as dev,
    ):
        assert ops._install_native_api() == [ops.OpsResult(True, "artifact")]
    artifact.assert_called_once()
    dev.assert_not_called()


@pytest.mark.parametrize(
    "system_value,expected",
    [("Darwin", "_install_homebrew_web"), ("Linux", "_install_native_web_systemd")],
)
def test_install_native_web_without_dev_uses_the_artifact_path(monkeypatch, system_value, expected):
    monkeypatch.setattr(ops.platform, "system", lambda: system_value)
    with (
        patch.object(ops, expected, return_value=[ops.OpsResult(True, "artifact")]) as artifact,
        patch.object(ops, "_install_native_web_dev") as dev,
    ):
        assert ops._install_native_web() == [ops.OpsResult(True, "artifact")]
    artifact.assert_called_once()
    dev.assert_not_called()


def test_install_rejects_dev_without_a_checkout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    assert ops.install(Args(dev=True, terraform=False, kubernetes=False)) == 2
    assert "--dev needs a source checkout" in capsys.readouterr().err


# --- mode switching ---


def test_switching_to_dev_stops_brew_services_and_drops_the_stale_venv(
    monkeypatch, checkout, tmp_path
):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    venv = tmp_path / "opt" / "nyxgpt-api" / "venv"
    venv.mkdir(parents=True)
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    _record_identity(monkeypatch, install_mode.INSTALL_MODE_ARTIFACT, "brew", "nyxgpt-api")
    # Both halves of the union, in one test, because this is the machine that
    # needs both. The marker records the plain names -- what the last install
    # through `nyxgpt ops` targeted -- while the machine also carries the
    # versioned services a candidate-channel install registers (#3853), which
    # no marker here describes. The dev switch has to stop *all four*: each
    # holds :8000/:3000 against the dev LaunchAgents and `keep_alive true`
    # brings it straight back. The marker alone would miss the `@3.0.0rc`
    # pair; discovery alone would miss `nyxgpt-web`, which brew reports as
    # nothing here at all. `nyxgpt-api` is `stopped` rather than `started`, so
    # its plist is what makes it a registration (#3861).
    _register_brew_plist(tmp_path, "nyxgpt-api")
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {
            "nyxgpt-api": "stopped",
            "nyxgpt-api@3.0.0rc": "started",
            "nyxgpt-web@3.0.0rc": "started",
        },
    )

    stopped: list[str] = []
    monkeypatch.setattr(
        ops,
        "_stop_brew_service",
        lambda name: stopped.append(name) or [ops.OpsResult(True, f"stopped {name}")],
    )

    results = ops._reconcile_install_mode(dev=True)

    assert all(r.ok for r in results), [r.message for r in results]
    assert stopped == [
        "nyxgpt-api",
        "nyxgpt-web",
        "nyxgpt-api@3.0.0rc",
        "nyxgpt-web@3.0.0rc",
    ]
    assert not venv.exists()
    assert install_mode.read_install_mode().is_dev is True


def test_switching_back_to_artifact_removes_the_dev_launchagents(monkeypatch, checkout, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    _record_identity(monkeypatch, install_mode.INSTALL_MODE_DEV, "launchd", "com.nyxgpt.api")

    la_dir = tmp_path / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    for label in install_mode.DEV_LAUNCHD_LABELS.values():
        (la_dir / f"{label}.plist").write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ops, "_stop_launchagent", lambda label: [ops.OpsResult(True, label)])

    results = ops._reconcile_install_mode(dev=False)

    assert all(r.ok for r in results), [r.message for r in results]
    assert list(la_dir.iterdir()) == []
    assert install_mode.read_install_mode().is_dev is False


def _register_brew_plist(home, service):
    """Put the LaunchAgent plist brew writes for `service` under `home`.

    The plist *is* the registration (#3861): `brew services list`'s Status
    column reports the last outcome and outlives the file, so a test about a
    service launchd will start again has to put the file there rather than
    only name a state.
    """
    la_dir = home / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True, exist_ok=True)
    plist = la_dir / f"homebrew.mxcl.{service}.plist"
    plist.write_text("<plist/>", encoding="utf-8")
    return plist


def _record_identity(monkeypatch, mode, manager, api_service):
    """Write a marker recording a *known* previous identity.

    Without one the marker reads back as the unknown identity, which #3861
    makes a deliberate mismatch -- so a test about a specific transition has
    to say what the machine was, or it is testing the unknown-previous path
    instead.
    """
    web_service = api_service.replace("api", "web")
    identity = install_mode.InstallIdentity.build(
        mode=mode,
        manager=manager,
        services={"api": api_service, "web": web_service},
        version="0.0.0",
        channel=install_mode.CHANNEL_DEV if mode == install_mode.INSTALL_MODE_DEV else "stable",
    )
    install_mode.write_install_mode(mode, None, identity=identity)
    return identity


def test_reconcile_retires_nothing_when_the_identity_and_the_machine_both_match(
    monkeypatch, checkout, tmp_path
):
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    venv = tmp_path / "opt" / "nyxgpt-api" / "venv"
    venv.mkdir(parents=True)
    # Record exactly the identity this run is about to install, which is what
    # a re-run of the same `nyxgpt up` on an already-installed machine leaves.
    identity = ops._native_install_identity(dev=False)
    install_mode.write_install_mode(install_mode.INSTALL_MODE_ARTIFACT, None, identity=identity)

    with (
        patch.object(ops, "_stop_brew_service") as stop,
        patch.object(ops, "_brew_services_snapshot", return_value={}),
    ):
        results = ops._reconcile_install_mode(dev=False)

    stop.assert_not_called()
    # An unchanged identity must not bounce services or rebuild a healthy venv.
    assert venv.exists()
    assert [r.message for r in results if "changing" in r.message] == []


def test_reconcile_retires_a_foreign_service_even_when_the_identity_is_unchanged(
    monkeypatch, checkout, tmp_path
):
    """A matching marker is not evidence that the machine matches it (#3861 review).

    The marker records what the last install *targeted*, not what is
    registered now: a retire that failed, a keg's service started by hand, or
    an install made outside `nyxgpt ops` all leave a foreign service beside a
    marker that already names the target. Gating the subtraction on a changed
    identity made `doctor`'s own remedy -- "re-run `nyxgpt up` ... to retire
    the ones that are not this install's" -- a no-op in every state doctor
    can fire in.
    """
    monkeypatch.setattr(ops, "REPO_ROOT", checkout)
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    identity = ops._native_install_identity(dev=False)
    install_mode.write_install_mode(install_mode.INSTALL_MODE_ARTIFACT, None, identity=identity)

    stopped: list[str] = []
    monkeypatch.setattr(
        ops,
        "_stop_brew_service",
        lambda name: stopped.append(name) or [ops.OpsResult(True, f"stopped {name}")],
    )
    # An older channel's keg, registered and crash-looping, under a marker
    # that says the current build is the one installed. `error` is not by
    # itself a registration -- a column-based read reported services launchd
    # had already forgotten (#3861, runs 32222041921 and 32228088507) -- so
    # the plist that makes it one is on disk, exactly as it is on a machine
    # launchd keeps restarting.
    _register_brew_plist(tmp_path, "nyxgpt-api@2.1.0")
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api@2.1.0": "error", **dict.fromkeys(identity.service_names, "started")},
    )

    results = ops._reconcile_install_mode(dev=False)

    assert stopped == ["nyxgpt-api@2.1.0"]
    # Reported as no identity *change* -- because there was none -- while the
    # foreign service is still retired.
    assert [r.message for r in results if "changing" in r.message] == []


# --- service manager dispatch ---


def test_macos_dev_mode_restarts_the_launchagent_not_the_brew_service(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")
    with (
        patch.object(ops, "_restart_launchagent", return_value=[]) as agent,
        patch.object(ops, "_restart_brew_service", return_value=[]) as brew,
    ):
        ops._restart_native_service("api")
    agent.assert_called_once_with("com.nyxgpt.api")
    brew.assert_not_called()


def test_macos_dev_mode_leaves_ollama_on_brew(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")
    with patch.object(ops, "_restart_brew_service", return_value=[]) as brew:
        ops._restart_native_service("ollama")
    brew.assert_called_once_with("ollama")


def test_macos_artifact_mode_still_restarts_the_brew_service(monkeypatch):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    with (
        patch.object(ops, "_restart_brew_service", return_value=[]) as brew,
        patch.object(ops, "_restart_launchagent", return_value=[]) as agent,
    ):
        ops._restart_native_service("api")
    brew.assert_called_once_with("nyxgpt-api")
    agent.assert_not_called()


def test_linux_dispatch_is_unaffected_by_dev_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")
    with patch.object(ops, "_stop_systemd_service", return_value=[]) as unit:
        ops._stop_native_service("web")
    unit.assert_called_once_with("nyxgpt-web")


def test_native_snapshot_reads_launchd_for_dev_mode_api_and_web(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {"ollama": "started"})
    monkeypatch.setattr(ops, "_launchd_agent_loaded", lambda label: label == "com.nyxgpt.api")
    snapshot = ops._native_services_snapshot()
    assert snapshot == {"api": "started", "web": "none", "ollama": "started"}


def test_self_heal_restarts_the_dev_launchagent(monkeypatch, tmp_path):
    monkeypatch.setattr(self_heal.platform, "system", lambda: "Darwin")
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")
    with (
        patch.object(
            self_heal, "_restart_launchagent", return_value=self_heal.HealResult(True, "ok")
        ) as agent,
        patch.object(self_heal, "_restart_brew_service") as brew,
    ):
        assert self_heal.restart_native_component("api").ok is True
    agent.assert_called_once_with("com.nyxgpt.api")
    brew.assert_not_called()


# --- status / doctor labelling ---


def test_status_labels_dev_mode_and_its_checkout(monkeypatch, capsys, checkout):
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, checkout)
    monkeypatch.setattr(ops, "_which", lambda tool: None)
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "started", "web": "started", "ollama": "started"},
            compose={},
            terraform={},
            conflicts=[],
            terraform_conflicts=[],
        ),
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    assert ops.status(Args()) == 0
    out = capsys.readouterr().out
    # Attributed to the native api/web (#3834), with a sibling line per
    # Terraform/Kubernetes deployment (#3835) -- never one unqualified line.
    assert f"Install mode (native api/web): dev (editable checkout at {checkout})" in out
    assert "native  api: started  [dev]" in out
    assert "native  web: started  [dev]" in out


def test_status_labels_artifact_mode_by_default(monkeypatch, capsys):
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_which", lambda tool: None)
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "started"},
            compose={},
            terraform={},
            conflicts=[],
            terraform_conflicts=[],
        ),
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    assert ops.status(Args()) == 0
    out = capsys.readouterr().out
    assert "Install mode (native api/web): artifact" in out
    assert "native  api: started  [artifact]" in out


def _render_status(monkeypatch, capsys, *, dev_checkout=None):
    """`ops status` output for a machine whose only deployment is native."""
    if dev_checkout is not None:
        install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, dev_checkout)
    else:
        install_mode.INSTALL_MODE_FILE.unlink(missing_ok=True)
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops, "_which", lambda tool: None)
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "started", "web": "started", "ollama": "started"},
            compose={},
            terraform={},
            conflicts=[],
            terraform_conflicts=[],
        ),
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})
    assert ops.status(Args()) == 0
    return capsys.readouterr().out


def _smoke_status_greps():
    """Every install-mode pattern the systemd smoke greps `ops status` for.

    Matched on the pattern rather than on the pipeline, because the script
    reads the output both ways: piped straight into `grep -q`, and captured
    into `dev_status_out` first so a failure can print it.
    """
    script = Path(__file__).resolve().parents[2] / "scripts" / "systemd-native-smoke.sh"
    return re.findall(r'grep -q "(Install mode[^"]+)"', script.read_text(encoding="utf-8"))


def _greps(pattern, text):
    """Run the real `grep -q` -- the smoke script's own matcher, not `re`."""
    return (
        subprocess.run(  # noqa: S603,S607 - fixed argv, test-only
            ["grep", "-q", pattern], input=text, text=True, check=False
        ).returncode
        == 0
    )


def test_the_systemd_smoke_scripts_status_greps_match_what_status_prints(
    monkeypatch, capsys, checkout
):
    """The smoke script's assertions are a contract on `ops status`'s text.

    `scripts/systemd-native-smoke.sh` is the executed evidence for dev mode,
    and it reads the mode out of `ops status` with `grep`. Two issues in a row
    changed that text -- #3834 attributed the line to the native api/web,
    #3835 added a sibling line per Terraform deployment -- and each time the
    script's greps could stop matching a correctly-behaving stack, which is
    how `linux-native-dev-smoke` failed on this branch. This pins the two
    together in a unit test, so the next format change fails here in seconds
    rather than in a Linux smoke job -- and fails whichever side moves.
    """
    dev_out = _render_status(monkeypatch, capsys, dev_checkout=checkout)
    artifact_out = _render_status(monkeypatch, capsys)

    patterns = [p.replace("$CHECKOUT", str(checkout)) for p in _smoke_status_greps()]
    assert patterns, "the smoke script no longer greps `ops status` -- is dev mode still proven?"

    for pattern in patterns:
        assert _greps(pattern, dev_out) or _greps(
            pattern, artifact_out
        ), f"smoke script greps for {pattern!r}, which `ops status` never prints"

    dev_patterns = [p for p in patterns if "dev (" in p]
    artifact_patterns = [p for p in patterns if "artifact" in p]
    assert dev_patterns and artifact_patterns

    # Non-vacuity: each pattern must reject the other mode's output, or the
    # smoke's "switched back to the artifact path" check proves nothing.
    for pattern in dev_patterns:
        assert _greps(pattern, dev_out)
        assert not _greps(pattern, artifact_out)
    for pattern in artifact_patterns:
        assert _greps(pattern, artifact_out)
        assert not _greps(pattern, dev_out)


def test_doctor_flags_a_dev_install_whose_checkout_is_gone(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "deleted-checkout")
    monkeypatch.setattr(ops, "_which", lambda tool: None)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "installed-package")
    monkeypatch.setattr(ops, "_stale_venv_doctor_issues", lambda: [])
    rc = ops.doctor(Args())
    out = capsys.readouterr().out
    assert rc == 2
    assert "Install mode (native api/web): dev" in out
    assert "its checkout is missing" in out


# --- infra_status (the admin dashboard's Infrastructure page) ---


def _infra_status_stubs(monkeypatch):
    """Stub out infra_status's probes so only the install-mode field varies."""
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={"api": "started"}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(ops.self_heal, "compose_probe_available", lambda: True)


def test_infra_status_reports_artifact_mode_by_default(monkeypatch):
    _infra_status_stubs(monkeypatch)
    reported = ops.infra_status()["install_mode"]
    assert reported["mode"] == install_mode.INSTALL_MODE_ARTIFACT
    assert reported["checkout"] is None
    assert reported["components"] == ["api", "web"]


def test_infra_status_reports_dev_mode_and_its_checkout(monkeypatch, checkout):
    _infra_status_stubs(monkeypatch)
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, checkout)
    reported = ops.infra_status()["install_mode"]
    assert reported["mode"] == install_mode.INSTALL_MODE_DEV
    assert reported["checkout"] == str(checkout)
    assert str(checkout) in reported["label"]


# --- test-isolation guard: the suite must never reconcile the real machine ---


# Every step `ops.install()` runs, by the name the step list dispatches
# through. Stubbing all of them leaves `install()` itself -- and, in the
# fault-injection half below, the real `_reconcile_install_mode` -- as the
# only live code.
_INSTALL_STEPS = (
    "_sync_packaged_resources",
    "_clear_intentional_stops",
    "_install_config",
    "_report_orphaned_launchd_jobs",
    "_stop_superseded_brew_services",
    "_ensure_docker_engine",
    "migrate_legacy_volumes",
    "_reconcile_phantom_compose_app_containers",
    "_ensure_web_deps",
    "_ensure_mcp_deps",
    "_ensure_cassandra_container",
    "_install_cassandra_log_follower_service",
    "_install_ollama_log_follower_service",
    "_install_ollama_env_agent",
    "_install_native_api",
    "_install_native_web",
    "_ensure_native_ollama_service",
    "_ensure_required_models",
    "_cleanup_stale_log_symlinks",
    "sync_env_from_config",
    "_generate_compose_config",
)


def _dev_machine(monkeypatch, tmp_path):
    """Stage a tmp_path copy of what a `nyxgpt up --dev` machine looks like.

    Everything `_reconcile_install_mode` can reach on a mode switch is
    redirected into `tmp_path` first, so the fault-injection test below
    demonstrates the damage without doing it to the runner: the marker (via
    the conftest fixture and this file's own), the api venv, the LaunchAgent
    directory, and `launchctl` itself.
    """
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ops, "_stop_brew_service", lambda name: [ops.OpsResult(True, name)])
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_emit_results", lambda action, results: True)
    monkeypatch.setattr(ops, "_ops_action_outcome", lambda results: ("success", ""))
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    for step in _INSTALL_STEPS:
        monkeypatch.setattr(ops, step, lambda *a, **k: [ops.OpsResult(True, "ok")])

    booted_out: list[str] = []
    monkeypatch.setattr(
        ops,
        "_stop_launchagent",
        lambda label: booted_out.append(label) or [ops.OpsResult(True, "")],
    )

    venv = tmp_path / "opt" / "nyxgpt-api" / "venv"
    venv.mkdir(parents=True)
    la_dir = tmp_path / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    for label in install_mode.DEV_LAUNCHD_LABELS.values():
        (la_dir / f"{label}.plist").write_text("<plist/>", encoding="utf-8")
    install_mode.write_install_mode(
        install_mode.INSTALL_MODE_DEV,
        tmp_path / "checkout",
        identity=install_mode.InstallIdentity.build(
            mode=install_mode.INSTALL_MODE_DEV,
            manager=install_mode.MANAGER_LAUNCHD,
            services=dict(install_mode.DEV_LAUNCHD_LABELS),
            version="0.0.0",
            channel=install_mode.CHANNEL_DEV,
            checkout=tmp_path / "checkout",
        ),
    )

    return venv, la_dir, booted_out


def test_install_tests_patch_the_mode_step_and_so_leave_the_machine_alone(monkeypatch, tmp_path):
    """The convention every `ops.install(...)` unit test follows must hold.

    `install()`'s tests patch each step out; the install-mode step is patched
    the same way (`tests/unit/test_ops.py`, `test_ops_lifecycle_actions.py`,
    `test_ops_systemd.py`). With it patched, running the suite on a dev-mode
    machine -- the one `nyxgpt up --dev` produces, i.e. the owner's Mac --
    leaves its recorded mode, its api venv and its running LaunchAgents
    exactly as they were.
    """
    venv, la_dir, booted_out = _dev_machine(monkeypatch, tmp_path)
    before = install_mode.INSTALL_MODE_FILE.read_bytes()

    with patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]):
        rc = ops.install(
            Args(dev=False, skip_observability=True, terraform=False, kubernetes=False)
        )

    assert rc == 0
    assert install_mode.INSTALL_MODE_FILE.read_bytes() == before
    assert install_mode.read_install_mode().is_dev is True
    assert venv.exists()
    assert sorted(p.name for p in la_dir.iterdir()) == [
        "com.nyxgpt.api.plist",
        "com.nyxgpt.web.plist",
    ]
    assert booted_out == []


def test_an_unpatched_mode_step_really_would_clobber_that_machine(monkeypatch, tmp_path):
    """Fault injection: prove the guard above is not vacuously true (#3753).

    Same install, same staged dev-mode machine, only the install-mode step
    left unpatched -- and the mode switch it then decides on is destructive:
    the marker is rewritten to artifact, the shared api venv is deleted and
    the dev LaunchAgents are booted out from under the running services.
    That is the damage the patch in the test above prevents; if this test
    ever stops failing-by-default it means the step became harmless and the
    convention can be retired.
    """
    venv, la_dir, booted_out = _dev_machine(monkeypatch, tmp_path)

    rc = ops.install(Args(dev=False, skip_observability=True, terraform=False, kubernetes=False))

    assert rc == 0
    assert install_mode.read_install_mode().is_dev is False
    assert not venv.exists()
    assert list(la_dir.iterdir()) == []
    assert booted_out == list(install_mode.DEV_LAUNCHD_LABELS.values())
