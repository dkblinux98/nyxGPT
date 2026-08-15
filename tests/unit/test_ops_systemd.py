"""Unit tests for the Linux native (systemd --user) install path (#3508).

Mirrors the conventions test_ops.py already uses for the macOS (Homebrew
services + launchd) path -- mocked `_run`/`_which`/`subprocess.run`, no real
systemctl/npm/pip invoked. Every test here pins `platform.system()` to
"Linux" explicitly (the suite may run on either OS), the same way test_ops.py
pins its own tests to "Darwin".
"""

import logging
import shlex
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


def test_install_native_ollama_systemd_takes_over_system_service(monkeypatch, tmp_path):
    """#3632: a running system-wide ollama.service must be stopped/disabled so
    nyxgpt-ollama.service can take port 11434 over -- not adopted (the
    adoption behaviour shipped in the first round was rejected in acceptance
    testing)."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(
        ops,
        "_which",
        lambda name: {"systemctl": "/usr/bin/systemctl", "ollama": "/usr/bin/ollama"}.get(name),
    )
    monkeypatch.setattr(ops, "_migrate_native_ollama_models", lambda models_dir: [])
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0, stdout="active"))
    privileged = []
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: privileged.append(cmd) or _cp(0))
    monkeypatch.setattr(ops, "_wait_for_port_free", lambda host, port, **k: True)
    tpl = tmp_path / "nyxgpt-ollama.service"
    tpl.write_text("ExecStart=__NYXGPT_OLLAMA_BIN__ serve\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_find_systemd_unit_template", lambda name: (tpl, [tpl]))

    results = ops._install_native_ollama_systemd()

    assert all(r.ok for r in results)
    assert ["systemctl", "disable", "--now", "ollama.service"] in privileged
    assert "Stopped and disabled system-wide ollama.service" in results[0].message
    # ...and the nyxgpt unit is then actually installed on the freed port.
    dst = home / ".config" / "systemd" / "user" / "nyxgpt-ollama.service"
    assert "ExecStart=/usr/bin/ollama serve" in dst.read_text(encoding="utf-8")


def test_install_native_ollama_systemd_refuses_when_port_cannot_be_freed(monkeypatch, tmp_path):
    """#3632: if the system unit can't be disabled, nyxgpt-ollama.service must
    NOT be installed/started -- it could only crash-loop against the taken
    port, which is the original defect."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(
        ops,
        "_which",
        lambda name: {"systemctl": "/usr/bin/systemctl", "ollama": "/usr/bin/ollama"}.get(name),
    )
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0, stdout="active"))
    monkeypatch.setattr(
        ops, "_privileged_run", lambda cmd, **k: _cp(1, stderr="a password is required")
    )
    template_spy = []
    monkeypatch.setattr(
        ops, "_find_systemd_unit_template", lambda name: template_spy.append(name) or (None, [])
    )

    results = ops._install_native_ollama_systemd()

    assert results[0].ok is False
    assert "could not be disabled" in results[0].message
    assert "sudo systemctl disable --now ollama.service" in results[0].details
    assert template_spy == []
    assert not (home / ".config" / "systemd" / "user" / "nyxgpt-ollama.service").exists()


def test_takeover_system_ollama_service_fails_when_port_stays_busy(monkeypatch):
    """`disable --now` returning 0 isn't enough: the socket can linger, and
    starting nyxgpt-ollama into it would crash-loop exactly as before."""
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: _cp(0))
    monkeypatch.setattr(ops, "_wait_for_port_free", lambda host, port, **k: False)

    port_free, results = ops._takeover_system_ollama_service()

    assert port_free is False
    assert results[-1].ok is False
    assert "still in use" in results[-1].message


def test_takeover_system_ollama_service_without_sudo(monkeypatch):
    """No root reachable at all (`_privileged_run` -> None) is reported with the
    exact command an operator can run instead, never a prompt."""
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: None)

    port_free, results = ops._takeover_system_ollama_service()

    assert port_free is False
    assert results[0].ok is False
    assert "sudo systemctl disable --now ollama.service" in results[0].details


# --- _system_ollama_service_enabled / _system_ollama_service_conflicts ---


@pytest.mark.parametrize("stdout", ["enabled\n", "enabled-runtime\n"])
def test_system_ollama_service_enabled_true(monkeypatch, stdout):
    monkeypatch.setattr(
        ops, "_which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    calls = []
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: calls.append(cmd) or _cp(0, stdout=stdout))

    assert ops._system_ollama_service_enabled() is True
    assert calls == [["systemctl", "is-enabled", "ollama.service"]]


@pytest.mark.parametrize("stdout", ["disabled\n", "masked\n", "static\n", ""])
def test_system_ollama_service_enabled_false(monkeypatch, stdout):
    monkeypatch.setattr(
        ops, "_which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(1, stdout=stdout))

    assert ops._system_ollama_service_enabled() is False


def test_system_ollama_service_enabled_false_for_static_unit(monkeypatch):
    """`systemctl disable` on a static unit (no [Install] section) is a no-op, so
    reporting it as a conflict would leave `ops doctor` flagging one forever."""
    monkeypatch.setattr(
        ops, "_which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(0, stdout="static\n"))

    assert ops._system_ollama_service_enabled() is False


def test_system_ollama_service_conflicts_when_only_enabled(monkeypatch):
    """A stopped-but-enabled system unit still conflicts: it grabs the port back at boot."""
    monkeypatch.setattr(ops, "_system_ollama_service_active", lambda: False)
    monkeypatch.setattr(ops, "_system_ollama_service_enabled", lambda: True)

    assert ops._system_ollama_service_conflicts() is True


# --- _system_ollama_service_active ---


def test_system_ollama_service_active_true(monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    calls = []
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: calls.append(cmd) or _cp(0, stdout="active\n")
    )

    assert ops._system_ollama_service_active() is True
    assert calls == [["systemctl", "is-active", "ollama.service"]]


@pytest.mark.parametrize("stdout", ["inactive\n", "unknown\n", "failed\n", ""])
def test_system_ollama_service_active_false_when_not_active(monkeypatch, stdout):
    monkeypatch.setattr(
        ops, "_which", lambda name: "/usr/bin/systemctl" if name == "systemctl" else None
    )
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(3, stdout=stdout))

    assert ops._system_ollama_service_active() is False


def test_system_ollama_service_active_false_without_systemctl(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda name: None)
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: (_ for _ in ()).throw(AssertionError("should not be called"))
    )

    assert ops._system_ollama_service_active() is False


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
        "_reconcile_install_mode",
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


# --- _linux_ollama_port_conflict_issue ---


def test_linux_ollama_port_conflict_issue_none_when_not_linux(monkeypatch):
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    assert ops._linux_ollama_port_conflict_issue() is None


def test_linux_ollama_port_conflict_issue_none_when_no_system_unit(monkeypatch):
    monkeypatch.setattr(ops, "_system_ollama_service_conflicts", lambda: False)
    assert ops._linux_ollama_port_conflict_issue() is None


def test_linux_ollama_port_conflict_issue_flags_active_or_enabled_system_unit(monkeypatch):
    """#3632 round 2: the conflict is reported whenever the system unit is
    active *or* enabled, since an enabled-but-stopped unit takes the port back
    at the next boot."""
    monkeypatch.setattr(ops, "_system_ollama_service_conflicts", lambda: True)

    issue = ops._linux_ollama_port_conflict_issue()
    assert issue is not None
    assert "port 11434" in issue
    assert "nyxgpt ops install" in issue
    assert "sudo systemctl disable --now ollama.service" in issue


def test_doctor_reports_linux_ollama_port_conflict(monkeypatch, tmp_path):
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
    monkeypatch.setattr(
        ops, "_linux_ollama_port_conflict_issue", lambda: "System-wide ollama.service is bound..."
    )

    rc = ops.doctor(SimpleNamespaceLike())
    assert rc == 2


# --- Docker engine bootstrap (#3632) ---


def test_ensure_docker_engine_installs_missing_docker(monkeypatch):
    """#3632: `ops install` on a clean Linux box must install Docker itself
    rather than failing the observability stack with an undocumented
    prerequisite."""
    present = {"apt-get": "/usr/bin/apt-get"}
    monkeypatch.setattr(ops, "_which", lambda name: present.get(name))
    privileged = []

    def _fake_privileged(cmd, **kwargs):
        privileged.append(cmd)
        if cmd[:2] == ["apt-get", "install"]:
            present["docker"] = "/usr/bin/docker"
        return _cp(0)

    monkeypatch.setattr(ops, "_privileged_run", _fake_privileged)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_docker_daemon_reachable", lambda: True)

    results = ops._ensure_docker_engine()

    assert all(r.ok for r in results)
    assert ["apt-get", "update"] in privileged
    assert ["apt-get", "install", "-y", "docker.io"] in privileged
    assert ["systemctl", "enable", "--now", "docker"] in privileged


def test_ensure_docker_engine_adds_user_to_docker_group_when_daemon_unreachable(monkeypatch):
    """The socket is root:docker 0660, so a fresh install leaves docker calls
    denied until the user is in the group -- and until their login session is
    recreated (#3632's `ops status` vs. web UI disagreement)."""
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_running_as_root", lambda: False)
    monkeypatch.setattr(ops, "_docker_daemon_reachable", lambda: False)
    privileged = []
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: privileged.append(cmd) or _cp(0))

    results = ops._ensure_docker_engine()

    usermod = [cmd for cmd in privileged if cmd[0] == "usermod"]
    assert usermod and usermod[0][:3] == ["usermod", "-aG", "docker"]
    messages = " ".join(r.message for r in results)
    details = " ".join(r.details for r in results)
    assert "'docker' group" in messages
    assert "loginctl terminate-user" in details


def test_ensure_docker_engine_reports_actionable_failure_without_root(monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None
    )
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: None)

    results = ops._ensure_docker_engine()

    assert results[0].ok is False
    assert "apt-get install -y docker.io" in results[0].details


def test_ensure_docker_engine_does_not_install_on_macos(monkeypatch):
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(ops, "_which", lambda name: None)
    monkeypatch.setattr(
        ops, "_privileged_run", lambda cmd, **k: pytest.fail(f"must not run privileged: {cmd}")
    )

    results = ops._ensure_docker_engine()

    assert results[0].ok is False
    assert "Docker Desktop" in results[0].details


def test_docker_access_doctor_issue_flags_unreachable_daemon(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(ops, "_docker_daemon_reachable", lambda: False)

    issue = ops._docker_access_doctor_issue()
    assert issue is not None
    assert "loginctl terminate-user" in issue


def test_docker_access_doctor_issue_silent_when_healthy(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(ops, "_docker_daemon_reachable", lambda: True)

    assert ops._docker_access_doctor_issue() is None


def test_docker_access_doctor_issue_silent_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda name: None)
    assert ops._docker_access_doctor_issue() is None


# --- Amazon Linux 2023 Docker provisioning (#3760) ---


@pytest.fixture
def _no_docker_socket_hop(monkeypatch):
    """Keep the process-wide Docker socket hop out of neighbouring tests."""
    monkeypatch.setattr(ops, "_DOCKER_SOCKET_HOP", None)


def test_docker_engine_installs_on_amazon_linux_without_a_compose_package(monkeypatch):
    """Amazon Linux 2023 has `docker` but no compose package at all, so the old
    combined `dnf install docker docker-compose-plugin` transaction failed as a
    unit and reported "Could not install Docker automatically" on a host whose
    engine would have installed fine (#3760)."""
    present = {"dnf": "/usr/bin/dnf"}
    monkeypatch.setattr(ops, "_which", lambda name: present.get(name))
    privileged = []

    def _fake_privileged(cmd, **kwargs):
        privileged.append(cmd)
        if cmd[:2] == ["dnf", "install"] and "docker" in cmd:
            present["docker"] = "/usr/bin/docker"
            return _cp(0)
        if cmd[:2] == ["dnf", "install"]:  # docker-compose-plugin: no such package here
            return _cp(1, stderr="No match for argument: docker-compose-plugin")
        return _cp(0)

    monkeypatch.setattr(ops, "_privileged_run", _fake_privileged)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_docker_daemon_reachable", lambda: True)

    results = ops._ensure_docker_engine()

    assert ["dnf", "install", "-y", "docker"] in privileged
    assert all(r.ok for r in results), [r.message for r in results]


def test_compose_plugin_falls_back_to_the_release_binary(monkeypatch):
    """No distro package -> fetch Docker's own plugin binary system-wide, and
    prove it works before reporting success."""
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ops.platform, "machine", lambda: "aarch64")
    privileged = []
    monkeypatch.setattr(
        ops,
        "_privileged_run",
        lambda cmd, **k: privileged.append(cmd)
        or (_cp(1) if cmd[:2] == ["dnf", "install"] else _cp(0)),
    )
    # Unusable until the binary lands, usable afterwards.
    monkeypatch.setattr(
        ops,
        "_compose_available",
        lambda: any(cmd[0] == "chmod" for cmd in privileged),
    )

    results = ops._install_linux_compose_plugin()

    dest = str(ops.COMPOSE_PLUGIN_DIR / "docker-compose")
    curl = [cmd for cmd in privileged if cmd[0] == "curl"]
    assert curl, privileged
    assert curl[0][-2:] == [
        dest,
        "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64",
    ]
    assert ["mkdir", "-p", str(ops.COMPOSE_PLUGIN_DIR)] in privileged
    assert ["chmod", "0755", dest] in privileged
    assert results[-1].ok is True
    assert dest in results[-1].message


def test_compose_plugin_binary_reports_failure_when_it_still_does_not_work(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ops.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: _cp(0))
    monkeypatch.setattr(ops, "_compose_available", lambda: False)

    results = ops._install_compose_plugin_binary()

    assert results[0].ok is False
    assert "Amazon Linux 2023" in results[0].details


def test_compose_plugin_binary_names_the_missing_root_rather_than_a_generic_failure(monkeypatch):
    """`_privileged_run` returning None means root is unreachable, not that the
    command failed -- say which, the way the engine path does."""
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ops.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: None)

    results = ops._install_compose_plugin_binary()

    assert results[0].ok is False
    assert "root required" in results[0].message


def test_compose_plugin_package_install_is_preferred_over_the_binary(monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None
    )
    privileged = []
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: privileged.append(cmd) or _cp(0))
    monkeypatch.setattr(ops, "_compose_available", lambda: True)

    results = ops._install_linux_compose_plugin()

    assert ["apt-get", "install", "-y", "docker-compose-v2"] in privileged
    assert not [cmd for cmd in privileged if cmd[0] == "curl"]
    assert results[0].ok is True


def test_manual_install_hint_names_amazon_linux():
    assert "Amazon Linux 2023" in ops._DOCKER_MANUAL_INSTALL_HINT
    assert "docker-compose-linux-$(uname -m)" in ops._DOCKER_MANUAL_INSTALL_HINT


_HOP_HOME = "/home/ec2-user"


def _hop_run_stub(*, available=("sg", "sudo"), sudo_resets_env=False):
    """A `_run` stand-in that emulates each hop's *environment* semantics.

    A mocked `subprocess.run` cannot observe sudo's `env_reset`, so the stub
    models it explicitly: `sg docker -c ...` hands the caller's environment
    through untouched, while a sudoers configuration that ignores
    `--preserve-env` replaces `HOME` with `/root` (Amazon Linux/RHEL
    `always_set_home`) and drops every `env=`-passed variable.
    """

    def _run(cmd, **kwargs):
        sentinel = (kwargs.get("env") or {}).get(ops._HOP_ENV_PROBE_VAR, "")
        if cmd[:3] == ["sg", "docker", "-c"]:
            if "sg" not in available:
                return _cp(1, stderr="sg: group 'docker' does not exist")
            home, forwarded = _HOP_HOME, sentinel
        elif cmd[:2] == ["sudo", "-n"]:
            if "sudo" not in available:
                return _cp(1, stderr="sudo: a password is required")
            home, forwarded = ("/root", "") if sudo_resets_env else (_HOP_HOME, sentinel)
        else:  # a bare `docker ...` -- the socket this session cannot reach
            return _cp(1, stderr="permission denied ... /var/run/docker.sock")
        if "printf" in cmd[-1]:
            return _cp(0, stdout=f"{home}\n{forwarded}")
        return _cp(0, stdout="27.0.3")

    return _run


def _hop_engine_env(monkeypatch, run_stub):
    """Wire `_ensure_docker_engine` up to the point where it needs a hop."""
    monkeypatch.setenv("HOME", _HOP_HOME)
    monkeypatch.setattr(ops, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ops, "_running_as_root", lambda: False)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_docker_daemon_reachable", lambda: False)
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: _cp(0))
    monkeypatch.setattr(ops, "_run", run_stub)


def test_docker_calls_hop_to_the_docker_group_within_the_same_run(
    monkeypatch, _no_docker_socket_hop
):
    """`usermod -aG docker` never reaches the already-open session, which is
    what killed the second pass of the cloud deploy mid-run (#3760). Rather
    than telling the operator to reconnect, ops re-runs its Docker calls under
    the group it just granted itself."""
    _hop_engine_env(monkeypatch, _hop_run_stub())

    results = ops._ensure_docker_engine()

    assert ops._docker_socket_hop() == "sg"
    assert results[-1].ok is True
    assert "`sg docker`" in results[-1].message
    # The real group change is still made -- the hop is only for this run.
    assert "new login sessions" in results[-1].details
    assert ops._apply_docker_socket_hop(["docker", "ps"]) == ["sg", "docker", "-c", "docker ps"]
    # Already-privileged and non-Docker argv are left exactly as they are.
    assert ops._apply_docker_socket_hop(["sudo", "-n", "docker", "ps"]) == [
        "sudo",
        "-n",
        "docker",
        "ps",
    ]
    assert ops._apply_docker_socket_hop(["systemctl", "status"]) == ["systemctl", "status"]


def test_docker_hop_falls_back_to_sudo_when_sg_is_unusable(monkeypatch, _no_docker_socket_hop):
    """A host where the group change itself could not be made still gets a hop,
    via passwordless sudo -- but only the environment-preserving form."""
    _hop_engine_env(monkeypatch, _hop_run_stub(available=("sudo",)))

    results = ops._ensure_docker_engine()

    assert ops._docker_socket_hop() == "sudo"
    assert results[-1].ok is True
    assert ops._apply_docker_socket_hop(["docker", "ps"]) == [
        "sudo",
        "-n",
        "--preserve-env",
        "docker",
        "ps",
    ]


def test_docker_hop_refused_when_it_would_reset_the_environment(monkeypatch, _no_docker_socket_hop):
    """The Critical finding on #3766: `sudo`'s `env_reset` + `always_set_home`
    makes the Docker CLI see `HOME=/root`, so `docker-compose.yml` interpolates
    every bind mount under `/root/.nyxGPT/volumes` while the ownership fixes
    chown the user's home, and `env=`-forwarded GlitchTip secrets never reach
    the container. A hop that does that is worse than no hop, so it is refused
    and the loud reconnect guidance is reported instead."""
    _hop_engine_env(monkeypatch, _hop_run_stub(available=("sudo",), sudo_resets_env=True))

    results = ops._ensure_docker_engine()

    assert ops._docker_socket_hop() is None
    assert results[-1].ok is False
    assert "loginctl terminate-user" in results[-1].details


def test_docker_hop_not_used_when_nothing_reaches_the_daemon(monkeypatch, _no_docker_socket_hop):
    _hop_engine_env(monkeypatch, _hop_run_stub(available=()))

    results = ops._ensure_docker_engine()

    assert ops._docker_socket_hop() is None
    assert results[-1].ok is False
    assert "loginctl terminate-user" in results[-1].details


def test_docker_engine_step_reports_no_failure_once_a_hop_recovers_it(
    monkeypatch, _no_docker_socket_hop
):
    """#3762: `[4/23] docker engine` printed `[FAIL] ...` and then `[OK]` lines
    for the same step. A group change ops then routed around is a `[NOTE]`, not
    a failure -- and the step's overall verdict must say it passed."""
    _hop_engine_env(monkeypatch, _hop_run_stub())
    # The group change itself fails; the `sg docker` hop still gets there.
    monkeypatch.setattr(
        ops, "_privileged_run", lambda cmd, **k: _cp(1 if cmd[0] == "usermod" else 0)
    )

    results = ops._ensure_docker_engine()

    assert all(r.ok for r in results), [r.message for r in results if not r.ok]
    superseded = [r for r in results if ops._result_status_label(r) == "NOTE"]
    assert superseded, [r.message for r in results]
    assert "'docker' group" in superseded[0].message
    # The original remediation survives for anyone reading the detail line.
    assert "loginctl terminate-user" in superseded[0].details
    assert results[-1].ok is True and "`sg docker`" in results[-1].message


def test_docker_engine_step_keeps_the_group_failure_when_nothing_recovers_it(
    monkeypatch, _no_docker_socket_hop
):
    """The other direction: with no hop available the failed attempt is part of
    why the step failed, so it must not be softened into a note."""
    _hop_engine_env(monkeypatch, _hop_run_stub(available=()))
    monkeypatch.setattr(
        ops, "_privileged_run", lambda cmd, **k: _cp(1 if cmd[0] == "usermod" else 0)
    )

    results = ops._ensure_docker_engine()

    failures = [r.message for r in results if not r.ok]
    assert any("'docker' group" in m for m in failures)
    assert results[-1].ok is False


def test_hop_env_probe_checks_home_and_a_forwarded_variable(monkeypatch, _no_docker_socket_hop):
    """Both mechanisms the hop must not break are exercised: `${HOME}` (Compose
    bind-mount interpolation) and an `env=`-passed variable (`docker compose
    exec -e VAR` secret forwarding, CodeQL #105/#106)."""
    monkeypatch.setenv("HOME", _HOP_HOME)
    seen = []

    def _run(cmd, **kwargs):
        seen.append((cmd, kwargs.get("env", {})))
        return _cp(0, stdout=f"{_HOP_HOME}\nnyxgpt-hop-probe")

    monkeypatch.setattr(ops, "_run", _run)

    assert ops._docker_hop_preserves_env("sg") is True
    cmd, env = seen[0]
    assert cmd[:3] == ["sg", "docker", "-c"]
    assert "$HOME" in cmd[3] and f"${ops._HOP_ENV_PROBE_VAR}" in cmd[3]
    assert env[ops._HOP_ENV_PROBE_VAR] == "nyxgpt-hop-probe"


def test_run_routes_docker_through_the_hop_while_it_is_active(monkeypatch, _no_docker_socket_hop):
    """The rewrite lives in `_run` so no Docker call site can forget it."""
    monkeypatch.setattr(ops, "_DOCKER_SOCKET_HOP", "sg")
    seen = []

    def _fake_subprocess_run(cmd, **kwargs):
        seen.append(cmd)
        return _cp(0)

    monkeypatch.setattr(ops.subprocess, "run", _fake_subprocess_run)

    ops._run(["docker", "ps", "-a"], check=False)
    ops._run(["systemctl", "is-active", "docker"], check=False)

    assert seen == [
        ["sg", "docker", "-c", "docker ps -a"],
        ["systemctl", "is-active", "docker"],
    ]


def test_hop_quotes_compose_argv_and_keeps_secrets_off_the_shell_string(
    monkeypatch, _no_docker_socket_hop
):
    """`sg -c` takes a command *string*, so argv must be shell-quoted -- and the
    `-e VAR` secret forwarding stays bare (value in the environment, never on
    the command line) exactly as `_glitchtip_ensure_superuser` builds it."""
    monkeypatch.setattr(ops, "_DOCKER_SOCKET_HOP", "sg")
    argv = [
        "docker",
        "compose",
        "-f",
        "/opt/nyx gpt/docker-compose.yml",
        "exec",
        "-T",
        "-e",
        "DJANGO_SUPERUSER_PASSWORD",
        "web",
        "sh",
        "-c",
        "createsuperuser --noinput",
    ]

    wrapped = ops._apply_docker_socket_hop(argv)

    assert wrapped[:3] == ["sg", "docker", "-c"]
    assert shlex.split(wrapped[3]) == argv
    assert "hunter2" not in wrapped[3]


def test_run_logs_the_unwrapped_argv_so_redaction_still_applies(
    monkeypatch, caplog, _no_docker_socket_hop
):
    """`_redact_cmd` masks argv element by element; `sg -c` collapses argv into
    one shell string it could not see into, so the log keeps the logical argv."""
    monkeypatch.setattr(ops, "_DOCKER_SOCKET_HOP", "sg")
    monkeypatch.setattr(ops.subprocess, "run", lambda cmd, **k: _cp(1, stderr="boom"))

    with caplog.at_level(logging.WARNING, logger="nyxgpt.ops"):
        ops._run(["docker", "login", "--password", "hunter2"], check=False)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in logged
    assert "--password ***" in logged


# --- Observability bind-mount ownership (#3632) ---


def test_ensure_observability_volume_dirs_chowns_to_container_uid(monkeypatch, tmp_path):
    """dockerd creates a missing bind-mount source root-owned; Prometheus runs
    as uid 65534 and crash-loops on it. Pre-create + chown before the stack starts."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: False)
    privileged = []
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: privileged.append(cmd) or _cp(0))

    results = ops._ensure_observability_volume_dirs()

    assert all(r.ok for r in results)
    prom = home / ".nyxGPT" / "volumes" / "prometheus"
    assert prom.is_dir()
    assert ["chown", "-R", "65534:65534", str(prom)] in privileged
    assert ["chown", "-R", "472:0", str(home / ".nyxGPT" / "volumes" / "grafana")] in privileged
    assert ["chown", "-R", "10001:10001", str(home / ".nyxGPT" / "volumes" / "loki")] in privileged
    # Directories whose image fixes up its own data dir are created but not chowned.
    assert (home / ".nyxGPT" / "volumes" / "glitchtip-postgres").is_dir()


def test_ensure_observability_volume_dirs_falls_back_to_acl_grant(monkeypatch, tmp_path):
    """Without passwordless root, a directory we own is handed to the container's
    uid with a POSIX ACL -- never a world-writable chmod, which would expose
    grafana.db (sessions, hashed credentials) to every local user (bandit B103)."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: None)
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: False)
    granted = []
    monkeypatch.setattr(
        ops, "_grant_dir_acl_to_uid", lambda path, uid: granted.append((path, uid)) or True
    )

    results = ops._ensure_observability_volume_dirs()

    assert all(r.ok for r in results)
    prom = home / ".nyxGPT" / "volumes" / "prometheus"
    assert (prom, 65534) in granted
    assert (home / ".nyxGPT" / "volumes" / "grafana", 472) in granted
    assert not prom.stat().st_mode & 0o002, "must never be made world-writable"


def test_ensure_observability_volume_dir_is_idempotent_once_acl_grants(monkeypatch, tmp_path):
    """An already-granted directory must not re-run a sudo chown on every install."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: True)
    monkeypatch.setattr(
        ops, "_privileged_run", lambda cmd, **k: pytest.fail(f"must not run privileged: {cmd}")
    )

    result = ops._ensure_observability_volume_dir("prometheus")

    assert result.ok is True
    assert "65534" in result.message


def test_ensure_observability_volume_dir_reports_when_acl_unavailable(monkeypatch, tmp_path):
    """No passwordless root and no ACL support (or root-owned leftovers inside):
    fail actionably rather than loosening the directory for every local user."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: None)
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: False)
    monkeypatch.setattr(ops, "_grant_dir_acl_to_uid", lambda path, uid: False)

    result = ops._ensure_observability_volume_dir("grafana")

    assert result.ok is False
    assert "sudo chown -R 472:0" in result.details
    assert not (home / ".nyxGPT" / "volumes" / "grafana").stat().st_mode & 0o002


def test_ensure_observability_volume_dirs_is_noop_off_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(
        ops, "_privileged_run", lambda cmd, **k: pytest.fail(f"must not run privileged: {cmd}")
    )

    results = ops._ensure_observability_volume_dirs()
    assert results[0].ok is True


def test_ensure_observability_volume_dir_reports_root_owned_dir(monkeypatch, tmp_path):
    """The already-broken machine: the directory exists root-owned from a
    previous run, and we have no way to fix it -- say exactly what to run."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_privileged_run", lambda cmd, **k: _cp(1))
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: False)
    monkeypatch.setattr(
        ops,
        "_grant_dir_acl_to_uid",
        lambda path, uid: pytest.fail("setfacl needs ownership; must not be attempted"),
    )
    monkeypatch.setattr(ops.os, "getuid", lambda: 1000)

    class _Stat:
        st_uid = 0
        st_mode = 0o40755

    monkeypatch.setattr(ops.Path, "stat", lambda self, **k: _Stat())

    result = ops._ensure_observability_volume_dir("prometheus")

    assert result.ok is False
    assert "sudo chown -R 65534:65534" in result.details


# --- POSIX ACL helpers (#3632) ---


def test_grant_dir_acl_to_uid_runs_setfacl_recursively(monkeypatch, tmp_path):
    """Recursive on purpose: root-owned leftovers inside a directory we own must
    make the grant fail rather than report success on a still-broken container."""
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/setfacl")
    calls = []
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: calls.append(cmd) or _cp(0))

    assert ops._grant_dir_acl_to_uid(tmp_path, 65534) is True
    assert calls == [["/usr/bin/setfacl", "-R", "-m", "u:65534:rwx", str(tmp_path)]]


def test_grant_dir_acl_to_uid_false_without_setfacl(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda name: None)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: pytest.fail(f"must not run: {cmd}"))

    assert ops._grant_dir_acl_to_uid(tmp_path, 65534) is False


def test_grant_dir_acl_to_uid_false_when_setfacl_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/setfacl")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: _cp(1, stderr="Operation not supported"))

    assert ops._grant_dir_acl_to_uid(tmp_path, 65534) is False


def test_dir_acl_grants_uid_reads_getfacl(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/getfacl")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: _cp(
            0, stdout="user::rwx\nuser:65534:rwx\ngroup::r-x\nmask::rwx\nother::r-x\n"
        ),
    )

    assert ops._dir_acl_grants_uid(tmp_path, 65534) is True
    assert ops._dir_acl_grants_uid(tmp_path, 472) is False


def test_dir_acl_grants_uid_ignores_mask_stripped_entry(monkeypatch, tmp_path):
    """An entry the ACL mask has cut down is annotated `#effective:` and is not
    a real grant -- treating it as one would leave the container crash-looping."""
    monkeypatch.setattr(ops, "_which", lambda name: "/usr/bin/getfacl")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: _cp(0, stdout="user:65534:rwx\t#effective:r--\n")
    )

    assert ops._dir_acl_grants_uid(tmp_path, 65534) is False


def test_dir_acl_grants_uid_false_without_getfacl(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda name: None)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: pytest.fail(f"must not run: {cmd}"))

    assert ops._dir_acl_grants_uid(tmp_path, 65534) is False


def test_observability_volume_doctor_issues_flags_root_owned_dir(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    (home / ".nyxGPT" / "volumes" / "prometheus").mkdir(parents=True)
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: False)
    monkeypatch.setattr(ops.os, "getuid", lambda: 1000)

    class _Stat:
        st_uid = 0
        st_mode = 0o40755

    monkeypatch.setattr(ops.Path, "stat", lambda self, **k: _Stat())

    issues = ops._observability_volume_doctor_issues()

    assert any("cannot write to it" in i and "65534" in i for i in issues)


def test_observability_volume_doctor_issues_flags_dir_we_own_without_acl(monkeypatch, tmp_path):
    """Owning the directory ourselves is *not* enough -- the container runs as a
    different uid, so a dir with no ACL grant is still a crash-loop."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    for name in ("prometheus", "grafana", "loki"):
        (home / ".nyxGPT" / "volumes" / name).mkdir(parents=True)
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: False)

    issues = ops._observability_volume_doctor_issues()

    assert len(issues) == 3


def test_observability_volume_doctor_issues_silent_when_acl_grants(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    for name in ("prometheus", "grafana", "loki"):
        (home / ".nyxGPT" / "volumes" / name).mkdir(parents=True)
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: True)

    assert ops._observability_volume_doctor_issues() == []


def test_observability_volume_doctor_issues_silent_on_legacy_world_writable(monkeypatch, tmp_path):
    """A machine reconciled by the first cut of this fix is genuinely working;
    doctor must not nag about a mode nyxGPT no longer produces."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    for name in ("prometheus", "grafana", "loki"):
        (home / ".nyxGPT" / "volumes" / name).mkdir(parents=True, mode=0o777)
    monkeypatch.setattr(ops, "_dir_acl_grants_uid", lambda path, uid: False)
    monkeypatch.setattr(ops.os, "getuid", lambda: 1000)

    class _Stat:
        st_uid = 0
        st_mode = 0o40777

    monkeypatch.setattr(ops.Path, "stat", lambda self, **k: _Stat())

    assert ops._observability_volume_doctor_issues() == []


# --- Native web wrapper auth key (#3632) ---


def test_native_web_wrapper_exports_auth_api_key():
    """Enabling [auth] must not break the native web UI: web/src/lib/apiProxy.ts
    reads NYXGPT_AUTH_API_KEY, which was previously only wired in Compose mode."""
    tpl = ops._NATIVE_WEB_WRAPPER_TEMPLATE
    assert "read -r HOST PORT API_BASE AUTH_KEY" in tpl
    assert "cfg.getboolean('auth', 'enabled', fallback=False)" in tpl
    assert 'export NYXGPT_AUTH_API_KEY="$AUTH_KEY"' in tpl


def test_install_includes_docker_engine_and_volume_dir_steps():
    """Both new reconciliation steps must actually be wired into `ops install`,
    and the volume-dir step must run before the observability stack starts."""
    source = ops.install.__code__.co_consts
    names = [c for c in source if isinstance(c, str)]
    assert "docker engine" in names
    assert "observability volume dirs" in names
