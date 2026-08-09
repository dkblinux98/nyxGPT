"""Unit tests for the Linux native (systemd --user) install path (#3508).

Mirrors the conventions test_ops.py already uses for the macOS (Homebrew
services + launchd) path -- mocked `_run`/`_which`/`subprocess.run`, no real
systemctl/npm/pip invoked. Every test here pins `platform.system()` to
"Linux" explicitly (the suite may run on either OS), the same way test_ops.py
pins its own tests to "Darwin".
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import ops

pytestmark = pytest.mark.unit


class SimpleNamespaceLike:
    """Minimal CLI-args stand-in (attribute bag, like argparse.Namespace)."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


@pytest.fixture(autouse=True)
def _force_linux_native_path(monkeypatch):
    """Pin `platform.system()` to "Linux" for every test in this file."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def _no_terraform_or_kubernetes_managed_components(monkeypatch):
    """Default `ops._terraform_or_kubernetes_managed_components()` to empty.

    Mirrors test_ops.py's fixture of the same name/purpose: `stop()` calls it
    (via `self_heal.list_component_status()`, which shells out to
    docker/kubectl) to decide which components to mark intentionally
    stopped -- default to a fast, deterministic empty set instead of hitting
    whatever docker/kubectl happen to be on the test host.
    """
    monkeypatch.setattr(ops, "_terraform_or_kubernetes_managed_components", lambda: set())


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


# --- _is_macos / _is_linux / _unsupported_os_result ---


@pytest.mark.parametrize(
    "system_value,is_macos,is_linux",
    [("Darwin", True, False), ("Linux", False, True), ("Windows", False, False)],
)
def test_os_dispatch_helpers(monkeypatch, system_value, is_macos, is_linux):
    monkeypatch.setattr(ops.platform, "system", lambda: system_value)
    assert ops._is_macos() is is_macos
    assert ops._is_linux() is is_linux


def test_unsupported_os_result_reports_platform(monkeypatch):
    monkeypatch.setattr(ops.platform, "system", lambda: "Windows")
    results = ops._unsupported_os_result("do a thing")
    assert len(results) == 1
    assert results[0].ok is False
    assert "do a thing" in results[0].message
    assert "Windows" in results[0].message


# --- _find_systemd_unit_template ---


def test_find_systemd_unit_template_returns_the_synced_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "OPS_SYSTEMD_TEMPLATES_DIR", tmp_path / ".nyxGPT" / "ops" / "systemd")
    target = ops.OPS_SYSTEMD_TEMPLATES_DIR / "nyxgpt-api.service"
    target.parent.mkdir(parents=True)
    target.write_text("[Service]\n", encoding="utf-8")

    tpl, candidates = ops._find_systemd_unit_template("nyxgpt-api.service")
    assert tpl == target
    assert candidates == [target]


def test_find_systemd_unit_template_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "OPS_SYSTEMD_TEMPLATES_DIR", tmp_path / "nowhere")
    tpl, candidates = ops._find_systemd_unit_template("nyxgpt-api.service")
    assert tpl is None
    assert len(candidates) == 1


# --- _install_systemd_unit_from_template ---


def test_install_systemd_unit_from_template_substitutes_home_and_extras(monkeypatch, tmp_path):
    tpl = tmp_path / "nyxgpt-ollama.service"
    tpl.write_text(
        "Environment=OLLAMA_MODELS=__NYXGPT_HOME__/.nyxGPT/volumes/ollama/models\n"
        "ExecStart=__NYXGPT_OLLAMA_BIN__ serve\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    dst = tmp_path / "dst" / "nyxgpt-ollama.service"

    ops._install_systemd_unit_from_template(
        tpl, dst, substitutions={"__NYXGPT_OLLAMA_BIN__": "/usr/bin/ollama"}
    )

    text = dst.read_text(encoding="utf-8")
    assert "__NYXGPT_HOME__" not in text
    assert "__NYXGPT_OLLAMA_BIN__" not in text
    assert f"{home}/.nyxGPT/volumes/ollama/models" in text
    assert "ExecStart=/usr/bin/ollama serve" in text


# --- _reload_and_activate_systemd_unit ---


def test_reload_and_activate_systemd_unit_success(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp(0)

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._reload_and_activate_systemd_unit("nyxgpt-api.service")
    assert all(r.ok for r in results)
    assert calls[0] == ["systemctl", "--user", "daemon-reload"]
    assert calls[1] == ["systemctl", "--user", "enable", "nyxgpt-api.service"]
    assert calls[2] == ["systemctl", "--user", "restart", "nyxgpt-api.service"]


def test_reload_and_activate_systemd_unit_daemon_reload_failure_short_circuits(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["systemctl", "--user", "daemon-reload"]:
            return _cp(1, stderr="boom")
        return _cp(0)

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._reload_and_activate_systemd_unit("nyxgpt-api.service")
    assert len(results) == 1
    assert results[0].ok is False
    assert "daemon-reload failed" in results[0].message
    # enable/restart never attempted after a failed reload.
    assert len(calls) == 1


def test_reload_and_activate_systemd_unit_restart_failure_reported(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "restart"]:
            return _cp(1, stderr="unit failed")
        return _cp(0)

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._reload_and_activate_systemd_unit("nyxgpt-api.service")
    assert results[-1].ok is False
    assert "Failed to start systemd unit" in results[-1].message


# --- _install_cassandra_logs_systemd_unit / _install_ollama_logs_systemd_unit ---


def test_install_cassandra_logs_systemd_unit_missing_template(monkeypatch):
    monkeypatch.setattr(
        ops, "_find_systemd_unit_template", lambda name: (None, [Path("/a"), Path("/b")])
    )
    results = ops._install_cassandra_logs_systemd_unit()
    assert results[0].ok is False
    assert "Missing Cassandra logs systemd unit template" in results[0].message


def test_install_cassandra_logs_systemd_unit_installs_when_template_found(monkeypatch, tmp_path):
    tpl = tmp_path / "nyxgpt-cassandra-logs.service"
    tpl.write_text("ExecStart=__NYXGPT_HOME__/.nyxGPT/scripts/follow-cassandra-logs.sh\n")
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0))

    results = ops._install_cassandra_logs_systemd_unit()
    assert all(r.ok for r in results)
    dst = home / ".config" / "systemd" / "user" / tpl.name
    assert dst.exists()
    assert "__NYXGPT_HOME__" not in dst.read_text(encoding="utf-8")


def test_install_ollama_logs_systemd_unit_missing_template(monkeypatch):
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (None, [Path("/a")]))
    results = ops._install_ollama_logs_systemd_unit()
    assert results[0].ok is False
    assert "Missing Ollama logs systemd unit template" in results[0].message


# --- _systemd_services_snapshot / _native_services_snapshot ---


def test_systemd_services_snapshot_no_systemctl(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    assert ops._systemd_services_snapshot() == {}


def test_systemd_services_snapshot_only_reports_installed_units(monkeypatch, tmp_path):
    """A unit whose file was never installed must be absent from the snapshot
    (not merely "none") -- otherwise a fresh machine that's never run
    `nyxgpt ops install` would misreport every native component as "down"
    rather than simply absent (mirrors `_brew_services_snapshot`'s contract).
    """
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/systemctl")
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "nyxgpt-api.service").write_text("[Service]\n", encoding="utf-8")
    (unit_dir / "nyxgpt-web.service").write_text("[Service]\n", encoding="utf-8")
    # nyxgpt-ollama.service intentionally not installed.

    def fake_run(cmd, **kwargs):
        unit = cmd[-1]
        if unit == "nyxgpt-api.service":
            return _cp(0, stdout="active\n")
        return _cp(3, stdout="inactive\n")

    monkeypatch.setattr(ops, "_run", fake_run)
    snapshot = ops._systemd_services_snapshot()
    assert snapshot["nyxgpt-api"] == "started"
    assert snapshot["nyxgpt-web"] == "none"
    assert "nyxgpt-ollama" not in snapshot


def test_native_services_snapshot_dispatches_by_os(monkeypatch):
    monkeypatch.setattr(ops, "_systemd_services_snapshot", lambda: {"nyxgpt-api": "started"})
    snapshot = ops._native_services_snapshot()
    assert snapshot == {"api": "started", "web": "none", "ollama": "none"}


def test_native_services_snapshot_unsupported_os_returns_none_for_all(monkeypatch):
    monkeypatch.setattr(ops.platform, "system", lambda: "Windows")
    snapshot = ops._native_services_snapshot()
    assert snapshot == {"api": "none", "web": "none", "ollama": "none"}


# --- _restart_systemd_service / _stop_systemd_service ---


def test_restart_systemd_service_no_systemctl(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._restart_systemd_service("nyxgpt-api")
    assert results[0].ok is False
    assert "systemctl not found" in results[0].message


def test_restart_systemd_service_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0))
    results = ops._restart_systemd_service("nyxgpt-api")
    assert results[0].ok is True
    assert "Restarted systemd unit: nyxgpt-api" in results[0].message


def test_restart_systemd_service_failure(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(1, stderr="nope"))
    results = ops._restart_systemd_service("nyxgpt-api")
    assert results[0].ok is False


def test_stop_systemd_service_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0))
    results = ops._stop_systemd_service("nyxgpt-api")
    assert results[0].ok is True
    assert "Stopped systemd unit: nyxgpt-api" in results[0].message


# --- _install_native_ollama_systemd ---


def test_install_native_ollama_systemd_missing_binary(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._install_native_ollama_systemd()
    assert results[0].ok is False
    assert "ollama not found on PATH" in results[0].message


def test_install_native_ollama_systemd_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/ollama" if name == "ollama" else None)
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_migrate_native_ollama_models", lambda models_dir: [])
    tpl = tmp_path / "nyxgpt-ollama.service"
    tpl.write_text("ExecStart=__NYXGPT_OLLAMA_BIN__ serve\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0))

    results = ops._install_native_ollama_systemd()
    assert all(r.ok for r in results)
    dst = home / ".config" / "systemd" / "user" / "nyxgpt-ollama.service"
    assert "ExecStart=/usr/bin/ollama serve" in dst.read_text(encoding="utf-8")


# --- _install_native_api_systemd ---


def _make_fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src" / "nyxgpt").mkdir(parents=True)
    (repo / "src" / "nyxgpt" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "nyxGPT"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    (repo / "example.config.ini").write_text("[api]\nhost = 127.0.0.1\n", encoding="utf-8")
    return repo


def test_install_native_api_systemd_venv_creation_failure(monkeypatch, tmp_path):
    repo = _make_fake_repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(1, stderr="no venv module"))

    results = ops._install_native_api_systemd()
    assert results[-1].ok is False
    assert "Failed to create nyxgpt-api venv" in results[-1].message


def test_install_native_api_systemd_happy_path(monkeypatch, tmp_path):
    repo = _make_fake_repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0))
    tpl = tmp_path / "nyxgpt-api.service"
    tpl.write_text("ExecStart=__NYXGPT_HOME__/.nyxGPT/opt/nyxgpt-api/bin/nyxgpt-api\n")
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))

    results = ops._install_native_api_systemd()
    assert all(r.ok for r in results), results

    wrapper = home / ".nyxGPT" / "opt" / "nyxgpt-api" / "bin" / "nyxgpt-api"
    assert wrapper.exists()
    content = wrapper.read_text(encoding="utf-8")
    assert "uvicorn nyxgpt.app:app" in content
    assert str(home / ".nyxGPT" / "opt" / "nyxgpt-api" / "venv") in content

    dst = home / ".config" / "systemd" / "user" / "nyxgpt-api.service"
    assert dst.exists()
    assert "__NYXGPT_HOME__" not in dst.read_text(encoding="utf-8")


def test_install_native_api_systemd_missing_unit_template(monkeypatch, tmp_path):
    repo = _make_fake_repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0))
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (None, [Path("/a")]))

    results = ops._install_native_api_systemd()
    assert results[-1].ok is False
    assert "Missing nyxgpt-api systemd unit template" in results[-1].message


# --- _install_native_web_systemd ---


def _make_fake_web_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text("{}", encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "nyxGPT"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    return repo


def test_install_native_web_systemd_missing_npm(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._install_native_web_systemd()
    assert len(results) == 1
    assert results[0].ok is False
    assert "npm not found" in results[0].message


def test_install_native_web_systemd_happy_path(monkeypatch, tmp_path):
    repo = _make_fake_web_repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    monkeypatch.setattr(ops.subprocess, "run", lambda cmd, **k: _cp(0, stdout="", stderr=""))
    tpl = tmp_path / "nyxgpt-web.service"
    tpl.write_text("ExecStart=__NYXGPT_HOME__/.nyxGPT/opt/nyxgpt-web/bin/nyxgpt-web\n")
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))

    results = ops._install_native_web_systemd()
    assert all(r.ok for r in results), results

    wrapper = home / ".nyxGPT" / "opt" / "nyxgpt-web" / "bin" / "nyxgpt-web"
    assert wrapper.exists()
    content = wrapper.read_text(encoding="utf-8")
    assert "npm run start" in content

    dst = home / ".config" / "systemd" / "user" / "nyxgpt-web.service"
    assert dst.exists()


def test_install_native_web_systemd_npm_ci_failure_stops_early(monkeypatch, tmp_path):
    repo = _make_fake_web_repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    monkeypatch.setattr(ops.subprocess, "run", lambda cmd, **k: _cp(1, stderr="ci failed"))

    results = ops._install_native_web_systemd()
    assert results[-1].ok is False
    assert "npm ci failed" in results[-1].message


def test_install_native_web_systemd_failed_rebuild_preserves_previous_build(monkeypatch, tmp_path):
    """A failed rebuild must not destroy the previously-installed, still-running
    build (#3508 review) -- the live `nyxgpt-web.service` wrapper keeps `cd`ing
    into a build directory that must still exist after a failed `npm ci`/
    `npm run build`, not one an in-place `rmtree` deleted before the rebuild
    was known to succeed.
    """
    repo = _make_fake_web_repo(tmp_path)
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    monkeypatch.setattr(ops.subprocess, "run", lambda cmd, **k: _cp(0, stdout="", stderr=""))
    tpl = tmp_path / "nyxgpt-web.service"
    tpl.write_text("ExecStart=__NYXGPT_HOME__/.nyxGPT/opt/nyxgpt-web/bin/nyxgpt-web\n")
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))

    # First install succeeds and leaves a real, "currently running" build.
    results = ops._install_native_web_systemd()
    assert all(r.ok for r in results), results
    build_dir = home / ".nyxGPT" / "opt" / "nyxgpt-web" / "build"
    live_extracted = build_dir / "nyxgpt-web-9.9.9"
    assert live_extracted.is_dir()
    marker = live_extracted / "MARKER"
    marker.write_text("still the live build", encoding="utf-8")

    # Second install's rebuild fails partway through (npm run build).
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda cmd, **k: _cp(0) if cmd[1:2] == ["ci"] else _cp(1, stderr="build failed"),
    )
    results = ops._install_native_web_systemd()
    assert results[-1].ok is False
    assert "npm run build failed" in results[-1].message

    # The previous, still-running build must survive the failed rebuild.
    assert live_extracted.is_dir()
    assert marker.read_text(encoding="utf-8") == "still the live build"


# --- Top-level dispatcher wrappers ---


@pytest.mark.parametrize(
    "dispatcher,linux_target",
    [
        ("_install_native_api", "_install_native_api_systemd"),
        ("_install_native_web", "_install_native_web_systemd"),
        ("_ensure_native_ollama_service", "_install_native_ollama_systemd"),
        ("_install_cassandra_log_follower_service", "_install_cassandra_logs_systemd_unit"),
        ("_install_ollama_log_follower_service", "_install_ollama_logs_systemd_unit"),
    ],
)
def test_dispatcher_calls_systemd_impl_on_linux(monkeypatch, dispatcher, linux_target):
    sentinel = [ops.OpsResult(True, "called")]
    with patch.object(ops, linux_target, return_value=sentinel) as mock_impl:
        result = getattr(ops, dispatcher)()
    mock_impl.assert_called_once()
    assert result == sentinel


@pytest.mark.parametrize(
    "dispatcher",
    [
        "_install_native_api",
        "_install_native_web",
        "_ensure_native_ollama_service",
        "_install_cassandra_log_follower_service",
        "_install_ollama_log_follower_service",
    ],
)
def test_dispatcher_reports_unsupported_os(monkeypatch, dispatcher):
    monkeypatch.setattr(ops.platform, "system", lambda: "Windows")
    results = getattr(ops, dispatcher)()
    assert results[0].ok is False
    assert "unsupported OS" in results[0].message


def test_install_ollama_env_agent_is_a_noop_on_linux():
    results = ops._install_ollama_env_agent()
    assert len(results) == 1
    assert results[0].ok is True
    assert "not needed on Linux" in results[0].message


def test_restart_native_service_dispatches_to_systemd(monkeypatch):
    with patch.object(
        ops, "_restart_systemd_service", return_value=[ops.OpsResult(True, "ok")]
    ) as m:
        ops._restart_native_service("api")
    m.assert_called_once_with("nyxgpt-api")


def test_stop_native_service_dispatches_to_systemd(monkeypatch):
    with patch.object(ops, "_stop_systemd_service", return_value=[ops.OpsResult(True, "ok")]) as m:
        ops._stop_native_service("web")
    m.assert_called_once_with("nyxgpt-web")


def test_restart_native_log_follower_dispatches_to_systemd(monkeypatch):
    with patch.object(
        ops, "_restart_systemd_service", return_value=[ops.OpsResult(True, "ok")]
    ) as m:
        ops._restart_native_log_follower("cassandra-logs")
    m.assert_called_once_with("nyxgpt-cassandra-logs")


def test_stop_native_log_follower_dispatches_to_systemd(monkeypatch):
    with patch.object(ops, "_stop_systemd_service", return_value=[ops.OpsResult(True, "ok")]) as m:
        ops._stop_native_log_follower("ollama-logs")
    m.assert_called_once_with("nyxgpt-ollama-logs")


# --- detect_deployment_mode / install / restart / stop / status / doctor wiring ---


def test_detect_deployment_mode_uses_systemd_snapshot_on_linux(monkeypatch):
    monkeypatch.setattr(ops, "_systemd_services_snapshot", lambda: {"nyxgpt-api": "started"})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    mode = ops.detect_deployment_mode()
    assert mode.native["api"] == "started"
    assert mode.native["web"] == "none"


def test_install_uses_systemd_steps_on_linux(monkeypatch, capsys):
    ok_results = [ops.OpsResult(True, "ok")]
    for step in (
        "_sync_packaged_resources",
        "_clear_intentional_stops",
        "_install_config",
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
    ):
        monkeypatch.setattr(ops, step, lambda *a, **k: ok_results)
    monkeypatch.setattr(ops, "_emit_results", lambda action, results: True)
    monkeypatch.setattr(ops, "_ops_action_outcome", lambda results: ("success", ""))
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)

    rc = ops.install(
        SimpleNamespaceLike(skip_observability=True, terraform=False, kubernetes=False)
    )
    assert rc == 0


def test_restart_api_calls_systemd_restart_on_linux(monkeypatch):
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_compose_conflict_result", lambda component, compose: None)
    monkeypatch.setattr(ops.self_heal, "clear_intentionally_stopped", lambda component: None)
    monkeypatch.setattr(ops, "_ops_action_outcome", lambda results: ("success", ""))
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        ops,
        "_restart_systemd_service",
        lambda unit: calls.append(unit) or [ops.OpsResult(True, "ok")],
    )

    rc = ops.restart(SimpleNamespaceLike(target="api"))
    assert rc == 0
    assert calls == ["nyxgpt-api"]


def test_stop_ollama_calls_systemd_stop_on_linux(monkeypatch):
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "none", "web": "none", "ollama": "started", "cassandra": "absent"},
            compose={},
            conflicts=[],
        ),
    )
    monkeypatch.setattr(ops.self_heal, "mark_intentionally_stopped", lambda component: None)
    monkeypatch.setattr(ops, "_ops_action_outcome", lambda results: ("success", ""))
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(
        ops,
        "_stop_systemd_service",
        lambda unit: calls.append(unit) or [ops.OpsResult(True, "ok")],
    )

    rc = ops.stop(SimpleNamespaceLike(target="ollama"))
    assert rc == 0
    assert calls == ["nyxgpt-ollama"]


def test_status_prints_systemd_section_on_linux(monkeypatch, capsys):
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "none", "web": "none", "ollama": "none", "cassandra": "absent"},
            compose={},
            conflicts=[],
        ),
    )
    monkeypatch.setattr(
        ops, "_which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: _cp(0, stdout="nyxgpt-api.service loaded active running\n")
    )
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {})

    rc = ops.status(SimpleNamespaceLike())
    assert rc == 0
    out = capsys.readouterr().out
    assert "systemd --user services" in out
    assert "systemd unit nyxgpt-cassandra-logs.service" in out


def test_doctor_requires_systemctl_not_brew_on_linux(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "repo")
    monkeypatch.setattr(ops, "_which", lambda name: None)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_log_aggregation_wiring_issue", lambda: None)
    monkeypatch.setattr(ops, "_tracing_wiring_issue", lambda: None)
    monkeypatch.setattr(ops, "_tracing_packages_doctor_issue", lambda: None)
    monkeypatch.setattr(ops, "_glitchtip_secrets_doctor_issues", lambda: [])
    monkeypatch.setattr(ops, "_error_tracking_dsn_drift_issue", lambda: None)
    monkeypatch.setattr(ops, "_stale_venv_doctor_issues", lambda: [])
    monkeypatch.setattr(ops, "detect_deployment_mode", lambda: ops.DeploymentMode({}, {}, []))
    ollama_drift_spy = []
    monkeypatch.setattr(
        ops, "_ollama_env_drift_issue", lambda: ollama_drift_spy.append(True) or None
    )

    rc = ops.doctor(SimpleNamespaceLike())
    assert rc == 2
    # ollama env drift is a macOS-only check -- never invoked on Linux.
    assert ollama_drift_spy == []
