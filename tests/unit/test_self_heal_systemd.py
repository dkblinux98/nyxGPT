"""Unit tests for self_heal.py's Linux native (systemd --user) probes (#3508).

Mirrors test_self_heal.py's mocked-subprocess conventions for the equivalent
Homebrew-based tests. Every test here pins `platform.system()` to "Linux"
explicitly (the suite may run on either OS).
"""

import subprocess

import pytest

from nyxgpt import self_heal

pytestmark = pytest.mark.unit

# Captured before the `_no_system_ollama_service_by_default` autouse fixture
# below ever monkeypatches the module attribute -- the `_system_ollama_service_active`
# tests exercise the real implementation and restore it via this reference.
_real_system_ollama_service_active = self_heal._system_ollama_service_active


@pytest.fixture(autouse=True)
def _force_linux_native_path(monkeypatch):
    """Pin `platform.system()` to "Linux" for every test in this file."""
    monkeypatch.setattr(self_heal.platform, "system", lambda: "Linux")


@pytest.fixture(autouse=True)
def _no_system_ollama_service_by_default(monkeypatch):
    """Default `_system_ollama_service_active()` to False (no real systemctl call).

    Tests that specifically exercise the #3632 adoption path override this
    with their own `monkeypatch.setattr` call, which -- since it runs after
    this fixture on the same `monkeypatch` instance -- takes precedence.
    """
    monkeypatch.setattr(self_heal, "_system_ollama_service_active", lambda: False)


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


# --- _is_macos / _is_linux ---


@pytest.mark.parametrize(
    "system_value,is_macos,is_linux",
    [("Darwin", True, False), ("Linux", False, True), ("Windows", False, False)],
)
def test_os_dispatch_helpers(monkeypatch, system_value, is_macos, is_linux):
    monkeypatch.setattr(self_heal.platform, "system", lambda: system_value)
    assert self_heal._is_macos() is is_macos
    assert self_heal._is_linux() is is_linux


# --- _systemd_services_snapshot ---


def test_systemd_services_snapshot_no_systemctl(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    assert self_heal._systemd_services_snapshot() == {}


def test_systemd_services_snapshot_only_reports_installed_units(monkeypatch, tmp_path):
    """A unit whose file was never installed must be absent from the snapshot
    (not merely "none") -- mirrors `_brew_services_snapshot`'s "not in this
    dict" == "not installed" contract.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(self_heal.Path, "home", lambda: home)
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/systemctl")
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

    monkeypatch.setattr(self_heal, "_run", fake_run)
    snapshot = self_heal._systemd_services_snapshot()
    assert snapshot["nyxgpt-api"] == "started"
    assert snapshot["nyxgpt-web"] == "none"
    assert "nyxgpt-ollama" not in snapshot


def test_systemd_services_snapshot_swallows_run_exception(monkeypatch, tmp_path, caplog):
    home = tmp_path / "home"
    monkeypatch.setattr(self_heal.Path, "home", lambda: home)
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/systemctl")
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "nyxgpt-api.service").write_text("[Service]\n", encoding="utf-8")

    def raising_run(cmd, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(self_heal, "_run", raising_run)
    assert self_heal._systemd_services_snapshot() == {}


# --- _list_native_component_status (Linux branch) ---


def test_list_native_component_status_reports_systemd_units(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_systemd_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-web": "none"},
    )
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")

    statuses = self_heal._list_native_component_status()
    by_service = {s.service: s for s in statuses}
    assert by_service["api"].state == "started"
    assert by_service["api"].healthy is True
    assert by_service["api"].source == "native"
    # "web" is present in the snapshot (installed but stopped) -- reported,
    # just unhealthy, distinct from "ollama" which is entirely absent from
    # the snapshot (never installed) and so isn't reported at all.
    assert by_service["web"].state == "none"
    assert by_service["web"].healthy is False
    assert "ollama" not in by_service
    assert "cassandra" not in by_service


def test_list_native_component_status_includes_native_cassandra_on_linux(monkeypatch):
    monkeypatch.setattr(self_heal, "_systemd_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "running")

    statuses = self_heal._list_native_component_status()
    cassandra = next(s for s in statuses if s.service == "cassandra")
    assert cassandra.healthy is True
    assert cassandra.source == "native"


def test_list_native_component_status_reports_serving_system_ollama(monkeypatch):
    """#3632: `ops install` normally disables a system ollama.service so
    nyxgpt-ollama can own port 11434. When it couldn't (no passwordless root),
    self-heal must report health via the unit that is actually serving rather
    than restart-looping the nyxgpt unit that can only lose the port race.
    """
    monkeypatch.setattr(self_heal, "_system_ollama_service_active", lambda: True)
    # Even with a stale nyxgpt-ollama.service still installed and crash-looping
    # (snapshot reports it "none"), the serving unit must win over it.
    monkeypatch.setattr(self_heal, "_systemd_services_snapshot", lambda: {"nyxgpt-ollama": "none"})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")

    statuses = self_heal._list_native_component_status()
    by_service = {s.service: s for s in statuses}
    assert by_service["ollama"].healthy is True
    assert by_service["ollama"].state == "active"
    assert by_service["ollama"].container == "ollama.service"
    assert by_service["ollama"].source == "native"


# --- _system_ollama_service_active ---


def test_system_ollama_service_active_true(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda name: "/usr/bin/systemctl")
    calls = []
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, **k: calls.append(cmd) or _cp(0, stdout="active\n")
    )

    assert _real_system_ollama_service_active() is True
    assert calls == [["systemctl", "is-active", "ollama.service"]]


def test_system_ollama_service_active_false_when_inactive(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda name: "/usr/bin/systemctl")
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **k: _cp(3, stdout="inactive\n"))

    assert _real_system_ollama_service_active() is False


def test_system_ollama_service_active_false_without_systemctl(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda name: None)
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, **k: (_ for _ in ()).throw(AssertionError("not called"))
    )

    assert _real_system_ollama_service_active() is False


# --- _restart_systemd_service ---


def test_restart_systemd_service_no_systemctl(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    result = self_heal._restart_systemd_service("nyxgpt-api")
    assert result.ok is False
    assert "systemctl not found" in result.message


def test_restart_systemd_service_success(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **k: _cp(0))
    result = self_heal._restart_systemd_service("nyxgpt-api")
    assert result.ok is True
    assert "Restarted systemd unit: nyxgpt-api" in result.message


def test_restart_systemd_service_failure(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **k: _cp(1, stderr="nope"))
    result = self_heal._restart_systemd_service("nyxgpt-api")
    assert result.ok is False


def test_restart_systemd_service_run_raises(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/systemctl")

    def raising_run(cmd, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(self_heal, "_run", raising_run)
    result = self_heal._restart_systemd_service("nyxgpt-api")
    assert result.ok is False
    assert "Failed to restart nyxgpt-api" in result.message


# --- restart_native_component (Linux branch) ---


def test_restart_native_component_systemd_service_success(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/systemctl")
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **k: _cp(0))
    result = self_heal.restart_native_component("api")
    assert result.ok is True
    assert "nyxgpt-api" in result.message


def test_restart_native_component_unknown_on_linux(monkeypatch):
    result = self_heal.restart_native_component("does-not-exist")
    assert result.ok is False
    assert "Unknown native component" in result.message


def test_restart_native_component_cassandra_still_uses_docker_on_linux(monkeypatch):
    monkeypatch.setattr(self_heal, "_cassandra_active_elsewhere", lambda: None)
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **k: _cp(0))
    result = self_heal.restart_native_component("cassandra")
    assert result.ok is True
    assert self_heal.NATIVE_CASSANDRA_CONTAINER in result.message


# --- _native_component_logs (web branch, Linux) ---


def test_native_component_logs_web_reads_from_log_dir_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    (tmp_path / "nyxgpt-web.log").write_text("stdout line\n", encoding="utf-8")
    (tmp_path / "nyxgpt-web.err.log").write_text("stderr line\n", encoding="utf-8")

    result = self_heal._native_component_logs("web", tail=10)
    assert result.ok is True
    assert "stdout line" in result.details
    assert "stderr line" in result.details


def test_native_component_logs_web_missing_files_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    result = self_heal._native_component_logs("web", tail=10)
    assert result.ok is False
    assert "No log files found for web" in result.message


def test_native_component_logs_web_never_calls_brew_prefix_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    (tmp_path / "nyxgpt-web.log").write_text("hi\n", encoding="utf-8")

    def _boom():
        raise AssertionError("_brew_prefix should not be called on Linux")

    monkeypatch.setattr(self_heal, "_brew_prefix", _boom)
    result = self_heal._native_component_logs("web", tail=10)
    assert result.ok is True


# --- _native_component_logs (api branch, Linux, #3629) ---


def test_native_component_logs_api_combines_structured_and_launchd_files_on_linux(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    (tmp_path / "api.log").write_text("structured line\n", encoding="utf-8")
    (tmp_path / "nyxgpt-api.log").write_text("stdout line\n", encoding="utf-8")
    (tmp_path / "nyxgpt-api.err.log").write_text("stderr line\n", encoding="utf-8")

    result = self_heal._native_component_logs("api", tail=10)
    assert result.ok is True
    assert "structured line" in result.details
    assert "stdout line" in result.details
    assert "stderr line" in result.details


def test_native_component_logs_api_pre_logging_failure_visible_via_stderr_on_linux(
    monkeypatch, tmp_path
):
    # #3629: the P6-1 startup refusal (#3500) fires before configure_logging
    # runs, so api.log never exists at all -- only the systemd unit's own
    # stderr file does.
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    (tmp_path / "nyxgpt-api.err.log").write_text(
        "ERROR: Refusing to start: [api] host ...\n", encoding="utf-8"
    )

    result = self_heal._native_component_logs("api", tail=10)
    assert result.ok is True
    assert "Refusing to start" in result.details


def test_native_component_logs_api_missing_files_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    result = self_heal._native_component_logs("api", tail=10)
    assert result.ok is False
    assert "No log files found for api" in result.message


def test_native_component_logs_api_never_calls_brew_prefix_on_linux(monkeypatch, tmp_path):
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    (tmp_path / "api.log").write_text("hi\n", encoding="utf-8")

    def _boom():
        raise AssertionError("_brew_prefix should not be called on Linux")

    monkeypatch.setattr(self_heal, "_brew_prefix", _boom)
    result = self_heal._native_component_logs("api", tail=10)
    assert result.ok is True
