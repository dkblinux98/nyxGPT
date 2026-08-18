"""Unit tests for the self-heal watchdog (src/nyxgpt/self_heal.py).

These exercise the module's Docker Compose interaction with subprocess.run
mocked out, so no docker daemon or actual compose stack is needed.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nyxgpt import metrics as prom_metrics
from nyxgpt import self_heal

# The real project docker-compose.yml -- #3621 retired self_heal.COMPOSE_FILE's
# REPO_ROOT-relative default (a dev-checkout-only fallback), so tests that
# want to parse real declared services/fetch real service logs need to point
# at this file explicitly rather than relying on the (now ops-managed,
# ~/.nyxGPT-rooted) default resolving to it by accident.
_REAL_COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


class CP:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_survey(monkeypatch, components, *, probe=None):
    """Stub the one-pass survey `status()` reads (rows + the probe behind them).

    `status()` goes through `component_survey()` rather than
    `list_component_status()` so the component rows and
    `compose_probe_available`/`compose_probe_reason` come from the same
    `docker compose ps` and cannot contradict each other (#3812).
    """
    survey = self_heal.ComponentSurvey(
        components=components,
        compose_probe=probe if probe is not None else self_heal.ComposeProbe(available=True),
    )
    monkeypatch.setattr(self_heal, "component_survey", lambda: survey)
    monkeypatch.setattr(self_heal, "list_component_status", lambda: survey.components)
    return survey


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
_real_list_terraform_component_status = self_heal._list_terraform_component_status
_real_list_kubernetes_component_status = self_heal._list_kubernetes_component_status


@pytest.fixture(autouse=True)
def _force_macos_native_path(monkeypatch):
    """Pin `platform.system()` to "Darwin".

    This file predates the Linux/systemd native path (#3508) and assumes
    the original macOS-only (Homebrew) native code path throughout --
    `_isolated_state` below neutralizes `_brew_services_snapshot` (not
    `_systemd_services_snapshot`) by default, and several tests patch
    `_brew_services_snapshot`/`_brew_prefix` directly. Without this, those
    tests would silently exercise the Linux dispatch branch instead whenever
    the suite runs on a real Linux host (CI runs on ubuntu-latest). The
    Linux path has its own tests in test_self_heal_systemd.py.
    """
    monkeypatch.setattr(self_heal.platform, "system", lambda: "Darwin")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(self_heal, "_state_path", lambda: tmp_path / "self_heal_state.json")
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/docker")
    # Neutralize native/local-first detection by default so pre-existing
    # Compose-only tests (and their generic `_run` mocks) aren't affected by
    # it -- tests that actually exercise native detection override these two.
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")
    # Same neutralization for Terraform and Kubernetes: several existing
    # tests stub `_native_container_state`/`_run` with name-agnostic lambdas
    # (e.g. "return running for any container") to exercise the native
    # Cassandra check specifically -- left live, `_list_terraform_component_status`
    # (which also calls `_native_container_state`, just for `nyxgpt-tf-*`
    # names) and `_list_kubernetes_component_status` (its own `kubectl get
    # pods` call) would pick those up too and report spurious extra
    # components. Tests exercising either override the relevant stub
    # themselves (see e.g. test_list_terraform_component_status_* below).
    monkeypatch.setattr(self_heal, "_list_terraform_component_status", lambda: [])
    monkeypatch.setattr(self_heal, "_list_kubernetes_component_status", lambda already_managed: [])


@pytest.mark.unit
def test_resolve_compose_file_uses_env_override(monkeypatch):
    monkeypatch.setenv("NYXGPT_COMPOSE_FILE", "/etc/nyxgpt/docker-compose.yml")
    assert self_heal._resolve_compose_file() == Path("/etc/nyxgpt/docker-compose.yml")


@pytest.mark.unit
def test_run_logs_cmd_rc_stderr_tail_on_nonzero_exit(caplog):
    # #3415 gap 5: subprocess evidence (probe/restart failures) must reach
    # Loki even though self_heal's `_run` never raises (always check=False).
    with caplog.at_level("DEBUG", logger="nyxgpt.self_heal"):
        cp = self_heal._run(["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"])

    assert cp.returncode == 2
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit"
    assert records[0].levelno == logging.WARNING
    assert "rc=2" in records[0].getMessage()
    assert records[0].returncode == 2
    assert "boom" in records[0].stderr_tail


@pytest.mark.unit
def test_run_expected_true_logs_debug_not_warning_on_nonzero_exit(caplog):
    with caplog.at_level("DEBUG", logger="nyxgpt.self_heal"):
        cp = self_heal._run(
            ["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(2)"],
            expected=True,
        )

    assert cp.returncode == 2
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit at DEBUG"
    assert records[0].levelno == logging.DEBUG
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.unit
def test_resolve_compose_file_defaults_to_ops_managed_location(monkeypatch, tmp_path):
    # #3621: no more REPO_ROOT module-path check or config.ini fallback --
    # `nyxgpt ops install` syncs the packaged docker-compose.yml to this
    # fixed, ops-managed location regardless of how nyxGPT is installed.
    monkeypatch.delenv("NYXGPT_COMPOSE_FILE", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(self_heal.Path, "home", lambda: home)

    assert self_heal._resolve_compose_file() == home / ".nyxGPT" / "docker-compose.yml"


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
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=stdout))
    # Isolate from the tracing-enabled-by-default desired-state check (#3415):
    # this test is only about ps-json parsing, not observability profiles.
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: set())

    statuses = self_heal.list_component_status()

    by_service = {s.service: s for s in statuses}
    assert set(by_service) == {"api", "web", "cassandra"}
    assert by_service["api"].healthy is True
    assert by_service["web"].healthy is True  # no healthcheck -> running is enough
    assert by_service["cassandra"].healthy is False


@pytest.mark.unit
def test_compose_probe_one_shot_exited_zero_is_skipped(monkeypatch):
    # glitchtip-migrate's healthy end state -- exited 0, no running container --
    # must never surface as a component to track at all (#3381).
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(
            stdout=_ps_line("glitchtip-migrate", state="exited", exit_code=0)
        ),
    )
    assert self_heal.compose_probe().statuses == ()


@pytest.mark.unit
def test_compose_probe_one_shot_exited_nonzero_reported_unhealthy(monkeypatch):
    # A genuinely failed migration must still surface -- the one-shot
    # exemption is for the "not a long-running service" shape, not for
    # masking a real failure (#3381).
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(
            stdout=_ps_line("glitchtip-migrate", state="exited", exit_code=1)
        ),
    )
    statuses = self_heal.compose_probe().statuses
    assert len(statuses) == 1
    assert statuses[0].service == "glitchtip-migrate"
    assert statuses[0].state == "exited"
    assert statuses[0].healthy is False


@pytest.mark.unit
def test_list_component_status_unhealthy_container(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(
            stdout=_ps_line("prometheus", state="running", health="unhealthy")
        ),
    )
    statuses = self_heal.list_component_status()
    assert statuses[0].healthy is False
    assert statuses[0].health == "unhealthy"


@pytest.mark.unit
def test_list_component_status_starting_container_is_not_unhealthy(monkeypatch):
    """A container inside its Docker start_period ("starting") must NOT be
    reported unhealthy, or the watchdog restarts it before it can finish
    starting -- and for the api container (which runs the watchdog itself)
    that is a permanent crash loop. Regression guard for the Compose
    self-heal loop.
    """
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(
            stdout=_ps_line("api", state="running", health="starting")
        ),
    )
    statuses = self_heal.list_component_status()
    assert statuses[0].health == "starting"
    assert statuses[0].healthy is True


@pytest.mark.unit
def test_list_component_status_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0, **_k):
        raise OSError("docker daemon not reachable")

    monkeypatch.setattr(self_heal, "_run", _boom)

    with caplog.at_level("WARNING"):
        statuses = self_heal.list_component_status()

    assert statuses == []
    assert "`docker compose ps` could not be run" in caplog.text
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
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=stdout))
    # Isolate from the tracing-enabled-by-default desired-state check (#3415):
    # this test is only about ps-json parsing, not observability profiles.
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: set())

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
        self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(returncode=1, stderr="boom")
    )
    assert self_heal.list_component_status() == []


@pytest.mark.unit
def test_compose_probe_nonzero_exit_logs_warning(monkeypatch, caplog):
    # #3588: a failed `docker compose ps` (e.g. COMPOSE_FILE doesn't exist
    # from this process's vantage point) must never fail silently -- that's
    # exactly what made the observability tier vanish without a trace in
    # Terraform mode.
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(returncode=1, stderr="boom")
    )
    with caplog.at_level("WARNING", logger="nyxgpt.self_heal"):
        assert self_heal.compose_probe().statuses == ()
    assert "`docker compose ps` exited" in caplog.text


@pytest.mark.unit
def test_compose_probe_available_true_when_docker_and_compose_file_present(monkeypatch, tmp_path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", compose_file)
    assert self_heal.compose_probe_available() is True


@pytest.mark.unit
def test_compose_probe_available_false_when_docker_missing(monkeypatch, tmp_path):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", compose_file)
    assert self_heal.compose_probe_available() is False


@pytest.mark.unit
def test_compose_probe_available_false_when_compose_file_unreachable(monkeypatch, tmp_path):
    # The Terraform-managed api container's exact failure mode before #3588's
    # fix: docker is on PATH, but COMPOSE_FILE resolved to a path that was
    # never bind-mounted into this container.
    monkeypatch.setattr(self_heal, "_which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", tmp_path / "does-not-exist.yml")
    assert self_heal.compose_probe_available() is False


@pytest.mark.unit
def test_list_component_status_includes_observability_alongside_terraform_core(monkeypatch):
    """Regression for #3588: the Compose observability survey must run (and
    surface its results) no matter which mode owns the core components --
    Terraform mode reporting the core four must never come at the cost of
    silently dropping the observability tier."""
    monkeypatch.setattr(
        self_heal, "_list_terraform_component_status", _real_list_terraform_component_status
    )
    terraform_containers = set(self_heal.TERRAFORM_CONTAINERS.values())
    monkeypatch.setattr(
        self_heal,
        "_native_container_state",
        lambda name: "running" if name in terraform_containers else "absent",
    )
    monkeypatch.setattr(self_heal, "_native_container_health", lambda name: "")
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(stdout=_ps_line("grafana", state="running")),
    )
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: set())

    statuses = self_heal.list_component_status()
    by_service = {s.service: s for s in statuses}

    assert by_service["grafana"].source == "compose"
    assert by_service["grafana"].healthy is True
    for core in ("api", "web", "ollama", "cassandra"):
        assert by_service[core].source == "terraform"


@pytest.mark.unit
def test_brew_services_snapshot_parses_output(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(
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
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(returncode=1))
    assert _real_brew_services_snapshot() == {}


@pytest.mark.unit
def test_brew_services_snapshot_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0, **_k):
        raise OSError("brew not reachable")

    monkeypatch.setattr(self_heal, "_run", _boom)

    with caplog.at_level("WARNING"):
        assert _real_brew_services_snapshot() == {}
    assert "failed to query brew services list" in caplog.text


@pytest.mark.unit
def test_native_container_state_running(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout="running\n"))
    assert _real_native_container_state("nyxgpt-cassandra") == "running"


@pytest.mark.unit
def test_native_container_state_absent_when_no_output(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    assert _real_native_container_state("nyxgpt-cassandra") == "absent"


@pytest.mark.unit
def test_native_container_state_no_docker(monkeypatch):
    # No docker to ask means the state is undetermined, not established as
    # gone -- "absent" is reserved for an answer Docker actually gave (#3812).
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    assert _real_native_container_state("nyxgpt-cassandra") == "unknown"


@pytest.mark.unit
def test_native_container_state_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0, **_k):
        raise OSError("docker daemon not reachable")

    monkeypatch.setattr(self_heal, "_run", _boom)

    with caplog.at_level("WARNING"):
        # Unqueryable, not absent (#3812).
        assert _real_native_container_state("nyxgpt-cassandra") == "unknown"
    assert "failed to query docker state" in caplog.text


@pytest.mark.unit
def test_list_native_component_status_reports_brew_and_cassandra(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
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
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")

    assert self_heal.list_component_status() == []


@pytest.mark.unit
def test_list_component_status_compose_managed_component_skips_native_duplicate(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(stdout=_ps_line("api", state="running")),
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
@pytest.mark.parametrize("bad", ["--privileged", "-rf", "a b", "x;rm -rf /", "../etc", "$(id)", ""])
def test_restart_component_rejects_unsafe_names(monkeypatch, bad):
    """An externally-influenced service name that could inject a CLI flag or
    shell metacharacter must be refused before it ever reaches `_run` (CodeQL
    #4, py/command-line-injection)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.restart_component(bad)

    assert not result.ok
    assert "invalid service name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--all", "-n=evil", "pod;reboot", "../x", ""])
def test_heal_kubernetes_pod_rejects_unsafe_names(monkeypatch, bad):
    """A crafted pod name must not reach `kubectl delete pod` (CodeQL #4)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.heal_kubernetes_pod(bad)

    assert not result.ok
    assert "invalid pod name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--rm", "-d", "a b", "x;rm -rf /", "../etc", "$(id)", ""])
def test_bring_up_compose_service_rejects_unsafe_names(monkeypatch, bad):
    """An unsafe service name must be refused before it reaches `docker compose up`
    (CodeQL #4, py/command-line-injection) -- closes the coverage gap for the third
    guarded sink alongside restart_component / heal_kubernetes_pod."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal._bring_up_compose_service(bad)

    assert not result.ok
    assert "invalid service name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--privileged", "-rf", "a b", "x;rm -rf /", "../etc", "$(id)", ""])
def test_restart_brew_service_rejects_unsafe_names(monkeypatch, bad):
    """An unsafe name must be refused before it reaches `brew services restart`
    (CodeQL #4, py/command-line-injection)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal._restart_brew_service(bad)

    assert not result.ok
    assert "invalid service name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--privileged", "-rf", "a b", "x;rm -rf /", "../etc", "$(id)", ""])
def test_restart_native_container_rejects_unsafe_names(monkeypatch, bad):
    """An unsafe name must be refused before it reaches `docker restart`
    (CodeQL #4, py/command-line-injection)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal._restart_native_container(bad)

    assert not result.ok
    assert "invalid container name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--all", "-f", "a b", "x;reboot", "../etc", "$(id)", ""])
def test_docker_container_logs_rejects_unsafe_names(monkeypatch, bad):
    """An unsafe container name must be refused before it reaches `docker logs`
    (CodeQL #4, py/command-line-injection)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal._docker_container_logs(bad, tail=10)

    assert not result.ok
    assert "invalid container name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--all", "-n=evil", "pod;reboot", "../x", ""])
def test_kubernetes_pod_logs_rejects_unsafe_names(monkeypatch, bad):
    """An unsafe pod name must be refused before it reaches `kubectl logs`
    (CodeQL #4, py/command-line-injection)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal._kubernetes_pod_logs(bad, tail=10)

    assert not result.ok
    assert "invalid pod name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--filter", "-a", "a b", "x;id", "../etc", "$(id)", ""])
def test_native_container_state_rejects_unsafe_names(monkeypatch, bad):
    """An unsafe container name must be refused before it reaches
    `docker ps --filter` (CodeQL #4, py/command-line-injection)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    assert self_heal._native_container_state(bad) == "absent"
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--filter", "-a", "a b", "x;id", "../etc", "$(id)", ""])
def test_native_container_health_rejects_unsafe_names(monkeypatch, bad):
    """An unsafe container name must be refused before it reaches
    `docker inspect` (CodeQL #4, py/command-line-injection)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    assert self_heal._native_container_health(bad) == ""
    run_mock.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["--tail=0", "-f", "a b", "x;id", "../etc", "$(id)", ""])
def test_compose_component_logs_rejects_unsafe_names(monkeypatch, bad):
    """An unsafe service name must be refused before it reaches
    `docker compose logs` (CodeQL #4, py/command-line-injection)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal._compose_component_logs(bad, tail=10)

    assert not result.ok
    assert "invalid service name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
def test_restart_component_failure(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(returncode=1, stderr="boom")
    )
    result = self_heal.restart_component("api")
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_restart_component_run_raises(monkeypatch):
    def _boom(cmd, timeout=30.0, **_k):
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
        self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(returncode=1, stderr="boom")
    )
    result = self_heal.restart_native_component("web")
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_restart_native_component_brew_service_run_raises(monkeypatch):
    def _boom(cmd, timeout=30.0, **_k):
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
        self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(returncode=1, stderr="boom")
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
def test_compose_file_services_parses_declared_services(monkeypatch):
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", _REAL_COMPOSE_FILE)
    services = self_heal._compose_file_services()
    assert {"api", "web", "glitchtip", "grafana", "loki"} <= services


@pytest.mark.unit
def test_compose_component_logs_refuses_undeclared_service(monkeypatch):
    """A well-formed name that is not declared in the compose file must be
    refused without reaching `docker compose logs` (CodeQL #4: the argv only
    ever receives service names selected from the compose file itself)."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal._compose_component_logs("not-a-declared-service", tail=10)

    assert not result.ok
    assert "Unknown compose service" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
def test_component_logs_success(monkeypatch):
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", _REAL_COMPOSE_FILE)
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
@pytest.mark.parametrize("bad", ["--rm", "; rm -rf /", "../etc", "$(id)", ""])
def test_component_logs_rejects_unsafe_names(monkeypatch, bad):
    """An externally-influenced service name (`GET /self-heal/logs?service=`) must be
    refused before it ever reaches a subprocess argv (CodeQL #4,
    py/command-line-injection) -- mirrors test_restart_component_rejects_unsafe_names."""
    run_mock = MagicMock(return_value=CP(returncode=0))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.component_logs(bad)

    assert not result.ok
    assert "invalid service name" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
def test_component_logs_failure(monkeypatch):
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", _REAL_COMPOSE_FILE)
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(returncode=1, stderr="no such service"),
    )
    result = self_heal.component_logs("glitchtip")
    assert not result.ok
    assert "no such service" in result.details


@pytest.mark.unit
def test_component_logs_run_raises(monkeypatch):
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", _REAL_COMPOSE_FILE)

    def _boom(cmd, timeout=30.0, **_k):
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


def _status(service, *, source="compose", state="running", container=""):
    return self_heal.ComponentStatus(
        service=service,
        container=container or service,
        state=state,
        health="",
        healthy=state in ("running", "started"),
        source=source,
    )


@pytest.mark.unit
def test_component_logs_native_api_tails_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("api", source="native")]
    )
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    monkeypatch.setattr(self_heal, "_brew_prefix", lambda: None)
    (tmp_path / "api.log").write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = self_heal.component_logs("api", tail=2)

    assert result.ok
    assert "api" in result.message
    assert "line2\nline3" in result.details


@pytest.mark.unit
def test_component_logs_native_api_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("api", source="native")]
    )
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    monkeypatch.setattr(self_heal, "_brew_prefix", lambda: None)

    result = self_heal.component_logs("api")

    assert not result.ok
    assert "No log files found for api" in result.message


@pytest.mark.unit
def test_component_logs_native_api_reads_launchd_files(monkeypatch, tmp_path):
    # #3629: a startup refusal (e.g. P6-1's bind-address check in app.py's
    # lifespan, #3500) fires before configure_logging runs, so it never
    # reaches api.log -- only Homebrew's own launchd stderr file.
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("api", source="native")]
    )
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    monkeypatch.setattr(self_heal, "_brew_prefix", lambda: str(tmp_path))
    log_dir = tmp_path / "var" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "nyxgpt-api.log").write_text("api starting\n", encoding="utf-8")
    (log_dir / "nyxgpt-api.err.log").write_text(
        "ERROR: Refusing to start: [api] host ...\n", encoding="utf-8"
    )

    result = self_heal.component_logs("api")

    assert result.ok
    assert "api starting" in result.details
    assert "Refusing to start" in result.details


@pytest.mark.unit
def test_component_logs_native_api_missing_launchd_files_falls_back_to_structured(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("api", source="native")]
    )
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    monkeypatch.setattr(self_heal, "_brew_prefix", lambda: None)
    (tmp_path / "api.log").write_text("structured only\n", encoding="utf-8")

    result = self_heal.component_logs("api")

    assert result.ok
    assert "structured only" in result.details


@pytest.mark.unit
def test_component_logs_compose_api_uses_compose_logs(monkeypatch):
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", _REAL_COMPOSE_FILE)
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("api", source="compose")]
    )
    run_mock = MagicMock(return_value=CP(stdout="api-1  | starting up\n"))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.component_logs("api", tail=75)

    assert result.ok
    assert "starting up" in result.details
    cmd = run_mock.call_args[0][0]
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert cmd[-5:] == ["logs", "--no-color", "--tail", "75", "api"]


@pytest.mark.unit
def test_component_logs_native_web_reads_launchd_files(monkeypatch, tmp_path):
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("web", source="native")]
    )
    monkeypatch.setattr(self_heal, "_brew_prefix", lambda: str(tmp_path))
    log_dir = tmp_path / "var" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "nyxgpt-web.log").write_text("web starting\n", encoding="utf-8")
    (log_dir / "nyxgpt-web.err.log").write_text("a warning\n", encoding="utf-8")

    result = self_heal.component_logs("web")

    assert result.ok
    assert "web starting" in result.details
    assert "a warning" in result.details


@pytest.mark.unit
def test_component_logs_native_web_no_brew(monkeypatch):
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("web", source="native")]
    )
    monkeypatch.setattr(self_heal, "_brew_prefix", lambda: None)

    result = self_heal.component_logs("web")

    assert not result.ok
    assert "Homebrew not found" in result.message


@pytest.mark.unit
def test_component_logs_native_cassandra_uses_docker_logs(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [_status("cassandra", source="native", container="nyxgpt-cassandra")],
    )
    run_mock = MagicMock(return_value=CP(stdout="Cassandra starting up\n"))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.component_logs("cassandra", tail=30)

    assert result.ok
    assert "Cassandra starting up" in result.details
    assert run_mock.call_args[0][0] == ["docker", "logs", "--tail", "30", "nyxgpt-cassandra"]


@pytest.mark.unit
def test_component_logs_native_ollama_tails_aggregated_file(monkeypatch, tmp_path):
    # ~/.nyxGPT/logs/ollama.log is kept up to date regardless of mode by the
    # com.nyxgpt.ollama-logs LaunchAgent (scripts/follow-ollama-logs.sh) --
    # see docs/api.md#ollama-logs.
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("ollama", source="native")]
    )
    monkeypatch.setattr(self_heal, "get_log_dir", lambda: tmp_path)
    (tmp_path / "ollama.log").write_text("llama runner started\n", encoding="utf-8")

    result = self_heal.component_logs("ollama")

    assert result.ok
    assert "llama runner started" in result.details


@pytest.mark.unit
def test_component_logs_terraform_uses_docker_logs(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [_status("api", source="terraform", container="nyxgpt-tf-api")],
    )
    run_mock = MagicMock(return_value=CP(stdout="uvicorn running\n"))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.component_logs("api")

    assert result.ok
    assert run_mock.call_args[0][0] == ["docker", "logs", "--tail", "200", "nyxgpt-tf-api"]


@pytest.mark.unit
def test_component_logs_kubernetes_uses_kubectl_logs(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            _status(
                "nyxgpt-api-stable-abc123",
                source="kubernetes",
                container="nyxgpt-api-stable-abc123",
            )
        ],
    )
    monkeypatch.setattr(self_heal, "_which", lambda prog: f"/usr/bin/{prog}")
    run_mock = MagicMock(return_value=CP(stdout="pod log line\n"))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.component_logs("nyxgpt-api-stable-abc123")

    assert result.ok
    assert run_mock.call_args[0][0] == [
        "kubectl",
        "logs",
        "-n",
        self_heal.K8S_NAMESPACE,
        "--tail",
        "200",
        "nyxgpt-api-stable-abc123",
    ]


@pytest.mark.unit
def test_component_logs_absent_service_explicit_error(monkeypatch):
    monkeypatch.setattr(
        self_heal, "list_component_status", lambda: [_status("grafana", state="absent")]
    )
    run_mock = MagicMock(return_value=CP(stdout="should never be called"))
    monkeypatch.setattr(self_heal, "_run", run_mock)

    result = self_heal.component_logs("grafana")

    assert not result.ok
    assert "not currently running" in result.message
    run_mock.assert_not_called()


@pytest.mark.unit
def test_component_logs_unknown_service_explicit_error(monkeypatch):
    monkeypatch.setattr(self_heal, "list_component_status", lambda: [])
    monkeypatch.setattr(self_heal, "_which", lambda _: None)

    result = self_heal.component_logs("totally-unknown-service")

    assert not result.ok
    assert "docker not found" in result.message


@pytest.mark.unit
def test_tail_text_file_returns_last_n_lines(tmp_path):
    path = tmp_path / "x.log"
    path.write_text("\n".join(f"line{i}" for i in range(1, 11)) + "\n", encoding="utf-8")

    assert self_heal._tail_text_file(path, 3) == "line8\nline9\nline10"


@pytest.mark.unit
def test_tail_text_file_missing_file_returns_empty(tmp_path):
    assert self_heal._tail_text_file(tmp_path / "missing.log", 10) == ""


@pytest.mark.unit
def test_brew_prefix_uses_brew_output(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_which", lambda prog: "/usr/bin/brew" if prog == "brew" else None
    )
    monkeypatch.setattr(self_heal, "_run", lambda cmd, **_k: CP(stdout="/opt/homebrew\n"))

    assert self_heal._brew_prefix() == "/opt/homebrew"


@pytest.mark.unit
def test_brew_prefix_no_brew_no_conventional_dir(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    monkeypatch.setattr(self_heal.Path, "is_dir", lambda self: False)

    assert self_heal._brew_prefix() is None


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
def test_heal_now_records_probe_evidence_on_event(monkeypatch):
    # #3415 gap 7: the heal event must carry the raw probe evidence that
    # triggered it, not just a human-readable `reason` string, so an
    # operator can read *why* from the event instead of reproducing it.
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus(
                "web", "nyxgpt-web-1", "exited", "unhealthy", False, source="compose"
            )
        ],
    )
    monkeypatch.setattr(
        self_heal,
        "restart_component",
        lambda service: self_heal.HealResult(False, f"Failed to restart {service}", "exit 1"),
    )

    result = self_heal.heal_now(max_consecutive_restarts=5, backoff_seconds=30.0)

    event = result["healed"][0]
    evidence = event["evidence"]
    assert evidence["probe_type"] == "compose"
    assert evidence["state"] == "exited"
    assert evidence["health"] == "unhealthy"
    assert evidence["healthy_before"] is False
    assert evidence["container"] == "nyxgpt-web-1"
    assert evidence["manual"] is False
    assert evidence["restart_count_before"] == 0
    assert evidence["max_consecutive_restarts"] == 5
    assert evidence["backoff_seconds"] == 30.0
    assert evidence["heal_result_details"] == "exit 1"

    events = self_heal.recent_events()
    assert events[0]["evidence"]["probe_type"] == "compose"


@pytest.mark.unit
def test_heal_now_stamps_correlation_id_on_event_and_env(monkeypatch):
    """An autonomous heal (no CLI env var, no dashboard request) mints its
    own correlation id and sets it onto the process env, so the restart
    subprocess it drives (inherited env, no explicit env= anywhere) and the
    HealEvent it records can be joined after the fact (#3390, #3430)."""
    monkeypatch.delenv("NYXGPT_CORRELATION_ID", raising=False)
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

    event = result["healed"][0]
    assert event["correlation_id"]
    assert event["correlation_id"] == os.environ["NYXGPT_CORRELATION_ID"]


@pytest.mark.unit
def test_heal_now_reuses_ambient_correlation_id_when_present(monkeypatch):
    """A CLI-triggered heal (`nyxgpt self-heal heal`) already has
    NYXGPT_CORRELATION_ID set by the CLI dispatch -- the heal event must
    reuse that id rather than minting a new, disconnected one."""
    monkeypatch.setenv("NYXGPT_CORRELATION_ID", "cli-corr-id")
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

    event = result["healed"][0]
    assert event["correlation_id"] == "cli-corr-id"


@pytest.mark.unit
def test_heal_now_logs_evidence_extra(monkeypatch, caplog):
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

    with caplog.at_level("INFO", logger="nyxgpt.self_heal"):
        self_heal.heal_now()

    records = [r for r in caplog.records if "restart of web succeeded" in r.getMessage()]
    assert records
    assert records[0].evidence["state"] == "exited"


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
    _patch_survey(
        monkeypatch,
        [
            self_heal.ComponentStatus("api", "nyxgpt-api-1", "running", "healthy", True),
            self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False),
        ],
    )

    data = self_heal.status()

    assert data["enabled"] is True
    assert data["mode"] == "compose"
    assert data["unhealthy_count"] == 1
    assert len(data["components"]) == 2
    assert data["events"] == []
    assert "compose_probe_available" in data


@pytest.mark.unit
@pytest.mark.parametrize(
    "sources,expected",
    [
        ([], "none"),
        (["compose"], "compose"),
        (["native"], "native"),
        (["terraform"], "terraform"),
        (["kubernetes"], "kubernetes"),
        (["compose", "terraform"], "terraform"),
        (["native", "kubernetes"], "kubernetes"),
    ],
)
def test_detected_mode_prefers_terraform_then_kubernetes_then_compose_then_native(
    sources, expected
):
    components = [
        self_heal.ComponentStatus(
            "api",
            "c",
            "running",
            "healthy",
            True,
            source=source,
            # A Kubernetes row is named after a Pod and carries its tier in
            # `tier` (#3828); every other source names the component itself.
            tier="core" if source == "kubernetes" else "",
        )
        for source in sources
    ]
    assert self_heal.detected_mode(components) == expected


@pytest.mark.unit
def test_detected_mode_ignores_non_core_components():
    components = [
        self_heal.ComponentStatus("grafana", "c", "running", "healthy", True, source="compose"),
    ]
    assert self_heal.detected_mode(components) == "none"


@pytest.mark.unit
def test_detected_mode_names_kubernetes_from_pod_named_rows():
    # #3828: the page printed "Nothing detected running" directly above a list
    # of four running Pods, because no Pod name is in CORE_APP_SERVICES.
    components = [
        self_heal.ComponentStatus(
            pod, pod, "Running", "ready", True, source="kubernetes", tier="core"
        )
        for pod in (
            "nyxgpt-api-stable-abc123",
            "nyxgpt-web-stable-def456",
            "cassandra-0",
            "ollama-0",
        )
    ]
    assert self_heal.detected_mode(components) == "kubernetes"
    assert self_heal.kubernetes_mode_active(components) is True


@pytest.mark.unit
def test_detected_mode_ignores_kubernetes_observability_only():
    # `nyxgpt ops observability --kubernetes` can put that tier on a cluster
    # while the core stack runs natively -- that is not a Kubernetes
    # deployment of nyxGPT itself.
    components = [
        self_heal.ComponentStatus(
            "grafana-7f9",
            "grafana-7f9",
            "Running",
            "ready",
            True,
            source="kubernetes",
            tier="observability",
        ),
        self_heal.ComponentStatus("api", "nyxgpt-api", "started", "", True, source="native"),
    ]
    assert self_heal.detected_mode(components) == "native"
    assert self_heal.kubernetes_mode_active(components) is False


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
    assert (
        "self-heal: heal pass complete (checked=1, unhealthy=1, undetermined=0, healed=1"
        in caplog.text
    )

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
def test_heal_now_increments_giveup_metric_after_max_consecutive_restarts(monkeypatch):
    service = "giveup-metric-test-svc"
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [self_heal.ComponentStatus(service, f"nyxgpt-{service}-1", "exited", "", False)],
    )
    monkeypatch.setattr(
        self_heal,
        "restart_component",
        lambda svc: self_heal.HealResult(False, f"Failed to restart {svc}"),
    )

    for _ in range(2):
        self_heal.heal_now(max_consecutive_restarts=2, backoff_seconds=0.0)
    assert _metric_value("nyxgpt_selfheal_giveup_total", service=service) is None

    self_heal.heal_now(max_consecutive_restarts=2, backoff_seconds=0.0)
    assert _metric_value("nyxgpt_selfheal_giveup_total", service=service) == 1

    self_heal.heal_now(max_consecutive_restarts=2, backoff_seconds=0.0)
    assert _metric_value("nyxgpt_selfheal_giveup_total", service=service) == 2


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
    _patch_survey(
        monkeypatch,
        [
            self_heal.ComponentStatus("api", "nyxgpt-api-1", "running", "healthy", True),
            self_heal.ComponentStatus("web", "nyxgpt-web-1", "exited", "", False),
        ],
    )

    self_heal.status()

    assert _metric_value("nyxgpt_selfheal_unhealthy_components") == 1


@pytest.mark.unit
def test_watchdog_stop_logs(monkeypatch, caplog):
    # Disabled like every sibling watchdog test: left enabled, the loop thread
    # runs the *real* heal pass, which shells out to `systemctl restart
    # nyxgpt-api.service`. That outlives stop()'s join timeout and its
    # "Subprocess exited non-zero" record then lands in whichever later test
    # happens to hold caplog (it was reaching test_error_tracking.py).
    monkeypatch.setattr(self_heal, "is_enabled", lambda: False)

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
    # Tracing defaults to enabled (#3415 owner decision); every other
    # observability profile stays opt-in.
    monkeypatch.setattr(self_heal, "load_config", lambda: ConfigParser())
    assert self_heal._enabled_observability_profiles() == {"tracing"}


@pytest.mark.unit
def test_enabled_observability_profiles_missing_config_returns_empty(monkeypatch):
    def _boom():
        raise FileNotFoundError("no config.ini")

    monkeypatch.setattr(self_heal, "load_config", _boom)
    assert self_heal._enabled_observability_profiles() == set()


@pytest.mark.unit
def test_enabled_observability_profiles_log_aggregation_maps_to_logging_profile(monkeypatch):
    monkeypatch.setattr(self_heal, "load_config", lambda: _cfg(log_aggregation=True, tracing=False))
    assert self_heal._enabled_observability_profiles() == {"logging"}


@pytest.mark.unit
def test_enabled_observability_profiles_error_tracking_maps_to_errors_profile(monkeypatch):
    monkeypatch.setattr(self_heal, "load_config", lambda: _cfg(error_tracking=True, tracing=False))
    assert self_heal._enabled_observability_profiles() == {"errors"}


@pytest.mark.unit
def test_observability_services_resolves_enabled_profiles(monkeypatch):
    """`observability_services` is the public composition of the enabled
    profiles and their Compose services -- what `ops.up(--skip-observability)`
    excludes from its health-wait (#3508)."""
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"tracing"})
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(stdout="jaeger\notel-collector\napi\n"),
    )

    # `api` is a core app service and must not be excludable as observability.
    assert self_heal.observability_services() == {"jaeger", "otel-collector"}


@pytest.mark.unit
def test_observability_services_empty_when_no_profile_enabled(monkeypatch):
    """Nothing enabled -> nothing to exclude, and no Docker call to make it."""
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: set())
    run_mock = MagicMock()
    monkeypatch.setattr(self_heal, "_run", run_mock)

    assert self_heal.observability_services() == set()
    run_mock.assert_not_called()


@pytest.mark.unit
def test_observability_services_empty_when_docker_missing(monkeypatch):
    """Can't tell -> exclude nothing, so the caller waits on those components
    rather than silently ignoring what it couldn't account for."""
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_which", lambda _: None)

    assert self_heal.observability_services() == set()


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
        lambda cmd, timeout=30.0, **_k: CP(stdout="grafana\nprometheus\napi\nweb\n"),
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
        lambda cmd, timeout=30.0, **_k: CP(stdout="glitchtip\nglitchtip-migrate\n"),
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
        lambda cmd, timeout=30.0, **_k: CP(stdout="glitchtip\nglitchtip-migrate\n"),
    )
    services = self_heal._desired_compose_services({"errors"}, exclude_one_shot=False)
    assert services == {"glitchtip", "glitchtip-migrate"}


@pytest.mark.unit
def test_desired_compose_services_command_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(
        self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(returncode=1, stderr="boom")
    )
    assert self_heal._desired_compose_services({"monitoring"}) == set()


@pytest.mark.unit
def test_desired_compose_services_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0, **_k):
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
        lambda cmd, timeout=30.0, **_k: CP(stdout="glitchtip\nglitchtip-migrate\n"),
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
def test_record_health_check_sets_labeled_component_healthy_gauge():
    """#3575: the bare unhealthy count can't say which component -- a
    labeled per-service gauge must name it."""
    statuses = [
        self_heal.ComponentStatus("api", "c", "running", "healthy", True),
        self_heal.ComponentStatus("web", "c", "exited", "", False),
    ]
    self_heal._record_health_check(statuses)

    assert _metric_value("nyxgpt_selfheal_component_healthy", service="api") == 1
    assert _metric_value("nyxgpt_selfheal_component_healthy", service="web") == 0


@pytest.mark.unit
def test_record_health_check_component_healthy_gauge_treats_disabled_present_as_healthy():
    """A `desired=False` component (its observability profile is off) isn't
    counted toward `nyxgpt_selfheal_unhealthy_components` -- the labeled gauge
    must report the same "not alarming" verdict (1) for it, or the two series
    would disagree about the exact same component (#3575)."""
    statuses = [
        self_heal.ComponentStatus("grafana", "c", "exited", "", False, desired=False),
    ]
    self_heal._record_health_check(statuses)

    assert _metric_value("nyxgpt_selfheal_component_healthy", service="grafana") == 1


@pytest.mark.unit
def test_record_health_check_labeled_gauge_sum_matches_unhealthy_count():
    """Count-consistency guard (#3575 AC): the aggregate
    `nyxgpt_selfheal_unhealthy_components` must always equal the number of
    `nyxgpt_selfheal_component_healthy` series reporting 0 for these
    services, since both are derived from the same per-component verdict."""
    statuses = [
        self_heal.ComponentStatus("api", "c", "running", "healthy", True),
        self_heal.ComponentStatus("web", "c", "exited", "", False),
        self_heal.ComponentStatus("ollama", "c", "exited", "", False),
        self_heal.ComponentStatus("grafana", "c", "exited", "", False, desired=False),
    ]

    unhealthy_count = self_heal._record_health_check(statuses)

    labeled_unhealthy = sum(
        1
        for s in statuses
        if _metric_value("nyxgpt_selfheal_component_healthy", service=s.service) == 0
    )
    assert labeled_unhealthy == unhealthy_count == 2


@pytest.mark.unit
def test_record_health_check_updates_last_check_timestamp():
    before = time.time()
    self_heal._record_health_check(
        [self_heal.ComponentStatus("api", "c", "running", "healthy", True)]
    )
    after = time.time()

    last_check = _metric_value("nyxgpt_selfheal_last_check_timestamp")
    assert before <= last_check <= after


@pytest.mark.unit
def test_status_reports_zero_restart_count_and_not_giving_up_by_default(monkeypatch):
    _patch_survey(monkeypatch, [self_heal.ComponentStatus("web", "c", "running", "healthy", True)])

    data = self_heal.status()

    component = data["components"][0]
    assert component["restart_count"] == 0
    assert component["giving_up"] is False


@pytest.mark.unit
def test_status_reports_giving_up_once_restart_budget_exhausted(monkeypatch, tmp_path):
    """#3575: a component self-heal has given up on (exhausted
    max_consecutive_restarts) must look different from one it's still
    actively retrying -- otherwise an operator can't tell "will heal itself
    shortly" apart from "needs me to intervene"."""
    state_path = tmp_path / "self_heal_state.json"
    monkeypatch.setattr(self_heal, "_state_path", lambda: state_path)
    _patch_survey(
        monkeypatch,
        [self_heal.ComponentStatus("glitchtip-worker", "c", "running", "unhealthy", False)],
    )
    monkeypatch.setattr(
        self_heal, "get_watchdog", lambda: self_heal.Watchdog(max_consecutive_restarts=3)
    )
    self_heal._save_state({**self_heal._default_state(), "restart_counts": {"glitchtip-worker": 3}})

    data = self_heal.status()

    component = data["components"][0]
    assert component["restart_count"] == 3
    assert component["giving_up"] is True


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
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
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
    def _run_stub(cmd, timeout=30.0, **_k):
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
        lambda cmd, timeout=30.0, **_k: CP(stdout=_ps_line("grafana", state="running")),
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
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
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
        lambda cmd, timeout=30.0, **_k: CP(stdout=_ps_line("grafana", state="exited")),
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
    def _run_stub(cmd, timeout=30.0, **_k):
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
    def _boom(cmd, timeout=120.0, **_k):
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


# --- Intentional-stop registry (#3406) ---


@pytest.mark.unit
def test_is_intentionally_stopped_defaults_false():
    assert self_heal.is_intentionally_stopped("api") is False
    assert self_heal.list_intentionally_stopped() == []


@pytest.mark.unit
def test_mark_and_clear_intentionally_stopped_roundtrip():
    self_heal.mark_intentionally_stopped("api")
    assert self_heal.is_intentionally_stopped("api") is True
    assert self_heal.list_intentionally_stopped() == ["api"]

    self_heal.clear_intentionally_stopped("api")
    assert self_heal.is_intentionally_stopped("api") is False
    assert self_heal.list_intentionally_stopped() == []


@pytest.mark.unit
def test_mark_intentionally_stopped_is_idempotent_and_tracks_multiple():
    self_heal.mark_intentionally_stopped("api")
    self_heal.mark_intentionally_stopped("api")
    self_heal.mark_intentionally_stopped("web")

    assert self_heal.list_intentionally_stopped() == ["api", "web"]


@pytest.mark.unit
def test_clear_intentionally_stopped_never_marked_is_noop():
    self_heal.clear_intentionally_stopped("api")
    assert self_heal.list_intentionally_stopped() == []


@pytest.mark.unit
def test_list_component_status_marks_intentionally_stopped_component_desired_false(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {"nyxgpt-api": "stopped"})
    self_heal.mark_intentionally_stopped("api")

    statuses = self_heal.list_component_status()
    by_service = {s.service: s for s in statuses}

    assert by_service["api"].desired is False
    assert by_service["api"].healthy is False


@pytest.mark.unit
def test_list_component_status_leaves_other_components_desired_true(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    monkeypatch.setattr(
        self_heal,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "stopped", "nyxgpt-web": "started"},
    )
    self_heal.mark_intentionally_stopped("api")

    statuses = self_heal.list_component_status()
    by_service = {s.service: s for s in statuses}

    assert by_service["api"].desired is False
    assert by_service["web"].desired is True


@pytest.mark.unit
def test_heal_now_skips_intentionally_stopped_component_automatically(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {"nyxgpt-api": "stopped"})
    self_heal.mark_intentionally_stopped("api")
    restart_mock = MagicMock()
    monkeypatch.setattr(self_heal, "restart_native_component", restart_mock)

    result = self_heal.heal_now()

    restart_mock.assert_not_called()
    assert result["healed"] == []


@pytest.mark.unit
def test_heal_now_manual_heal_overrides_intentional_stop(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {"nyxgpt-api": "stopped"})
    self_heal.mark_intentionally_stopped("api")
    restart_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted api"))
    monkeypatch.setattr(self_heal, "restart_native_component", restart_mock)

    result = self_heal.heal_now(service="api")

    restart_mock.assert_called_once_with("api")
    assert result["healed"][0]["service"] == "api"


# --- Terraform mode (#3406) ---


@pytest.mark.unit
def test_native_container_health_reports_status(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout="healthy\n"))
    assert self_heal._native_container_health("nyxgpt-tf-api") == "healthy"


@pytest.mark.unit
def test_native_container_health_no_healthcheck_is_blank(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    assert self_heal._native_container_health("nyxgpt-tf-web") == ""


@pytest.mark.unit
def test_native_container_health_no_docker(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    assert self_heal._native_container_health("nyxgpt-tf-api") == ""


@pytest.mark.unit
def test_native_container_health_run_failure(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(returncode=1))
    assert self_heal._native_container_health("nyxgpt-tf-api") == ""


@pytest.mark.unit
def test_native_container_health_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=30.0, **_k):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(self_heal, "_run", _boom)
    with caplog.at_level("WARNING"):
        assert self_heal._native_container_health("nyxgpt-tf-api") == ""
    assert "failed to query docker health" in caplog.text


@pytest.mark.unit
def test_list_terraform_component_status_reports_running_and_healthy(monkeypatch):
    states = {
        "nyxgpt-tf-ollama": "running",
        "nyxgpt-tf-cassandra": "running",
        "nyxgpt-tf-api": "running",
        "nyxgpt-tf-web": "running",
    }
    healths = {"nyxgpt-tf-ollama": "healthy", "nyxgpt-tf-cassandra": "unhealthy"}
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: states[name])
    monkeypatch.setattr(self_heal, "_native_container_health", lambda name: healths.get(name, ""))

    statuses = _real_list_terraform_component_status()
    by_service = {s.service: s for s in statuses}

    assert by_service["ollama"].source == "terraform"
    assert by_service["ollama"].healthy is True
    assert by_service["cassandra"].healthy is False
    assert by_service["cassandra"].health == "unhealthy"
    # web has no Docker HEALTHCHECK in terraform/main.tf -- running is enough.
    assert by_service["web"].healthy is True
    assert by_service["web"].container == "nyxgpt-tf-web"


@pytest.mark.unit
def test_list_terraform_component_status_skips_absent_containers(monkeypatch):
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")

    assert _real_list_terraform_component_status() == []


@pytest.mark.unit
def test_list_terraform_component_status_reports_even_when_another_mode_also_present(
    monkeypatch,
):
    # Cross-mode exclusion is no longer this probe's job -- it always reports
    # every present Terraform container; `_resolve_core_component_conflicts`
    # (see list_component_status) is what decides which mode's entry wins
    # when more than one is present for the same component (#3428).
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "running")
    monkeypatch.setattr(self_heal, "_native_container_health", lambda name: "")

    statuses = _real_list_terraform_component_status()

    assert {s.service for s in statuses} == {"api", "web", "ollama", "cassandra"}
    assert all(s.source == "terraform" for s in statuses)


@pytest.mark.unit
def test_restart_terraform_component_known(monkeypatch):
    restart_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted nyxgpt-tf-api"))
    monkeypatch.setattr(self_heal, "_restart_native_container", restart_mock)

    result = self_heal.restart_terraform_component("api")

    restart_mock.assert_called_once_with("nyxgpt-tf-api")
    assert result.ok


@pytest.mark.unit
def test_restart_terraform_component_unknown():
    result = self_heal.restart_terraform_component("bogus")
    assert not result.ok
    assert "Unknown terraform component" in result.message


@pytest.mark.unit
def test_heal_now_dispatches_terraform_restart_for_terraform_source(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus(
                "api", "nyxgpt-tf-api", "exited", "", False, source="terraform"
            )
        ],
    )
    restart_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted nyxgpt-tf-api"))
    monkeypatch.setattr(self_heal, "restart_terraform_component", restart_mock)

    result = self_heal.heal_now()

    restart_mock.assert_called_once_with("api")
    assert result["healed"][0]["service"] == "api"


# --- Leftover-artifact shadowing across mode switches (#3428) ---
#
# Bug: `nyxgpt-cassandra` (native) is deliberately kept around, stopped, as
# the path back to native mode while Terraform is the active deployment (see
# the issue's owner-confirmation comment) -- but the old exclusion chain let
# whichever mode was checked *first* claim the "cassandra" row just because
# its container/service existed, regardless of whether it was actually
# running. These tests exercise `_resolve_core_component_conflicts` (via
# `list_component_status`) for every mode pairing, not just the one the
# owner hit.


@pytest.mark.unit
def test_resolve_core_component_conflicts_running_beats_stopped_leftover():
    running = self_heal.ComponentStatus(
        "cassandra", "nyxgpt-tf-cassandra", "running", "", True, source="terraform"
    )
    stopped_leftover = self_heal.ComponentStatus(
        "cassandra", "nyxgpt-cassandra", "exited", "", False, source="native"
    )

    resolved = self_heal._resolve_core_component_conflicts([stopped_leftover, running])

    assert len(resolved) == 1
    winner = resolved[0]
    assert winner.source == "terraform"
    assert winner.healthy is True
    assert "inert native leftover" in winner.note
    assert "nyxgpt-cassandra" in winner.note
    assert "exited" in winner.note


@pytest.mark.unit
def test_resolve_core_component_conflicts_ties_break_by_priority_when_none_running():
    native_stopped = self_heal.ComponentStatus(
        "cassandra", "nyxgpt-cassandra", "exited", "", False, source="native"
    )
    terraform_stopped = self_heal.ComponentStatus(
        "cassandra", "nyxgpt-tf-cassandra", "exited", "", False, source="terraform"
    )

    resolved = self_heal._resolve_core_component_conflicts([terraform_stopped, native_stopped])

    assert len(resolved) == 1
    # Neither is running -- historical compose > native > terraform priority
    # decides, so native still outranks terraform here, preserving prior
    # behavior for the fully-down case.
    assert resolved[0].source == "native"
    assert "inert terraform leftover" in resolved[0].note


@pytest.mark.unit
def test_resolve_core_component_conflicts_single_candidate_passes_through_unchanged():
    only = self_heal.ComponentStatus(
        "web", "nyxgpt-tf-web", "running", "", True, source="terraform"
    )
    resolved = self_heal._resolve_core_component_conflicts([only])
    assert resolved == [only]
    assert resolved[0].note == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    "active_mode,leftover_mode",
    [
        ("terraform", "native"),  # the exact scenario reported in #3428
        ("native", "terraform"),
        ("compose", "native"),
        ("compose", "terraform"),
        ("native", "compose"),
        ("terraform", "compose"),
    ],
)
def test_cassandra_active_mode_wins_over_stopped_leftover_from_other_mode(
    monkeypatch, active_mode, leftover_mode
):
    """Whichever mode actually has Cassandra running claims the row; a
    stopped leftover from an inactive mode never claims it (or even appears
    as a second row) instead."""
    monkeypatch.setattr(
        self_heal, "_list_terraform_component_status", _real_list_terraform_component_status
    )
    monkeypatch.setattr(self_heal, "_native_container_health", lambda name: "")
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})

    docker_states = {"nyxgpt-cassandra": "absent", "nyxgpt-tf-cassandra": "absent"}
    compose_present = {"present": False, "state": "absent"}

    for mode, running in ((active_mode, True), (leftover_mode, False)):
        state = "running" if running else "exited"
        if mode == "native":
            docker_states["nyxgpt-cassandra"] = state
        elif mode == "terraform":
            docker_states["nyxgpt-tf-cassandra"] = state
        elif mode == "compose":
            compose_present["present"] = True
            compose_present["state"] = state

    monkeypatch.setattr(
        self_heal, "_native_container_state", lambda name: docker_states.get(name, "absent")
    )
    stdout = (
        _ps_line("cassandra", state=compose_present["state"]) if compose_present["present"] else ""
    )
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=stdout))

    statuses = self_heal.list_component_status()
    cassandra_entries = [s for s in statuses if s.service == "cassandra"]

    assert len(cassandra_entries) == 1, "leftover must not appear as a separate row"
    winner = cassandra_entries[0]
    assert winner.source == active_mode
    assert winner.healthy is True
    assert winner.state == "running"
    assert leftover_mode in winner.note
    assert "inert" in winner.note


@pytest.mark.unit
def test_heal_now_auto_pass_heals_terraform_cassandra_not_native_leftover(monkeypatch):
    """#3428 regression at the desired-state level: an unhealthy Terraform
    Cassandra (health check failing, container still running) must be what
    auto-heal restarts, even with a stopped native leftover present -- never
    the leftover, and never both (no flapping between the two)."""
    monkeypatch.setattr(
        self_heal, "_list_terraform_component_status", _real_list_terraform_component_status
    )
    monkeypatch.setattr(self_heal, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))

    docker_states = {"nyxgpt-cassandra": "exited", "nyxgpt-tf-cassandra": "running"}
    healths = {"nyxgpt-tf-cassandra": "unhealthy"}
    monkeypatch.setattr(
        self_heal, "_native_container_state", lambda name: docker_states.get(name, "absent")
    )
    monkeypatch.setattr(self_heal, "_native_container_health", lambda name: healths.get(name, ""))

    terraform_restart = MagicMock(return_value=self_heal.HealResult(True, "Restarted"))
    native_restart = MagicMock(return_value=self_heal.HealResult(True, "Restarted"))
    monkeypatch.setattr(self_heal, "restart_terraform_component", terraform_restart)
    monkeypatch.setattr(self_heal, "restart_native_component", native_restart)

    first = self_heal.heal_now()
    second = self_heal.heal_now()

    terraform_restart.assert_called_once_with("cassandra")
    native_restart.assert_not_called()
    assert [e["service"] for e in first["healed"]] == ["cassandra"]
    # Second pass (immediately after, well inside the backoff window) must
    # not flap onto the native leftover -- it just backs off, same target.
    assert second["healed"] == []


@pytest.mark.unit
def test_restart_native_component_cassandra_refuses_when_terraform_running(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_native_container_state",
        lambda name: "running" if name == "nyxgpt-tf-cassandra" else "absent",
    )
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    restart_mock = MagicMock()
    monkeypatch.setattr(self_heal, "_restart_native_container", restart_mock)

    result = self_heal.restart_native_component("cassandra")

    assert not result.ok
    assert "Refused to restart native Cassandra" in result.message
    assert "nyxgpt-tf-cassandra" in result.details
    restart_mock.assert_not_called()


@pytest.mark.unit
def test_restart_native_component_cassandra_refuses_when_compose_running(monkeypatch):
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(stdout=_ps_line("cassandra", state="running")),
    )
    restart_mock = MagicMock()
    monkeypatch.setattr(self_heal, "_restart_native_container", restart_mock)

    result = self_heal.restart_native_component("cassandra")

    assert not result.ok
    assert "Refused to restart native Cassandra" in result.message
    restart_mock.assert_not_called()


@pytest.mark.unit
def test_restart_native_component_cassandra_proceeds_when_nothing_else_running(monkeypatch):
    monkeypatch.setattr(self_heal, "_native_container_state", lambda name: "absent")
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    restart_mock = MagicMock(return_value=self_heal.HealResult(True, "Restarted nyxgpt-cassandra"))
    monkeypatch.setattr(self_heal, "_restart_native_container", restart_mock)

    result = self_heal.restart_native_component("cassandra")

    assert result.ok
    restart_mock.assert_called_once_with(self_heal.NATIVE_CASSANDRA_CONTAINER)


@pytest.mark.unit
def test_heal_now_records_refused_cassandra_collision_as_heal_event(monkeypatch):
    """The refusal must be a recorded heal-event outcome (ok=False), not a
    silent skip -- so an operator sees *why* nothing happened."""
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus(
                "cassandra", "nyxgpt-cassandra", "exited", "", False, source="native"
            )
        ],
    )
    monkeypatch.setattr(
        self_heal,
        "_cassandra_active_elsewhere",
        lambda: "nyxgpt-tf-cassandra",
    )

    result = self_heal.heal_now(service="cassandra")

    assert len(result["healed"]) == 1
    event = result["healed"][0]
    assert event["service"] == "cassandra"
    assert event["ok"] is False
    assert "Refused" in event["message"]


# --- Kubernetes mode (#3406) ---


def _k8s_pods_json(*pods):
    return json.dumps({"items": list(pods)})


def _k8s_pod(
    name,
    *,
    phase="Running",
    ready=True,
    unschedulable=False,
    owner=None,
    waiting=None,
    labels=None,
    deleting=False,
):
    """One Pod object shaped like `kubectl get pod -o json` returns it.

    `unschedulable=True` adds the `PodScheduled=False`/`Unschedulable`
    condition a Pod carries when the scheduler could not place it -- the
    #3832 shape. `owner` adds the ReplicaSet ownerReference that gives the Pod
    a heal identity surviving its own recreation. `labels` defaults to the
    api pool's so a plain Pod passes the #3828 core-tier filter; `deleting`
    stamps the `deletionTimestamp` of a Pod already on its way out.
    """
    conditions = [{"type": "Ready", "status": "True" if ready else "False"}]
    if unschedulable:
        conditions.append(
            {
                "type": "PodScheduled",
                "status": "False",
                "reason": "Unschedulable",
                "message": "0/1 nodes are available: 1 Insufficient memory.",
            }
        )
    metadata = {
        "name": name,
        "labels": {"app": "nyxgpt-api-canary-pool"} if labels is None else labels,
    }
    if owner is not None:
        metadata["ownerReferences"] = [{"kind": "ReplicaSet", "name": owner}]
    if deleting:
        metadata["deletionTimestamp"] = "2026-08-18T10:00:00Z"
    status = {"phase": phase, "conditions": conditions}
    if waiting is not None:
        status["containerStatuses"] = [{"state": {"waiting": {"reason": waiting}}}]
    return {"metadata": metadata, "status": status}


def _k8s_heal_run(pod, *, delete=None):
    """`_run` stub for `heal_kubernetes_pod`: answers the pre-delete state read.

    #3832 made the delete conditional on re-reading the Pod, so a stub has to
    answer both calls -- `kubectl get pod -o json` with `pod`, then the delete
    with `delete` (default success).
    """
    delete_result = delete if delete is not None else CP(returncode=0)

    def _run(cmd, timeout=30.0, **_k):
        if cmd[1] == "get":
            return CP(stdout=json.dumps(pod))
        return delete_result

    return _run


@pytest.mark.unit
def test_list_kubernetes_component_status_parses_pods(monkeypatch):
    stdout = _k8s_pods_json(
        _k8s_pod("nyxgpt-api-blue-abc"),
        _k8s_pod("nyxgpt-api-stable-def", ready=False),
    )
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=15.0, **_k: CP(stdout=stdout))

    statuses = _real_list_kubernetes_component_status(set())
    by_service = {s.service: s for s in statuses}

    assert by_service["nyxgpt-api-blue-abc"].source == "kubernetes"
    assert by_service["nyxgpt-api-blue-abc"].healthy is True
    assert by_service["nyxgpt-api-stable-def"].healthy is False
    assert by_service["nyxgpt-api-stable-def"].health == "not-ready"


@pytest.mark.unit
def test_list_kubernetes_component_status_covers_every_core_tier(monkeypatch):
    # #3828: the probe selected `app=nyxgpt-api-canary-pool`, so web,
    # Cassandra and Ollama were watched by nothing at all in Kubernetes mode.
    stdout = _k8s_pods_json(
        _k8s_pod("nyxgpt-api-stable-abc", labels={"app": "nyxgpt-api-canary-pool"}),
        _k8s_pod("nyxgpt-web-stable-def", labels={"app": "nyxgpt-web-canary-pool"}),
        _k8s_pod("cassandra-0", labels={"app": "cassandra", "tier": "data"}),
        _k8s_pod("ollama-0", labels={"app": "ollama", "tier": "llm"}, ready=False),
    )
    captured = {}

    def _capture(cmd, timeout=15.0, **_k):
        captured["cmd"] = cmd
        return CP(stdout=stdout)

    monkeypatch.setattr(self_heal, "_run", _capture)

    statuses = _real_list_kubernetes_component_status(set())
    by_service = {s.service: s for s in statuses}

    assert set(by_service) == {
        "nyxgpt-api-stable-abc",
        "nyxgpt-web-stable-def",
        "cassandra-0",
        "ollama-0",
    }
    assert all(s.tier == "core" for s in statuses)
    assert by_service["ollama-0"].healthy is False
    # One namespace-wide query, no api-only selector.
    assert "-l" not in captured["cmd"]


@pytest.mark.unit
def test_list_kubernetes_component_status_covers_in_cluster_observability(monkeypatch):
    stdout = _k8s_pods_json(
        _k8s_pod("grafana-7f9", labels={"app": "grafana", "tier": "observability"}),
        _k8s_pod("promtail-x2k", labels={"app": "promtail", "tier": "observability"}, ready=False),
    )
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=15.0, **_k: CP(stdout=stdout))

    statuses = _real_list_kubernetes_component_status(set())

    assert [s.tier for s in statuses] == ["observability", "observability"]
    assert [s.healthy for s in statuses] == [True, False]


@pytest.mark.unit
def test_list_kubernetes_component_status_ignores_foreign_and_terminating_pods(monkeypatch):
    stdout = _k8s_pods_json(
        # Someone else's workload in the namespace: never a heal target.
        _k8s_pod("random-job-abc", labels={"app": "someone-elses-job"}),
        _k8s_pod("no-labels-at-all", labels={}),
        # Already being replaced -- healing it would burn a restart attempt on
        # a deletion that has already happened.
        _k8s_pod("nyxgpt-api-stable-old", deleting=True, ready=False),
    )
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=15.0, **_k: CP(stdout=stdout))

    assert _real_list_kubernetes_component_status(set()) == []


@pytest.mark.unit
def test_list_kubernetes_component_status_not_running_phase_is_unhealthy(monkeypatch):
    stdout = _k8s_pods_json(_k8s_pod("nyxgpt-api-canary-xyz", phase="Pending", ready=False))
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=15.0, **_k: CP(stdout=stdout))

    statuses = _real_list_kubernetes_component_status(set())

    assert statuses[0].healthy is False
    assert statuses[0].state == "Pending"


@pytest.mark.unit
def test_list_kubernetes_component_status_skips_already_managed(monkeypatch):
    stdout = _k8s_pods_json(_k8s_pod("nyxgpt-api-blue-abc"))
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=15.0, **_k: CP(stdout=stdout))

    statuses = _real_list_kubernetes_component_status({"nyxgpt-api-blue-abc"})

    assert statuses == []


@pytest.mark.unit
def test_list_kubernetes_component_status_no_kubectl(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    assert _real_list_kubernetes_component_status(set()) == []


@pytest.mark.unit
def test_list_kubernetes_component_status_run_failure(monkeypatch):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=15.0, **_k: CP(returncode=1))
    assert _real_list_kubernetes_component_status(set()) == []


@pytest.mark.unit
def test_list_kubernetes_component_status_run_raises(monkeypatch, caplog):
    def _boom(cmd, timeout=15.0, **_k):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(self_heal, "_run", _boom)
    with caplog.at_level("WARNING"):
        assert _real_list_kubernetes_component_status(set()) == []
    assert "failed to query kubernetes pods" in caplog.text


@pytest.mark.unit
def test_list_kubernetes_component_status_invalid_json(monkeypatch, caplog):
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=15.0, **_k: CP(stdout="not json"))
    with caplog.at_level("WARNING"):
        assert _real_list_kubernetes_component_status(set()) == []
    assert "failed to parse kubectl get pods output" in caplog.text


@pytest.mark.unit
def test_component_survey_reads_observability_in_cluster_in_kubernetes_mode(monkeypatch):
    # #3828: in Kubernetes mode the page showed the Compose-specific
    # "cannot determine from here" banner over a screen of `unknown`
    # observability rows, while that tier was running in-cluster and
    # queryable all along (#3787).
    monkeypatch.setattr(
        self_heal,
        "compose_probe",
        lambda: self_heal.ComposeProbe(available=False, reason="`docker` is not installed here"),
    )
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda profiles: {"grafana"})
    monkeypatch.setattr(
        self_heal,
        "_list_kubernetes_component_status",
        lambda already_managed: [
            self_heal.ComponentStatus(
                "nyxgpt-api-stable-abc",
                "nyxgpt-api-stable-abc",
                "Running",
                "ready",
                True,
                source="kubernetes",
                tier="core",
            ),
            self_heal.ComponentStatus(
                "grafana-7f9",
                "grafana-7f9",
                "Running",
                "ready",
                True,
                source="kubernetes",
                tier="observability",
            ),
        ],
    )

    survey = self_heal.component_survey()
    by_service = {s.service: s for s in survey.components}

    # The Compose placeholder for grafana is gone; the in-cluster Pod is the
    # observability tier's row.
    assert "grafana" not in by_service
    assert by_service["grafana-7f9"].healthy is True
    assert all(s.known for s in survey.components)


@pytest.mark.unit
def test_component_survey_keeps_compose_placeholders_outside_kubernetes_mode(monkeypatch):
    # The suppression above is keyed on a core tier running as Pods -- an
    # observability-only cluster must not blind the Compose survey.
    monkeypatch.setattr(
        self_heal,
        "compose_probe",
        lambda: self_heal.ComposeProbe(available=False, reason="`docker` is not installed here"),
    )
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda profiles: {"grafana"})
    monkeypatch.setattr(
        self_heal,
        "_list_kubernetes_component_status",
        lambda already_managed: [
            self_heal.ComponentStatus(
                "grafana-7f9",
                "grafana-7f9",
                "Running",
                "ready",
                True,
                source="kubernetes",
                tier="observability",
            ),
        ],
    )

    by_service = {s.service: s for s in self_heal.component_survey().components}

    assert by_service["grafana"].known is False
    assert by_service["grafana"].state == "unknown"


@pytest.mark.unit
def test_status_reports_kubernetes_as_the_observability_source(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "component_survey",
        lambda: self_heal.ComponentSurvey(
            components=[
                self_heal.ComponentStatus(
                    "nyxgpt-api-stable-abc",
                    "nyxgpt-api-stable-abc",
                    "Running",
                    "ready",
                    True,
                    source="kubernetes",
                    tier="core",
                )
            ],
            compose_probe=self_heal.ComposeProbe(available=False, reason="no docker here"),
        ),
    )

    payload = self_heal.status()

    assert payload["mode"] == "kubernetes"
    assert payload["observability_source"] == "kubernetes"
    assert payload["components"][0]["tier"] == "core"


@pytest.mark.unit
def test_status_reports_compose_as_the_observability_source_elsewhere(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "component_survey",
        lambda: self_heal.ComponentSurvey(
            components=[
                self_heal.ComponentStatus("api", "nyxgpt-api", "started", "", True, source="native")
            ],
            compose_probe=self_heal.ComposeProbe(available=True),
        ),
    )

    assert self_heal.status()["observability_source"] == "compose"


@pytest.mark.unit
def test_heal_now_heals_every_kubernetes_tier_not_just_the_api_pool(monkeypatch):
    # The coverage half of #3828: an unhealthy web/Cassandra/Ollama/Grafana
    # Pod is a heal target in Kubernetes mode, exactly like an api Pod.
    unhealthy = [
        self_heal.ComponentStatus(
            pod, pod, "Running", "not-ready", False, source="kubernetes", tier=tier
        )
        for pod, tier in (
            ("nyxgpt-web-stable-def", "core"),
            ("cassandra-0", "core"),
            ("ollama-0", "core"),
            ("grafana-7f9", "observability"),
        )
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: unhealthy)
    heal_mock = MagicMock(return_value=self_heal.HealResult(True, "Deleted pod"))
    monkeypatch.setattr(self_heal, "heal_kubernetes_pod", heal_mock)

    result = self_heal.heal_now()

    assert [c.args[0] for c in heal_mock.call_args_list] == [
        "nyxgpt-web-stable-def",
        "cassandra-0",
        "ollama-0",
        "grafana-7f9",
    ]
    assert len(result["healed"]) == 4


@pytest.mark.unit
def test_heal_kubernetes_pod_success(monkeypatch):
    calls = []

    def _run(cmd, timeout=30.0, **_k):
        calls.append(cmd)
        if cmd[1] == "get":
            return CP(stdout=json.dumps(_k8s_pod("nyxgpt-api-blue-abc", ready=False)))
        return CP(returncode=0)

    monkeypatch.setattr(self_heal, "_run", _run)

    result = self_heal.heal_kubernetes_pod("nyxgpt-api-blue-abc")

    assert result.ok
    assert "Deleted pod nyxgpt-api-blue-abc" in result.message
    assert calls[-1] == ["kubectl", "delete", "pod", "nyxgpt-api-blue-abc", "-n", "nyxgpt"]


@pytest.mark.unit
def test_heal_kubernetes_pod_failure(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "_run",
        _k8s_heal_run(
            _k8s_pod("nyxgpt-api-blue-abc", ready=False),
            delete=CP(returncode=1, stderr="boom"),
        ),
    )
    result = self_heal.heal_kubernetes_pod("nyxgpt-api-blue-abc")
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_heal_kubernetes_pod_run_raises(monkeypatch):
    def _boom(cmd, timeout=60.0, **_k):
        if cmd[1] == "get":
            return CP(stdout=json.dumps(_k8s_pod("nyxgpt-api-blue-abc", ready=False)))
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

    monkeypatch.setattr(self_heal, "_run", _boom)
    result = self_heal.heal_kubernetes_pod("nyxgpt-api-blue-abc")
    assert not result.ok
    assert "TimeoutExpired" in result.details


@pytest.mark.unit
def test_heal_kubernetes_pod_no_kubectl(monkeypatch):
    monkeypatch.setattr(self_heal, "_which", lambda _: None)
    result = self_heal.heal_kubernetes_pod("nyxgpt-api-blue-abc")
    assert not result.ok
    assert "kubectl not found" in result.message


@pytest.mark.unit
def test_heal_now_dispatches_kubernetes_heal_for_kubernetes_source(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus(
                "nyxgpt-api-blue-abc",
                "nyxgpt-api-blue-abc",
                "Running",
                "not-ready",
                False,
                source="kubernetes",
            )
        ],
    )
    heal_mock = MagicMock(return_value=self_heal.HealResult(True, "Deleted pod"))
    monkeypatch.setattr(self_heal, "heal_kubernetes_pod", heal_mock)

    result = self_heal.heal_now()

    heal_mock.assert_called_once_with("nyxgpt-api-blue-abc")
    assert result["healed"][0]["service"] == "nyxgpt-api-blue-abc"


# ---------------------------------------------------------------------------
# #3832: self-heal deleted an unschedulable Pod every 15 seconds forever --
# seven Pods in 4.5 minutes on the owner's local k8s round, each
# `FailedScheduling: Insufficient memory`. Deleting a Pending Pod is never a
# remedy (there is no stuck container to recover and deletion cannot create
# capacity), and every deletion reset the Pod age the operator needed to see
# the real cause. The loop erased its own evidence.
# ---------------------------------------------------------------------------


def _k8s_only_run(pods_json, recorder):
    """`_run` stub for a full `heal_now` pass in Kubernetes mode.

    Answers `kubectl get pods` with `pods_json`, `kubectl get pod` with the
    matching single Pod, and records every command so a test can assert on
    what self-heal actually *ran* -- "zero delete calls" is a claim about
    commands, not about return values. Everything else (the Compose probe)
    answers empty.
    """

    def _run(cmd, timeout=30.0, **_k):
        recorder.append(cmd)
        if cmd[0] != "kubectl":
            return CP(stdout="")
        if cmd[1] == "get" and cmd[2] == "pod":
            wanted = cmd[3]
            for pod in json.loads(pods_json)["items"]:
                if pod["metadata"]["name"] == wanted:
                    return CP(stdout=json.dumps(pod))
            return CP(returncode=1)
        if cmd[1] == "get":
            return CP(stdout=pods_json)
        return CP(returncode=0)

    return _run


@pytest.fixture
def k8s_live(monkeypatch):
    """Run the real Kubernetes probe (the autouse fixture stubs it out)."""
    monkeypatch.setattr(
        self_heal, "_list_kubernetes_component_status", _real_list_kubernetes_component_status
    )


@pytest.mark.unit
def test_unschedulable_pod_is_reported_with_its_scheduling_reason(monkeypatch, k8s_live):
    """The operator must be able to read WHY, from the status row itself."""
    pods = _k8s_pods_json(
        _k8s_pod("nyxgpt-api-stable-r56wb", phase="Pending", ready=False, unschedulable=True)
    )
    monkeypatch.setattr(self_heal, "_run", _k8s_only_run(pods, []))

    status = _real_list_kubernetes_component_status(set())[0]

    assert status.healthy is False
    assert status.state == "Pending"
    assert status.health == "unschedulable"
    assert status.healable is False
    assert "Insufficient memory" in status.note
    assert "deleting a Pod cannot create capacity" in status.note


@pytest.mark.unit
def test_unschedulable_pod_produces_zero_delete_calls(monkeypatch, k8s_live):
    """The regression test #3832 asks for: no `kubectl delete pod`, ever.

    Twenty automatic passes -- more than the watchdog ran in the 4.5 minutes
    that destroyed seven Pods -- against a Pod the scheduler refused.
    """
    pods = _k8s_pods_json(
        _k8s_pod(
            "nyxgpt-api-stable-r56wb",
            phase="Pending",
            ready=False,
            unschedulable=True,
            owner="nyxgpt-api-stable-7d9c",
        )
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(self_heal, "_run", _k8s_only_run(pods, commands))

    for _ in range(20):
        result = self_heal.heal_now(backoff_seconds=0.0)

    assert result["healed"] == []
    assert not any("delete" in cmd for cmd in commands), commands


@pytest.mark.unit
def test_pending_pod_that_is_merely_starting_is_not_deleted_either(monkeypatch, k8s_live):
    """A Pod pulling its image resolves itself; acting on it only restarts the clock."""
    pods = _k8s_pods_json(
        _k8s_pod("nyxgpt-api-canary-xyz", phase="Pending", ready=False, waiting="ContainerCreating")
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(self_heal, "_run", _k8s_only_run(pods, commands))

    status = _real_list_kubernetes_component_status(set())[0]
    result = self_heal.heal_now(backoff_seconds=0.0)

    assert status.health == "starting"
    assert status.healable is False
    assert "still starting" in status.note
    assert result["healed"] == []
    assert not any("delete" in cmd for cmd in commands), commands


@pytest.mark.unit
def test_manual_heal_of_an_unschedulable_pod_is_refused_and_recorded(monkeypatch, k8s_live):
    """An operator clicking "Heal now" gets the reason back, not a deletion.

    This guard is the one exception to "manual overrides everything": the
    action is not a repair for an operator either, and taking it would reset
    the Pod age they are looking at.
    """
    pods = _k8s_pods_json(
        _k8s_pod("nyxgpt-api-stable-r56wb", phase="Pending", ready=False, unschedulable=True)
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(self_heal, "_run", _k8s_only_run(pods, commands))

    result = self_heal.heal_now(service="nyxgpt-api-stable-r56wb")

    assert not any("delete" in cmd for cmd in commands), commands
    assert len(result["healed"]) == 1
    event = result["healed"][0]
    assert event["ok"] is False
    assert event["action"] == "refused"
    assert "Insufficient memory" in event["message"]


@pytest.mark.unit
def test_running_but_not_ready_pod_is_still_deleted(monkeypatch, k8s_live):
    """The one state deletion repairs must keep working (no over-correction)."""
    pods = _k8s_pods_json(_k8s_pod("nyxgpt-api-stable-abc", ready=False))
    commands: list[list[str]] = []
    monkeypatch.setattr(self_heal, "_run", _k8s_only_run(pods, commands))

    result = self_heal.heal_now(backoff_seconds=0.0)

    assert ["kubectl", "delete", "pod", "nyxgpt-api-stable-abc", "-n", "nyxgpt"] in commands
    assert result["healed"][0]["ok"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "pod",
    [
        _k8s_pod("nyxgpt-api-stable-r56wb", phase="Pending", ready=False, unschedulable=True),
        _k8s_pod("nyxgpt-api-stable-r56wb", phase="Pending", ready=False),
        _k8s_pod("nyxgpt-api-stable-r56wb", phase="Failed", ready=False),
    ],
)
def test_heal_kubernetes_pod_refuses_anything_not_running(monkeypatch, pod):
    """The guard lives at the destructive action, not only at its caller.

    `heal_kubernetes_pod` is public and reachable from the dashboard, so a
    future caller that has not consulted a survey must not be able to
    reintroduce the loop.
    """
    commands: list[list[str]] = []
    monkeypatch.setattr(self_heal, "_run", _k8s_only_run(json.dumps({"items": [pod]}), commands))

    result = self_heal.heal_kubernetes_pod("nyxgpt-api-stable-r56wb")

    assert result.ok is False
    assert "Refused to delete" in result.message
    assert not any("delete" in cmd for cmd in commands), commands


@pytest.mark.unit
def test_heal_kubernetes_pod_refuses_when_the_state_cannot_be_read(monkeypatch):
    """An unread state is not a reason to destroy a Pod (#3812's rule, applied to a delete)."""
    commands: list[list[str]] = []

    def _run(cmd, timeout=30.0, **_k):
        commands.append(cmd)
        return CP(returncode=1, stderr="the server could not find the requested resource")

    monkeypatch.setattr(self_heal, "_run", _run)

    result = self_heal.heal_kubernetes_pod("nyxgpt-api-stable-r56wb")

    assert result.ok is False
    assert "refusing to delete it blind" in result.message
    assert not any("delete" in cmd for cmd in commands), commands


@pytest.mark.unit
def test_heal_budget_is_keyed_on_the_owning_replicaset_not_the_pod_name(monkeypatch, k8s_live):
    """Healing REPLACES a Pod, so a per-name budget is a budget that never runs out.

    The Pod is Running-but-not-ready (so deletion is a legitimate remedy) and
    comes back under a new name each pass, exactly as a ReplicaSet recreates
    it. Keyed by name, the consecutive-restart cap of 3 could never fire --
    that is why #3832's cap of 5 did not stop seven deletions.
    """
    names = iter(f"nyxgpt-api-stable-{suffix}" for suffix in ("r56wb", "r9sw9", "znhm9", "jlhh8"))
    current = {"name": next(names)}

    def _run(cmd, timeout=30.0, **_k):
        if cmd[0] != "kubectl":
            return CP(stdout="")
        pod = _k8s_pod(current["name"], ready=False, owner="nyxgpt-api-stable-7d9c")
        if cmd[1] == "get" and cmd[2] == "pod":
            return CP(stdout=json.dumps(pod))
        if cmd[1] == "get":
            return CP(stdout=json.dumps({"items": [pod]}))
        current["name"] = next(names, current["name"])  # the ReplicaSet recreates it
        return CP(returncode=0)

    monkeypatch.setattr(self_heal, "_run", _run)

    deletes = 0
    for _ in range(6):
        deletes += len(
            self_heal.heal_now(max_consecutive_restarts=3, backoff_seconds=0.0)["healed"]
        )

    assert deletes == 3


@pytest.mark.unit
def test_rolling_attempt_cap_survives_a_health_flicker(monkeypatch):
    """The consecutive counter resets on any glimpse of health; the window cap does not.

    A component that comes up healthy just long enough to zero the counter and
    then fails again would otherwise be restarted forever. Two heals per
    window is the cap here, and the flicker in between must not buy a third.
    """
    healthy = {"value": False}

    def _components():
        return [
            self_heal.ComponentStatus(
                "flapper", "nyxgpt-flapper-1", "running", "", healthy["value"]
            )
        ]

    monkeypatch.setattr(self_heal, "list_component_status", _components)
    monkeypatch.setattr(
        self_heal, "restart_component", lambda service: self_heal.HealResult(True, "restarted")
    )

    attempts = 0
    for i in range(6):
        healthy["value"] = i == 2  # one healthy pass in the middle
        attempts += len(
            self_heal.heal_now(
                max_consecutive_restarts=5, backoff_seconds=0.0, max_attempts_per_window=2
            )["healed"]
        )

    assert attempts == 2


@pytest.mark.unit
def test_expired_heal_attempts_leave_the_state_file(monkeypatch):
    """The window is rolling, and its bookkeeping does not accumulate forever."""
    now = time.time()
    state = {"heal_attempts": {"old": [now - 5000.0], "recent": [now - 10.0], "junk": "nope"}}

    pruned = self_heal._prune_heal_attempts(state, now, 900.0)

    assert pruned == {"recent": [now - 10.0]}
    assert state["heal_attempts"] == pruned


@pytest.mark.unit
def test_status_does_not_claim_to_have_given_up_on_a_pod_it_never_touched(monkeypatch):
    """ "Gave up after N restarts" is false for a component nothing was tried on."""
    _patch_survey(
        monkeypatch,
        [
            self_heal.ComponentStatus(
                "nyxgpt-api-stable-r56wb",
                "nyxgpt-api-stable-r56wb",
                "Pending",
                "unschedulable",
                False,
                source="kubernetes",
                healable=False,
                heal_key="kubernetes/replicaset/nyxgpt-api-stable-7d9c",
                note="Pending (Unschedulable): 0/1 nodes are available",
            )
        ],
    )

    component = self_heal.status()["components"][0]

    assert component["giving_up"] is False
    assert component["healable"] is False
    assert component["heal_key"] == "kubernetes/replicaset/nyxgpt-api-stable-7d9c"
    assert "Unschedulable" in component["note"]


# ---------------------------------------------------------------------------
# #3812: an unqueryable Compose probe must never be rendered as a definite
# negative. Owner acceptance on the rc12 cloud install saw the Self-Heal panel
# report "11 unhealthy" with every observability component `absent` while all
# eleven containers were up and healthy: `docker` was on PATH and the compose
# file was present (so the "can I check?" flag said yes), but the systemd
# --user session predated the ec2-user docker-group change, so every
# `docker compose ps` exited 125 against an unreachable daemon.
# ---------------------------------------------------------------------------


_PERMISSION_DENIED_STDERR = (
    "permission denied while trying to connect to the Docker daemon socket at "
    "unix:///var/run/docker.sock: Get "
    '"http://%2Fvar%2Frun%2Fdocker.sock/v1.47/containers/json": dial unix '
    "/var/run/docker.sock: connect: permission denied"
)


def _unreachable_daemon(monkeypatch, tmp_path, *, returncode=125):
    """Docker installed, compose file present, daemon unreachable -- #3812's condition."""
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", compose_file)
    monkeypatch.setattr(self_heal, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(
        self_heal,
        "_run",
        lambda cmd, timeout=30.0, **_k: CP(returncode=returncode, stderr=_PERMISSION_DENIED_STDERR),
    )
    return compose_file


@pytest.mark.unit
def test_compose_probe_unavailable_when_ps_fails_despite_binary_and_file(monkeypatch, tmp_path):
    # The core inversion: availability is now the fact that the survey ran,
    # not the fact that a binary and a file exist. Both existed here.
    compose_file = _unreachable_daemon(monkeypatch, tmp_path)
    assert self_heal._which("docker") is not None and compose_file.exists()

    probe = self_heal.compose_probe()

    assert probe.available is False
    assert probe.statuses == ()
    assert self_heal.compose_probe_available() is False


@pytest.mark.unit
def test_compose_probe_reason_names_the_exit_code_and_docker_error(monkeypatch, tmp_path):
    # AC: the failure reason reaches the UI, not just a log file the owner
    # would have to go find -- and the actionable half is docker's own line.
    _unreachable_daemon(monkeypatch, tmp_path)

    reason = self_heal.compose_probe().reason

    assert "125" in reason
    assert "permission denied" in reason
    assert len(reason.splitlines()) == 1


@pytest.mark.unit
def test_unqueryable_probe_reports_components_unknown_never_absent(monkeypatch, tmp_path):
    """The regression guard the issue asks for: a non-zero `docker compose ps`
    must never yield component rows marked `absent`."""
    _unreachable_daemon(monkeypatch, tmp_path)
    observability = {
        "grafana",
        "prometheus",
        "loki",
        "promtail",
        "jaeger",
        "otel-collector",
        "glitchtip",
        "host-api-relay",
    }
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda profiles: observability)

    statuses = self_heal.list_component_status()
    compose_rows = [s for s in statuses if s.source == "compose"]

    assert {s.service for s in compose_rows} == observability
    assert [s.state for s in compose_rows if s.state == "absent"] == []
    for s in compose_rows:
        assert s.known is False
        assert s.state == "unknown"
        assert "125" in s.note


@pytest.mark.unit
def test_probe_that_ran_still_reports_genuinely_absent_components(monkeypatch, tmp_path):
    # The other half: when the survey *does* run and finds nothing, a torn-down
    # profile is still reported absent (and unhealthy). #3812 must not buy its
    # honesty by making every real absence unknown -- that would hide #3356.
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}\n")
    monkeypatch.setattr(self_heal, "COMPOSE_FILE", compose_file)
    monkeypatch.setattr(self_heal, "_run", lambda cmd, timeout=30.0, **_k: CP(stdout=""))
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(self_heal, "_desired_compose_services", lambda profiles: {"grafana"})

    statuses = self_heal.list_component_status()

    assert [(s.service, s.state, s.known) for s in statuses] == [("grafana", "absent", True)]
    assert self_heal._record_health_check(statuses) == 1


@pytest.mark.unit
def test_unknown_components_are_not_counted_unhealthy(monkeypatch, tmp_path):
    # AC: "the health rollup does not count unknown components as unhealthy".
    # This is the assertion that fails on the pre-fix code with "11 unhealthy".
    _unreachable_daemon(monkeypatch, tmp_path)
    monkeypatch.setattr(self_heal, "_enabled_observability_profiles", lambda: {"monitoring"})
    monkeypatch.setattr(
        self_heal, "_desired_compose_services", lambda profiles: {"grafana", "prometheus"}
    )

    data = self_heal.status()

    assert data["unhealthy_count"] == 0
    assert data["unknown_count"] == 2
    assert data["compose_probe_available"] is False
    assert "125" in data["compose_probe_reason"]
    assert all(c["known"] is False and c["giving_up"] is False for c in data["components"])


@pytest.mark.unit
def test_unknown_component_leaves_its_health_gauge_untouched(monkeypatch):
    # Neither 0 (which would alarm about containers that are probably running)
    # nor 1 (which would assert a health this pass never established): the
    # series is simply not written, the standard "could not scrape" reading.
    service = "gauge-unknown-test-svc"
    prom_metrics.SELFHEAL_COMPONENT_HEALTHY.labels(service=service).set(1.0)

    self_heal._record_health_check(
        [
            self_heal.ComponentStatus(
                service, "", "unknown", "", False, note="daemon unreachable", known=False
            )
        ]
    )

    assert _metric_value("nyxgpt_selfheal_component_healthy", service=service) == 1.0
    assert _metric_value("nyxgpt_selfheal_unhealthy_components") == 0


@pytest.mark.unit
def test_heal_now_does_not_act_on_unknown_components(monkeypatch):
    # Healing an unread state would `up -d` containers that are already
    # running, and would fail anyway for the probe's own reason -- burning the
    # restart budget until self-heal "gave up" on eleven healthy services.
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus(
                "grafana", "", "unknown", "", False, note="daemon unreachable", known=False
            )
        ],
    )
    restart_mock = MagicMock()
    bring_up_mock = MagicMock()
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)
    monkeypatch.setattr(self_heal, "_bring_up_compose_service", bring_up_mock)

    result = self_heal.heal_now()

    restart_mock.assert_not_called()
    bring_up_mock.assert_not_called()
    assert result["healed"] == []
    assert [c["service"] for c in result["undetermined"]] == ["grafana"]


@pytest.mark.unit
def test_heal_now_manual_still_acts_on_an_unknown_component(monkeypatch):
    # An explicit operator "Heal now" overrides the guard, exactly like the
    # desired=False and backoff overrides: the restart's own error is then
    # reported back honestly rather than the click doing nothing.
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [
            self_heal.ComponentStatus(
                "grafana", "", "unknown", "", False, note="daemon unreachable", known=False
            )
        ],
    )
    restart_mock = MagicMock(return_value=self_heal.HealResult(False, "Failed to restart grafana"))
    monkeypatch.setattr(self_heal, "restart_component", restart_mock)

    result = self_heal.heal_now("grafana")

    restart_mock.assert_called_once_with("grafana")
    assert result["healed"][0]["ok"] is False
