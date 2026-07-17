"""Unit tests for the self-heal watchdog (src/nyxgpt/self_heal.py).

These exercise the module's Docker Compose interaction with subprocess.run
mocked out, so no docker daemon or actual compose stack is needed.
"""

from __future__ import annotations

import json
import subprocess
import time
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


def _ps_line(service, *, state="running", health=""):
    return json.dumps(
        {"Service": service, "Name": f"nyxgpt-{service}-1", "State": state, "Health": health}
    )


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(self_heal, "_state_path", lambda: tmp_path / "self_heal_state.json")
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/docker")


@pytest.mark.unit
def test_resolve_compose_file_uses_env_override(monkeypatch):
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    assert self_heal._resolve_compose_file() == Path("/etc/nyxgpt/docker-compose.yml")


@pytest.mark.unit
def test_resolve_compose_file_defaults_to_repo_root(monkeypatch):
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)
    assert self_heal._resolve_compose_file() == self_heal.REPO_ROOT / "docker-compose.yml"


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
