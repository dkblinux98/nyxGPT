"""Unit tests for the self-heal watchdog (src/nyxgpt/self_heal.py).

These exercise the module's Docker Compose interaction with subprocess.run
mocked out, so no docker daemon or actual compose stack is needed.
"""

from __future__ import annotations

import json
import subprocess
import time
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nyxgpt import metrics as prom_metrics
from nyxgpt import self_heal


class CP:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _ps_line(service, *, state="running", health="", exit_code=0):
    return json.dumps(
        {
            "Service": service,
            "Name": f"nyxgpt-{service}-1",
            "State": state,
            "Health": health,
            "ExitCode": exit_code,
        }
    )


# Captured before the autouse fixture below stubs these two module attributes
# out (for every other test's sake) -- the tests that exercise these helpers'
# own logic call these plain function objects directly, bypassing the stub.
_real_brew_services_snapshot = self_heal._brew_services_snapshot
_real_native_container_state = self_heal._native_container_state


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(self_heal, "_state_path", lambda: tmp_path / "self_heal_state.json")
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/docker")
    # Neutralize native/local-first detection by default so pre-existing
    # Compose-only tests (and their generic `_run` mocks) aren't affected by
    # it -- tests that actually exercise native detection override these two.
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")


@pytest.mark.unit
def test_resolve_compose_file_uses_env_override(monkeypatch):
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    assert self_heal._resolve_compose_file() == Path("/etc/nyxgpt/docker-compose.yml")


@pytest.mark.unit
def test_resolve_compose_file_defaults_to_repo_root(monkeypatch):
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)
    assert self_heal._resolve_compose_file() == self_heal.REPO_ROOT / "docker-compose.yml"


@pytest.mark.unit
def test_resolve_compose_file_falls_back_to_config_ini(monkeypatch, tmp_path):
    # Brew-Cellar layout: no env override, no compose file on the module path;
    # the path recorded by `nyxgpt ops install` in config.ini wins.
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)
    monkeypatch.setattr(self_heal, "REPO_ROOT", tmp_path / "cellar")

    compose = tmp_path / "repo" / "docker-compose.yml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services: {}\n", encoding="utf-8")

    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    (home / ".nyxGPT" / "config.ini").write_text(
        f"[paths]\ncompose_file = {compose}\n", encoding="utf-8"
    )
    monkeypatch.setattr(self_heal.Path, "home", lambda: home)

    assert self_heal._resolve_compose_file() == compose


@pytest.mark.unit
def test_resolve_compose_file_ignores_config_when_path_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)
    monkeypatch.setattr(self_heal, "REPO_ROOT", tmp_path / "cellar")

    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    (home / ".nyxGPT" / "config.ini").write_text(
        "[paths]\ncompose_file = /nonexistent/docker-compose.yml\n", encoding="utf-8"
    )
    monkeypatch.setattr(self_heal.Path, "home", lambda: home)

    assert self_heal._resolve_compose_file() == tmp_path / "cellar" / "docker-compose.yml"


@pytest.mark.unit
def test_load_state_recovers_from_corrupt_json(monkeypatch, tmp_path):
    state_path = tmp_path / "self_heal_state.json"
    state_path.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(self_heal, "_state_path", lambda: state_path)

    # A corrupt state file must not blow up callers -- it falls back to the
    # default (disabled, no history) rather than propagating the JSON error.
    assert self_heal.is_enabled() is False
    assert self_heal.recent_events() == []


@pytest.mark.unit
def test_list_component_status_parses_ps_json(monkeypatch):
    stdout = "\n".join(
        [
            _ps_line("api", state="running", health="healthy"),
            _ps_line("web", state="running", health=""),
            _ps_line("cassandra", state="exited", health=""),
            _ps_line("glitchtip-migrate", state="exited", health=""),
        ]
    )
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout=stdout))

    statuses = self_heal.list_component_status()

    by_service = {s.service: s for s in statuses}
    assert set(by_service) == {"api", "web", "cassandra"}
    assert by_service["api"].healthy is True
    assert by_service["web"].healthy is True  # no healthcheck -> running is enough
    assert by_service["cassandra"].healthy is False


@pytest.mark.unit
def test_list_compose_component_status_one_shot_exited_zero_is_skipped(monkeypatch):
    # glitchtip-migrate's healthy end state -- exited 0, no running container --
    # must never surface as a component to track at all (#3381).
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(
            stdout=_ps_line("glitchtip-migrate", state="exited", exit_code=0)
        ),
    )
    assert self_heal._list_compose_component_status() == []


@pytest.mark.unit
def test_list_compose_component_status_one_shot_exited_nonzero_reported_unhealthy(monkeypatch):
    # A genuinely failed migration must still surface -- the one-shot
    # exemption is for the "not a long-running service" shape, not for
    # masking a real failure (#3381).
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(
            stdout=_ps_line("glitchtip-migrate", state="exited", exit_code=1)
        ),
    )
    statuses = self_heal._list_compose_component_status()
    assert len(statuses) == 1
    assert statuses[0].service == "glitchtip-migrate"
    assert statuses[0].state == "exited"
    assert statuses[0].healthy is False


@pytest.mark.unit
def test_list_component_status_unhealthy_container(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(
            stdout=_ps_line("prometheus", state="running", health="unhealthy")
        ),
    )
    statuses = self_heal.list_component_status()
    assert statuses[0].healthy is False
    assert statuses[0].health == "unhealthy"


@pytest.mark.unit
def test_list_component_status_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0):
        raise OSError("docker daemon not reachable")

    monkeypatch.setattr(self_heal, "_run", _boom)

    with caplog.at_level("WARNING"):
        statuses = self_heal.list_component_status()

    assert statuses == []
    assert "failed to query docker compose ps" in caplog.text
    assert "docker daemon not reachable" in caplog.text


@pytest.mark.unit
def test_list_component_status_skips_blank_and_invalid_lines(monkeypatch):
    stdout = "\n".join(
        [
            "",
            "not json at all",
            _ps_line("api", state="running", health="healthy"),
        ]
    )
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout=stdout))

    statuses = self_heal.list_component_status()

    # The blank line and the malformed JSON line are both skipped silently;
    # only the well-formed entry survives.
    assert [s.service for s in statuses] == ["api"]


@pytest.mark.unit
def test_list_component_status_no_docker(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    assert self_heal.list_component_status() == []


@pytest.mark.unit
def test_list_component_status_compose_failure(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0: CP(returncode=1, stderr="boom")
    )
    assert self_heal.list_component_status() == []


@pytest.mark.unit
def test_brew_services_snapshot_parses_output(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(
            stdout="nyxgpt-api started user ~/foo\nollama stopped user ~/bar\n"
        ),
    )
    snapshot = _real_brew_services_snapshot()
    assert snapshot["nyxgpt-api"] == "started"
    assert snapshot["ollama"] == "stopped"


@pytest.mark.unit
def test_brew_services_snapshot_no_brew(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    assert _real_brew_services_snapshot() == {}


@pytest.mark.unit
def test_brew_services_snapshot_run_failure(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(returncode=1))
    assert _real_brew_services_snapshot() == {}


@pytest.mark.unit
def test_brew_services_snapshot_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0):
        raise OSError("brew not reachable")

    monkeypatch.setattr(self_heal, "_run", _boom)

    with caplog.at_level("WARNING"):
        assert _real_brew_services_snapshot() == {}
    assert "failed to query brew services list" in caplog.text


@pytest.mark.unit
def test_native_container_state_running(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout="running\n"))
    assert _real_native_container_state("nyxgpt-cassandra") == "running"


@pytest.mark.unit
def test_native_container_state_absent_when_no_output(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout=""))
    assert _real_native_container_state("nyxgpt-cassandra") == "absent"


@pytest.mark.unit
def test_native_container_state_no_docker(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    assert _real_native_container_state("nyxgpt-cassandra") == "absent"


@pytest.mark.unit
def test_native_container_state_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0):
        raise OSError("docker daemon not reachable")

    monkeypatch.setattr(self_heal, "_run", _boom)

    with caplog.at_level("WARNING"):
        assert _real_native_container_state("nyxgpt-cassandra") == "absent"
    assert "failed to query docker state" in caplog.text


@pytest.mark.unit
def test_list_native_component_status_reports_brew_and_cassandra(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout=""))
    monkeypatch.setattr(
        self_heal,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-web": "stopped"},
    )
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "running")

    statuses = self_heal.list_component_status()
    by_service = {s.service: s for s in statuses}

    assert by_service["api"].source == "native"
    assert by_service["api"].healthy is True
    assert by_service["web"].source == "native"
    assert by_service["web"].healthy is False
    # ollama isn't in the brew snapshot -- not installed via `nyxgpt ops install`
    # yet, so it's out of scope rather than reported "down".
    assert "ollama" not in by_service
    assert by_service["cassandra"].source == "native"
    assert by_service["cassandra"].container == "nyxgpt-cassandra"
    assert by_service["cassandra"].healthy is True


@pytest.mark.unit
def test_list_native_component_status_skips_absent_cassandra(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout=""))
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")

    assert self_heal.list_component_status() == []


@pytest.mark.unit
def test_list_component_status_compose_managed_component_skips_native_duplicate(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout=_ps_line("api", state="running"))
    )
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {"nyxgpt-api": "started"})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "running")

    statuses = self_heal.list_component_status()
    api_entries = [s for s in statuses if s.service == "api"]

    # Compose already reports "api" -- it isn't also checked/reported natively.
    assert len(api_entries) == 1
    assert api_entries[0].source == "compose"


@pytest.mark.unit
def test_restart_component_success(monkeypatch):
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.restart_component("api")

    assert result.ok
    assert "Restarted api" in result.message
    cmd = run_mock.call_args[0][0]
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert cmd[-2:] == ["restart", "api"]


@pytest.mark.unit
def test_restart_component_failure(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0: CP(returncode=1, stderr="boom")
    )
    result = self_heal.restart_component("api")
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_restart_component_run_raises(monkeypatch):
    def _boom(cmd, timeout=30.0):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(self_heal, "_run", _boom)

    result = self_heal.restart_component("api")

    assert not result.ok
    assert "Failed to restart api" in result.message
    assert "TimeoutExpired" in result.details


@pytest.mark.unit
def test_restart_component_no_docker(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    result = self_heal.restart_component("api")
    assert not result.ok
    assert "docker not found" in result.message


@pytest.mark.unit
def test_restart_native_component_brew_service_success(monkeypatch):
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.restart_native_component("api")

    assert result.ok
    assert "Restarted brew service: nyxgpt-api" in result.message
    cmd = run_mock.call_args[0][0]
    assert cmd == ["brew", "services", "restart", "nyxgpt-api"]


@pytest.mark.unit
def test_restart_native_component_brew_service_failure(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0: CP(returncode=1, stderr="boom")
    )
    result = self_heal.restart_native_component("web")
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_restart_native_component_brew_service_run_raises(monkeypatch):
    def _boom(cmd, timeout=30.0):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(self_heal, "_run", _boom)

    result = self_heal.restart_native_component("ollama")

    assert not result.ok
    assert "Failed to restart ollama" in result.message
    assert "TimeoutExpired" in result.details


@pytest.mark.unit
def test_restart_native_component_no_brew(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_which", lambda prog: None if prog == "brew" else "/usr/bin/docker"
    )
    result = self_heal.restart_native_component("ollama")
    assert not result.ok
    assert "brew not found" in result.message


@pytest.mark.unit
def test_restart_native_component_cassandra_success(monkeypatch):
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.restart_native_component("cassandra")

    assert result.ok
    assert "Restarted docker container: nyxgpt-cassandra" in result.message
    cmd = run_mock.call_args[0][0]
    assert cmd == ["docker", "restart", "nyxgpt-cassandra"]


@pytest.mark.unit
def test_restart_native_component_cassandra_failure(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0: CP(returncode=1, stderr="boom")
    )
    result = self_heal.restart_native_component("cassandra")
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_restart_native_component_cassandra_no_docker(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    result = self_heal.restart_native_component("cassandra")
    assert not result.ok
    assert "docker not found" in result.message


@pytest.mark.unit
def test_restart_native_component_unknown(monkeypatch):
    result = self_heal.restart_native_component("does-not-exist")
    assert not result.ok
    assert "Unknown native component" in result.message


@pytest.mark.unit
def test_component_logs_success(monkeypatch):
    run_mock = MagicMock(
        return_value=CP(stdout="glitchtip_1  | Confirm your account: http://...\n")
    )
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.component_logs("glitchtip", tail=50)

    assert result.ok
    assert "glitchtip" in result.message
    assert "Confirm your account" in result.details
    cmd = run_mock.call_args[0][0]
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert cmd[-5:] == ["logs", "--no-color", "--tail", "50", "glitchtip"]


@pytest.mark.unit
def test_component_logs_failure(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0: CP(returncode=1, stderr="no such service")
    )
    result = self_heal.component_logs("glitchtip")
    assert not result.ok
    assert "no such service" in result.details


@pytest.mark.unit
def test_component_logs_run_raises(monkeypatch):
    def _boom(cmd, timeout=30.0):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(self_heal, "_run", _boom)

    result = self_heal.component_logs("glitchtip")

    assert not result.ok
    assert "Failed to fetch logs for glitchtip" in result.message
    assert "TimeoutExpired" in result.details


@pytest.mark.unit
def test_component_logs_no_docker(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    result = self_heal.component_logs("glitchtip")
    assert not result.ok
    assert "docker not found" in result.message


@pytest.mark.unit
def test_is_enabled_defaults_false():
    assert self_heal.is_enabled() is False


@pytest.mark.unit
def test_set_enabled_roundtrip():
    assert self_heal.set_enabled(True) is True
    assert self_heal.is_enabled() is True
    assert self_heal.set_enabled(False) is False
    assert self_heal.is_enabled() is False


@pytest.mark.unit
def test_seed_enabled_default_only_applies_once():
    self_heal.seed_enabled_default(True)
    assert self_heal.is_enabled() is True

    # Seeding again with a different default must not override an existing state file.
    self_heal.seed_enabled_default(False)
    assert self_heal.is_enabled() is True


@pytest.mark.unit
def test_heal_now_skips_healthy_components(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("api", "nyxgpt-api-1", "running", "healthy", True)],
    )
    restart_mock = MagicMock()
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    result = self_heal.heal_now()

    assert result["healed"] == []
    restart_mock.assert_not_called()


@pytest.mark.unit
def test_heal_now_restarts_unhealthy_component(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False)],
    )
    monkeypatch.setattr(
        self_heal,
        "restart_component",
        lambda service: self_heal.HealResult(True, f"Restarted {service}"),
    )

    result = self_heal.heal_now()

    assert len(result["healed"]) == 1
    event = result["healed"][0]
    assert event["service"] == "web"
    assert event["ok"] is True
    assert event["restart_count"] == 1

    events = self_heal.recent_events()
    assert len(events) == 1
    assert events[0]["service"] == "web"


@pytest.mark.unit
def test_heal_now_respects_backoff(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False)],
    )
    restart_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted web"))
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    self_heal.heal_now(backoff_seconds=3600.0)
    self_heal.heal_now(backoff_seconds=3600.0)

    restart_mock.assert_called_once()


@pytest.mark.unit
def test_heal_now_gives_up_after_max_consecutive_restarts(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False)],
    )
    restart_mock = MagicMock(return_value=self_heal.HealResult(False, "Failed to restart web"))
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    for _ in range(5):
        self_heal.heal_now(max_consecutive_restarts=2, backoff_seconds=0.0)

    assert restart_mock.call_count == 2


@pytest.mark.unit
def test_heal_now_manual_restarts_even_when_healthy(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("api", "nyxgpt-api-1", "running", "healthy", True)],
    )
    restart_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted api"))
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    result = self_heal.heal_now(service="api")

    restart_mock.assert_called_once_with("api")
    assert result["healed"][0]["reason"] == "manual heal-now"


@pytest.mark.unit
def test_heal_now_dispatches_native_restart_for_native_source(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("ollama", "ollama", "stopped", "", False, "native")],
    )
    restart_component_mock = MagicMock()
    restart_native_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted ollama"))
    monkeypatch.setattr(self_heal, "restart_component", restart_component_mock)
    monkeypatch.setattr(self_heal, "restart_native_component", restart_native_mock)

    result = self_heal.heal_now()

    restart_native_mock.assert_called_once_with("ollama")
    restart_component_mock.assert_not_called()
    assert result["healed"][0]["service"] == "ollama"


@pytest.mark.unit
def test_heal_now_unknown_service_returns_error(monkeypatch):
    monkeypatch.setattr(self_heal, "list_component_status", lambda: [])

    result = self_heal.heal_now(service="does-not-exist")

    assert result["healed"] == []
    assert "Unknown or not-running component" in result["error"]


@pytest.mark.unit
def test_status_aggregates_enabled_components_and_events(monkeypatch):
    self_heal.set_enabled(True)
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus("api", "nyxgpt-api-1", "running", "healthy", True),
            self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False),
        ],
    )

    data = self_heal.status()

    assert data["enabled"] is True
    assert data["unhealthy_count"] == 1
    assert len(data["components"]) == 2
    assert data["events"] == []


@pytest.mark.unit
def test_watchdog_calls_heal_now_only_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(self_heal, "heal_now", lambda **kwargs: calls.append(1))
    monkeypatch.setattr(self_heal, "is_enabled", lambda: True)

    watchdog = self_heal.Watchdog(interval_seconds=0.01)
    watchdog.start()
    time.sleep(0.1)
    watchdog.stop(timeout=1.0)

    assert len(calls) >= 1


@pytest.mark.unit
def test_watchdog_skips_heal_now_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(self_heal, "heal_now", lambda **kwargs: calls.append(1))
    monkeypatch.setattr(self_heal, "is_enabled", lambda: False)

    watchdog = self_heal.Watchdog(interval_seconds=0.01)
    watchdog.start()
    time.sleep(0.1)
    watchdog.stop(timeout=1.0)

    assert calls == []


@pytest.mark.unit
def test_watchdog_start_is_a_noop_when_already_running(monkeypatch, caplog):
    monkeypatch.setattr(self_heal, "heal_now", lambda **kwargs: None)
    monkeypatch.setattr(self_heal, "is_enabled", lambda: False)

    watchdog = self_heal.Watchdog(interval_seconds=0.01)
    watchdog.start()
    first_thread = watchdog._thread
    try:
        with caplog.at_level("WARNING"):
            watchdog.start()  # second call while the loop thread is alive
        assert "already running" in caplog.text
        # The original thread is left in place, not replaced.
        assert watchdog._thread is first_thread
    finally:
        watchdog.stop(timeout=1.0)


def _metric_value(name, **labels):
    return prom_metrics.REGISTRY.get_sample_value(name, labels or None)


@pytest.mark.unit
def test_heal_now_logs_health_check_per_component(monkeypatch, caplog):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus("api", "nyxgpt-api-1", "running", "healthy", True),
            self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False),
        ],
    )

    with caplog.at_level("DEBUG"):
        self_heal.heal_now()

    assert "self-heal: health check api healthy=True" in caplog.text
    assert "self-heal: health check web healthy=False" in caplog.text
    assert _metric_value("nyxgpt_selfheal_unhealthy_components") == 1


@pytest.mark.unit
def test_heal_now_logs_restart_attempt_and_success(monkeypatch, caplog):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False)],
    )
    monkeypatch.setattr(
        self_heal,
        "restart_component",
        lambda service: self_heal.HealResult(True, f"Restarted {service}"),
    )

    with caplog.at_level("INFO"):
        self_heal.heal_now()

    assert "self-heal: attempting restart of web" in caplog.text
    assert "self-heal: restart of web succeeded (restart_count=1)" in caplog.text
    assert "self-heal: heal pass complete (checked=1, unhealthy=1, healed=1" in caplog.text

    assert _metric_value("nyxgpt_selfheal_restarts_total", service="web", result="ok") >= 1
    assert _metric_value("nyxgpt_selfheal_restart_count", service="web") == 1
    assert _metric_value("nyxgpt_selfheal_last_recovery_timestamp", service="web") is not None


@pytest.mark.unit
def test_heal_now_logs_restart_failure_as_error(monkeypatch, caplog):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False)],
    )
    monkeypatch.setattr(
        self_heal,
        "restart_component",
        lambda service: self_heal.HealResult(False, f"Failed to restart {service}"),
    )

    with caplog.at_level("ERROR"):
        self_heal.heal_now()

    assert "self-heal: restart of web failed (restart_count=1)" in caplog.text
    assert _metric_value("nyxgpt_selfheal_restarts_total", service="web", result="failed") >= 1


@pytest.mark.unit
def test_heal_now_logs_backoff_skip(monkeypatch, caplog):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False)],
    )
    restart_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted web"))
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    self_heal.heal_now(backoff_seconds=3600.0)
    with caplog.at_level("DEBUG"):
        self_heal.heal_now(backoff_seconds=3600.0)

    assert "self-heal: skipping restart of web, backoff active" in caplog.text
    restart_mock.assert_called_once()


@pytest.mark.unit
def test_heal_now_logs_give_up_after_max_consecutive_restarts(monkeypatch, caplog):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False)],
    )
    monkeypatch.setattr(
        self_heal,
        "restart_component",
        lambda service: self_heal.HealResult(False, f"Failed to restart {service}"),
    )

    for _ in range(2):
        self_heal.heal_now(max_consecutive_restarts=2, backoff_seconds=0.0)

    with caplog.at_level("WARNING"):
        self_heal.heal_now(max_consecutive_restarts=2, backoff_seconds=0.0)

    assert "self-heal: giving up on web, 2 consecutive restart(s) already failed" in caplog.text


@pytest.mark.unit
def test_heal_now_resets_restart_count_metric_on_recovery(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False)],
    )
    monkeypatch.setattr(
        self_heal,
        "restart_component",
        lambda service: self_heal.HealResult(True, f"Restarted {service}"),
    )
    self_heal.heal_now(backoff_seconds=0.0)
    assert _metric_value("nyxgpt_selfheal_restart_count", service="web") == 1

    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("web", "nyxgpt-web-1", "running", "healthy", True)],
    )
    self_heal.heal_now()

    assert _metric_value("nyxgpt_selfheal_restart_count", service="web") == 0


@pytest.mark.unit
def test_status_updates_unhealthy_components_gauge(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus("api", "nyxgpt-api-1", "running", "healthy", True),
            self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False),
        ],
    )

    self_heal.status()

    assert _metric_value("nyxgpt_selfheal_unhealthy_components") == 1


@pytest.mark.unit
def test_watchdog_stop_logs(caplog):
    watchdog = self_heal.Watchdog(interval_seconds=0.01)
    watchdog.start()
    with caplog.at_level("INFO"):
        watchdog.stop(timeout=1.0)
    assert "Self-heal watchdog stopped" in caplog.text


@pytest.mark.unit
def test_watchdog_loop_survives_heal_now_exception(monkeypatch, caplog):
    def _boom(**kwargs):
        raise RuntimeError("heal pass exploded")

    monkeypatch.setattr(self_heal, "heal_now", _boom)
    monkeypatch.setattr(self_heal, "is_enabled", lambda: True)

    watchdog = self_heal.Watchdog(interval_seconds=0.01)
    with caplog.at_level("ERROR"):
        watchdog.start()
        time.sleep(0.1)
        watchdog.stop(timeout=1.0)

    # The exception is caught and logged, not left to kill the daemon thread.
    assert "error during automatic heal pass" in caplog.text
    assert not watchdog._thread


# --- Desired-state reconciliation (#3356) ---


def _cfg(**enabled_by_section):
    """Build a ConfigParser with `[section] enabled = true/false` for each kwarg."""
    parser = ConfigParser()
    for section, enabled in enabled_by_section.items():
        parser.add_section(section)
        parser.set(section, "enabled", "true" if enabled else "false")
    return parser


@pytest.mark.unit
def test_enabled_observability_profiles_maps_sections_to_compose_profiles(monkeypatch):
    cfg = _cfg(monitoring=True, log_aggregation=False, tracing=True, error_tracking=False)
    monkeypatch.setattr(self_heal, "load_config", lambda: cfg)

    assert self_heal._enabled_observability_profiles() == {"monitoring", "tracing"}


@pytest.mark.unit
def test_enabled_observability_profiles_all_off_by_default(monkeypatch):
    monkeypatch.setattr(self_heal, "load_config", lambda: ConfigParser())
    assert self_heal._enabled_observability_profiles() == set()


@pytest.mark.unit
def test_enabled_observability_profiles_missing_config_returns_empty(monkeypatch):
    def _boom():
        raise FileNotFoundError("no config.ini")

    monkeypatch.setattr(self_heal, "load_config", _boom)
    assert self_heal._enabled_observability_profiles() == set()


@pytest.mark.unit
def test_enabled_observability_profiles_log_aggregation_maps_to_logging_profile(monkeypatch):
    monkeypatch.setattr(self_heal, "load_config", lambda: _cfg(log_aggregation=True))
    assert self_heal._enabled_observability_profiles() == {"logging"}


@pytest.mark.unit
def test_enabled_observability_profiles_error_tracking_maps_to_errors_profile(monkeypatch):
    monkeypatch.setattr(self_heal, "load_config", lambda: _cfg(error_tracking=True))
    assert self_heal._enabled_observability_profiles() == {"errors"}


@pytest.mark.unit
def test_desired_compose_services_no_profiles_skips_docker_call(monkeypatch):
    run_mock = MagicMock()
    monkeypatch.setattr(self_heal, "_run", run_mock)
    assert self_heal._desired_compose_services(set()) == set()
    run_mock.assert_not_called()


@pytest.mark.unit
def test_desired_compose_services_no_docker(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    assert self_heal._desired_compose_services({"monitoring"}) == set()


@pytest.mark.unit
def test_desired_compose_services_resolves_and_excludes_core_services(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(stdout="grafana\nprometheus\napi\nweb\n"),
    )
    services = self_heal._desired_compose_services({"monitoring"})
    assert services == {"grafana", "prometheus"}


@pytest.mark.unit
def test_desired_compose_services_excludes_one_shot_services(monkeypatch):
    # glitchtip-migrate is resolved by `docker compose config --services`
    # alongside the long-running glitchtip service, but it must never count
    # as "desired" -- otherwise it's reported absent forever once it exits 0
    # and is (correctly) never present (#3381).
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(stdout="glitchtip\nglitchtip-migrate\n"),
    )
    services = self_heal._desired_compose_services({"errors"})
    assert services == {"glitchtip"}


@pytest.mark.unit
def test_desired_compose_services_exclude_one_shot_false_keeps_one_shot(monkeypatch):
    # `_mark_disabled_present_services` needs `glitchtip-migrate` to still be
    # recognized as belonging to the "errors" profile when it opts out of the
    # one-shot exclusion -- otherwise a present-but-failed migration job never
    # matches `all_observability_services` and is stuck `desired=True` forever
    # even after the profile is disabled (#3381 review follow-up).
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(stdout="glitchtip\nglitchtip-migrate\n"),
    )
    services = self_heal._desired_compose_services({"errors"}, exclude_one_shot=False)
    assert services == {"glitchtip", "glitchtip-migrate"}


@pytest.mark.unit
def test_desired_compose_services_command_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0: CP(returncode=1, stderr="boom")
    )
    assert self_heal._desired_compose_services({"monitoring"}) == set()


@pytest.mark.unit
def test_desired_compose_services_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0):
        raise OSError("docker daemon not reachable")

    monkeypatch.setattr(self_heal, "_run", _boom)
    with caplog.at_level("WARNING"):
        assert self_heal._desired_compose_services({"monitoring"}) == set()
    assert "failed to resolve desired compose services" in caplog.text


@pytest.mark.unit
def test_absent_desired_statuses_reports_missing_only():
    absent = self_heal._absent_desired_statuses(
        present_services={"prometheus"}, desired_services={"grafana", "loki", "prometheus"}
    )

    assert {s.service for s in absent} == {"grafana", "loki"}
    for status in absent:
        assert status.state == "absent"
        assert status.healthy is False
        assert status.source == "compose"
        assert status.desired is True


@pytest.mark.unit
def test_absent_desired_statuses_empty_when_nothing_desired():
    assert self_heal._absent_desired_statuses(present_services=set(), desired_services=set()) == []


@pytest.mark.unit
def test_absent_desired_statuses_empty_when_all_present():
    assert (
        self_heal._absent_desired_statuses(
            present_services={"grafana"}, desired_services={"grafana"}
        )
        == []
    )


@pytest.mark.unit
def test_mark_disabled_present_services_flags_present_but_disabled(monkeypatch):
    statuses = [
        self_heal.ComponentStatus("grafana", "nyxgpt-grafana-1", "exited", "", False),
        self_heal.ComponentStatus("prometheus", "nyxgpt-prometheus-1", "running", "healthy", True),
    ]
    monkeypatch.setattr(
        self_heal,
        "_desired_compose_services",
        lambda profiles, **kwargs: {"grafana", "prometheus"},
    )

    result = self_heal._mark_disabled_present_services(statuses, desired_services={"prometheus"})

    by_service = {s.service: s for s in result}
    assert by_service["grafana"].desired is False
    assert by_service["prometheus"].desired is True


@pytest.mark.unit
def test_mark_disabled_present_services_short_circuits_when_nothing_extra(monkeypatch):
    statuses = [self_heal.ComponentStatus("prometheus", "c", "running", "healthy", True)]
    desired_services_mock = MagicMock()
    monkeypatch.setattr(self_heal, "_desired_compose_services", desired_services_mock)

    result = self_heal._mark_disabled_present_services(statuses, desired_services={"prometheus"})

    assert result == statuses
    desired_services_mock.assert_not_called()


@pytest.mark.unit
def test_mark_disabled_present_services_ignores_core_services(monkeypatch):
    statuses = [self_heal.ComponentStatus("api", "c", "running", "healthy", True)]
    desired_services_mock = MagicMock()
    monkeypatch.setattr(self_heal, "_desired_compose_services", desired_services_mock)

    result = self_heal._mark_disabled_present_services(statuses, desired_services=set())

    assert result == statuses
    desired_services_mock.assert_not_called()


@pytest.mark.unit
def test_mark_disabled_present_services_leaves_unknown_extra_service_alone(monkeypatch):
    # Present, not currently desired, but also not part of ANY observability
    # profile (e.g. a container from an unrelated Compose project) -- not
    # something self-heal should second-guess as "disabled".
    statuses = [self_heal.ComponentStatus("mystery-sidecar", "c", "running", "healthy", True)]
    monkeypatch.setattr(
        self_heal, "_desired_compose_services", lambda profiles, **kwargs: {"grafana"}
    )

    result = self_heal._mark_disabled_present_services(statuses, desired_services=set())

    assert result[0].desired is True


@pytest.mark.unit
def test_mark_disabled_present_services_flags_failed_one_shot_service(monkeypatch):
    # Real code review regression (#3381): a `glitchtip-migrate` run that
    # actually failed (non-zero exit) stays present per
    # `_list_compose_component_status`'s own exemption. Once the "errors"
    # profile is disabled, `all_observability_services` must still recognize
    # `glitchtip-migrate` as belonging to that profile (via
    # `exclude_one_shot=False`) so it gets flagged `desired=False` instead of
    # being stuck `desired=True` and endlessly restarted by the watchdog.
    statuses = [self_heal.ComponentStatus("glitchtip-migrate", "c", "exited", "", False)]
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(stdout="glitchtip\nglitchtip-migrate\n"),
    )

    result = self_heal._mark_disabled_present_services(statuses, desired_services=set())

    assert result[0].service == "glitchtip-migrate"
    assert result[0].desired is False


@pytest.mark.unit
def test_record_health_check_excludes_disabled_present_from_unhealthy_count():
    statuses = [
        self_heal.ComponentStatus("grafana", "c", "exited", "", False, desired=False),
        self_heal.ComponentStatus("web", "c", "exited", "", False),
    ]
    assert self_heal._record_health_check(statuses) == 1


@pytest.mark.unit
def test_heal_now_skips_disabled_present_component(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("grafana", "c", "exited", "", False, desired=False)],
    )
    restart_mock = MagicMock()
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    result = self_heal.heal_now()

    assert result["healed"] == []
    restart_mock.assert_not_called()


@pytest.mark.unit
def test_heal_now_manual_overrides_disabled_present_component(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("grafana", "c", "exited", "", False, desired=False)],
    )
    restart_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted grafana"))
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    result = self_heal.heal_now(service="grafana")

    restart_mock.assert_called_once_with("grafana")
    assert result["healed"][0]["ok"] is True


@pytest.mark.unit
def test_list_component_status_reports_torn_down_monitoring_profile(monkeypatch):
    # docker compose ps -a: the stack is fully down, nothing at all is reported
    # -- this is the exact reported scenario (#3356): monitoring was enabled,
    # then `nyxgpt ops down` removed every container.
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout=""))
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda profiles: {"grafana"})

    statuses = self_heal.list_component_status()

    assert len(statuses) == 1
    assert statuses[0].service == "grafana"
    assert statuses[0].state == "absent"
    assert statuses[0].healthy is False


@pytest.mark.unit
def test_list_component_status_one_shot_service_not_reported_absent(monkeypatch):
    # End-to-end reproduction of #3381: with the "errors" profile enabled,
    # `docker compose config --services` lists both `glitchtip` and the
    # one-shot `glitchtip-migrate` migration job; only `glitchtip` is
    # actually running (the migration already exited 0 and is gone). The
    # migration job must not show up as an absent/unhealthy component.
    def _run_stub(cmd, timeout=30.0):
        if "config" in cmd:
            return CP(stdout="glitchtip\nglitchtip-migrate\n")
        return CP(stdout=_ps_line("glitchtip", state="running", health="healthy"))

    monkeypatch.setattr(self_heal, "_run", _run_stub)
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"errors"})

    statuses = self_heal.list_component_status()

    by_service = {s.service: s for s in statuses}
    assert "glitchtip-migrate" not in by_service
    assert by_service["glitchtip"].healthy is True


@pytest.mark.unit
def test_list_component_status_desired_service_already_present_not_duplicated(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(stdout=_ps_line("grafana", state="running")),
    )
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda profiles: {"grafana"})

    statuses = self_heal.list_component_status()

    assert len(statuses) == 1
    assert statuses[0].service == "grafana"
    assert statuses[0].state == "running"


@pytest.mark.unit
def test_list_component_status_disabled_profile_reports_nothing(monkeypatch):
    # monitoring disabled in config -- absence is expected, not a heal target.
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0: CP(stdout=""))
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: set())
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda profiles: set())

    assert self_heal.list_component_status() == []


@pytest.mark.unit
def test_list_component_status_flags_present_but_disabled_profile(monkeypatch):
    # grafana's container still exists (stopped) after `monitoring` was
    # disabled via the config wizard, which stops rather than removes
    # containers -- it must not be silently auto-restarted.
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0: CP(stdout=_ps_line("grafana", state="exited")),
    )
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: set())
    monkeypatch.setattr(
        self_heal,
        "_desired_compose_services",
        lambda profiles, **kwargs: (
            {"grafana"} if profiles == set(self_heal.OBSERVABILITY_PROFILES) else set()
        ),
    )

    statuses = self_heal.list_component_status()

    assert len(statuses) == 1
    assert statuses[0].service == "grafana"
    assert statuses[0].desired is False


@pytest.mark.unit
def test_list_component_status_flags_failed_one_shot_after_profile_disabled(monkeypatch):
    # End-to-end reproduction of the code review follow-up on #3381: the
    # "errors" profile was enabled, `glitchtip-migrate` failed (non-zero
    # exit) and is therefore still present, and the user then disabled
    # "errors". The failed migration container must be flagged
    # `desired=False` -- not left `desired=True` forever -- so the automatic
    # watchdog doesn't keep trying to restart a container the user
    # deliberately walked away from.
    def _run_stub(cmd, timeout=30.0):
        if "config" in cmd:
            return CP(stdout="glitchtip\nglitchtip-migrate\n")
        return CP(stdout=_ps_line("glitchtip-migrate", state="exited", exit_code=1))

    monkeypatch.setattr(self_heal, "_run", _run_stub)
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: set())

    statuses = self_heal.list_component_status()

    by_service = {s.service: s for s in statuses}
    assert by_service["glitchtip-migrate"].healthy is False
    assert by_service["glitchtip-migrate"].desired is False


@pytest.mark.unit
def test_bring_up_compose_service_success(monkeypatch):
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal._bring_up_compose_service("grafana")

    assert result.ok
    assert "Started grafana" in result.message
    cmd = run_mock.call_args[0][0]
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert cmd[-3:] == ["up", "-d", "grafana"]
    for profile in self_heal.OBSERVABILITY_PROFILES:
        assert profile in cmd


@pytest.mark.unit
def test_bring_up_compose_service_failure(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=120.0: CP(returncode=1, stderr="boom")
    )
    result = self_heal._bring_up_compose_service("grafana")
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_bring_up_compose_service_run_raises(monkeypatch):
    def _boom(cmd, timeout=120.0):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(self_heal, "_run", _boom)
    result = self_heal._bring_up_compose_service("grafana")
    assert not result.ok
    assert "Failed to start grafana" in result.message
    assert "TimeoutExpired" in result.details


@pytest.mark.unit
def test_bring_up_compose_service_no_docker(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    result = self_heal._bring_up_compose_service("grafana")
    assert not result.ok
    assert "docker not found" in result.message


@pytest.mark.unit
def test_heal_now_brings_up_absent_desired_component(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("grafana", "", "absent", "", False)],
    )
    bring_up_mock = MagicMock(return_value=self_heal.HealResult(True, "Started grafana"))
    restart_mock = MagicMock()
    monkeypatch.setattr(self_heal, "_bring_up_compose_service", bring_up_mock)
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    result = self_heal.heal_now()

    bring_up_mock.assert_called_once_with("grafana")
    restart_mock.assert_not_called()
    assert result["healed"][0]["service"] == "grafana"
    assert result["healed"][0]["ok"] is True


@pytest.mark.unit
def test_heal_now_manual_brings_up_absent_desired_component(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus("grafana", "", "absent", "", False)],
    )
    bring_up_mock = MagicMock(return_value=self_heal.HealResult(True, "Started grafana"))
    monkeypatch.setattr(self_heal, "_bring_up_compose_service", bring_up_mock)

    result = self_heal.heal_now(service="grafana")

    bring_up_mock.assert_called_once_with("grafana")
    assert result["healed"][0]["reason"] == "manual heal-now"
