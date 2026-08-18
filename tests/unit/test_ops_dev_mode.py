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

import subprocess
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
    venv = tmp_path / "opt" / "nyxgpt-api" / "venv"
    venv.mkdir(parents=True)

    stopped: list[str] = []
    monkeypatch.setattr(
        ops,
        "_stop_brew_service",
        lambda name: stopped.append(name) or [ops.OpsResult(True, f"stopped {name}")],
    )

    results = ops._reconcile_install_mode(dev=True)

    assert all(r.ok for r in results), [r.message for r in results]
    assert stopped == ["nyxgpt-api", "nyxgpt-web"]
    assert not venv.exists()
    assert install_mode.read_install_mode().is_dev is True


def test_switching_back_to_artifact_removes_the_dev_launchagents(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "co")

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


def test_reconcile_is_a_no_op_record_when_the_mode_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops, "_native_install_root", lambda c: tmp_path / "opt" / c)
    venv = tmp_path / "opt" / "nyxgpt-api" / "venv"
    venv.mkdir(parents=True)
    with patch.object(ops, "_stop_brew_service") as stop:
        results = ops._reconcile_install_mode(dev=False)
    stop.assert_not_called()
    # An unchanged mode must not bounce services or rebuild a healthy venv.
    assert venv.exists()
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
    install_mode.write_install_mode(install_mode.INSTALL_MODE_DEV, tmp_path / "checkout")

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
