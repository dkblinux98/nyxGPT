import hashlib
import json
import logging
import subprocess
import tarfile
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from nyxgpt import ops, self_heal

# Captured before the autouse fixture below can ever monkeypatch it, so tests
# that exercise this function's real logic can restore it for their duration.
_real_terraform_or_kubernetes_managed_components = ops._terraform_or_kubernetes_managed_components


@pytest.fixture(autouse=True)
def _no_terraform_or_kubernetes_managed_components(monkeypatch):
    """Default `ops._terraform_or_kubernetes_managed_components()` to empty.

    `down()`/`stop()` call it (via `self_heal.list_component_status()`,
    which shells out to docker/kubectl) to decide which core components to
    mark intentionally stopped. Individual tests that care about
    Terraform/Kubernetes-managed components override this within their own
    `with patch.object(...)` block; everyone else gets a fast, deterministic
    empty set instead of hitting whatever docker/kubectl happen to be on the
    test host.
    """
    monkeypatch.setattr(ops, "_terraform_or_kubernetes_managed_components", lambda: set())


@pytest.mark.unit
def test_ops_install_returns_zero_when_all_ok(capsys):
    # Mock internal steps to all succeed
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_install_config", return_value=ok_results),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_install_scripts", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
        patch.object(ops, "_persist_compose_file_path", return_value=ok_results),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok_results),
        patch.object(ops, "_reconcile_grafana_provisioning", return_value=ok_results) as obs,
        patch.object(ops, "_provision_glitchtip", return_value=ok_results),
    ):
        rc = ops.install(MagicMock(skip_observability=False, terraform=False, kubernetes=False))
        assert rc == 0
        out = capsys.readouterr().out
        assert "[OK]" in out
        obs.assert_called_once()


@pytest.mark.unit
def test_ops_install_returns_nonzero_when_any_fail(capsys):
    mixed = [ops.OpsResult(True, "ok"), ops.OpsResult(False, "bad", "details")]
    with (
        patch.object(ops, "_install_config", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "migrate_legacy_volumes", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops,
            "_reconcile_phantom_compose_app_containers",
            return_value=[ops.OpsResult(True, "ok")],
        ),
        patch.object(ops, "_install_scripts", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_web_deps", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_mcp_deps", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_cassandra_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_install_cassandra_launchagent", return_value=mixed),
        patch.object(ops, "_install_ollama_launchagent", return_value=mixed),
        patch.object(
            ops, "_install_ollama_env_launchagent", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_install_homebrew_api", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_install_homebrew_web", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_ollama_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "sync_env_from_config", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_persist_compose_file_path", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_ensure_glitchtip_secrets_dir", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(
            ops, "_reconcile_grafana_provisioning", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_provision_glitchtip", return_value=[ops.OpsResult(True, "ok")]),
    ):
        rc = ops.install(MagicMock(skip_observability=False, terraform=False, kubernetes=False))
        assert rc == 2
        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "details" in out


@pytest.mark.unit
def test_ops_install_skip_observability_flag_skips_the_step(capsys):
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_install_config", return_value=ok_results),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_install_scripts", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
        patch.object(ops, "_persist_compose_file_path", return_value=ok_results),
        patch.object(ops, "_reconcile_grafana_provisioning") as obs,
    ):
        rc = ops.install(MagicMock(skip_observability=True, terraform=False, kubernetes=False))
        assert rc == 0
        obs.assert_not_called()


@pytest.mark.unit
def test_ops_install_step_order_reconciles_before_creating(capsys):
    # Phantom reconciliation and the Cassandra container step must both run as
    # part of `nyxgpt ops install`, and reconciliation must run before anything
    # else so a leaked Compose app-tier container is cleared before native
    # services/the local Cassandra container are (re)created.
    ok_results = [ops.OpsResult(True, "ok")]
    call_order = []

    def _record(name):
        def _fn(*_a, **_k):
            call_order.append(name)
            return ok_results

        return _fn

    with (
        patch.object(
            ops, "_clear_intentional_stops", side_effect=_record("clear intentional stops")
        ),
        patch.object(ops, "_install_config", side_effect=_record("config")),
        patch.object(ops, "migrate_legacy_volumes", side_effect=_record("migrate volumes")),
        patch.object(
            ops, "_reconcile_phantom_compose_app_containers", side_effect=_record("reconcile")
        ),
        patch.object(ops, "_install_scripts", side_effect=_record("scripts")),
        patch.object(ops, "_ensure_web_deps", side_effect=_record("web deps")),
        patch.object(ops, "_ensure_mcp_deps", side_effect=_record("mcp deps")),
        patch.object(
            ops, "_ensure_cassandra_container", side_effect=_record("cassandra container")
        ),
        patch.object(ops, "_install_cassandra_launchagent", side_effect=_record("cassandra la")),
        patch.object(ops, "_install_ollama_launchagent", side_effect=_record("ollama la")),
        patch.object(ops, "_install_ollama_env_launchagent", side_effect=_record("ollama env la")),
        patch.object(ops, "_install_homebrew_api", side_effect=_record("homebrew api")),
        patch.object(ops, "_install_homebrew_web", side_effect=_record("homebrew web")),
        patch.object(ops, "_ensure_ollama_service", side_effect=_record("ollama service")),
        patch.object(
            ops, "_cleanup_stale_log_symlinks", side_effect=_record("stale log symlink cleanup")
        ),
        patch.object(ops, "sync_env_from_config", side_effect=_record("env sync")),
        patch.object(ops, "_persist_compose_file_path", side_effect=_record("compose file path")),
    ):
        rc = ops.install(MagicMock(skip_observability=True, terraform=False, kubernetes=False))
        assert rc == 0

    # Clearing intentional-stop markers comes first, then config (a fresh
    # machine needs config.ini before any other step can act on it), then
    # reconciliation before anything creates.
    assert call_order[0] == "clear intentional stops"
    assert call_order[1] == "config"
    assert call_order[2] == "migrate volumes"
    assert call_order[3] == "reconcile"
    assert "cassandra container" in call_order
    assert "ollama service" in call_order
    assert "env sync" in call_order
    assert "compose file path" in call_order


@pytest.mark.unit
def test_ops_install_clears_intentional_stop_markers_for_core_components():
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_install_config", return_value=ok_results),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_install_scripts", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
        patch.object(ops, "_persist_compose_file_path", return_value=ok_results),
        patch.object(ops.self_heal, "clear_intentionally_stopped") as clear_stopped,
    ):
        rc = ops.install(MagicMock(skip_observability=True, terraform=False, kubernetes=False))
        assert rc == 0

    assert clear_stopped.call_args_list == [
        call("api"),
        call("web"),
        call("ollama"),
        call("cassandra"),
    ]


@pytest.mark.unit
def test_ops_restart_all_ok(capsys):
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={}),
        patch.object(ops, "_restart_brew_service", return_value=ok) as rb,
        patch.object(ops, "_restart_docker_container", return_value=ok) as rd,
        patch.object(ops, "_restart_launchagent", return_value=ok) as rl,
        patch.object(ops, "_restart_observability_stack", return_value=ok) as ro,
    ):
        args = MagicMock()
        args.target = "all"
        rc = ops.restart(args)
        assert rc == 0

        # ensure we attempted expected components
        assert rb.call_count == 3  # api, web, ollama
        rd.assert_called_once_with("nyxgpt-cassandra")
        rl.assert_called_once_with("com.nyxgpt.cassandra-logs")
        ro.assert_called_once()

        out = capsys.readouterr().out
        assert "[OK]" in out


@pytest.mark.unit
def test_clear_intentional_stops_calls_self_heal_for_each_component():
    with patch.object(ops.self_heal, "clear_intentionally_stopped") as clear_stopped:
        results = ops._clear_intentional_stops(["api", "cassandra"])

    assert clear_stopped.call_args_list == [call("api"), call("cassandra")]
    assert len(results) == 1
    assert results[0].ok
    assert "api" in results[0].message and "cassandra" in results[0].message


@pytest.mark.unit
def test_ops_restart_all_clears_intentional_stop_markers(capsys):
    """Restarting a component is the "this is desired again" signal (#3406) --
    it clears self-heal's intentional-stop marker so an armed watchdog
    resumes guarding it against future crashes."""
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={}),
        patch.object(ops, "_restart_brew_service", return_value=ok),
        patch.object(ops, "_restart_docker_container", return_value=ok),
        patch.object(ops, "_restart_launchagent", return_value=ok),
        patch.object(ops, "_restart_observability_stack", return_value=ok),
        patch.object(ops.self_heal, "clear_intentionally_stopped") as clear_stopped,
    ):
        args = MagicMock()
        args.target = "all"
        rc = ops.restart(args)
        assert rc == 0

    assert clear_stopped.call_args_list == [
        call("api"),
        call("web"),
        call("ollama"),
        call("cassandra"),
    ]


@pytest.mark.unit
def test_ops_restart_conflict_refused_does_not_clear_intentional_stop(capsys):
    """A restart refused by the Compose port-collision guard never actually
    starts the native component, so it must not clear its marker either."""
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={"api": "running"}),
        patch.object(ops, "_restart_brew_service"),
        patch.object(ops.self_heal, "clear_intentionally_stopped") as clear_stopped,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.restart(args)
        assert rc == 2

    clear_stopped.assert_not_called()


@pytest.mark.unit
def test_ops_restart_returns_nonzero_on_failure(capsys):
    ok = [ops.OpsResult(True, "ok")]
    bad = [ops.OpsResult(False, "bad", "details")]
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={}),
        patch.object(ops, "_restart_brew_service", side_effect=[ok, bad, ok]),
        patch.object(ops, "_restart_docker_container", return_value=ok),
        patch.object(ops, "_restart_launchagent", return_value=ok),
        patch.object(ops, "_restart_observability_stack", return_value=ok),
    ):
        args = MagicMock()
        args.target = "all"
        rc = ops.restart(args)
        assert rc == 2

        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "details" in out


@pytest.mark.unit
def test_ops_restart_single_target_only_restarts_that_component(capsys):
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={}),
        patch.object(ops, "_restart_brew_service", return_value=ok) as rb,
        patch.object(ops, "_restart_docker_container", return_value=ok) as rd,
        patch.object(ops, "_restart_launchagent", return_value=ok) as rl,
        patch.object(ops, "_restart_observability_stack", return_value=ok) as ro,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.restart(args)
        assert rc == 0

        rb.assert_called_once_with("nyxgpt-api")
        rd.assert_not_called()
        rl.assert_not_called()
        ro.assert_not_called()

        out = capsys.readouterr().out
        assert "Restarted" in out or "[OK]" in out


@pytest.mark.unit
def test_ops_restart_observability_target_calls_restart_observability_stack(capsys):
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={}),
        patch.object(ops, "_restart_brew_service") as rb,
        patch.object(ops, "_restart_docker_container") as rd,
        patch.object(ops, "_restart_launchagent") as rl,
        patch.object(ops, "_restart_observability_stack", return_value=ok) as ro,
    ):
        args = MagicMock()
        args.target = "observability"
        rc = ops.restart(args)
        assert rc == 0

        rb.assert_not_called()
        rd.assert_not_called()
        rl.assert_not_called()
        ro.assert_called_once()


@pytest.mark.unit
def test_ops_restart_refuses_when_compose_stack_conflicts(capsys):
    # A live Compose 'api' service must block a native api restart instead of
    # starting a competing process that would collide on port 8000.
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={"api": "running"}),
        patch.object(ops, "_restart_brew_service") as rb,
        patch.object(ops, "_restart_docker_container") as rd,
        patch.object(ops, "_restart_launchagent") as rl,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.restart(args)
        assert rc == 2

        rb.assert_not_called()
        rd.assert_not_called()
        rl.assert_not_called()

        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "Refusing to restart local api" in out
        assert "port 8000" in out


@pytest.mark.unit
def test_ops_restart_cassandra_refuses_when_compose_cassandra_conflicts(capsys):
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={"cassandra": "running"}),
        patch.object(ops, "_restart_docker_container") as rd,
    ):
        args = MagicMock()
        args.target = "cassandra"
        rc = ops.restart(args)
        assert rc == 2

        rd.assert_not_called()
        out = capsys.readouterr().out
        assert "Refusing to restart local cassandra" in out
        assert "port 9042" in out


@pytest.mark.unit
def test_restart_docker_container_recovers_previously_running_container(monkeypatch):
    # docker restart fails but the container was running before -- recovery `docker start`
    # succeeds, so this must NOT be reported as an unqualified failure/DOWN.
    class CP:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    state_calls = {"n": 0}

    def fake_docker_container_state(name):
        state_calls["n"] += 1
        # 1st call: was_running check (True). 2nd call: post-restart-failure check (False,
        # container is down). 3rd call: post-recovery check (True, back up).
        return "running" if state_calls["n"] in (1, 3) else "exited"

    def fake_run(cmd, check=True, **_k):
        if cmd[:2] == ["docker", "restart"]:
            return CP(returncode=1, stderr="port is already allocated")
        if cmd[:2] == ["docker", "start"]:
            return CP(returncode=0)
        return CP(returncode=0)

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "_docker_container_state", fake_docker_container_state)
    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._restart_docker_container("nyxgpt-cassandra")
    assert len(results) == 1
    assert results[0].ok is False
    assert "recovered" in results[0].message


@pytest.mark.unit
def test_restart_docker_container_reports_down_when_unrecoverable(monkeypatch):
    class CP:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_docker_container_state(name):
        # Always report "not running" after the initial was_running check.
        fake_docker_container_state.n += 1
        return "running" if fake_docker_container_state.n == 1 else "exited"

    fake_docker_container_state.n = 0

    def fake_run(cmd, check=True, **_k):
        if cmd[:2] == ["docker", "restart"]:
            return CP(returncode=1, stderr="port is already allocated")
        if cmd[:2] == ["docker", "start"]:
            return CP(returncode=1, stderr="port is already allocated")
        return CP(returncode=0)

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "_docker_container_state", fake_docker_container_state)
    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._restart_docker_container("nyxgpt-cassandra")
    assert len(results) == 1
    assert results[0].ok is False
    assert "DOWN" in results[0].message
    assert "STOPPED" in results[0].message


@pytest.mark.unit
def test_detect_deployment_mode_flags_conflict(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-web": "stopped", "ollama": "stopped"},
    )
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"api": "running", "web": "exited"})

    mode = ops.detect_deployment_mode()
    assert mode.native["api"] == "started"
    assert mode.compose["api"] == "running"
    assert mode.conflicts == ["api"]


@pytest.mark.unit
def test_detect_deployment_mode_native_only_cassandra_reports_no_conflict(monkeypatch):
    """Regression for #3383: a native-only stack (one nyxgpt-cassandra Docker
    container, no Compose app tier) must report zero conflicts. Exercises the
    real _compose_stack_snapshot() (not a stub) against self_heal's combined
    compose+native view, since that's exactly where the phantom-backend bug
    lived.
    """
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "stopped", "nyxgpt-web": "stopped", "ollama": "stopped"},
    )
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")

    native_cassandra = self_heal.ComponentStatus(
        service="cassandra",
        container="nyxgpt-cassandra",
        state="running",
        health="",
        healthy=True,
        source="native",
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [native_cassandra])

    mode = ops.detect_deployment_mode()
    assert mode.native["cassandra"] == "running"
    assert mode.compose == {}
    assert mode.conflicts == []


@pytest.mark.unit
def test_detect_deployment_mode_true_dual_backend_conflict_still_reported(monkeypatch):
    """A genuine native-vs-Compose collision -- a real Compose-managed
    cassandra container AND the native one on the same port -- must still be
    detected and warned about.
    """
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "stopped", "nyxgpt-web": "stopped", "ollama": "stopped"},
    )
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")

    compose_cassandra = self_heal.ComponentStatus(
        service="cassandra",
        container="nyxgpt_cassandra_1",
        state="running",
        health="healthy",
        healthy=True,
        source="compose",
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [compose_cassandra])

    mode = ops.detect_deployment_mode()
    assert mode.native["cassandra"] == "running"
    assert mode.compose["cassandra"] == "running"
    assert mode.conflicts == ["cassandra"]


@pytest.mark.unit
def test_ops_status_smoke(monkeypatch, capsys):
    # Make status deterministic by stubbing _which and _run outputs
    class CP:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(cmd, check=True, **_k):
        if cmd[:3] == ["brew", "services", "list"]:
            return CP(stdout="Name Status User File\nnyxgpt-web started user plist\n")
        if cmd[:2] == ["launchctl", "list"]:
            return CP(stdout="123 com.nyxgpt.cassandra-logs\n")
        if cmd[:3] == ["docker", "ps", "--format"]:
            return CP(stdout="nyxgpt-cassandra\n")
        return CP(stdout="")

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Homebrew services" in out
    assert "com.nyxgpt.cassandra-logs" in out
    assert "docker  cassandra" in out
    assert "native  cassandra" not in out


@pytest.mark.unit
def test_ops_status_warns_on_conflict(monkeypatch, capsys):
    class CP:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_run", lambda *a, **k: CP(stdout=""))
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {"nyxgpt-api": "started"})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"api": "running"})

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "api" in out
    assert "docker/config.docker.ini" in out


@pytest.mark.unit
def test_ops_doctor_ok(monkeypatch, capsys, tmp_path):
    # Pretend config exists at ~/.nyxGPT/config.ini (as ops.doctor expects)
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.ini"
    # Tracing defaults to enabled (#3415); disable it here so this test stays
    # focused on the checks it's actually exercising.
    cfg.write_text("[project]\nname=nyxGPT\n\n[tracing]\nenabled = false\n", encoding="utf-8")

    # Make home dir resolve into tmp_path
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    # Tools exist
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")

    # Cassandra container is present and running
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    # Mock REPO_ROOT to point to tmp_path (no web/ directory)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)

    rc = ops.doctor(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "doctor: OK" in out


@pytest.mark.unit
def test_ops_doctor_warns_when_web_deps_missing(monkeypatch, capsys, tmp_path):
    # Pretend config exists
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")

    # Fake web dir without node_modules
    web_dir = tmp_path / "web"
    web_dir.mkdir()

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(MagicMock())
    assert rc == 2

    out = capsys.readouterr().out
    assert "Missing web deps" in out


@pytest.mark.unit
def test_ops_doctor_fail_when_missing_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "doctor: FAIL" in out
    assert "Missing config" in out


@pytest.mark.unit
def test_ops_doctor_warns_when_cassandra_container_missing(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "Missing local Cassandra container" in out
    assert "nyxgpt-cassandra" in out


@pytest.mark.unit
def test_ops_doctor_flags_compose_service_stuck_restarting(monkeypatch, capsys, tmp_path):
    """#3538: a crash-looping compose service (e.g. Grafana rejecting a bad
    alerting-provisioning file) must FAIL doctor, not report OK with the
    crash loop silently ignored -- `nyxgpt ops status` already sees the
    `restarting` state, doctor must consume it."""
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.ini").write_text(
        "[project]\nname=nyxGPT\n\n[tracing]\nenabled = false\n", encoding="utf-8"
    )

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(
        ops, "_compose_stack_snapshot", lambda: {"grafana": "restarting", "loki": "running"}
    )

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "restart/crash loop" in out
    assert "grafana" in out
    assert "loki" not in out.split("restart/crash loop")[1].split("\n")[0]


@pytest.mark.unit
def test_ops_doctor_ignores_compose_when_no_docker(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: None)

    def _boom():
        raise AssertionError("must not query the compose stack without docker on PATH")

    monkeypatch.setattr(ops, "_compose_stack_snapshot", _boom)

    rc = ops.doctor(MagicMock())
    assert rc == 2  # still fails on "Missing tool in PATH", just not from this check
    out = capsys.readouterr().out
    assert "restart/crash loop" not in out


def _write_log_aggregation_config(path, *, enabled=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[log_aggregation]\nenabled = {'true' if enabled else 'false'}\n",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_log_aggregation_wiring_issue_none_when_no_config(tmp_path):
    assert ops._log_aggregation_wiring_issue(tmp_path / "missing.ini") is None


@pytest.mark.unit
def test_log_aggregation_wiring_issue_none_when_disabled(tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_log_aggregation_config(cfg_path, enabled=False)
    assert ops._log_aggregation_wiring_issue(cfg_path) is None


@pytest.mark.unit
def test_log_aggregation_wiring_issue_none_when_promtail_not_running(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_log_aggregation_config(cfg_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    assert ops._log_aggregation_wiring_issue(cfg_path) is None


@pytest.mark.unit
def test_log_aggregation_wiring_issue_none_when_no_native_logs(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_log_aggregation_config(cfg_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    assert ops._log_aggregation_wiring_issue(cfg_path) is None


@pytest.mark.unit
def test_log_aggregation_wiring_issue_none_when_container_absent(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_log_aggregation_config(cfg_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    log_dir = tmp_path / ".nyxGPT" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "nyxgpt.log").write_text("2026-07-18 00:00:00 INFO [-] nyxgpt: hi\n")

    monkeypatch.setattr(ops, "_promtail_container_id", lambda: None)

    assert ops._log_aggregation_wiring_issue(cfg_path) is None


@pytest.mark.unit
def test_log_aggregation_wiring_issue_flags_missing_mount(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_log_aggregation_config(cfg_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    log_dir = tmp_path / ".nyxGPT" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "nyxgpt.log").write_text("2026-07-18 00:00:00 INFO [-] nyxgpt: hi\n")

    monkeypatch.setattr(ops, "_promtail_container_id", lambda: "abc123")
    monkeypatch.setattr(ops, "_promtail_native_mount_missing", lambda container_id: True)

    issue = ops._log_aggregation_wiring_issue(cfg_path)
    assert issue is not None
    assert "not reaching Loki" in issue


@pytest.mark.unit
def test_log_aggregation_wiring_issue_none_when_mount_present(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_log_aggregation_config(cfg_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    log_dir = tmp_path / ".nyxGPT" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "nyxgpt.log").write_text("2026-07-18 00:00:00 INFO [-] nyxgpt: hi\n")

    monkeypatch.setattr(ops, "_promtail_container_id", lambda: "abc123")
    monkeypatch.setattr(ops, "_promtail_native_mount_missing", lambda container_id: False)

    assert ops._log_aggregation_wiring_issue(cfg_path) is None


@pytest.mark.unit
def test_promtail_container_id_returns_none_when_not_running(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kwargs: SimpleNamespace(stdout="", returncode=0),
    )
    assert ops._promtail_container_id() is None


@pytest.mark.unit
def test_promtail_container_id_returns_id_when_running(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kwargs: SimpleNamespace(stdout="abc123\n", returncode=0),
    )
    assert ops._promtail_container_id() == "abc123"


@pytest.mark.unit
def test_promtail_native_mount_missing_true_when_destination_absent(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kwargs: SimpleNamespace(
            stdout='[{"Destination": "/etc/promtail/config.yml"}]', returncode=0
        ),
    )
    assert ops._promtail_native_mount_missing("abc123") is True


@pytest.mark.unit
def test_promtail_native_mount_missing_false_when_destination_present(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kwargs: SimpleNamespace(
            stdout=f'[{{"Destination": "{ops.PROMTAIL_NATIVE_LOG_MOUNT_MARKER}"}}]',
            returncode=0,
        ),
    )
    assert ops._promtail_native_mount_missing("abc123") is False


@pytest.mark.unit
def test_promtail_native_mount_missing_true_when_inspect_fails(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **kwargs: SimpleNamespace(stdout="", returncode=1),
    )
    assert ops._promtail_native_mount_missing("abc123") is True


def _write_tracing_config(path, *, enabled=True, otlp_endpoint="http://localhost:4318/v1/traces"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[tracing]\nenabled = {'true' if enabled else 'false'}\n"
        f"otlp_endpoint = {otlp_endpoint}\n",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_tracing_wiring_issue_none_when_no_config(tmp_path):
    assert ops._tracing_wiring_issue(tmp_path / "missing.ini") is None


@pytest.mark.unit
def test_tracing_wiring_issue_none_when_disabled(tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_tracing_config(cfg_path, enabled=False)
    assert ops._tracing_wiring_issue(cfg_path) is None


@pytest.mark.unit
def test_tracing_wiring_issue_flags_unreachable_collector(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_tracing_config(cfg_path)
    monkeypatch.setattr(ops.tracing, "otlp_endpoint_reachable", lambda endpoint, **kw: False)

    issue = ops._tracing_wiring_issue(cfg_path)
    assert issue is not None
    assert "nothing is" in issue
    assert "http://localhost:4318/v1/traces" in issue


@pytest.mark.unit
def test_tracing_wiring_issue_none_when_collector_reachable(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_tracing_config(cfg_path)
    monkeypatch.setattr(ops.tracing, "otlp_endpoint_reachable", lambda endpoint, **kw: True)

    assert ops._tracing_wiring_issue(cfg_path) is None


@pytest.mark.unit
def test_tracing_packages_doctor_issue_none_when_no_config(tmp_path):
    assert ops._tracing_packages_doctor_issue(tmp_path / "missing.ini") is None


@pytest.mark.unit
def test_tracing_packages_doctor_issue_none_when_disabled(tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_tracing_config(cfg_path, enabled=False)
    assert ops._tracing_packages_doctor_issue(cfg_path) is None


@pytest.mark.unit
def test_tracing_packages_doctor_issue_flags_missing_package(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_tracing_config(cfg_path)
    monkeypatch.setattr(
        ops.tracing, "missing_tracing_packages", lambda: ["opentelemetry-instrumentation-urllib"]
    )

    issue = ops._tracing_packages_doctor_issue(cfg_path)
    assert issue is not None
    assert "opentelemetry-instrumentation-urllib" in issue
    assert "nyxgpt ops install" in issue


@pytest.mark.unit
def test_tracing_packages_doctor_issue_none_when_all_present(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_tracing_config(cfg_path)
    monkeypatch.setattr(ops.tracing, "missing_tracing_packages", lambda: [])

    assert ops._tracing_packages_doctor_issue(cfg_path) is None


@pytest.mark.unit
def test_ops_doctor_flags_unreachable_otlp_collector(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _write_tracing_config(cfg_dir / "config.ini")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(ops.tracing, "otlp_endpoint_reachable", lambda endpoint, **kw: False)

    rc = ops.doctor(MagicMock())
    out = capsys.readouterr().out
    assert rc == 2
    assert "nothing is" in out
    assert "otlp_endpoint=http://localhost:4318/v1/traces" in out


@pytest.mark.unit
def test_ops_doctor_flags_missing_promtail_native_mount(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _write_log_aggregation_config(cfg_dir / "config.ini")

    log_dir = cfg_dir / "logs"
    log_dir.mkdir()
    (log_dir / "nyxgpt.log").write_text("2026-07-18 00:00:00 INFO [-] nyxgpt: hi\n")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})
    monkeypatch.setattr(ops, "_promtail_container_id", lambda: "abc123")
    monkeypatch.setattr(ops, "_promtail_native_mount_missing", lambda container_id: True)
    monkeypatch.setattr(ops, "_loki_recent_volume_by_logger", lambda *a, **kw: (None, None))

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "not reaching Loki" in out


@pytest.mark.unit
def test_ops_doctor_prints_loki_volume_by_logger_when_available(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _write_log_aggregation_config(cfg_dir / "config.ini")
    # Tracing defaults to enabled (#3415); disable it so this test stays
    # focused on the log-aggregation checks it's actually exercising.
    with (cfg_dir / "config.ini").open("a", encoding="utf-8") as f:
        f.write("\n[tracing]\nenabled = false\n")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})
    monkeypatch.setattr(
        ops,
        "_loki_recent_volume_by_logger",
        lambda *a, **kw: (
            {"self_heal": 0, "deploy": 0, "canary": 0, "chat": 142, "rag": 8},
            None,
        ),
    )

    rc = ops.doctor(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Log volume (last 24h) by logger:" in out
    assert "chat=142" in out
    assert "self_heal=0" in out


@pytest.mark.unit
def test_ops_doctor_omits_loki_volume_when_unreachable(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _write_log_aggregation_config(cfg_dir / "config.ini")
    # Tracing defaults to enabled (#3415); disable it so this test stays
    # focused on the log-aggregation checks it's actually exercising.
    with (cfg_dir / "config.ini").open("a", encoding="utf-8") as f:
        f.write("\n[tracing]\nenabled = false\n")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})
    monkeypatch.setattr(ops, "_loki_recent_volume_by_logger", lambda *a, **kw: (None, None))

    rc = ops.doctor(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Log volume" not in out


@pytest.mark.unit
def test_ops_doctor_flags_missing_grafana_doctor_token(monkeypatch, capsys, tmp_path):
    """#3438: a missing token is reported as an actionable issue, not a
    silent skip -- no `~/.nyxGPT/secrets/grafana-doctor-token` written."""
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _write_log_aggregation_config(cfg_dir / "config.ini")
    with (cfg_dir / "config.ini").open("a", encoding="utf-8") as f:
        f.write("\n[tracing]\nenabled = false\n")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})

    rc = ops.doctor(MagicMock())
    out = capsys.readouterr().out
    assert rc == 2
    assert "grafana-doctor-token" in out
    assert "nyxgpt ops install" in out


@pytest.mark.unit
def test_ops_doctor_flags_grafana_401_rejected_token(monkeypatch, capsys, tmp_path):
    """#3438: Grafana rejecting doctor's token (401) is reported as an
    actionable issue, not a silent `logger.warning` + skip."""
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _write_log_aggregation_config(cfg_dir / "config.ini")
    with (cfg_dir / "config.ini").open("a", encoding="utf-8") as f:
        f.write("\n[tracing]\nenabled = false\n")

    secrets_dir = cfg_dir / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "grafana-doctor-token").write_text("stale-token")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})

    class _FakeResponse:
        status_code = 401

        def raise_for_status(self):
            raise httpx.HTTPStatusError("401", request=None, response=self)

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return _FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", _FakeClient)

    rc = ops.doctor(MagicMock())
    out = capsys.readouterr().out
    assert rc == 2
    assert "rejected" in out
    assert "401" in out
    assert "nyxgpt ops install" in out


@pytest.mark.unit
def test_loki_recent_volume_by_logger_returns_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "grafana-doctor-token").write_text("a-token")

    class _FakeResponse:
        status_code = 200

        def __init__(self, value):
            self._value = value

        def raise_for_status(self):
            pass

        def json(self):
            return {"data": {"result": [{"value": [0, str(self._value)]}]}}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return _FakeResponse(7 if "self_heal" in params["query"] else 0)

    monkeypatch.setattr(ops.httpx, "Client", _FakeClient)

    volumes, issue = ops._loki_recent_volume_by_logger("http://localhost:3001")
    assert issue is None
    assert volumes["self_heal"] == 7
    assert volumes["chat"] == 0
    assert set(volumes) == set(ops.LOKI_CURATED_LOGGERS)


@pytest.mark.unit
def test_loki_recent_volume_by_logger_returns_none_on_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "grafana-doctor-token").write_text("a-token")

    class _FailingClient:
        def __init__(self, *a, **kw):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(ops.httpx, "Client", _FailingClient)

    assert ops._loki_recent_volume_by_logger("http://localhost:3001") == (None, None)


@pytest.mark.unit
def test_loki_recent_volume_by_logger_missing_token_is_actionable(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    volumes, issue = ops._loki_recent_volume_by_logger("http://localhost:3001")
    assert volumes is None
    assert "grafana-doctor-token" in issue
    assert "nyxgpt ops install" in issue


@pytest.mark.unit
def test_loki_recent_volume_by_logger_401_is_actionable_not_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "grafana-doctor-token").write_text("stale-token")

    class _FakeResponse:
        status_code = 401

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return _FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", _FakeClient)

    volumes, issue = ops._loki_recent_volume_by_logger("http://localhost:3001")
    assert volumes is None
    assert "401" in issue
    assert "rejected" in issue


def _write_config(path, *, api_key="", grafana_password="", auth_enabled="false"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""
[auth]
enabled = {auth_enabled}
api_key = {api_key}

[monitoring]
enabled = false
grafana_admin_password = {grafana_password}
""".lstrip(),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_sync_env_from_config_missing_config_fails(tmp_path):
    cfg_path = tmp_path / "config.ini"
    env_path = tmp_path / ".env"

    results = ops.sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    assert len(results) == 1
    assert results[0].ok is False
    assert "Missing config" in results[0].message
    assert not env_path.exists()


@pytest.mark.unit
def test_sync_env_from_config_auth_disabled_no_secrets_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path)
    env_path = tmp_path / ".env"

    results = ops.sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    assert len(results) == 1
    assert results[0].ok is True
    assert "auth disabled" in results[0].message
    assert not env_path.exists()


@pytest.mark.unit
def test_sync_env_from_config_auth_enabled_but_no_secrets_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, auth_enabled="true")
    env_path = tmp_path / ".env"

    results = ops.sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    assert len(results) == 1
    assert results[0].ok is False
    assert "No secrets found" in results[0].message


@pytest.mark.unit
def test_sync_env_from_config_creates_env_from_example(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="real-api-key", grafana_password="real-grafana-pw")

    example_path = tmp_path / ".env.example"
    example_path.write_text(
        "NYXGPT_API_PORT=8000\n"
        "NYXGPT_AUTH_API_KEY=change-me\n"
        "GRAFANA_ADMIN_PASSWORD=change-me\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)

    env_path = tmp_path / ".env"
    results = ops.sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    assert len(results) == 1
    assert results[0].ok is True
    assert env_path.exists()

    content = env_path.read_text(encoding="utf-8")
    assert "NYXGPT_AUTH_API_KEY=real-api-key" in content
    assert "GRAFANA_ADMIN_PASSWORD=real-grafana-pw" in content
    # Non-secret lines are preserved untouched.
    assert "NYXGPT_API_PORT=8000" in content

    # Secrets now live in .env -- restrict permissions like config.ini.
    mode = env_path.stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.unit
def test_sync_env_from_config_updates_existing_env_in_place(tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="new-api-key", grafana_password="new-grafana-pw")

    env_path = tmp_path / ".env"
    env_path.write_text(
        "NYXGPT_WEB_PORT=3000\n"
        "NYXGPT_AUTH_API_KEY=stale-value\n"
        "GRAFANA_ADMIN_PASSWORD=stale-value\n",
        encoding="utf-8",
    )

    results = ops.sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    assert results[0].ok is True
    content = env_path.read_text(encoding="utf-8")
    assert "NYXGPT_AUTH_API_KEY=new-api-key" in content
    assert "GRAFANA_ADMIN_PASSWORD=new-grafana-pw" in content
    assert "stale-value" not in content
    assert "NYXGPT_WEB_PORT=3000" in content
    # Only one line per secret key -- not duplicated/appended.
    assert content.count("NYXGPT_AUTH_API_KEY=") == 1


@pytest.mark.unit
def test_sync_env_from_config_syncs_only_the_secret_that_is_set(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="only-api-key-set")

    env_path = tmp_path / ".env"
    results = ops.sync_env_from_config(cfg_path=cfg_path, env_path=env_path)

    assert results[0].ok is True
    content = env_path.read_text(encoding="utf-8")
    assert "NYXGPT_AUTH_API_KEY=only-api-key-set" in content
    assert "GRAFANA_ADMIN_PASSWORD" not in content


@pytest.mark.unit
def test_env_sync_cli_wrapper_prints_result(tmp_path, capsys, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="cli-api-key", grafana_password="cli-grafana-pw")
    env_path = tmp_path / ".env"
    compose_cfg = tmp_path / "config.docker.ini"
    compose_cfg.write_text("[error_tracking]\nenabled = false\ndsn =\n", encoding="utf-8")
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", compose_cfg)

    args = MagicMock()
    args.config = str(cfg_path)
    args.env_file = str(env_path)

    rc = ops.env_sync(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert env_path.exists()


@pytest.mark.unit
def test_ops_logs_prints_output_on_success(capsys):
    with patch.object(
        ops.self_heal,
        "component_logs",
        return_value=ops.self_heal.HealResult(
            True, "Fetched last 50 log line(s) for glitchtip", "confirm: http://..."
        ),
    ) as cl:
        args = MagicMock()
        args.service = "glitchtip"
        args.tail = 50
        rc = ops.logs(args)

        assert rc == 0
        cl.assert_called_once_with("glitchtip", tail=50)
        out = capsys.readouterr().out
        assert "confirm: http://..." in out


@pytest.mark.unit
def test_ops_logs_honors_explicit_tail_zero(capsys):
    with patch.object(
        ops.self_heal,
        "component_logs",
        return_value=ops.self_heal.HealResult(True, "Fetched last 0 log line(s) for glitchtip", ""),
    ) as cl:
        args = MagicMock()
        args.service = "glitchtip"
        args.tail = 0
        rc = ops.logs(args)

        assert rc == 0
        cl.assert_called_once_with("glitchtip", tail=0)


@pytest.mark.unit
def test_ops_logs_returns_nonzero_on_failure(capsys):
    with patch.object(
        ops.self_heal,
        "component_logs",
        return_value=ops.self_heal.HealResult(
            False, "Failed to fetch logs for glitchtip", "no such service"
        ),
    ):
        args = MagicMock()
        args.service = "glitchtip"
        args.tail = 200
        rc = ops.logs(args)

        assert rc == 2
        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "no such service" in out


@pytest.mark.unit
def test_ops_logs_defaults_tail_to_200_when_not_given(capsys):
    args = MagicMock(spec=["service"])
    args.service = "glitchtip"
    with patch.object(
        ops.self_heal,
        "component_logs",
        return_value=ops.self_heal.HealResult(True, "ok", "output"),
    ) as cl:
        rc = ops.logs(args)
        assert rc == 0
        cl.assert_called_once_with("glitchtip", tail=200)


# --- Small pure helpers ---


@pytest.mark.unit
def test_run_invokes_subprocess_with_expected_kwargs():
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["echo", "hi"], returncode=0, stdout="hi\n", stderr=""
        )
        cp = ops._run(["echo", "hi"])
        run.assert_called_once_with(["echo", "hi"], check=True, text=True, capture_output=True)
        assert cp.stdout == "hi\n"


@pytest.mark.unit
def test_run_logs_cmd_rc_stderr_tail_on_nonzero_exit_when_check_false(caplog):
    # #3415 gap 5: subprocess evidence must reach Loki even when the caller
    # only inspects the returncode instead of catching an exception.
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["false"], returncode=1, stdout="", stderr="boom\n"
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            cp = ops._run(["false"], check=False)

    assert cp.returncode == 1
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit"
    record = records[0]
    assert record.levelno == logging.WARNING
    assert "rc=1" in record.getMessage()
    assert "false" in record.getMessage()
    assert record.cmd == ["false"]
    assert record.returncode == 1
    assert "boom" in record.stderr_tail


@pytest.mark.unit
def test_run_logs_cmd_rc_stderr_tail_on_nonzero_exit_when_check_true(caplog):
    with (
        caplog.at_level("DEBUG", logger="nyxgpt.ops"),
        pytest.raises(subprocess.CalledProcessError),
    ):
        ops._run(["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"])

    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit before raising"
    assert records[0].levelno == logging.WARNING
    assert "rc=1" in records[0].getMessage()
    assert records[0].returncode == 1
    assert "boom" in records[0].stderr_tail


@pytest.mark.unit
def test_run_expected_true_logs_debug_not_warning_on_nonzero_exit_check_false(caplog):
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["false"], returncode=1, stdout="", stderr="boom\n"
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            cp = ops._run(["false"], check=False, expected=True)

    assert cp.returncode == 1
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit at DEBUG"
    assert records[0].levelno == logging.DEBUG
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.unit
def test_run_expected_true_logs_debug_not_warning_on_nonzero_exit_check_true(caplog):
    with (
        caplog.at_level("DEBUG", logger="nyxgpt.ops"),
        pytest.raises(subprocess.CalledProcessError),
    ):
        ops._run(
            ["python3", "-c", "import sys; sys.stderr.write('boom'); sys.exit(1)"],
            expected=True,
        )

    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit at DEBUG before raising"
    assert records[0].levelno == logging.DEBUG
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.unit
def test_which_finds_and_misses():
    assert ops._which("python3") is not None
    assert ops._which("definitely-not-a-real-binary-xyz") is None


@pytest.mark.unit
def test_read_project_version_missing_pyproject_returns_default(monkeypatch, tmp_path):
    # "0.0.0" is a deliberately implausible sentinel -- an installed formula
    # carrying it signals "version undetermined", unlike the old "1.0.0.md"
    # typo it replaces, which looked like (and shipped as) a real version.
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    assert ops._read_project_version() == "0.0.0"


@pytest.mark.unit
def test_read_project_version_reads_from_pyproject(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "nyxGPT"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    assert ops._read_project_version() == "9.9.9"


@pytest.mark.unit
def test_project_version_public_wrapper_matches_private(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "nyxGPT"\nversion = "2.5.0"\n', encoding="utf-8"
    )
    assert ops.project_version() == "2.5.0"


@pytest.mark.unit
def test_build_and_load_k8s_image_public_wrapper_uses_given_tag(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ops,
        "_build_and_load_k8s_image",
        lambda image: calls.append(image) or [ops.OpsResult(True, "ok")],
    )
    results = ops.build_and_load_k8s_image("nyxgpt-api:1.2.3-abcd123")
    assert calls == ["nyxgpt-api:1.2.3-abcd123"]
    assert results == [ops.OpsResult(True, "ok")]


@pytest.mark.unit
def test_record_canary_action_uses_canary_prefixed_command(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ops,
        "_record_ops_action",
        lambda command, service, result, message="": calls.append(
            (command, service, result, message)
        ),
    )
    ops.record_canary_action("deploy", "success", "Deployed nyxgpt-api:1.2.3-abcd123")
    assert calls == [("canary-deploy", "api", "success", "Deployed nyxgpt-api:1.2.3-abcd123")]


# --- _install_config (first-run wizard on `nyxgpt ops install`, #3388) ---


@pytest.mark.unit
def test_install_config_existing_config_is_ok_without_wizard(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg = tmp_path / ".nyxGPT" / "config.ini"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[nyxgpt]\n", encoding="utf-8")

    results = ops._install_config()
    assert results[0].ok is True
    assert "already exists" in results[0].message


@pytest.mark.unit
def test_install_config_missing_without_tty_fails_with_instructions(monkeypatch, tmp_path):
    # CI/scripted installs must not hang on an interactive prompt: no config
    # plus no TTY is an honest failure pointing at `nyxgpt wizard`.
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops.sys.stdin, "isatty", lambda: False)

    results = ops._install_config()
    assert results[0].ok is False
    assert "nyxgpt wizard" in results[0].message


@pytest.mark.unit
def test_install_config_missing_with_tty_runs_wizard(monkeypatch, tmp_path):
    import nyxgpt.wizard as wizard_mod

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops.sys.stdin, "isatty", lambda: True)
    calls: dict[str, object] = {}

    def fake_run_wizard(output_path):
        calls["output_path"] = output_path
        return 0

    monkeypatch.setattr(wizard_mod, "run_wizard", fake_run_wizard)

    results = ops._install_config()
    assert results[0].ok is True
    assert "via wizard" in results[0].message
    assert calls["output_path"] == tmp_path / ".nyxGPT" / "config.ini"


@pytest.mark.unit
def test_install_config_wizard_failure_is_reported(monkeypatch, tmp_path):
    import nyxgpt.wizard as wizard_mod

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(wizard_mod, "run_wizard", lambda output_path: 1)

    results = ops._install_config()
    assert results[0].ok is False
    assert "did not complete" in results[0].message


@pytest.mark.unit
def test_copy_file_creates_parent_and_sets_mode(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "nested" / "dst.txt"

    ops._copy_file(src, dst, mode=0o755)

    assert dst.read_text(encoding="utf-8") == "hello"
    assert (dst.stat().st_mode & 0o777) == 0o755


@pytest.mark.unit
def test_copy_file_without_mode_leaves_permissions_default(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("hello", encoding="utf-8")
    dst = tmp_path / "dst.txt"

    ops._copy_file(src, dst)

    assert dst.read_text(encoding="utf-8") == "hello"


@pytest.mark.unit
def test_sha256_file_matches_hashlib(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"the quick brown fox" * 1000)

    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    assert ops._sha256_file(p) == expected


@pytest.mark.unit
def test_tap_repo_returns_run_output(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="/tap/dir\n"),
    )
    assert ops._tap_repo("dkblinux98/nyxgpt-local") == Path("/tap/dir")


# --- Deployment-mode detection internals (exercised directly, not stubbed out) ---


@pytest.mark.unit
def test_brew_services_snapshot_no_brew_returns_empty(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    assert ops._brew_services_snapshot() == {}


@pytest.mark.unit
def test_brew_services_snapshot_parses_output(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout="Name Status User File\nnyxgpt-api started user plist\n"
        ),
    )
    snapshot = ops._brew_services_snapshot()
    assert snapshot["nyxgpt-api"] == "started"


@pytest.mark.unit
def test_docker_container_state_no_docker_returns_absent(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    assert ops._docker_container_state("nyxgpt-cassandra") == "absent"


@pytest.mark.unit
def test_docker_container_state_parses_output(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="running\n"),
    )
    assert ops._docker_container_state("nyxgpt-cassandra") == "running"


@pytest.mark.unit
def test_docker_container_state_empty_output_is_absent(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="")
    )
    assert ops._docker_container_state("nyxgpt-cassandra") == "absent"


@pytest.mark.unit
def test_compose_stack_snapshot_returns_service_states():
    status = self_heal.ComponentStatus(
        service="api", container="nyxgpt-api", state="running", health="healthy", healthy=True
    )
    with patch.object(ops.self_heal, "list_component_status", return_value=[status]):
        assert ops._compose_stack_snapshot() == {"api": "running"}


@pytest.mark.unit
def test_compose_stack_snapshot_returns_empty_on_exception():
    with patch.object(
        ops.self_heal, "list_component_status", side_effect=RuntimeError("no compose")
    ):
        assert ops._compose_stack_snapshot() == {}


@pytest.mark.unit
def test_compose_stack_snapshot_excludes_native_sourced_statuses():
    """Regression for #3383: list_component_status() returns a combined
    compose+native+absent view; the snapshot must filter to source=="compose"
    only, or a native component (e.g. the native nyxgpt-cassandra container)
    folds into the "compose" dict and collides with detect_deployment_mode()'s
    native reading of that same container.
    """
    native_cassandra = self_heal.ComponentStatus(
        service="cassandra",
        container="nyxgpt-cassandra",
        state="running",
        health="",
        healthy=True,
        source="native",
    )
    compose_api = self_heal.ComponentStatus(
        service="api", container="nyxgpt-api", state="running", health="healthy", healthy=True
    )
    with patch.object(
        ops.self_heal, "list_component_status", return_value=[native_cassandra, compose_api]
    ):
        assert ops._compose_stack_snapshot() == {"api": "running"}


@pytest.mark.unit
def test_terraform_or_kubernetes_managed_components_filters_by_source(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_terraform_or_kubernetes_managed_components",
        _real_terraform_or_kubernetes_managed_components,
    )
    native_api = self_heal.ComponentStatus(
        service="api",
        container="nyxgpt-api",
        state="started",
        health="",
        healthy=True,
        source="native",
    )
    terraform_web = self_heal.ComponentStatus(
        service="web",
        container="nyxgpt-tf-web",
        state="running",
        health="healthy",
        healthy=True,
        source="terraform",
    )
    k8s_pod = self_heal.ComponentStatus(
        service="nyxgpt-api-stable-abc123",
        container="nyxgpt-api-stable-abc123",
        state="Running",
        health="ready",
        healthy=True,
        source="kubernetes",
    )
    with patch.object(
        ops.self_heal,
        "list_component_status",
        return_value=[native_api, terraform_web, k8s_pod],
    ):
        assert ops._terraform_or_kubernetes_managed_components() == {
            "web",
            "nyxgpt-api-stable-abc123",
        }


@pytest.mark.unit
def test_terraform_or_kubernetes_managed_components_returns_empty_on_exception(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_terraform_or_kubernetes_managed_components",
        _real_terraform_or_kubernetes_managed_components,
    )
    with patch.object(ops.self_heal, "list_component_status", side_effect=RuntimeError("boom")):
        assert ops._terraform_or_kubernetes_managed_components() == set()


# --- _restart_brew_service ---


@pytest.mark.unit
def test_restart_brew_service_not_found(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._restart_brew_service("nyxgpt-api")
    assert len(results) == 1
    assert results[0].ok is False
    assert "brew not found" in results[0].message


@pytest.mark.unit
def test_restart_brew_service_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    results = ops._restart_brew_service("nyxgpt-api")
    assert results[0].ok is True
    assert "Restarted brew service" in results[0].message


@pytest.mark.unit
def test_restart_brew_service_failure_includes_details(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="out", stderr="err"),
    )
    results = ops._restart_brew_service("nyxgpt-api")
    assert results[0].ok is False
    assert "Failed to restart brew service" in results[0].message
    assert "out" in results[0].details
    assert "err" in results[0].details


@pytest.mark.unit
def test_restart_brew_service_exception(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")

    def raise_run(cmd, **k):
        raise OSError("boom")

    monkeypatch.setattr(ops, "_run", raise_run)
    results = ops._restart_brew_service("nyxgpt-api")
    assert results[0].ok is False
    assert "Failed to restart brew service" in results[0].message
    assert "OSError" in results[0].details


# --- _restart_docker_container ---


@pytest.mark.unit
def test_restart_docker_container_no_docker(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._restart_docker_container("nyxgpt-cassandra")
    assert results[0].ok is False
    assert "docker not found" in results[0].message


@pytest.mark.unit
def test_restart_docker_container_run_raises(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")

    def raise_run(cmd, **k):
        raise OSError("boom")

    monkeypatch.setattr(ops, "_run", raise_run)
    results = ops._restart_docker_container("nyxgpt-cassandra")
    assert results[0].ok is False
    assert "Failed to restart docker container" in results[0].message
    assert "OSError" in results[0].details


@pytest.mark.unit
def test_restart_docker_container_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    results = ops._restart_docker_container("nyxgpt-cassandra")
    assert results[0].ok is True
    assert "Restarted docker container" in results[0].message


@pytest.mark.unit
def test_restart_docker_container_fails_when_not_previously_running(monkeypatch):
    # was_running is False (container was already stopped/absent) -- restart fails,
    # and since it wasn't running before there's nothing to "recover".
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "exited")

    def fake_run(cmd, **k):
        if cmd[:2] == ["docker", "restart"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such container")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._restart_docker_container("nyxgpt-cassandra")
    assert results[0].ok is False
    assert results[0].message == "Failed to restart docker container: nyxgpt-cassandra"
    assert "no such container" in results[0].details


# --- _restart_launchagent ---


@pytest.mark.unit
def test_restart_launchagent_success(monkeypatch):
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    results = ops._restart_launchagent("com.nyxgpt.cassandra-logs")
    assert results[0].ok is True
    assert "Restarted LaunchAgent" in results[0].message


@pytest.mark.unit
def test_restart_launchagent_failure(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="out", stderr="err"),
    )
    results = ops._restart_launchagent("com.nyxgpt.cassandra-logs")
    assert results[0].ok is False
    assert "Failed to restart LaunchAgent" in results[0].message
    assert "out" in results[0].details
    assert "err" in results[0].details


@pytest.mark.unit
def test_restart_launchagent_exception(monkeypatch):
    def raise_run(cmd, **k):
        raise OSError("boom")

    monkeypatch.setattr(ops, "_run", raise_run)
    results = ops._restart_launchagent("com.nyxgpt.cassandra-logs")
    assert results[0].ok is False
    assert "Failed to restart LaunchAgent" in results[0].message
    assert "OSError" in results[0].details


# --- _find_launchagent_template ---


@pytest.mark.unit
def test_find_launchagent_template_returns_first_existing_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    target = tmp_path / "ops" / "launchagents" / "com.nyxgpt.cassandra-logs.plist"
    target.parent.mkdir(parents=True)
    target.write_text("<plist/>", encoding="utf-8")

    tpl, candidates = ops._find_launchagent_template()
    assert tpl == target
    assert len(candidates) == 4


@pytest.mark.unit
def test_find_launchagent_template_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    tpl, candidates = ops._find_launchagent_template()
    assert tpl is None
    assert len(candidates) == 4


@pytest.mark.unit
def test_find_launchagent_template_skips_candidate_that_errors(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    bad_path = tmp_path / "ops" / "launchagents" / "com.nyxgpt.cassandra-logs.plist"
    real_exists = Path.exists

    def flaky_exists(self):
        if self == bad_path:
            raise OSError("permission denied")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", flaky_exists)
    tpl, candidates = ops._find_launchagent_template()
    assert tpl is None
    assert len(candidates) == 4


@pytest.mark.unit
def test_find_launchagent_template_accepts_a_different_plist_name(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    target = tmp_path / "ops" / "launchagents" / "com.nyxgpt.ollama-logs.plist"
    target.parent.mkdir(parents=True)
    target.write_text("<plist/>", encoding="utf-8")

    tpl, candidates = ops._find_launchagent_template("com.nyxgpt.ollama-logs.plist")
    assert tpl == target
    assert len(candidates) == 4
    assert all(c.name == "com.nyxgpt.ollama-logs.plist" for c in candidates)


# --- _install_scripts ---


@pytest.mark.unit
def test_install_scripts_installs_present_scripts(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    home = tmp_path / "home"
    (repo_root / "scripts").mkdir(parents=True)
    (repo_root / "scripts" / "run-web.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo_root / "scripts" / "follow-cassandra-logs.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo_root / "scripts" / "follow-ollama-logs.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)
    monkeypatch.setattr(ops.Path, "home", lambda: home)

    results = ops._install_scripts()
    assert all(r.ok for r in results)
    assert (home / ".nyxGPT" / "scripts" / "run-web.sh").exists()
    assert (home / ".nyxGPT" / "scripts" / "follow-cassandra-logs.sh").exists()
    assert (home / ".nyxGPT" / "scripts" / "follow-ollama-logs.sh").exists()


@pytest.mark.unit
def test_install_scripts_skips_missing_scripts(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    home = tmp_path / "home"
    repo_root.mkdir()

    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)
    monkeypatch.setattr(ops.Path, "home", lambda: home)

    results = ops._install_scripts()
    assert all(r.ok for r in results)
    assert all("skipped" in r.message for r in results)


# --- _install_cassandra_launchagent ---


@pytest.mark.unit
def test_install_cassandra_launchagent_missing_template(monkeypatch):
    monkeypatch.setattr(ops, "_find_launchagent_template", lambda: (None, [Path("/a"), Path("/b")]))
    results = ops._install_cassandra_launchagent()
    assert results[0].ok is False
    assert "Missing Cassandra logs LaunchAgent template" in results[0].message


@pytest.mark.unit
def test_install_cassandra_launchagent_installs_when_template_found(monkeypatch, tmp_path):
    tpl = tmp_path / "com.nyxgpt.cassandra-logs.plist"
    tpl.write_text(
        "<plist>__NYXGPT_HOME__/.nyxGPT/scripts/follow-cassandra-logs.sh</plist>",
        encoding="utf-8",
    )
    home = tmp_path / "home"

    monkeypatch.setattr(ops, "_find_launchagent_template", lambda: (tpl, [tpl]))
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    run_calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    results = ops._install_cassandra_launchagent()
    assert results[0].ok is True
    dst = home / "Library" / "LaunchAgents" / tpl.name
    assert dst.exists()
    installed = dst.read_text(encoding="utf-8")
    assert "__NYXGPT_HOME__" not in installed
    assert f"{home}/.nyxGPT/scripts/follow-cassandra-logs.sh" in installed
    assert len(run_calls) == 3


@pytest.mark.unit
def test_install_cassandra_launchagent_bootout_not_loaded_logs_debug_not_warning(
    monkeypatch, tmp_path, caplog
):
    """rc=5 ("not loaded") on the reload-before-bootstrap bootout is the normal
    first-install case (#3457) -- it must log at DEBUG, never WARNING."""
    tpl = tmp_path / "com.nyxgpt.cassandra-logs.plist"
    tpl.write_text(
        "<plist>__NYXGPT_HOME__/.nyxGPT/scripts/follow-cassandra-logs.sh</plist>",
        encoding="utf-8",
    )
    home = tmp_path / "home"

    monkeypatch.setattr(ops, "_find_launchagent_template", lambda: (tpl, [tpl]))
    monkeypatch.setattr(ops.Path, "home", lambda: home)

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[:2] == ["launchctl", "bootout"]:
            return subprocess.CompletedProcess(
                cmd, 5, stdout="", stderr="Could not find service in domain"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ops.subprocess, "run", fake_subprocess_run)

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        results = ops._install_cassandra_launchagent()

    assert results[0].ok is True
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected the bootout non-zero exit to still be logged"
    assert all(r.levelno == logging.DEBUG for r in records)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.unit
def test_install_cassandra_launchagent_bootstrap_failure_logs_warning(
    monkeypatch, tmp_path, caplog
):
    """A genuine `launchctl bootstrap` failure is a real problem, unlike the
    expected bootout rc=5 -- it must stay at WARNING (#3457)."""
    tpl = tmp_path / "com.nyxgpt.cassandra-logs.plist"
    tpl.write_text(
        "<plist>__NYXGPT_HOME__/.nyxGPT/scripts/follow-cassandra-logs.sh</plist>",
        encoding="utf-8",
    )
    home = tmp_path / "home"

    monkeypatch.setattr(ops, "_find_launchagent_template", lambda: (tpl, [tpl]))
    monkeypatch.setattr(ops.Path, "home", lambda: home)

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[:2] == ["launchctl", "bootstrap"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Input/output error")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ops.subprocess, "run", fake_subprocess_run)

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        ops._install_cassandra_launchagent()

    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    bootstrap_records = [r for r in records if "bootstrap" in r.getMessage()]
    assert bootstrap_records, "Expected the bootstrap failure to be logged"
    assert all(r.levelno == logging.WARNING for r in bootstrap_records)


# --- _install_ollama_launchagent ---


@pytest.mark.unit
def test_install_ollama_launchagent_missing_template(monkeypatch):
    monkeypatch.setattr(
        ops, "_find_launchagent_template", lambda name: (None, [Path("/a"), Path("/b")])
    )
    results = ops._install_ollama_launchagent()
    assert results[0].ok is False
    assert "Missing Ollama logs LaunchAgent template" in results[0].message


@pytest.mark.unit
def test_install_ollama_launchagent_installs_when_template_found(monkeypatch, tmp_path):
    tpl = tmp_path / "com.nyxgpt.ollama-logs.plist"
    tpl.write_text(
        "<plist>__NYXGPT_HOME__/.nyxGPT/scripts/follow-ollama-logs.sh</plist>",
        encoding="utf-8",
    )
    home = tmp_path / "home"

    calls = []

    def fake_find(name):
        calls.append(name)
        return tpl, [tpl]

    monkeypatch.setattr(ops, "_find_launchagent_template", fake_find)
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    run_calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    results = ops._install_ollama_launchagent()
    assert results[0].ok is True
    dst = home / "Library" / "LaunchAgents" / tpl.name
    assert dst.exists()
    installed = dst.read_text(encoding="utf-8")
    assert "__NYXGPT_HOME__" not in installed
    assert f"{home}/.nyxGPT/scripts/follow-ollama-logs.sh" in installed
    assert len(run_calls) == 3
    assert calls == ["com.nyxgpt.ollama-logs.plist"]


@pytest.mark.unit
def test_install_launchagent_from_template_uses_installing_users_home(monkeypatch, tmp_path):
    """Regression test for #3276 acceptance failure: the merged plist
    templates hard-coded the original author's home directory, so
    `nyxgpt ops install` produced a non-functional LaunchAgent for every
    other user. `_install_launchagent_from_template` must substitute the
    placeholder for the actual installing user's home directory.
    """
    tpl = tmp_path / "com.nyxgpt.ollama-logs.plist"
    tpl.write_text(
        "<plist>__NYXGPT_HOME__/.nyxGPT/scripts/follow-ollama-logs.sh</plist>",
        encoding="utf-8",
    )
    home = tmp_path / "some-other-user"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    dst = tmp_path / "installed.plist"

    ops._install_launchagent_from_template(tpl, dst)

    installed = dst.read_text(encoding="utf-8")
    assert "__NYXGPT_HOME__" not in installed
    assert installed == f"<plist>{home}/.nyxGPT/scripts/follow-ollama-logs.sh</plist>"


# --- _persist_compose_file_path ---


@pytest.mark.unit
def test_persist_compose_file_path_records_path(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", repo)

    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    cfg_path = home / ".nyxGPT" / "config.ini"
    cfg_path.write_text("[nyxgpt]\n", encoding="utf-8")
    monkeypatch.setattr(ops.Path, "home", lambda: home)

    results = ops._persist_compose_file_path()
    assert results[0].ok is True
    assert "Recorded compose-file path" in results[0].message

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("paths", "compose_file") == str(repo / "docker-compose.yml")

    # Idempotent: second run reports already-recorded, file unchanged.
    again = ops._persist_compose_file_path()
    assert again[0].ok is True
    assert "already recorded" in again[0].message


@pytest.mark.unit
def test_persist_compose_file_path_skips_outside_repo_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path / "cellar")
    results = ops._persist_compose_file_path()
    assert results[0].ok is True
    assert "not running from a repo checkout" in results[0].message


@pytest.mark.unit
def test_persist_compose_file_path_skips_without_config(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", repo)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "empty-home")

    results = ops._persist_compose_file_path()
    assert results[0].ok is True
    assert "no config.ini yet" in results[0].message


# --- _ensure_ollama_service ---


def _fake_run_recording(run_calls, *, fail_cmds=()):
    """Build a fake `ops._run` that records every command and fails (rc=1,
    stderr="boom") for any command whose argv starts with one of `fail_cmds`,
    succeeding (rc=0) otherwise. Used across `_ensure_ollama_service` tests
    to isolate a single subprocess call's failure without breaking the
    `launchctl setenv`/migration calls that happen alongside it.
    """

    def _fake_run(cmd, **_k):
        run_calls.append(cmd)
        if any(cmd[: len(f)] == f for f in fail_cmds):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0)

    return _fake_run


@pytest.mark.unit
def test_ensure_ollama_service_no_brew():
    with patch.object(ops, "_which", lambda _: None):
        results = ops._ensure_ollama_service()
    assert results[0].ok is False
    assert "Homebrew not found" in results[0].message


@pytest.mark.unit
def test_ensure_ollama_service_already_running_and_env_already_configured(monkeypatch, tmp_path):
    """Once the shared store has been applied once (marker present), a
    subsequent run against an already-running service must stay a no-op --
    same idempotency contract as every other `nyxgpt ops install` reconciler.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    marker = home / ".nyxGPT" / ".migration-state" / "ollama-native-env.configured"
    marker.parent.mkdir(parents=True)
    marker.touch()

    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {"ollama": "started"}),
        patch.object(ops, "_run", _fake_run_recording(run_calls)),
    ):
        results = ops._ensure_ollama_service()

    assert all(r.ok for r in results)
    assert "already running" in results[-1].message
    assert ["brew", "services", "restart", "ollama"] not in run_calls
    assert [
        "launchctl",
        "setenv",
        "OLLAMA_MODELS",
        str(home / ".nyxGPT/volumes/ollama/models"),
    ] in (run_calls)


@pytest.mark.unit
def test_ensure_ollama_service_restarts_running_service_first_time_env_is_configured(
    monkeypatch, tmp_path
):
    """The very first time the shared store is applied (no marker yet)
    against an already-running service, `launchctl setenv` alone can't reach
    the already-spawned `ollama serve` process -- a one-time restart is
    needed so it actually picks up the new OLLAMA_MODELS.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)

    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {"ollama": "started"}),
        patch.object(ops, "_run", _fake_run_recording(run_calls)),
    ):
        results = ops._ensure_ollama_service()

    assert all(r.ok for r in results)
    assert "Restarted brew service: ollama" in results[-1].message
    assert ["brew", "services", "restart", "ollama"] in run_calls
    marker = home / ".nyxGPT" / ".migration-state" / "ollama-native-env.configured"
    assert marker.exists()


@pytest.mark.unit
def test_ensure_ollama_service_starts_stopped_service(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {"ollama": "none"}),
        patch.object(ops, "_run", _fake_run_recording(run_calls)),
    ):
        results = ops._ensure_ollama_service()
    assert all(r.ok for r in results)
    assert "Started brew service: ollama" in results[-1].message
    assert run_calls[-1] == ["brew", "services", "start", "ollama"]


@pytest.mark.unit
def test_ensure_ollama_service_installs_formula_when_absent(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {}),
        patch.object(ops, "_run", _fake_run_recording(run_calls)),
    ):
        results = ops._ensure_ollama_service()
    assert all(r.ok for r in results)
    assert any("Installed ollama formula" in r.message for r in results)
    assert ["brew", "install", "ollama"] in run_calls
    assert run_calls[-1] == ["brew", "services", "start", "ollama"]


@pytest.mark.unit
def test_ensure_ollama_service_start_failure_reports_details(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {"ollama": "none"}),
        patch.object(
            ops, "_run", _fake_run_recording(run_calls, fail_cmds=[["brew", "services", "start"]])
        ),
    ):
        results = ops._ensure_ollama_service()
    fail_result = next(r for r in results if not r.ok)
    assert "Failed to start brew service: ollama" in fail_result.message
    assert "boom" in fail_result.details


@pytest.mark.unit
def test_ensure_ollama_service_points_native_ollama_at_shared_models_dir(monkeypatch, tmp_path):
    """Direct check of issue #3431's core acceptance criterion: `nyxgpt ops
    install` must configure native Ollama's `OLLAMA_MODELS` to the same
    directory Compose/Terraform's `ollama` container stores models in.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {"ollama": "none"}),
        patch.object(ops, "_run", _fake_run_recording(run_calls)),
    ):
        ops._ensure_ollama_service()

    shared_models_dir = home / ".nyxGPT" / "volumes" / "ollama" / "models"
    assert ["launchctl", "setenv", "OLLAMA_MODELS", str(shared_models_dir)] in run_calls
    assert shared_models_dir.is_dir()


# --- _shared_ollama_models_dir / _migrate_native_ollama_models / _set_native_ollama_models_env ---


@pytest.mark.unit
def test_shared_ollama_models_dir_is_models_subdir_of_shared_ollama_volume(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    d = ops._shared_ollama_models_dir()
    assert d == home / ".nyxGPT" / "volumes" / "ollama" / "models"
    assert d.is_dir()


@pytest.mark.unit
def test_migrate_native_ollama_models_no_native_store(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    dest = home / ".nyxGPT" / "volumes" / "ollama" / "models"
    dest.mkdir(parents=True)

    results = ops._migrate_native_ollama_models(dest)
    assert results[0].ok is True
    assert "nothing to migrate" in results[0].message
    marker = home / ".nyxGPT" / ".migration-state" / "ollama-native-models.migrated"
    assert marker.exists()


@pytest.mark.unit
def test_migrate_native_ollama_models_merges_missing_files_without_overwriting(
    monkeypatch, tmp_path
):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)

    src = home / ".ollama" / "models"
    (src / "blobs").mkdir(parents=True)
    (src / "manifests" / "registry.ollama.ai" / "library" / "qwen3").mkdir(parents=True)
    (src / "blobs" / "sha256-only-in-native").write_text("native blob data")
    (src / "blobs" / "sha256-in-both").write_text("native copy")
    (src / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "latest").write_text(
        "native manifest"
    )

    dest = home / ".nyxGPT" / "volumes" / "ollama" / "models"
    (dest / "blobs").mkdir(parents=True)
    (dest / "blobs" / "sha256-in-both").write_text("shared store copy (authoritative)")

    results = ops._migrate_native_ollama_models(dest)
    assert results[0].ok is True
    assert "Merged 2 native Ollama model file(s)" in results[0].message

    # Missing files get copied in...
    assert (dest / "blobs" / "sha256-only-in-native").read_text() == "native blob data"
    assert (
        dest / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "latest"
    ).read_text() == "native manifest"
    # ...but a file already present at the destination is never overwritten.
    assert (dest / "blobs" / "sha256-in-both").read_text() == "shared store copy (authoritative)"

    marker = home / ".nyxGPT" / ".migration-state" / "ollama-native-models.migrated"
    assert marker.exists()

    # Merged files are hardlinked (same inode), not copied, so multi-GB blobs
    # merge instantly with no extra disk use.
    assert (dest / "blobs" / "sha256-only-in-native").stat().st_ino == (
        src / "blobs" / "sha256-only-in-native"
    ).stat().st_ino


@pytest.mark.unit
def test_migrate_native_ollama_models_falls_back_to_copy_across_devices(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)

    src = home / ".ollama" / "models" / "blobs"
    src.mkdir(parents=True)
    (src / "sha256-cross-device").write_text("native blob data")

    dest = home / ".nyxGPT" / "volumes" / "ollama" / "models"
    dest.mkdir(parents=True)

    with patch.object(ops.os, "link", side_effect=OSError("cross-device link")):
        results = ops._migrate_native_ollama_models(dest)

    assert results[0].ok is True
    assert (dest / "blobs" / "sha256-cross-device").read_text() == "native blob data"


@pytest.mark.unit
def test_migrate_native_ollama_models_marker_skips_rescan_on_later_runs(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    marker = home / ".nyxGPT" / ".migration-state" / "ollama-native-models.migrated"
    marker.parent.mkdir(parents=True)
    marker.touch()

    src = home / ".ollama" / "models" / "blobs"
    src.mkdir(parents=True)
    (src / "sha256-should-be-ignored").write_text("should not be touched")

    dest = home / ".nyxGPT" / "volumes" / "ollama" / "models"
    dest.mkdir(parents=True)

    results = ops._migrate_native_ollama_models(dest)
    assert results[0].ok is True
    assert "already reconciled" in results[0].message
    assert not (dest / "blobs" / "sha256-should-be-ignored").exists()


@pytest.mark.unit
def test_set_native_ollama_models_env_success(tmp_path):
    run_calls = []
    with patch.object(
        ops, "_run", lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0)
    ):
        result = ops._set_native_ollama_models_env(tmp_path / "models")
    assert result.ok is True
    assert str(tmp_path / "models") in result.message
    assert run_calls == [["launchctl", "setenv", "OLLAMA_MODELS", str(tmp_path / "models")]]


@pytest.mark.unit
def test_set_native_ollama_models_env_failure_reports_details(tmp_path):
    with patch.object(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no launchd"),
    ):
        result = ops._set_native_ollama_models_env(tmp_path / "models")
    assert result.ok is False
    assert "Failed to set OLLAMA_MODELS via launchctl setenv" in result.message
    assert "no launchd" in result.details


# --- _ollama_env_drift_issue ---


@pytest.mark.unit
def test_ollama_env_drift_issue_none_without_launchctl(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    assert ops._ollama_env_drift_issue() is None


@pytest.mark.unit
def test_ollama_env_drift_issue_none_when_never_configured(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/bin/launchctl")
    assert ops._ollama_env_drift_issue() is None


@pytest.mark.unit
def test_ollama_env_drift_issue_none_when_env_matches(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/bin/launchctl")
    marker = home / ".nyxGPT" / ".migration-state" / "ollama-native-env.configured"
    marker.parent.mkdir(parents=True)
    marker.touch()
    expected = home / ".nyxGPT" / "volumes" / "ollama" / "models"
    with patch.object(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=f"{expected}\n"),
    ):
        assert ops._ollama_env_drift_issue() is None


@pytest.mark.unit
def test_ollama_env_drift_issue_flags_unset_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/bin/launchctl")
    marker = home / ".nyxGPT" / ".migration-state" / "ollama-native-env.configured"
    marker.parent.mkdir(parents=True)
    marker.touch()
    with patch.object(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="")):
        issue = ops._ollama_env_drift_issue()
    assert issue is not None
    assert "not set for this login session" in issue


@pytest.mark.unit
def test_ollama_env_drift_issue_flags_mismatched_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/bin/launchctl")
    marker = home / ".nyxGPT" / ".migration-state" / "ollama-native-env.configured"
    marker.parent.mkdir(parents=True)
    marker.touch()
    stale = home / ".ollama" / "models"
    with patch.object(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=f"{stale}\n"),
    ):
        issue = ops._ollama_env_drift_issue()
    assert issue is not None
    assert "not the shared store" in issue
    assert str(stale) in issue


# --- _install_ollama_env_launchagent ---


@pytest.mark.unit
def test_install_ollama_env_launchagent_missing_template(monkeypatch):
    monkeypatch.setattr(
        ops, "_find_launchagent_template", lambda name: (None, [Path("/a"), Path("/b")])
    )
    results = ops._install_ollama_env_launchagent()
    assert results[0].ok is False
    assert "Missing Ollama env LaunchAgent template" in results[0].message


@pytest.mark.unit
def test_install_ollama_env_launchagent_installs_when_template_found(monkeypatch, tmp_path):
    tpl = tmp_path / "com.nyxgpt.ollama-env.plist"
    tpl.write_text("<plist>__NYXGPT_HOME__/.nyxGPT/scripts/set-ollama-models-env.sh</plist>")
    home = tmp_path / "home"

    def fake_find(name):
        assert name == "com.nyxgpt.ollama-env.plist"
        return tpl, [tpl]

    monkeypatch.setattr(ops, "_find_launchagent_template", fake_find)
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))

    results = ops._install_ollama_env_launchagent()
    assert results[0].ok is True
    assert "Installed Ollama env LaunchAgent" in results[0].message
    installed = home / "Library" / "LaunchAgents" / "com.nyxgpt.ollama-env.plist"
    assert installed.exists()
    assert str(home) in installed.read_text(encoding="utf-8")


# --- _ensure_cassandra_container ---


@pytest.mark.unit
def test_ensure_cassandra_container_no_docker():
    with patch.object(ops, "_which", lambda _: None):
        results = ops._ensure_cassandra_container()
    assert results[0].ok is False
    assert "docker not found" in results[0].message


@pytest.mark.unit
def test_ensure_cassandra_container_already_running():
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(
            ops,
            "_docker_container_state",
            lambda name: "running" if name == "nyxgpt-cassandra" else "absent",
        ),
    ):
        results = ops._ensure_cassandra_container()
    assert results[0].ok is True
    assert "already running" in results[0].message


@pytest.mark.unit
def test_ensure_cassandra_container_refuses_when_terraform_cassandra_running():
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(
            ops,
            "_docker_container_state",
            lambda name: "running" if name == "nyxgpt-tf-cassandra" else "absent",
        ),
    ):
        results = ops._ensure_cassandra_container()
    assert results[0].ok is False
    assert "Terraform-managed Cassandra" in results[0].message


@pytest.mark.unit
def test_ensure_cassandra_container_starts_existing_stopped_container():
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_container_state", lambda name: "exited"),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        ),
    ):
        results = ops._ensure_cassandra_container()
    assert results[0].ok is True
    assert "Started existing" in results[0].message
    assert run_calls == [["docker", "start", "nyxgpt-cassandra"]]


@pytest.mark.unit
def test_ensure_cassandra_container_start_failure_reports_details():
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_container_state", lambda name: "exited"),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
        ),
    ):
        results = ops._ensure_cassandra_container()
    assert results[0].ok is False
    assert "Failed to start existing" in results[0].message
    assert "boom" in results[0].details


@pytest.mark.unit
def test_ensure_cassandra_container_creates_when_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("NYXGPT_BIND_ADDR", raising=False)
    monkeypatch.delenv("CASSANDRA_PORT", raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_container_state", lambda name: "absent"),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        ),
    ):
        results = ops._ensure_cassandra_container()
    assert results[0].ok is True
    assert "Created Cassandra container" in results[0].message
    data_dir = home / ".nyxGPT" / "volumes" / "cassandra"
    assert data_dir.is_dir()
    assert run_calls == [
        [
            "docker",
            "run",
            "-d",
            "--name",
            "nyxgpt-cassandra",
            "--restart",
            "unless-stopped",
            "-p",
            "127.0.0.1:9042:9042",
            "-v",
            f"{data_dir}:/var/lib/cassandra",
            "-e",
            "CASSANDRA_CLUSTER_NAME=nyxgpt",
            "cassandra:5.0.8",
        ]
    ]


@pytest.mark.unit
def test_ensure_cassandra_container_creates_with_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("NYXGPT_BIND_ADDR", "0.0.0.0")
    monkeypatch.setenv("CASSANDRA_PORT", "19042")
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_container_state", lambda name: "absent"),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        ),
    ):
        ops._ensure_cassandra_container()
    assert "-p" in run_calls[0]
    assert run_calls[0][run_calls[0].index("-p") + 1] == "0.0.0.0:19042:9042"


@pytest.mark.unit
def test_ensure_cassandra_container_create_failure_reports_details(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_container_state", lambda name: "absent"),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no such image"),
        ),
    ):
        results = ops._ensure_cassandra_container()
    assert results[0].ok is False
    assert "Failed to create Cassandra container" in results[0].message
    assert "no such image" in results[0].details


# --- Legacy named-volume migration (issue #3346) ---


@pytest.mark.unit
def test_docker_volume_exists_true_and_false():
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0)),
    ):
        assert ops._docker_volume_exists("nyxgpt_cassandra_data") is True

    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1)),
    ):
        assert ops._docker_volume_exists("nyxgpt_cassandra_data") is False


@pytest.mark.unit
def test_docker_volume_exists_false_without_docker():
    with patch.object(ops, "_which", lambda _: None):
        assert ops._docker_volume_exists("anything") is False


@pytest.mark.unit
def test_migrate_docker_volume_to_bind_dir_success(tmp_path):
    dest = tmp_path / "cassandra"
    dest.mkdir()
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    with patch.object(ops, "_run", fake_run):
        result = ops._migrate_docker_volume_to_bind_dir(
            "nyxgpt_cassandra_data", dest, label="cassandra"
        )
    assert result.ok is True
    assert "Migrated cassandra data" in result.message
    assert "Old volume removed" in result.details
    assert calls[0][:3] == ["docker", "run", "--rm"]
    assert calls[1] == ["docker", "volume", "rm", "nyxgpt_cassandra_data"]


@pytest.mark.unit
def test_migrate_docker_volume_to_bind_dir_copy_failure(tmp_path):
    dest = tmp_path / "cassandra"
    dest.mkdir()
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="copy boom")

    with patch.object(ops, "_run", fake_run):
        result = ops._migrate_docker_volume_to_bind_dir(
            "nyxgpt_cassandra_data", dest, label="cassandra"
        )
    assert result.ok is False
    assert "Failed to migrate cassandra data" in result.message
    assert "copy boom" in result.details
    # Copy failed -- must not attempt to remove the source volume.
    assert len(calls) == 1


@pytest.mark.unit
def test_migrate_docker_volume_to_bind_dir_rm_failure_is_non_fatal(tmp_path):
    dest = tmp_path / "cassandra"
    dest.mkdir()

    def fake_run(cmd, **k):
        if cmd[:3] == ["docker", "volume", "rm"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="volume in use")
        return subprocess.CompletedProcess(cmd, 0)

    with patch.object(ops, "_run", fake_run):
        result = ops._migrate_docker_volume_to_bind_dir(
            "nyxgpt_cassandra_data", dest, label="cassandra"
        )
    assert result.ok is True
    assert "Migrated cassandra data" in result.message
    assert "Could not remove the old volume" in result.details


@pytest.mark.unit
def test_migrate_docker_volume_to_bind_dir_rm_failure_logs_debug_not_warning(
    tmp_path, monkeypatch, caplog
):
    """The best-effort `docker volume rm` teardown-if-present is non-fatal by
    design (#3457) -- a failure to remove the old volume must log at DEBUG,
    never WARNING."""
    dest = tmp_path / "cassandra"
    dest.mkdir()

    def fake_subprocess_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "volume", "rm"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="volume in use")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ops.subprocess, "run", fake_subprocess_run)

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        result = ops._migrate_docker_volume_to_bind_dir(
            "nyxgpt_cassandra_data", dest, label="cassandra"
        )

    assert result.ok is True
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected the rm non-zero exit to still be logged"
    assert all(r.levelno == logging.DEBUG for r in records)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.unit
def test_migrate_legacy_volumes_skipped_without_docker(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    with patch.object(ops, "_which", lambda _: None):
        results = ops.migrate_legacy_volumes()
    assert len(results) == 1
    assert results[0].ok is True
    assert "docker not found" in results[0].message


@pytest.mark.unit
def test_migrate_legacy_volumes_refuses_when_dest_populated_and_legacy_volume_still_exists(
    monkeypatch, tmp_path
):
    """Regression test for the data-stranding bug found in review of #3346.

    If a legacy volume is still present but the destination directory is
    already non-empty (e.g. the new bind-mounted stack was started before
    `migrate-volumes` ran) and we've never confirmed reconciliation for this
    component, migration must fail loudly rather than silently reporting
    "already has data -- skipping" and leaving the old volume un-migrated
    and un-flagged.
    """
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    cassandra_dir = home / ".nyxGPT" / "volumes" / "cassandra"
    cassandra_dir.mkdir(parents=True)
    (cassandra_dir / "system.log").write_text("freshly created by the new bind-mounted service")

    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_volume_exists", lambda name: True),
    ):
        results = ops.migrate_legacy_volumes()

    cassandra_result = next(r for r in results if r.message.startswith("cassandra:"))
    assert cassandra_result.ok is False
    assert "refusing to auto-migrate" in cassandra_result.message
    # No marker written -- must keep warning on every subsequent run until
    # the user resolves it by hand.
    assert not (home / ".nyxGPT" / ".migration-state" / "cassandra.migrated").exists()


@pytest.mark.unit
def test_migrate_legacy_volumes_populated_dest_with_no_legacy_volume_is_fine(monkeypatch, tmp_path):
    """A destination populated by a genuinely fresh install (no prior named
    volumes ever existed) must not be mistaken for the stranding scenario."""
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    cassandra_dir = home / ".nyxGPT" / "volumes" / "cassandra"
    cassandra_dir.mkdir(parents=True)
    (cassandra_dir / "system.log").write_text("fresh install, never had named volumes")

    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_volume_exists", lambda name: False),
    ):
        results = ops.migrate_legacy_volumes()

    cassandra_result = next(r for r in results if "cassandra" in r.message)
    assert cassandra_result.ok is True
    assert "nothing to migrate" in cassandra_result.message
    assert (home / ".nyxGPT" / ".migration-state" / "cassandra.migrated").exists()


@pytest.mark.unit
def test_migrate_legacy_volumes_marker_skips_recheck_on_later_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    exists_calls = []

    def fake_exists(name):
        exists_calls.append(name)
        return False

    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_volume_exists", fake_exists),
    ):
        first = ops.migrate_legacy_volumes()
        calls_after_first_run = len(exists_calls)
        second = ops.migrate_legacy_volumes()

    assert all(r.ok for r in first)
    assert all(r.ok and "already reconciled" in r.message for r in second)
    # Second run must not re-check Docker for components already marked reconciled.
    assert len(exists_calls) == calls_after_first_run


@pytest.mark.unit
def test_migrate_legacy_volumes_reports_nothing_to_migrate(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_volume_exists", lambda name: False),
    ):
        results = ops.migrate_legacy_volumes()
    assert len(results) == len(ops.LEGACY_VOLUME_SOURCES)
    assert all(r.ok and "nothing to migrate" in r.message for r in results)


@pytest.mark.unit
def test_migrate_legacy_volumes_migrates_first_candidate_found(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    migrated = []

    def fake_migrate(volume_name, dest_dir, *, label):
        migrated.append((volume_name, label))
        return ops.OpsResult(True, f"Migrated {label} data from legacy volume '{volume_name}'")

    # Only the *second* candidate for cassandra (Terraform's) exists.
    def fake_exists(name):
        return name == "nyxgpt_tf_cassandra_data"

    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_volume_exists", fake_exists),
        patch.object(ops, "_migrate_docker_volume_to_bind_dir", fake_migrate),
    ):
        results = ops.migrate_legacy_volumes()

    assert ("nyxgpt_tf_cassandra_data", "cassandra") in migrated
    cassandra_result = next(
        r for r in results if "cassandra" in r.message and "Migrated" in r.message
    )
    assert cassandra_result.ok is True


@pytest.mark.unit
def test_migrate_legacy_volumes_notes_unmigrated_second_candidate(monkeypatch, tmp_path):
    """When both a Compose-era and Terraform-era legacy volume exist for the
    same shared destination, only the first (priority order) is migrated --
    but the second must be surfaced to the user, not silently dropped."""
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")

    def fake_migrate(volume_name, dest_dir, *, label):
        return ops.OpsResult(
            True,
            f"Migrated {label} data from legacy volume '{volume_name}'",
            "Old volume removed.",
        )

    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_docker_volume_exists", lambda name: True),
        patch.object(ops, "_migrate_docker_volume_to_bind_dir", fake_migrate),
    ):
        results = ops.migrate_legacy_volumes()

    cassandra_result = next(
        r for r in results if "cassandra" in r.message and "Migrated" in r.message
    )
    assert cassandra_result.ok is True
    assert "nyxgpt_tf_cassandra_data" in cassandra_result.details
    assert "also found but not migrated" in cassandra_result.details


@pytest.mark.unit
def test_migrate_volumes_cmd_returns_0_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    with patch.object(ops, "_which", lambda _: None):
        assert ops.migrate_volumes_cmd(SimpleNamespace()) == 0


@pytest.mark.unit
def test_migrate_volumes_cmd_returns_2_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")

    def fake_migrate():
        return [ops.OpsResult(False, "boom")]

    with patch.object(ops, "migrate_legacy_volumes", fake_migrate):
        assert ops.migrate_volumes_cmd(SimpleNamespace()) == 2


# --- _detect_phantom_compose_app_containers / _reconcile_phantom_compose_app_containers ---


@pytest.mark.unit
def test_detect_phantom_compose_app_containers_filters_to_core_app_services():
    with patch.object(
        ops,
        "_compose_stack_snapshot",
        return_value={"api": "running", "grafana": "running", "web": "exited"},
    ):
        phantoms = ops._detect_phantom_compose_app_containers()
    assert phantoms == {"api": "running"}


@pytest.mark.unit
def test_reconcile_phantom_compose_app_containers_no_docker():
    with patch.object(ops, "_which", lambda _: None):
        results = ops._reconcile_phantom_compose_app_containers()
    assert results == []


@pytest.mark.unit
def test_reconcile_phantom_compose_app_containers_none_detected():
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(ops, "_detect_phantom_compose_app_containers", return_value={}),
    ):
        results = ops._reconcile_phantom_compose_app_containers()
    assert results[0].ok is True
    assert "No phantom" in results[0].message


@pytest.mark.unit
def test_reconcile_phantom_compose_app_containers_stops_each_phantom():
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(
            ops,
            "_detect_phantom_compose_app_containers",
            return_value={"api": "running", "cassandra": "running"},
        ),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        ),
    ):
        results = ops._reconcile_phantom_compose_app_containers()
    assert len(results) == 2
    assert all(r.ok for r in results)
    assert any("api" in r.message for r in results)
    assert any("cassandra" in r.message for r in results)
    stopped_services = {cmd[-1] for cmd in run_calls}
    assert stopped_services == {"api", "cassandra"}


@pytest.mark.unit
def test_reconcile_phantom_compose_app_containers_reports_per_service_failure():
    with (
        patch.object(ops, "_which", lambda _: "/usr/local/bin/docker"),
        patch.object(
            ops, "_detect_phantom_compose_app_containers", return_value={"api": "running"}
        ),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="conflict"),
        ),
    ):
        results = ops._reconcile_phantom_compose_app_containers()
    assert results[0].ok is False
    assert "Failed to stop phantom Compose container for api" in results[0].message
    assert "conflict" in results[0].details


# --- _cleanup_stale_log_symlinks ---


@pytest.mark.unit
def test_cleanup_stale_log_symlinks_removes_existing_symlinks(monkeypatch, tmp_path):
    home = tmp_path / "home"
    log_dir = home / ".nyxGPT" / "logs"
    log_dir.mkdir(parents=True)
    stale_target = tmp_path / "brew-nyxgpt-api.log"
    stale_target.write_text("stale", encoding="utf-8")
    for name in ops._STALE_LOG_SYMLINK_NAMES:
        (log_dir / name).symlink_to(stale_target)

    monkeypatch.setattr(ops.Path, "home", lambda: home)

    results = ops._cleanup_stale_log_symlinks()
    assert all(r.ok for r in results)
    assert len(results) == len(ops._STALE_LOG_SYMLINK_NAMES)
    for name in ops._STALE_LOG_SYMLINK_NAMES:
        assert not (log_dir / name).exists()
        assert not (log_dir / name).is_symlink()


@pytest.mark.unit
def test_cleanup_stale_log_symlinks_leaves_real_files_and_missing_names_alone(
    monkeypatch, tmp_path
):
    home = tmp_path / "home"
    log_dir = home / ".nyxGPT" / "logs"
    log_dir.mkdir(parents=True)
    # A real (non-symlink) file at one of the names must be left in place --
    # only symlinks are considered stale.
    real_file = log_dir / ops._STALE_LOG_SYMLINK_NAMES[0]
    real_file.write_text("real content\n", encoding="utf-8")

    monkeypatch.setattr(ops.Path, "home", lambda: home)

    results = ops._cleanup_stale_log_symlinks()
    assert all(r.ok for r in results)
    assert real_file.read_text(encoding="utf-8") == "real content\n"


@pytest.mark.unit
def test_cleanup_stale_log_symlinks_reports_failure_on_exception(monkeypatch, tmp_path):
    home = tmp_path / "home"
    log_dir = home / ".nyxGPT" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / ops._STALE_LOG_SYMLINK_NAMES[0]).symlink_to(tmp_path / "missing-target.log")

    monkeypatch.setattr(ops.Path, "home", lambda: home)

    def raise_unlink(self):
        raise OSError("cannot unlink")

    monkeypatch.setattr(ops.Path, "unlink", raise_unlink)

    results = ops._cleanup_stale_log_symlinks()
    failed = next(r for r in results if ops._STALE_LOG_SYMLINK_NAMES[0] in r.message)
    assert failed.ok is False
    assert "Failed to remove stale log symlink" in failed.message


@pytest.mark.unit
def test_follow_ollama_logs_script_never_hard_requires_docker():
    """Regression guard for #3441: the old script `exit 1`'d immediately if
    `docker` wasn't on PATH, which would kill the LaunchAgent on a host
    that's never installed Docker Compose-mode tooling even though native
    Ollama log tailing needs no docker at all."""
    script = (ops.REPO_ROOT / "scripts" / "follow-ollama-logs.sh").read_text(encoding="utf-8")
    assert "exit 1" not in script


@pytest.mark.unit
def test_follow_ollama_logs_script_tails_native_homebrew_log():
    """The script must fall back to tailing Homebrew's own ollama.log
    directly (never a symlink into it) when no Compose container exists --
    a symlink target outside ~/.nyxGPT/logs is unreachable from inside
    promtail's container, which only bind-mounts that one directory (#3441)."""
    script = (ops.REPO_ROOT / "scripts" / "follow-ollama-logs.sh").read_text(encoding="utf-8")
    assert "brew --prefix" in script
    assert "var/log/ollama.log" in script
    assert "-L" in script  # detects (and removes) a stale symlink


# --- _create_dist_tarball ---


def _make_fake_api_repo_root(tmp_path):
    """A fake REPO_ROOT with just enough of a real nyxgpt package to vendor."""
    repo_root = tmp_path / "repo"
    (repo_root / "src" / "nyxgpt").mkdir(parents=True)
    (repo_root / "src" / "nyxgpt" / "__init__.py").write_text("", encoding="utf-8")
    (repo_root / "src" / "nyxgpt" / "app.py").write_text("# fake app module\n", encoding="utf-8")
    (repo_root / "pyproject.toml").write_text(
        '[project]\nname = "nyxGPT"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    # config_wizard builds its schema from example.config.ini at import time,
    # so the api tarball ships it for the venv install to find (#3406).
    (repo_root / "example.config.ini").write_text("[nyxgpt]\n", encoding="utf-8")
    return repo_root


def _make_fake_web_repo_root(tmp_path):
    """A fake REPO_ROOT with a web/ tree including gitignored build artifacts."""
    repo_root = tmp_path / "repo"
    web_dir = repo_root / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "package.json").write_text('{"name": "web"}\n', encoding="utf-8")
    (web_dir / "node_modules" / "somepkg").mkdir(parents=True)
    (web_dir / "node_modules" / "somepkg" / "index.js").write_text("", encoding="utf-8")
    (web_dir / ".next" / "cache").mkdir(parents=True)
    (web_dir / ".next" / "cache" / "x").write_text("", encoding="utf-8")
    return repo_root


@pytest.mark.unit
def test_create_dist_tarball_vendors_real_api_source(monkeypatch, tmp_path):
    """The api tarball vendors pyproject.toml + src/nyxgpt/ -- not a placeholder
    README -- so the formula can `pip install` a self-contained app (#3406)."""
    repo_root = _make_fake_api_repo_root(tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)

    tar_path = ops._create_dist_tarball(tmp_path, "nyxgpt-api", "1.2.3")
    assert tar_path == tmp_path / "dist" / "nyxgpt-api-1.2.3.tar.gz"
    assert tar_path.exists()
    with tarfile.open(tar_path, "r:gz") as tf:
        names = tf.getnames()
    assert "nyxgpt-api-1.2.3/pyproject.toml" in names
    # example.config.ini rides along so the venv install can place it next to
    # the package -- config_wizard needs it at import time (#3406, #3388).
    assert "nyxgpt-api-1.2.3/example.config.ini" in names
    assert "nyxgpt-api-1.2.3/src/nyxgpt/app.py" in names
    assert not any("README.txt" in n for n in names)
    # Temp staging dir must be cleaned up.
    assert not (tmp_path / "dist" / ".tmp-nyxgpt-api-1.2.3").exists()


@pytest.mark.unit
def test_create_dist_tarball_vendors_web_source_excluding_build_artifacts(monkeypatch, tmp_path):
    """The web tarball vendors web/ source, but never the gitignored
    node_modules/.next build output -- the formula rebuilds those fresh
    inside the Cellar keg (#3406)."""
    repo_root = _make_fake_web_repo_root(tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)

    tar_path = ops._create_dist_tarball(tmp_path, "nyxgpt-web", "1.2.3")
    with tarfile.open(tar_path, "r:gz") as tf:
        names = tf.getnames()
    assert "nyxgpt-web-1.2.3/package.json" in names
    assert not any("node_modules" in n for n in names)
    assert not any(".next" in n for n in names)


@pytest.mark.unit
def test_create_dist_tarball_overwrites_existing_tarball_and_tmp(monkeypatch, tmp_path):
    repo_root = _make_fake_api_repo_root(tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)

    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    existing = dist_dir / "nyxgpt-api-1.2.3.tar.gz"
    existing.write_text("stale", encoding="utf-8")
    tmp_dir = dist_dir / ".tmp-nyxgpt-api-1.2.3"
    tmp_dir.mkdir()
    (tmp_dir / "leftover.txt").write_text("leftover", encoding="utf-8")

    tar_path = ops._create_dist_tarball(tmp_path, "nyxgpt-api", "1.2.3")
    assert tar_path.exists()
    # No longer the stale plaintext content.
    with tarfile.open(tar_path, "r:gz") as tf:
        names = tf.getnames()
    assert "nyxgpt-api-1.2.3/pyproject.toml" in names


# --- _install_homebrew_api / _install_homebrew_web ---


@pytest.mark.unit
def test_install_homebrew_api_no_brew(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._install_homebrew_api()
    assert results[0].ok is False
    assert "Homebrew not found" in results[0].message


@pytest.mark.unit
def test_install_homebrew_api_missing_template(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    results = ops._install_homebrew_api()
    assert results[0].ok is False
    assert "Missing homebrew/nyxgpt-api.rb" in results[0].message


@pytest.mark.unit
def test_install_homebrew_api_success(monkeypatch, tmp_path):
    repo_root = _make_fake_api_repo_root(tmp_path)
    (repo_root / "homebrew").mkdir(parents=True)
    (repo_root / "homebrew" / "nyxgpt-api.rb").write_text(
        'url "file://tap/dist/nyxgpt-api-__VERSION__.tar.gz"\n'
        'sha256 "__SHA256__"\n'
        'version "__VERSION__"\n',
        encoding="utf-8",
    )
    tap_dir = tmp_path / "tap"

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)
    monkeypatch.setattr(ops, "_tap_repo", lambda tap: tap_dir)
    monkeypatch.setattr(ops, "_read_project_version", lambda: "2.0.0")
    run_calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    results = ops._install_homebrew_api()
    assert all(r.ok for r in results)
    formula = tap_dir / "Formula" / "nyxgpt-api.rb"
    assert formula.exists()
    content = formula.read_text(encoding="utf-8")
    assert "__VERSION__" not in content
    assert "__SHA256__" not in content
    assert 'version "2.0.0"' in content
    assert "nyxgpt-api-2.0.0.tar.gz" in content
    assert any(cmd[:2] in (["brew", "install"], ["brew", "reinstall"]) for cmd in run_calls)
    assert any(cmd[:3] == ["brew", "services", "start"] for cmd in run_calls)


@pytest.mark.unit
def test_install_homebrew_api_stamps_stale_concrete_values(monkeypatch, tmp_path):
    # Regex safety net: a formula copy still carrying concrete (stale) values
    # from before the placeholder templates -- e.g. a hardcoded 1.0.0 -- is
    # rewritten to the current version/sha, not installed as-is.
    repo_root = _make_fake_api_repo_root(tmp_path)
    (repo_root / "homebrew").mkdir(parents=True)
    (repo_root / "homebrew" / "nyxgpt-api.rb").write_text(
        'url "file://tap/dist/nyxgpt-api-1.0.0.tar.gz"\n'
        'sha256 "0000000000000000000000000000000000000000000000000000000000000000"\n'
        'version "1.0.0"\n',
        encoding="utf-8",
    )
    tap_dir = tmp_path / "tap"

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)
    monkeypatch.setattr(ops, "_tap_repo", lambda tap: tap_dir)
    monkeypatch.setattr(ops, "_read_project_version", lambda: "2.0.0")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))

    results = ops._install_homebrew_api()
    assert all(r.ok for r in results)
    content = (tap_dir / "Formula" / "nyxgpt-api.rb").read_text(encoding="utf-8")
    assert 'version "2.0.0"' in content
    assert "nyxgpt-api-2.0.0.tar.gz" in content
    assert "1.0.0" not in content.replace("2.0.0", "")
    assert 'sha256 "0000' not in content


@pytest.mark.unit
def test_install_homebrew_web_no_brew(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._install_homebrew_web()
    assert results[0].ok is False
    assert "Homebrew not found" in results[0].message


@pytest.mark.unit
def test_install_homebrew_web_missing_template(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    results = ops._install_homebrew_web()
    assert results[0].ok is False
    assert "Missing homebrew/nyxgpt-web.rb" in results[0].message


@pytest.mark.unit
def test_install_homebrew_web_success(monkeypatch, tmp_path):
    repo_root = _make_fake_web_repo_root(tmp_path)
    (repo_root / "homebrew").mkdir(parents=True)
    (repo_root / "homebrew" / "nyxgpt-web.rb").write_text(
        'url "__NYXGPT_WEB_URL__"\nsha256 "__NYXGPT_WEB_SHA256__"\nversion "__VERSION__"\n',
        encoding="utf-8",
    )
    tap_dir = tmp_path / "tap"

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)
    monkeypatch.setattr(ops, "_tap_repo", lambda tap: tap_dir)
    monkeypatch.setattr(ops, "_read_project_version", lambda: "2.0.0")
    run_calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    results = ops._install_homebrew_web()
    assert all(r.ok for r in results)
    formula = tap_dir / "Formula" / "nyxgpt-web.rb"
    content = formula.read_text(encoding="utf-8")
    assert "__NYXGPT_WEB_URL__" not in content
    assert "__NYXGPT_WEB_SHA256__" not in content
    assert "__VERSION__" not in content
    assert any(cmd[:2] in (["brew", "install"], ["brew", "reinstall"]) for cmd in run_calls)
    # A fresh install/reinstall must restart (not merely start) the service so
    # the running `next start` process actually picks up the newly built
    # `.next` output instead of continuing to serve the old build's chunk
    # manifest against the new on-disk chunks (#3445).
    assert any(cmd[:3] == ["brew", "services", "restart"] for cmd in run_calls)
    assert not any(cmd[:3] == ["brew", "services", "start"] for cmd in run_calls)


@pytest.mark.unit
def test_install_homebrew_web_already_up_to_date_uses_start_not_restart(monkeypatch, tmp_path):
    """When the vendored web source hasn't changed since the last install,
    `_brew_install_or_reinstall` skips the rebuild entirely -- and this must
    only `start` (idempotent) the service rather than bounce an
    already-healthy running process for no reason (#3445)."""
    repo_root = _make_fake_web_repo_root(tmp_path)
    (repo_root / "homebrew").mkdir(parents=True)
    (repo_root / "homebrew" / "nyxgpt-web.rb").write_text(
        'url "__NYXGPT_WEB_URL__"\nsha256 "__NYXGPT_WEB_SHA256__"\nversion "__VERSION__"\n',
        encoding="utf-8",
    )
    tap_dir = tmp_path / "tap"

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)
    monkeypatch.setattr(ops, "_tap_repo", lambda tap: tap_dir)
    monkeypatch.setattr(ops, "_read_project_version", lambda: "2.0.0")
    monkeypatch.setattr(
        ops,
        "_brew_install_or_reinstall",
        lambda *a, **k: "already up to date (skipped reinstall)",
    )
    run_calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    results = ops._install_homebrew_web()
    assert all(r.ok for r in results)
    assert any(cmd[:3] == ["brew", "services", "start"] for cmd in run_calls)
    assert not any(cmd[:3] == ["brew", "services", "restart"] for cmd in run_calls)


# --- real formula invariants (regression guards for launchd error 78, #3406) ---


@pytest.mark.unit
def test_web_formula_launches_via_bash_and_restores_typescript():
    """Homebrew's post-install Cleaner strips the wrapper's exec bit to 0444, so
    the service must invoke it via /bin/bash (a direct exec fails with launchd
    error 78). And `npm prune --omit=dev` removes TypeScript, which `next start`
    needs to load next.config.ts, so the formula must restore it (#3406)."""
    formula = Path(__file__).resolve().parents[2] / "homebrew" / "nyxgpt-web.rb"
    text = formula.read_text(encoding="utf-8")
    assert 'run ["/bin/bash", opt_bin/"nyxgpt-web"]' in text
    assert '"npm", "install", "--no-save", "typescript"' in text


@pytest.mark.unit
def test_api_formula_launches_via_bash():
    """The api service also invokes its wrapper via /bin/bash, so Cleaner
    stripping the exec bit can't cause launchd error 78 (#3406)."""
    formula = Path(__file__).resolve().parents[2] / "homebrew" / "nyxgpt-api.rb"
    text = formula.read_text(encoding="utf-8")
    assert 'run ["/bin/bash", opt_bin/"nyxgpt-api"]' in text


# --- _brew_install_or_reinstall ---


@pytest.mark.unit
def test_brew_install_or_reinstall_installs_when_not_present(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: (
            calls.append(cmd) or subprocess.CompletedProcess(cmd, 1)
            if cmd[:3] == ["brew", "list", "--versions"]
            else calls.append(cmd) or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    decision = ops._brew_install_or_reinstall(
        "tap/nyxgpt-api", "nyxgpt-api", sha256="abc123", marker_dir=tmp_path
    )
    assert decision == "installed"
    assert ["brew", "fetch", "--force", "tap/nyxgpt-api"] in calls
    assert ["brew", "install", "--overwrite", "tap/nyxgpt-api"] in calls
    assert (tmp_path / ".nyxgpt-api.sha256").read_text(encoding="utf-8") == "abc123"


@pytest.mark.unit
def test_brew_install_or_reinstall_reinstalls_when_source_changed(monkeypatch, tmp_path):
    (tmp_path / ".nyxgpt-api.sha256").write_text("old-sha", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    decision = ops._brew_install_or_reinstall(
        "tap/nyxgpt-api", "nyxgpt-api", sha256="new-sha", marker_dir=tmp_path
    )
    assert "reinstalled" in decision
    assert ["brew", "reinstall", "tap/nyxgpt-api"] in calls
    assert (tmp_path / ".nyxgpt-api.sha256").read_text(encoding="utf-8") == "new-sha"


@pytest.mark.unit
def test_brew_install_or_reinstall_skips_when_unchanged(monkeypatch, tmp_path):
    (tmp_path / ".nyxgpt-api.sha256").write_text("same-sha", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    decision = ops._brew_install_or_reinstall(
        "tap/nyxgpt-api", "nyxgpt-api", sha256="same-sha", marker_dir=tmp_path
    )
    assert "skipped" in decision
    assert calls == [["brew", "list", "--versions", "nyxgpt-api"]]
    assert (tmp_path / ".nyxgpt-api.sha256").read_text(encoding="utf-8") == "same-sha"


@pytest.mark.unit
def test_brew_install_or_reinstall_raises_and_keeps_no_marker_on_failure(monkeypatch, tmp_path):
    """A failed `brew reinstall` must raise (not report a false success) and
    must NOT record the checksum -- otherwise the next run sees a matching
    marker and skips, so the broken install never retries (the bug that let a
    failed api keg rebuild report 'reinstalled' and stick as a stale wrapper)."""

    def fake_run(cmd, **k):
        if cmd[:3] == ["brew", "list", "--versions"]:
            return subprocess.CompletedProcess(cmd, 0)  # already installed
        if cmd[:2] == ["brew", "reinstall"]:
            return subprocess.CompletedProcess(cmd, 1, stderr="ensurepip failed")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)

    with pytest.raises(RuntimeError, match="brew reinstall nyxgpt-api failed"):
        ops._brew_install_or_reinstall(
            "tap/nyxgpt-api", "nyxgpt-api", sha256="newsha", marker_dir=tmp_path
        )
    assert not (tmp_path / ".nyxgpt-api.sha256").exists()


# --- _hash_paths ---


@pytest.mark.unit
def test_hash_paths_stable_for_unchanged_content(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    assert ops._hash_paths([src]) == ops._hash_paths([src])


@pytest.mark.unit
def test_hash_paths_changes_when_file_content_changes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.txt"
    f.write_text("hello", encoding="utf-8")
    before = ops._hash_paths([src])
    f.write_text("goodbye", encoding="utf-8")
    after = ops._hash_paths([src])
    assert before != after


@pytest.mark.unit
def test_hash_paths_changes_when_file_added(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    before = ops._hash_paths([src])
    (src / "b.txt").write_text("new", encoding="utf-8")
    after = ops._hash_paths([src])
    assert before != after


@pytest.mark.unit
def test_hash_paths_skips_excluded_dirs(tmp_path):
    src = tmp_path / "src"
    (src / "node_modules").mkdir(parents=True)
    (src / "node_modules" / "pkg.js").write_text("ignored", encoding="utf-8")
    (src / "keep.txt").write_text("keep", encoding="utf-8")

    before = ops._hash_paths([src], excludes=frozenset({"node_modules"}))
    (src / "node_modules" / "pkg.js").write_text("changed", encoding="utf-8")
    after = ops._hash_paths([src], excludes=frozenset({"node_modules"}))
    assert before == after


@pytest.mark.unit
def test_hash_paths_includes_standalone_files(tmp_path):
    f = tmp_path / "pyproject.toml"
    f.write_text("[project]\nname = 'x'\n", encoding="utf-8")
    before = ops._hash_paths([f])
    f.write_text("[project]\nname = 'y'\n", encoding="utf-8")
    after = ops._hash_paths([f])
    assert before != after


# --- _docker_build_if_needed ---


@pytest.mark.unit
def test_docker_build_if_needed_builds_when_image_missing(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    marker_dir = tmp_path / "markers"

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)
    decision = ops._docker_build_if_needed(
        "nyxgpt-api:local", src, fingerprint_paths=[src], marker_dir=marker_dir
    )
    assert decision == "built"
    assert ["docker", "build", "-t", "nyxgpt-api:local", str(src)] in calls
    assert (marker_dir / ".nyxgpt-api_local.sha256").exists()


@pytest.mark.unit
def test_docker_build_if_needed_rebuilds_when_source_changed(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    (marker_dir / ".nyxgpt-api_local.sha256").write_text("stale-fingerprint", encoding="utf-8")

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)
    decision = ops._docker_build_if_needed(
        "nyxgpt-api:local", src, fingerprint_paths=[src], marker_dir=marker_dir
    )
    assert "rebuilt" in decision
    assert ["docker", "build", "-t", "nyxgpt-api:local", str(src)] in calls
    new_fingerprint = (marker_dir / ".nyxgpt-api_local.sha256").read_text(encoding="utf-8")
    assert new_fingerprint != "stale-fingerprint"


@pytest.mark.unit
def test_docker_build_if_needed_skips_when_unchanged(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    marker_dir = tmp_path / "markers"
    marker_dir.mkdir()
    fingerprint = ops._hash_paths([src])
    (marker_dir / ".nyxgpt-api_local.sha256").write_text(fingerprint, encoding="utf-8")

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)
    decision = ops._docker_build_if_needed(
        "nyxgpt-api:local", src, fingerprint_paths=[src], marker_dir=marker_dir
    )
    assert "skipped" in decision
    assert calls == [["docker", "image", "inspect", "nyxgpt-api:local"]]


@pytest.mark.unit
def test_docker_build_if_needed_raises_and_keeps_no_marker_on_failure(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    marker_dir = tmp_path / "markers"

    def fake_run(cmd, **k):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1)
        if cmd[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(cmd, 1, stderr="build boom")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)
    with pytest.raises(RuntimeError, match="docker build nyxgpt-api:local failed"):
        ops._docker_build_if_needed(
            "nyxgpt-api:local", src, fingerprint_paths=[src], marker_dir=marker_dir
        )
    assert not (marker_dir / ".nyxgpt-api_local.sha256").exists()


@pytest.mark.unit
def test_docker_build_if_needed_passes_build_args(monkeypatch, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello", encoding="utf-8")
    marker_dir = tmp_path / "markers"

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)
    ops._docker_build_if_needed(
        "nyxgpt-web:local",
        src,
        fingerprint_paths=[src],
        marker_dir=marker_dir,
        build_args={"NEXT_PUBLIC_API_BASE_URL": "http://localhost:8000"},
    )
    assert [
        "docker",
        "build",
        "-t",
        "nyxgpt-web:local",
        "--build-arg",
        "NEXT_PUBLIC_API_BASE_URL=http://localhost:8000",
        str(src),
    ] in calls


# --- _build_terraform_docker_images ---


@pytest.mark.unit
def test_build_terraform_docker_images_no_docker(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    results = ops._build_terraform_docker_images()
    assert results[0].ok is False
    assert "docker not found" in results[0].message


@pytest.mark.unit
def test_build_terraform_docker_images_builds_api_and_web(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_terraform_docker_images()
    assert all(r.ok for r in results)
    assert any(ops.TF_API_IMAGE in r.message and "built" in r.message for r in results)
    assert any(ops.TF_WEB_IMAGE in r.message and "built" in r.message for r in results)
    assert any(c[:4] == ["docker", "build", "-t", ops.TF_API_IMAGE] for c in calls)
    assert any(c[:4] == ["docker", "build", "-t", ops.TF_WEB_IMAGE] for c in calls)


@pytest.mark.unit
def test_build_terraform_docker_images_skips_both_when_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)

    api_fingerprint = ops._hash_paths(ops._API_IMAGE_FINGERPRINT_PATHS)
    web_fingerprint = ops._hash_paths([ops.REPO_ROOT / "web"], excludes=ops._WEB_VENDOR_EXCLUDES)
    (tmp_path / ".nyxgpt-api_local.sha256").write_text(api_fingerprint, encoding="utf-8")
    (tmp_path / ".nyxgpt-web_local.sha256").write_text(web_fingerprint, encoding="utf-8")

    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)  # docker image inspect: found

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_terraform_docker_images()
    assert all(r.ok for r in results)
    assert all("skipped rebuild" in r.message for r in results)
    assert not any(c[:2] == ["docker", "build"] for c in calls)


# --- _vendor_tree ---


@pytest.mark.unit
def test_vendor_tree_excludes_named_directories(tmp_path):
    src = tmp_path / "src"
    (src / "node_modules" / "pkg").mkdir(parents=True)
    (src / "node_modules" / "pkg" / "index.js").write_text("", encoding="utf-8")
    (src / "keep.txt").write_text("keep", encoding="utf-8")

    dst = tmp_path / "dst"
    ops._vendor_tree(src, dst, excludes=frozenset({"node_modules"}))

    assert (dst / "keep.txt").exists()
    assert not (dst / "node_modules").exists()


# --- _ensure_web_deps ---


def _fake_subprocess_run(*, ci_rc=0, ci_stderr="", install_rc=0, resolve_rc=0):
    def _run(cmd, cwd=None, text=True, capture_output=True):
        if cmd[:2] == ["npm", "ci"]:
            return subprocess.CompletedProcess(cmd, ci_rc, stdout="", stderr=ci_stderr)
        if cmd[:2] == ["npm", "install"]:
            return subprocess.CompletedProcess(
                cmd, install_rc, stdout="", stderr="npm install failed"
            )
        if cmd[:1] == ["node"]:
            return subprocess.CompletedProcess(cmd, resolve_rc, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    return _run


@pytest.mark.unit
def test_ensure_web_deps_no_web_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    results = ops._ensure_web_deps()
    assert results[0].ok is True
    assert "Web directory not present" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_no_node(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: None if prog == "node" else "/usr/bin/x")
    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "node not found" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_no_npm(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: None if prog == "npm" else "/usr/bin/x")
    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "npm not found" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_already_installed(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    (web_dir / "node_modules").mkdir(parents=True)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops.subprocess, "run", _fake_subprocess_run(resolve_rc=0))

    results = ops._ensure_web_deps()
    assert results[0].ok is True
    assert "already installed" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_npm_ci_succeeds(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops.subprocess, "run", _fake_subprocess_run(ci_rc=0, resolve_rc=0))

    results = ops._ensure_web_deps()
    assert results[0].ok is True
    assert "npm ci" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_npm_ci_succeeds_but_undici_missing(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops.subprocess, "run", _fake_subprocess_run(ci_rc=0, resolve_rc=1))

    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "undici still missing" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_npm_ci_lockfile_mismatch_falls_back_to_install(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        _fake_subprocess_run(
            ci_rc=1,
            ci_stderr="can only install packages when your package.json and package-lock.json are in sync",
            install_rc=0,
            resolve_rc=0,
        ),
    )

    results = ops._ensure_web_deps()
    assert results[0].ok is True
    assert "lockfile was out of sync" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_npm_ci_lockfile_mismatch_install_succeeds_but_undici_missing(
    monkeypatch, tmp_path
):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        _fake_subprocess_run(
            ci_rc=1,
            ci_stderr="can only install packages when your package.json and package-lock.json are in sync",
            install_rc=0,
            resolve_rc=1,
        ),
    )

    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "undici still missing" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_npm_ci_lockfile_mismatch_install_also_fails(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        _fake_subprocess_run(
            ci_rc=1,
            ci_stderr="can only install packages when your package.json and package-lock.json are in sync",
            install_rc=1,
        ),
    )

    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "after npm ci mismatch" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_npm_ci_other_failure(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(
        ops.subprocess, "run", _fake_subprocess_run(ci_rc=1, ci_stderr="network error")
    )

    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "Failed to install web deps via npm ci" in results[0].message
    assert "network error" in results[0].details


@pytest.mark.unit
def test_ensure_web_deps_no_lockfile_npm_install_succeeds(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops.subprocess, "run", _fake_subprocess_run(install_rc=0, resolve_rc=0))

    results = ops._ensure_web_deps()
    assert results[0].ok is True
    assert "npm install" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_no_lockfile_npm_install_succeeds_but_undici_missing(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops.subprocess, "run", _fake_subprocess_run(install_rc=0, resolve_rc=1))

    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "undici still missing" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_no_lockfile_npm_install_fails(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops.subprocess, "run", _fake_subprocess_run(install_rc=1))

    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "Failed to install web deps via npm install" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_can_resolve_handles_exception(monkeypatch, tmp_path):
    # node_modules already present, but the `node -p require.resolve(...)` probe itself
    # throws (e.g. node binary vanished) -- _can_resolve must swallow it and return False,
    # letting install fall through to the npm-install path rather than crashing.
    web_dir = tmp_path / "web"
    (web_dir / "node_modules").mkdir(parents=True)
    (web_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")

    def fake_run(cmd, cwd=None, text=True, capture_output=True):
        if cmd[:1] == ["node"]:
            raise OSError("node vanished")
        if cmd[:2] == ["npm", "ci"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network error")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(ops.subprocess, "run", fake_run)

    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "Failed to install web deps via npm ci" in results[0].message


@pytest.mark.unit
def test_ensure_web_deps_handles_exception(monkeypatch, tmp_path):
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")

    def raise_run(cmd, cwd=None, text=True, capture_output=True):
        raise OSError("exploded")

    monkeypatch.setattr(ops.subprocess, "run", raise_run)

    results = ops._ensure_web_deps()
    assert results[0].ok is False
    assert "Failed to install web deps" in results[0].message
    assert "OSError" in results[0].details


# --- _ensure_mcp_deps ---


@pytest.mark.unit
def test_ensure_mcp_deps_no_package_json(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    results = ops._ensure_mcp_deps()
    assert results[0].ok is True
    assert "No root package.json found" in results[0].message


@pytest.mark.unit
def test_ensure_mcp_deps_no_npm(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._ensure_mcp_deps()
    assert results[0].ok is False
    assert "npm not found" in results[0].message


@pytest.mark.unit
def test_ensure_mcp_deps_already_installed(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    sentinel = tmp_path / "node_modules" / "@modelcontextprotocol" / "server-github"
    sentinel.mkdir(parents=True)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/npm")

    results = ops._ensure_mcp_deps()
    assert results[0].ok is True
    assert "already installed" in results[0].message


@pytest.mark.unit
def test_ensure_mcp_deps_install_succeeds(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda cmd, cwd=None, text=True, capture_output=True: subprocess.CompletedProcess(cmd, 0),
    )

    results = ops._ensure_mcp_deps()
    assert results[0].ok is True
    assert "Installed MCP deps" in results[0].message


@pytest.mark.unit
def test_ensure_mcp_deps_install_fails(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda cmd, cwd=None, text=True, capture_output=True: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="boom"
        ),
    )

    results = ops._ensure_mcp_deps()
    assert results[0].ok is False
    assert "Failed to install MCP deps" in results[0].message
    assert "boom" in results[0].details


@pytest.mark.unit
def test_ensure_mcp_deps_install_raises(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/npm")

    def raise_run(cmd, cwd=None, text=True, capture_output=True):
        raise OSError("boom")

    monkeypatch.setattr(ops.subprocess, "run", raise_run)

    results = ops._ensure_mcp_deps()
    assert results[0].ok is False
    assert "Failed to install MCP deps" in results[0].message
    assert "OSError" in results[0].details


@pytest.mark.unit
def test_ensure_mcp_deps_uses_npm_ci_when_lockfile_present(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/npm")

    seen_cmd = []

    def fake_run(cmd, cwd=None, text=True, capture_output=True):
        seen_cmd.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops.subprocess, "run", fake_run)

    results = ops._ensure_mcp_deps()
    assert seen_cmd == [["npm", "ci"]]
    assert results[0].ok is True
    assert "npm ci" in results[0].message


@pytest.mark.unit
def test_ensure_mcp_deps_uses_npm_install_when_no_lockfile(monkeypatch, tmp_path):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/npm")

    seen_cmd = []

    def fake_run(cmd, cwd=None, text=True, capture_output=True):
        seen_cmd.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops.subprocess, "run", fake_run)

    results = ops._ensure_mcp_deps()
    assert seen_cmd == [["npm", "install"]]
    assert results[0].ok is True
    assert "npm install" in results[0].message


# --- install() step-failure handling ---


@pytest.mark.unit
def test_ops_install_catches_exception_from_a_step(capsys):
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_install_scripts", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", side_effect=RuntimeError("kaboom")),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
    ):
        rc = ops.install(MagicMock(terraform=False, kubernetes=False))
        assert rc == 2
        out = capsys.readouterr().out
        assert "ops install failed: web deps" in out
        assert "RuntimeError" in out


# --- status() remaining branches ---


@pytest.mark.unit
def test_ops_status_shows_compose_components_without_conflict(monkeypatch, capsys):
    class CP:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_run", lambda *a, **k: CP(stdout=""))
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "compose grafana: running" in out
    assert "Compose components" in out


@pytest.mark.unit
def test_ops_status_brew_and_docker_not_found(monkeypatch, capsys):
    class CP:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    monkeypatch.setattr(ops, "_which", lambda _: None)
    monkeypatch.setattr(ops, "_run", lambda *a, **k: CP(stdout=""))
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Homebrew services: brew not found" in out
    assert "Docker: docker not found" in out


@pytest.mark.unit
def test_ops_status_launchctl_error(monkeypatch, capsys):
    class CP:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(cmd, check=True, **_k):
        if cmd[:2] == ["launchctl", "list"]:
            raise OSError("launchctl missing")
        return CP(stdout="")

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "LaunchAgent com.nyxgpt.cassandra-logs: ERROR" in out
    assert "launchctl missing" in out


# --- doctor() remaining branches ---


@pytest.mark.unit
def test_ops_doctor_reports_non_executable_script(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")

    scripts_dir = cfg_dir / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "run-web.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o644)

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "Script not executable" in out


@pytest.mark.unit
def test_ops_doctor_reports_missing_brew_and_docker(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")
    (tmp_path / "web").mkdir()

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: None)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "Missing tool in PATH: brew" in out
    assert "Missing tool in PATH: docker" in out
    assert "Missing tool in PATH: node" in out
    assert "Missing tool in PATH: npm" in out


@pytest.mark.unit
def test_ops_doctor_web_deps_present_and_undici_resolves(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True)
    # Tracing defaults to enabled (#3415); disable it so this test stays
    # focused on the web-deps checks it's actually exercising.
    (cfg_dir / "config.ini").write_text(
        "[project]\nname=nyxGPT\n\n[tracing]\nenabled = false\n", encoding="utf-8"
    )

    web_dir = tmp_path / "web"
    (web_dir / "node_modules").mkdir(parents=True)

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda cmd, cwd=None, text=True, capture_output=True: subprocess.CompletedProcess(cmd, 0),
    )

    rc = ops.doctor(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "doctor: OK" in out


@pytest.mark.unit
def test_ops_doctor_web_deps_present_but_undici_unresolvable(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")

    web_dir = tmp_path / "web"
    (web_dir / "node_modules").mkdir(parents=True)

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(
        ops.subprocess,
        "run",
        lambda cmd, cwd=None, text=True, capture_output=True: subprocess.CompletedProcess(cmd, 1),
    )

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "Missing web dependency: undici" in out


@pytest.mark.unit
def test_ops_doctor_can_resolve_handles_exception(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")

    web_dir = tmp_path / "web"
    (web_dir / "node_modules").mkdir(parents=True)

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    def raise_run(cmd, cwd=None, text=True, capture_output=True):
        raise OSError("boom")

    monkeypatch.setattr(ops.subprocess, "run", raise_run)

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "Missing web dependency: undici" in out


# --- restart(): web/ollama compose conflicts ---


@pytest.mark.unit
def test_ops_restart_web_refuses_when_compose_web_conflicts(capsys):
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={"web": "running"}),
        patch.object(ops, "_restart_brew_service") as rb,
    ):
        args = MagicMock()
        args.target = "web"
        rc = ops.restart(args)
        assert rc == 2

        rb.assert_not_called()
        out = capsys.readouterr().out
        assert "Refusing to restart local web" in out
        assert "port 3000" in out


@pytest.mark.unit
def test_ops_restart_ollama_refuses_when_compose_ollama_conflicts(capsys):
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={"ollama": "running"}),
        patch.object(ops, "_restart_brew_service") as rb,
    ):
        args = MagicMock()
        args.target = "ollama"
        rc = ops.restart(args)
        assert rc == 2

        rb.assert_not_called()
        out = capsys.readouterr().out
        assert "Refusing to restart local ollama" in out
        assert "port 11434" in out


# --- env_sync(): details line ---


@pytest.mark.unit
def test_env_sync_cli_wrapper_prints_details_on_failure(tmp_path, capsys, monkeypatch):
    cfg_path = tmp_path / "missing-config.ini"
    env_path = tmp_path / ".env"
    compose_cfg = tmp_path / "config.docker.ini"
    compose_cfg.write_text("[error_tracking]\nenabled = false\ndsn =\n", encoding="utf-8")
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", compose_cfg)

    args = MagicMock()
    args.config = str(cfg_path)
    args.env_file = str(env_path)

    rc = ops.env_sync(args)
    assert rc == 2

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "nyxgpt wizard" in out


# --- Structured logging ---


@pytest.mark.unit
def test_emit_results_logs_ok_at_info_and_failure_at_warning(caplog):
    results = [
        ops.OpsResult(True, "step one ok"),
        ops.OpsResult(False, "step two failed", "subprocess stderr detail"),
    ]

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        ok = ops._emit_results("install", results)

    assert ok is False
    info_records = [r for r in caplog.records if r.levelname == "INFO"]
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]

    assert any("step one ok" in r.getMessage() for r in info_records)
    assert any(getattr(r, "action", None) == "install" for r in info_records)

    assert len(warning_records) == 1
    warn = warning_records[0]
    assert "step two failed" in warn.getMessage()
    assert warn.details == "subprocess stderr detail"
    assert warn.ok is False
    assert warn.component == "ops"


@pytest.mark.unit
def test_ops_install_logs_start_and_summary(caplog):
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_install_scripts", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        caplog.at_level("INFO", logger="nyxgpt.ops"),
    ):
        rc = ops.install(MagicMock(terraform=False, kubernetes=False))

    assert rc == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("install starting" in m for m in messages)
    assert any("install succeeded" in m for m in messages)


@pytest.mark.unit
def test_ops_install_logs_error_when_step_raises(caplog):
    with (
        patch.object(ops, "migrate_legacy_volumes", return_value=[]),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=[]),
        patch.object(ops, "_install_scripts", side_effect=RuntimeError("boom")),
        patch.object(ops, "_ensure_web_deps", return_value=[]),
        patch.object(ops, "_ensure_mcp_deps", return_value=[]),
        patch.object(ops, "_ensure_cassandra_container", return_value=[]),
        patch.object(ops, "_install_cassandra_launchagent", return_value=[]),
        patch.object(ops, "_install_ollama_launchagent", return_value=[]),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=[]),
        patch.object(ops, "_install_homebrew_api", return_value=[]),
        patch.object(ops, "_install_homebrew_web", return_value=[]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=[]),
        caplog.at_level("INFO", logger="nyxgpt.ops"),
    ):
        rc = ops.install(MagicMock(terraform=False, kubernetes=False))

    assert rc == 2
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    assert "scripts" in error_records[0].getMessage()
    assert error_records[0].exc_info is not None


@pytest.mark.unit
def test_ops_restart_logs_target_and_summary(caplog):
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_compose_stack_snapshot", return_value={}),
        patch.object(ops, "_restart_brew_service", return_value=ok),
        patch.object(ops, "_restart_docker_container", return_value=ok),
        patch.object(ops, "_restart_launchagent", return_value=ok),
    ):
        args = MagicMock()
        args.target = "api"
        with caplog.at_level("INFO", logger="nyxgpt.ops"):
            rc = ops.restart(args)

    assert rc == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("restart starting (target=api)" in m for m in messages)
    assert any("restart succeeded" in m for m in messages)


@pytest.mark.unit
def test_ops_doctor_logs_issues_at_warning(caplog, monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: None)

    with caplog.at_level("INFO", logger="nyxgpt.ops"):
        rc = ops.doctor(MagicMock())

    assert rc == 2
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    assert warning_records[0].issues
    assert any("Missing config" in i for i in warning_records[0].issues)


@pytest.mark.unit
def test_ops_doctor_logs_ok_at_info(caplog, monkeypatch, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True)
    # Tracing defaults to enabled (#3415); disable it so this test stays
    # focused on the doctor logging behavior it's actually exercising.
    (cfg_dir / "config.ini").write_text(
        "[project]\nname=nyxGPT\n\n[tracing]\nenabled = false\n", encoding="utf-8"
    )
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    with caplog.at_level("INFO", logger="nyxgpt.ops"):
        rc = ops.doctor(MagicMock())

    assert rc == 0
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("no issues" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
def test_ops_logs_failure_logged_at_warning_with_details(caplog):
    with patch.object(
        ops.self_heal,
        "component_logs",
        return_value=ops.self_heal.HealResult(False, "Failed to fetch logs", "no such service"),
    ):
        args = MagicMock()
        args.service = "glitchtip"
        args.tail = 50
        with caplog.at_level("INFO", logger="nyxgpt.ops"):
            rc = ops.logs(args)

    assert rc == 2
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    assert warning_records[0].details == "no such service"
    assert warning_records[0].service == "glitchtip"


@pytest.mark.unit
def test_ops_logs_success_does_not_log_full_output(caplog):
    with patch.object(
        ops.self_heal,
        "component_logs",
        return_value=ops.self_heal.HealResult(
            True, "Fetched last 50 log line(s)", "SECRET LOG BODY"
        ),
    ):
        args = MagicMock()
        args.service = "glitchtip"
        args.tail = 50
        with caplog.at_level("INFO", logger="nyxgpt.ops"):
            rc = ops.logs(args)

    assert rc == 0
    messages = [r.getMessage() for r in caplog.records]
    assert not any("SECRET LOG BODY" in m for m in messages)
    assert any("Fetched last 50 log line(s)" in m for m in messages)


@pytest.mark.unit
def test_detect_deployment_mode_logs_conflict_at_warning(caplog, monkeypatch):
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-web": "stopped", "ollama": "stopped"},
    )
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"api": "running", "web": "exited"})

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        ops.detect_deployment_mode()

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    assert warning_records[0].conflicts == ["api"]


@pytest.mark.unit
def test_env_sync_logs_summary(caplog, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="cli-api-key")
    env_path = tmp_path / ".env"
    compose_cfg = tmp_path / "config.docker.ini"
    compose_cfg.write_text("[error_tracking]\nenabled = false\ndsn =\n", encoding="utf-8")
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", compose_cfg)

    args = MagicMock()
    args.config = str(cfg_path)
    args.env_file = str(env_path)

    with caplog.at_level("INFO", logger="nyxgpt.ops"):
        rc = ops.env_sync(args)

    assert rc == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("env-sync starting" in m for m in messages)
    assert any("env-sync succeeded" in m for m in messages)


# --- Observability stack ---


@pytest.mark.unit
def test_compose_available_false_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    assert ops._compose_available() is False


@pytest.mark.unit
def test_compose_available_false_when_compose_plugin_missing(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="unknown command")
    )
    assert ops._compose_available() is False


@pytest.mark.unit
def test_compose_available_true(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="v2.38.2")
    )
    assert ops._compose_available() is True


@pytest.mark.unit
def test_start_observability_stack_skips_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: False)
    results = ops._start_observability_stack()
    assert len(results) == 1
    assert results[0].ok is True
    assert "Skipped observability stack" in results[0].message


_ALL_COMPOSE_SERVICES = (
    "grafana\nprometheus\nloki\npromtail\notel-collector\njaeger\n"
    "glitchtip\nglitchtip-worker\nglitchtip-postgres\nglitchtip-redis\nglitchtip-migrate\n"
    "nyxgpt\nollama\ncassandra\napi\nweb\n"
)


def _fake_run_resolving_services(calls, *, up_rc=0):
    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        if cmd[-2:] == ["config", "--services"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=_ALL_COMPOSE_SERVICES)
        return subprocess.CompletedProcess(cmd, up_rc, stderr="up failed" if up_rc else "")

    return fake_run


@pytest.mark.unit
def test_start_observability_stack_runs_compose_up_with_all_profiles(monkeypatch):
    calls = []

    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_run", _fake_run_resolving_services(calls))
    monkeypatch.setattr(ops, "_enable_observability_config", lambda: None)

    results = ops._start_observability_stack()

    assert len(results) == 1
    assert results[0].ok is True
    assert len(calls) == 2

    services_cmd = calls[0]
    assert services_cmd[:2] == ["docker", "compose"]
    for profile in ops.OBSERVABILITY_PROFILES:
        assert "--profile" in services_cmd
        assert profile in services_cmd
    assert services_cmd[-2:] == ["config", "--services"]

    up_cmd = calls[1]
    assert up_cmd[:2] == ["docker", "compose"]
    for profile in ops.OBSERVABILITY_PROFILES:
        assert "--profile" in up_cmd
        assert profile in up_cmd
    up_idx = up_cmd.index("up")
    assert up_cmd[up_idx : up_idx + 2] == ["up", "-d"]
    started_services = up_cmd[up_idx + 2 :]
    assert started_services
    assert "grafana" in started_services
    assert "glitchtip" in started_services
    # Regression guard: `--profile` alone does not stop Compose from also
    # starting unprofiled "default" services, so the core app stack
    # (ollama/cassandra/api/web) must never appear in the resolved `up -d`
    # service list -- otherwise `nyxgpt ops install` on a native host would
    # silently launch a full Dockerized copy of the app alongside it.
    assert not (ops.CORE_APP_SERVICES & set(started_services))


@pytest.mark.unit
def test_start_observability_stack_excludes_core_app_services_even_if_listed(monkeypatch):
    """Even if `config --services` reports core services, they must be filtered out."""
    calls = []
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_run", _fake_run_resolving_services(calls))
    monkeypatch.setattr(ops, "_enable_observability_config", lambda: None)

    ops._start_observability_stack()

    up_cmd = calls[1]
    for core_service in ops.CORE_APP_SERVICES:
        assert core_service not in up_cmd


@pytest.mark.unit
def test_start_observability_stack_reports_config_resolution_failure(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="profile error"),
    )
    enable_calls = []
    monkeypatch.setattr(ops, "_enable_observability_config", lambda: enable_calls.append(True))

    results = ops._start_observability_stack()

    assert len(results) == 1
    assert results[0].ok is False
    assert "profile error" in results[0].details
    # Config flags must not be flipped to enabled when the stack failed to start.
    assert enable_calls == []


@pytest.mark.unit
def test_start_observability_stack_reports_up_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_run", _fake_run_resolving_services(calls, up_rc=1))
    enable_calls = []
    monkeypatch.setattr(ops, "_enable_observability_config", lambda: enable_calls.append(True))

    results = ops._start_observability_stack()

    assert len(results) == 1
    assert results[0].ok is False
    assert "up failed" in results[0].details
    assert enable_calls == []


@pytest.mark.unit
def test_start_observability_stack_no_services_resolved(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout="nyxgpt\nollama\ncassandra\napi\nweb\n"
        ),
    )
    enable_calls = []
    monkeypatch.setattr(ops, "_enable_observability_config", lambda: enable_calls.append(True))

    results = ops._start_observability_stack()

    assert len(results) == 1
    assert results[0].ok is False
    assert "No observability services resolved" in results[0].message
    assert enable_calls == []


@pytest.mark.unit
def test_enable_observability_config_sets_flags(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[monitoring]\nenabled = false\n\n[tracing]\nenabled = false\n",
        encoding="utf-8",
    )

    ops._enable_observability_config(cfg_path=cfg_path)

    from configparser import ConfigParser

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("monitoring", "enabled") == "true"
    assert parser.get("tracing", "enabled") == "true"
    assert parser.get("log_aggregation", "enabled") == "true"
    # error_tracking needs a GlitchTip DSN nothing here can safely create --
    # it must be left untouched.
    assert not parser.has_section("error_tracking")


@pytest.mark.unit
def test_enable_observability_config_is_idempotent(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[monitoring]\nenabled = true\n", encoding="utf-8")
    before_mtime = cfg_path.stat().st_mtime_ns

    ops._enable_observability_config(cfg_path=cfg_path)
    ops._enable_observability_config(cfg_path=cfg_path)

    # Second call re-flips already-true monitoring plus adds the other
    # sections once; a further no-op call shouldn't rewrite the file again.
    from configparser import ConfigParser

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("monitoring", "enabled") == "true"
    assert parser.get("tracing", "enabled") == "true"
    assert cfg_path.stat().st_mtime_ns >= before_mtime


@pytest.mark.unit
def test_enable_observability_config_missing_file_is_a_noop(tmp_path):
    cfg_path = tmp_path / "does-not-exist.ini"
    # Must not raise.
    ops._enable_observability_config(cfg_path=cfg_path)
    assert not cfg_path.exists()


# --- Grafana provisioning drift detection / datasource verification (#3424) ---


def _write_grafana_fixture(tmp_path, monkeypatch, *, datasource_yml: str, compose_yml: str = "x"):
    datasource_path = (
        tmp_path / "docker" / "grafana" / "provisioning" / "datasources" / "datasource.yml"
    )
    datasource_path.parent.mkdir(parents=True)
    datasource_path.write_text(datasource_yml, encoding="utf-8")

    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(compose_yml, encoding="utf-8")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.self_heal, "COMPOSE_FILE", compose_path)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    return datasource_path, compose_path


_SAMPLE_DATASOURCE_YML = """
datasources:
  - name: Prometheus
    uid: prometheus
    type: prometheus
  - name: Jaeger
    uid: jaeger
    type: jaeger
  - name: GlitchTip
    uid: glitchtip-infinity
    type: yesoreyeram-infinity-datasource
"""


@pytest.mark.unit
def test_grafana_provisioned_datasource_uids_parses_declared_uids(tmp_path, monkeypatch):
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    assert set(ops._grafana_provisioned_datasource_uids()) == {
        "prometheus",
        "jaeger",
        "glitchtip-infinity",
    }


@pytest.mark.unit
def test_grafana_provisioned_datasource_uids_spans_every_file_in_the_dir(tmp_path, monkeypatch):
    """#3432: GlitchTip lives in its own glitchtip.yml, split out of
    datasource.yml so a bad `$__file{}` interpolation in one can't take the
    other's datasources down -- uid scraping must still see both files."""
    datasource_path, _ = _write_grafana_fixture(
        tmp_path,
        monkeypatch,
        datasource_yml="datasources:\n  - name: Prometheus\n    uid: prometheus\n",
    )
    (datasource_path.parent / "glitchtip.yml").write_text(
        "datasources:\n  - name: GlitchTip\n    uid: glitchtip-infinity\n", encoding="utf-8"
    )

    assert set(ops._grafana_provisioned_datasource_uids()) == {"prometheus", "glitchtip-infinity"}


@pytest.mark.unit
def test_grafana_provisioning_fingerprint_changes_when_glitchtip_yml_changes(tmp_path, monkeypatch):
    """A change to glitchtip.yml alone (e.g. the GlitchTip datasource being
    added/edited) must be detected as drift even though datasource.yml is
    untouched -- otherwise `_reconcile_grafana_provisioning` would never
    recreate Grafana to pick it up."""
    datasource_path, _ = _write_grafana_fixture(
        tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML
    )
    glitchtip_path = datasource_path.parent / "glitchtip.yml"
    glitchtip_path.write_text("datasources: []\n", encoding="utf-8")
    ops._record_grafana_provisioning_fingerprint()
    assert ops._grafana_provisioning_drifted() is False

    glitchtip_path.write_text("datasources:\n  - name: GlitchTip\n    uid: glitchtip-infinity\n")
    assert ops._grafana_provisioning_drifted() is True


@pytest.mark.unit
def test_grafana_provisioned_datasource_uids_empty_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    assert ops._grafana_provisioned_datasource_uids() == []


@pytest.mark.unit
def test_grafana_provisioning_drifted_true_without_a_prior_marker(tmp_path, monkeypatch):
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    assert ops._grafana_provisioning_drifted() is True


@pytest.mark.unit
def test_grafana_provisioning_drifted_false_once_fingerprint_recorded(tmp_path, monkeypatch):
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    ops._record_grafana_provisioning_fingerprint()
    assert ops._grafana_provisioning_drifted() is False


@pytest.mark.unit
def test_grafana_provisioning_drifted_true_after_datasource_yml_changes(tmp_path, monkeypatch):
    datasource_path, _ = _write_grafana_fixture(
        tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML
    )
    ops._record_grafana_provisioning_fingerprint()
    assert ops._grafana_provisioning_drifted() is False

    datasource_path.write_text(_SAMPLE_DATASOURCE_YML + "\n# an added datasource\n")
    assert ops._grafana_provisioning_drifted() is True


@pytest.mark.unit
def test_recreate_grafana_skipped_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: False)
    assert ops._recreate_grafana_if_provisioning_drifted() is None


@pytest.mark.unit
def test_recreate_grafana_skipped_when_not_drifted(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_grafana_provisioning_drifted", lambda: False)
    assert ops._recreate_grafana_if_provisioning_drifted() is None


@pytest.mark.unit
def test_recreate_grafana_skipped_when_not_yet_running(monkeypatch):
    """Nothing stale to recreate if grafana was never up -- the caller's own
    `up -d` creates it fresh with current provisioning already."""
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_grafana_provisioning_drifted", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "exited"})
    assert ops._recreate_grafana_if_provisioning_drifted() is None


@pytest.mark.unit
def test_recreate_grafana_force_recreates_when_drifted_and_running(monkeypatch):
    calls = []
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_grafana_provisioning_drifted", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops._recreate_grafana_if_provisioning_drifted()

    assert result is not None
    assert result.ok is True
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:2] == ["docker", "compose"]
    assert "--force-recreate" in cmd
    assert cmd[-1] == "grafana"


@pytest.mark.unit
def test_recreate_grafana_reports_compose_failure(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_grafana_provisioning_drifted", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="boom")
    )

    result = ops._recreate_grafana_if_provisioning_drifted()

    assert result is not None
    assert result.ok is False
    assert "boom" in result.details


@pytest.mark.unit
def test_verify_grafana_datasources_resolve_ok_when_all_present(monkeypatch):
    monkeypatch.setattr(
        ops, "_grafana_provisioned_datasource_uids", lambda: ["prometheus", "jaeger"]
    )

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"uid": "prometheus"}, {"uid": "jaeger"}, {"uid": "glitchtip-infinity"}]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._verify_grafana_datasources_resolve("http://localhost:3001", "admin")

    assert result.ok is True
    assert "2" in result.message


@pytest.mark.unit
def test_verify_grafana_datasources_resolve_no_datasources_declared_is_ok(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_provisioned_datasource_uids", lambda: [])
    result = ops._verify_grafana_datasources_resolve("http://localhost:3001", "admin")
    assert result.ok is True


@pytest.mark.unit
def test_verify_grafana_datasources_resolve_fails_when_uid_missing(monkeypatch):
    monkeypatch.setattr(
        ops, "_grafana_provisioned_datasource_uids", lambda: ["prometheus", "jaeger"]
    )
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"uid": "prometheus"}]

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._verify_grafana_datasources_resolve("http://localhost:3001", "admin", attempts=2)

    assert result.ok is False
    assert "jaeger" in result.message
    assert "nyxgpt ops logs grafana" in result.details


@pytest.mark.unit
def test_verify_grafana_datasources_resolve_fails_when_unreachable(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_provisioned_datasource_uids", lambda: ["prometheus"])
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._verify_grafana_datasources_resolve("http://localhost:3001", "admin", attempts=2)

    assert result.ok is False
    assert "Could not reach Grafana" in result.message


@pytest.mark.unit
def test_grafana_expected_plugin_ids_parses_gf_install_plugins(tmp_path, monkeypatch):
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text(
        'GF_INSTALL_PLUGINS: "grafana-lokiexplore-app,yesoreyeram-infinity-datasource"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ops.self_heal, "COMPOSE_FILE", compose_path)

    assert ops._grafana_expected_plugin_ids() == [
        "grafana-lokiexplore-app",
        "yesoreyeram-infinity-datasource",
    ]


@pytest.mark.unit
def test_grafana_expected_plugin_ids_empty_when_not_declared(tmp_path, monkeypatch):
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(ops.self_heal, "COMPOSE_FILE", compose_path)

    assert ops._grafana_expected_plugin_ids() == []


@pytest.mark.unit
def test_verify_grafana_plugins_installed_ok_when_all_enabled(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_expected_plugin_ids", lambda: ["grafana-lokiexplore-app"])

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"enabled": True}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._verify_grafana_plugins_installed("http://localhost:3001", "admin")

    assert result.ok is True


@pytest.mark.unit
def test_verify_grafana_plugins_installed_no_plugins_declared_is_ok(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_expected_plugin_ids", lambda: [])
    result = ops._verify_grafana_plugins_installed("http://localhost:3001", "admin")
    assert result.ok is True


@pytest.mark.unit
def test_verify_grafana_plugins_installed_fails_when_plugin_missing(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_expected_plugin_ids", lambda: ["grafana-lokiexplore-app"])
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)

    class FakeResponse:
        status_code = 404

        def json(self):
            return {"message": "Plugin not found"}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._verify_grafana_plugins_installed("http://localhost:3001", "admin", attempts=2)

    assert result.ok is False
    assert "grafana-lokiexplore-app" in result.message
    assert "nyxgpt ops logs grafana" in result.details


@pytest.mark.unit
def test_provision_grafana_doctor_token_reuses_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "grafana-doctor-token").write_text("existing-token")

    def _boom(*a, **kw):
        raise AssertionError("should not call Grafana when a token file already exists")

    monkeypatch.setattr(ops.httpx, "Client", _boom)

    result = ops._provision_grafana_doctor_token("http://localhost:3001", "admin")

    assert result.ok is True
    assert "already holds" in result.message


@pytest.mark.unit
def test_provision_grafana_doctor_token_mints_new_service_account(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            calls.append(("get", path, params))
            return FakeResponse({"serviceAccounts": []})

        def post(self, path, json=None):
            calls.append(("post", path, json))
            if path == "/api/serviceaccounts":
                return FakeResponse({"id": 7})
            return FakeResponse({"key": "glsa_minted_token"})

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._provision_grafana_doctor_token("http://localhost:3001", "admin")

    assert result.ok is True
    token_path = tmp_path / ".nyxGPT" / "secrets" / "grafana-doctor-token"
    assert token_path.read_text() == "glsa_minted_token"
    assert calls[1] == (
        "post",
        "/api/serviceaccounts",
        {"name": "nyxgpt-ops-doctor", "role": "Viewer"},
    )
    assert calls[2] == ("post", "/api/serviceaccounts/7/tokens", {"name": "nyxgpt-ops-doctor"})


@pytest.mark.unit
def test_provision_grafana_doctor_token_reuses_existing_service_account(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return FakeResponse({"serviceAccounts": [{"id": 3, "name": "nyxgpt-ops-doctor"}]})

        def post(self, path, json=None):
            assert path == "/api/serviceaccounts/3/tokens"
            return FakeResponse({"key": "glsa_reused_sa_token"})

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._provision_grafana_doctor_token("http://localhost:3001", "admin")

    assert result.ok is True
    token_path = tmp_path / ".nyxGPT" / "secrets" / "grafana-doctor-token"
    assert token_path.read_text() == "glsa_reused_sa_token"


@pytest.mark.unit
def test_provision_grafana_doctor_token_fails_when_grafana_unreachable(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    class FailingClient:
        def __init__(self, *a, **kw):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(ops.httpx, "Client", FailingClient)

    result = ops._provision_grafana_doctor_token("http://localhost:3001", "admin")

    assert result.ok is False
    assert not (tmp_path / ".nyxGPT" / "secrets" / "grafana-doctor-token").exists()


@pytest.mark.unit
def test_grafana_admin_password_generates_and_persists_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg = ConfigParser()
    cfg.add_section("monitoring")

    password = ops._grafana_admin_password(cfg)

    assert password
    path = tmp_path / ".nyxGPT" / "secrets" / "grafana-admin-password"
    assert path.read_text().strip() == password
    # Re-reading (e.g. a second `nyxgpt ops install` run) reuses the same secret.
    assert ops._grafana_admin_password(cfg) == password


@pytest.mark.unit
def test_grafana_admin_password_never_empty_even_with_blank_config_value(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg = ConfigParser()
    cfg.add_section("monitoring")
    cfg.set("monitoring", "grafana_admin_password", "")

    assert ops._grafana_admin_password(cfg) != ""


@pytest.mark.unit
def test_grafana_admin_password_prefers_explicit_config_value(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg = ConfigParser()
    cfg.add_section("monitoring")
    cfg.set("monitoring", "grafana_admin_password", "owner-chosen-secret")

    assert ops._grafana_admin_password(cfg) == "owner-chosen-secret"
    # The ops-managed secret file is never created when config.ini already has one.
    assert not (tmp_path / ".nyxGPT" / "secrets" / "grafana-admin-password").exists()


@pytest.mark.unit
def test_reconcile_grafana_admin_credential_skips_reset_when_already_working(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_admin_password", lambda cfg: "already-good")
    monkeypatch.setattr(ops, "_grafana_admin_auth_status", lambda url, pw: "ok")

    def _boom(*a, **kw):
        raise AssertionError("should not reset when the current password already authenticates")

    monkeypatch.setattr(ops, "_reset_grafana_admin_password", _boom)

    password, result = ops._reconcile_grafana_admin_credential(
        "http://localhost:3001", ConfigParser()
    )

    assert password == "already-good"
    assert result.ok is True


@pytest.mark.unit
def test_reconcile_grafana_admin_credential_resets_on_401_then_succeeds(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_admin_password", lambda cfg: "ops-managed-secret")
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)

    reset_calls = []

    def _auth_status(url, pw):
        # Fails before the reset (stale/empty container password), then
        # succeeds afterward -- simulates a long-lived volume (#3458).
        return "ok" if reset_calls else "unauthorized"

    monkeypatch.setattr(ops, "_grafana_admin_auth_status", _auth_status)
    monkeypatch.setattr(
        ops,
        "_reset_grafana_admin_password",
        lambda pw: reset_calls.append(pw) or ops.OpsResult(True, "Reset Grafana admin password"),
    )

    password, result = ops._reconcile_grafana_admin_credential(
        "http://localhost:3001", ConfigParser(), attempts=2
    )

    assert password == "ops-managed-secret"
    assert result.ok is True
    assert reset_calls == ["ops-managed-secret"]


@pytest.mark.unit
def test_reconcile_grafana_admin_credential_reports_one_failure_when_reset_fails(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_admin_password", lambda cfg: "ops-managed-secret")
    monkeypatch.setattr(ops, "_grafana_admin_auth_status", lambda url, pw: "unauthorized")
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        ops,
        "_reset_grafana_admin_password",
        lambda pw: ops.OpsResult(False, "Failed to reset Grafana admin password", "boom"),
    )

    password, result = ops._reconcile_grafana_admin_credential(
        "http://localhost:3001", ConfigParser(), attempts=2
    )

    assert password is None
    assert result.ok is False
    assert "boom" in result.details


@pytest.mark.unit
def test_reconcile_grafana_admin_credential_reports_one_failure_when_reset_verify_fails(
    monkeypatch,
):
    monkeypatch.setattr(ops, "_grafana_admin_password", lambda cfg: "ops-managed-secret")
    monkeypatch.setattr(ops, "_grafana_admin_auth_status", lambda url, pw: "unauthorized")
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        ops,
        "_reset_grafana_admin_password",
        lambda pw: ops.OpsResult(True, "Reset Grafana admin password"),
    )

    password, result = ops._reconcile_grafana_admin_credential(
        "http://localhost:3001", ConfigParser(), attempts=2
    )

    assert password is None
    assert result.ok is False
    assert "still doesn't authenticate" in result.message
    assert "rejects the password" in result.details


@pytest.mark.unit
def test_reconcile_grafana_admin_credential_distinguishes_unreachable_from_401(monkeypatch):
    """A crash-looping/still-starting Grafana (#3538) must not be reported with
    the same "still doesn't authenticate" message a genuine 401 gets -- they
    need opposite operator responses (check the boot log vs. re-check the
    credential)."""
    monkeypatch.setattr(ops, "_grafana_admin_password", lambda cfg: "ops-managed-secret")
    monkeypatch.setattr(ops, "_grafana_admin_auth_status", lambda url, pw: "unreachable")
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        ops,
        "_reset_grafana_admin_password",
        lambda pw: ops.OpsResult(True, "Reset Grafana admin password"),
    )

    password, result = ops._reconcile_grafana_admin_credential(
        "http://localhost:3001", ConfigParser(), attempts=2
    )

    assert password is None
    assert result.ok is False
    assert result.message == "Grafana is unreachable after reset"
    assert "crash loop" in result.details or "crash-looping" in result.details
    assert "still doesn't authenticate" not in result.message


@pytest.mark.unit
def test_grafana_admin_authenticates_true_on_200(monkeypatch):
    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    assert ops._grafana_admin_authenticates("http://localhost:3001", "admin") is True


@pytest.mark.unit
def test_grafana_admin_authenticates_false_on_401(monkeypatch):
    class FakeResponse:
        status_code = 401

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    assert ops._grafana_admin_authenticates("http://localhost:3001", "wrong") is False


@pytest.mark.unit
def test_grafana_admin_authenticates_false_when_unreachable(monkeypatch):
    class FailingClient:
        def __init__(self, *a, **kw):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(ops.httpx, "Client", FailingClient)

    assert ops._grafana_admin_authenticates("http://localhost:3001", "admin") is False


@pytest.mark.unit
def test_grafana_admin_auth_status_ok_on_200(monkeypatch):
    class FakeResponse:
        status_code = 200

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    assert ops._grafana_admin_auth_status("http://localhost:3001", "admin") == "ok"


@pytest.mark.unit
def test_grafana_admin_auth_status_unauthorized_on_401(monkeypatch):
    class FakeResponse:
        status_code = 401

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    assert ops._grafana_admin_auth_status("http://localhost:3001", "wrong") == "unauthorized"


@pytest.mark.unit
def test_grafana_admin_auth_status_unreachable_on_connect_error(monkeypatch):
    class FailingClient:
        def __init__(self, *a, **kw):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(ops.httpx, "Client", FailingClient)

    assert ops._grafana_admin_auth_status("http://localhost:3001", "admin") == "unreachable"


@pytest.mark.unit
def test_grafana_admin_auth_status_unreachable_on_unexpected_status(monkeypatch):
    class FakeResponse:
        status_code = 503

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    assert ops._grafana_admin_auth_status("http://localhost:3001", "admin") == "unreachable"


@pytest.mark.unit
def test_reset_grafana_admin_password_runs_grafana_cli_via_compose_exec(tmp_path, monkeypatch):
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ops.self_heal, "COMPOSE_FILE", compose_path)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)

    captured_cmd = []

    def _fake_run(cmd, check=False):
        captured_cmd.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Admin password changed", stderr="")

    monkeypatch.setattr(ops, "_run", _fake_run)

    result = ops._reset_grafana_admin_password("new-secret")

    assert result.ok is True
    assert captured_cmd == [
        "docker",
        "compose",
        "-f",
        str(compose_path),
        "exec",
        "-T",
        "grafana",
        "grafana",
        "cli",
        "admin",
        "reset-admin-password",
        "new-secret",
    ]


@pytest.mark.unit
def test_reset_grafana_admin_password_fails_without_docker_compose(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: False)

    result = ops._reset_grafana_admin_password("new-secret")

    assert result.ok is False
    assert "Docker Compose not found" in result.details


@pytest.mark.unit
def test_reset_grafana_admin_password_fails_on_nonzero_exit(tmp_path, monkeypatch):
    compose_path = tmp_path / "docker-compose.yml"
    compose_path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ops.self_heal, "COMPOSE_FILE", compose_path)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, check=False: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="grafana: no such service"
        ),
    )

    result = ops._reset_grafana_admin_password("new-secret")

    assert result.ok is False
    assert "no such service" in result.details


@pytest.mark.unit
def test_reconcile_grafana_provisioning_records_fingerprint_and_verifies(tmp_path, monkeypatch):
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)
    monkeypatch.setattr(
        ops, "_start_observability_stack", lambda: [ops.OpsResult(True, "stack up")]
    )
    verify_calls = []
    monkeypatch.setattr(
        ops,
        "_verify_grafana_datasources_resolve",
        lambda *a, **k: verify_calls.append((a, k)) or ops.OpsResult(True, "verified"),
    )

    # No config.ini in this tmp home -- verification is skipped, not attempted.
    results = ops._reconcile_grafana_provisioning()

    assert all(r.ok for r in results)
    assert verify_calls == []
    assert not ops._grafana_provisioning_drifted()


@pytest.mark.unit
def test_reconcile_grafana_provisioning_verifies_when_config_exists(tmp_path, monkeypatch):
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    (tmp_path / ".nyxGPT").mkdir()
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.write_text(
        "[monitoring]\ngrafana_ui_url = http://localhost:3001\ngrafana_admin_password = secret\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)
    monkeypatch.setattr(
        ops, "_start_observability_stack", lambda: [ops.OpsResult(True, "stack up")]
    )
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    monkeypatch.setattr(ops, "_wait_for_grafana_healthy", lambda: True)
    credential_calls = []
    monkeypatch.setattr(
        ops,
        "_reconcile_grafana_admin_credential",
        lambda url, cfg: credential_calls.append(
            (url, cfg.get("monitoring", "grafana_admin_password"))
        )
        or ("secret", ops.OpsResult(True, "credential reconciled")),
    )
    plugin_verify_calls = []
    monkeypatch.setattr(
        ops,
        "_verify_grafana_plugins_installed",
        lambda url, pw: plugin_verify_calls.append((url, pw)) or ops.OpsResult(True, "verified"),
    )
    verify_calls = []
    monkeypatch.setattr(
        ops,
        "_verify_grafana_datasources_resolve",
        lambda url, pw: verify_calls.append((url, pw)) or ops.OpsResult(True, "verified"),
    )
    token_calls = []
    monkeypatch.setattr(
        ops,
        "_provision_grafana_doctor_token",
        lambda url, pw: token_calls.append((url, pw)) or ops.OpsResult(True, "token ready"),
    )

    results = ops._reconcile_grafana_provisioning()

    assert all(r.ok for r in results)
    assert credential_calls == [("http://localhost:3001", "secret")]
    assert plugin_verify_calls == [("http://localhost:3001", "secret")]
    assert verify_calls == [("http://localhost:3001", "secret")]
    assert token_calls == [("http://localhost:3001", "secret")]


@pytest.mark.unit
def test_reconcile_grafana_provisioning_reports_single_failure_when_credential_broken(
    tmp_path, monkeypatch
):
    """A broken admin credential must surface as ONE actionable failure, not
    three separate 401s from the plugin/datasource/token checks (#3458)."""
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    (tmp_path / ".nyxGPT").mkdir()
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.write_text(
        "[monitoring]\ngrafana_ui_url = http://localhost:3001\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)
    monkeypatch.setattr(
        ops, "_start_observability_stack", lambda: [ops.OpsResult(True, "stack up")]
    )
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    monkeypatch.setattr(ops, "_wait_for_grafana_healthy", lambda: True)
    monkeypatch.setattr(
        ops,
        "_reconcile_grafana_admin_credential",
        lambda url, cfg: (
            None,
            ops.OpsResult(False, "Could not reconcile Grafana admin credential"),
        ),
    )

    def _boom(*a, **kw):
        raise AssertionError("credential-dependent checks must not run when reconciliation failed")

    monkeypatch.setattr(ops, "_verify_grafana_plugins_installed", _boom)
    monkeypatch.setattr(ops, "_verify_grafana_datasources_resolve", _boom)
    monkeypatch.setattr(ops, "_provision_grafana_doctor_token", _boom)

    results = ops._reconcile_grafana_provisioning()

    failures = [r for r in results if not r.ok]
    assert len(failures) == 1
    assert "Could not reconcile Grafana admin credential" in failures[0].message


@pytest.mark.unit
def test_reconcile_grafana_provisioning_reports_single_failure_when_never_healthy(
    tmp_path, monkeypatch
):
    """A crash-looping Grafana (#3538, e.g. a bad alerting-provisioning file)
    must surface as one clear failure here -- not as a misleading credential
    "still doesn't authenticate" message from a later step."""
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    (tmp_path / ".nyxGPT").mkdir()
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.write_text(
        "[monitoring]\ngrafana_ui_url = http://localhost:3001\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)
    monkeypatch.setattr(
        ops, "_start_observability_stack", lambda: [ops.OpsResult(True, "stack up")]
    )
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    monkeypatch.setattr(ops, "_wait_for_grafana_healthy", lambda: False)

    def _boom(*a, **kw):
        raise AssertionError("must not authenticate against a Grafana that never became healthy")

    monkeypatch.setattr(ops, "_reconcile_grafana_admin_credential", _boom)

    results = ops._reconcile_grafana_provisioning()

    failures = [r for r in results if not r.ok]
    assert len(failures) == 1
    assert "never became healthy" in failures[0].message


@pytest.mark.unit
def test_reconcile_grafana_provisioning_skips_start_when_recreate_fails(tmp_path, monkeypatch):
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    monkeypatch.setattr(
        ops,
        "_recreate_grafana_if_provisioning_drifted",
        lambda: ops.OpsResult(False, "recreate failed", "boom"),
    )
    start_calls = []
    monkeypatch.setattr(ops, "_start_observability_stack", lambda: start_calls.append(True) or [])

    results = ops._reconcile_grafana_provisioning()

    assert len(results) == 1
    assert results[0].ok is False
    assert start_calls == []


@pytest.mark.unit
def test_reconcile_grafana_provisioning_skips_verification_when_start_fails(tmp_path, monkeypatch):
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)
    monkeypatch.setattr(
        ops,
        "_start_observability_stack",
        lambda: [ops.OpsResult(False, "start failed", "boom")],
    )
    verify_calls = []
    monkeypatch.setattr(
        ops,
        "_verify_grafana_datasources_resolve",
        lambda *a, **k: verify_calls.append(True) or ops.OpsResult(True, "verified"),
    )

    results = ops._reconcile_grafana_provisioning()

    assert any(not r.ok for r in results)
    assert verify_calls == []
    # A failed start must not be recorded as a successfully-applied fingerprint.
    assert ops._grafana_provisioning_drifted()


@pytest.mark.unit
def test_observability_cli_entrypoint_returns_zero_on_success(capsys):
    with patch.object(
        ops, "_reconcile_grafana_provisioning", return_value=[ops.OpsResult(True, "up")]
    ):
        rc = ops.observability(MagicMock())
        assert rc == 0
        assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_observability_cli_entrypoint_returns_nonzero_on_failure(capsys):
    with patch.object(
        ops,
        "_reconcile_grafana_provisioning",
        return_value=[ops.OpsResult(False, "down", "boom")],
    ):
        rc = ops.observability(MagicMock())
        assert rc == 2
        assert "[FAIL]" in capsys.readouterr().out


# --- _stop_brew_service ---


@pytest.mark.unit
def test_stop_brew_service_not_found(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._stop_brew_service("nyxgpt-api")
    assert results[0].ok is False
    assert "brew not found" in results[0].message


@pytest.mark.unit
def test_stop_brew_service_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    results = ops._stop_brew_service("nyxgpt-api")
    assert results[0].ok is True
    assert "Stopped brew service" in results[0].message


@pytest.mark.unit
def test_stop_brew_service_failure_includes_details(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="out", stderr="err"),
    )
    results = ops._stop_brew_service("nyxgpt-api")
    assert results[0].ok is False
    assert "Failed to stop brew service" in results[0].message
    assert "out" in results[0].details
    assert "err" in results[0].details


@pytest.mark.unit
def test_stop_brew_service_exception(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")

    def raise_run(cmd, **k):
        raise OSError("boom")

    monkeypatch.setattr(ops, "_run", raise_run)
    results = ops._stop_brew_service("nyxgpt-api")
    assert results[0].ok is False
    assert "OSError" in results[0].details


# --- _stop_docker_container ---


@pytest.mark.unit
def test_stop_docker_container_no_docker(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._stop_docker_container("nyxgpt-cassandra")
    assert results[0].ok is False
    assert "docker not found" in results[0].message


@pytest.mark.unit
def test_stop_docker_container_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    results = ops._stop_docker_container("nyxgpt-cassandra")
    assert results[0].ok is True
    assert "Stopped docker container" in results[0].message


@pytest.mark.unit
def test_stop_docker_container_failure(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="no such container"),
    )
    results = ops._stop_docker_container("nyxgpt-cassandra")
    assert results[0].ok is False
    assert "no such container" in results[0].details


@pytest.mark.unit
def test_stop_docker_container_exception(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/docker")

    def raise_run(cmd, **k):
        raise OSError("boom")

    monkeypatch.setattr(ops, "_run", raise_run)
    results = ops._stop_docker_container("nyxgpt-cassandra")
    assert results[0].ok is False
    assert "OSError" in results[0].details


# --- _stop_launchagent ---


@pytest.mark.unit
def test_stop_launchagent_success(monkeypatch):
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    results = ops._stop_launchagent("com.nyxgpt.cassandra-logs")
    assert results[0].ok is True
    assert "Stopped LaunchAgent" in results[0].message


@pytest.mark.unit
def test_stop_launchagent_already_stopped_is_ok(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 1, stderr="Could not find service in domain"
        ),
    )
    results = ops._stop_launchagent("com.nyxgpt.cassandra-logs")
    assert results[0].ok is True
    assert "already stopped" in results[0].message


@pytest.mark.unit
def test_stop_launchagent_real_failure(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="out", stderr="err"),
    )
    results = ops._stop_launchagent("com.nyxgpt.cassandra-logs")
    assert results[0].ok is False
    assert "Failed to stop LaunchAgent" in results[0].message
    assert "out" in results[0].details
    assert "err" in results[0].details


@pytest.mark.unit
def test_stop_launchagent_exception(monkeypatch):
    def raise_run(cmd, **k):
        raise OSError("boom")

    monkeypatch.setattr(ops, "_run", raise_run)
    results = ops._stop_launchagent("com.nyxgpt.cassandra-logs")
    assert results[0].ok is False
    assert "OSError" in results[0].details


@pytest.mark.unit
def test_stop_launchagent_not_loaded_logs_debug_not_warning(monkeypatch, caplog):
    """The bootout `_stop_launchagent` issues during `nyxgpt ops down`/`stop`
    is the same unload-if-loaded pattern (#3457) -- "not loaded" must log at
    DEBUG, never WARNING."""

    def fake_subprocess_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 5, stdout="", stderr="Could not find service in domain"
        )

    monkeypatch.setattr(ops.subprocess, "run", fake_subprocess_run)

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        results = ops._stop_launchagent("com.nyxgpt.cassandra-logs")

    assert results[0].ok is True
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected the bootout non-zero exit to still be logged"
    assert all(r.levelno == logging.DEBUG for r in records)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


# --- _compose_stop_service ---


@pytest.mark.unit
def test_compose_stop_service_no_docker(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._compose_stop_service("api")
    assert results[0].ok is False
    assert "docker not found" in results[0].message


@pytest.mark.unit
def test_compose_stop_service_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    results = ops._compose_stop_service("api")
    assert results[0].ok is True
    assert "Stopped Compose service: api" in results[0].message


@pytest.mark.unit
def test_compose_stop_service_failure(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="boom")
    )
    results = ops._compose_stop_service("api")
    assert results[0].ok is False
    assert "boom" in results[0].details


# --- _resolve_observability_services / _resolve_app_services ---


@pytest.mark.unit
def test_resolve_observability_services_success(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=_ALL_COMPOSE_SERVICES),
    )
    services, err = ops._resolve_observability_services()
    assert err is None
    assert "grafana" in services
    assert not (ops.CORE_APP_SERVICES & set(services))


@pytest.mark.unit
def test_resolve_observability_services_failure(monkeypatch):
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="boom")
    )
    services, err = ops._resolve_observability_services()
    assert services is None
    assert err is not None
    assert err.ok is False
    assert "boom" in err.details


@pytest.mark.unit
def test_resolve_app_services_success(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=_ALL_COMPOSE_SERVICES),
    )
    services, err = ops._resolve_app_services()
    assert err is None
    assert set(services) == ops.CORE_APP_SERVICES & {
        s.strip() for s in _ALL_COMPOSE_SERVICES.splitlines() if s.strip()
    }


@pytest.mark.unit
def test_resolve_app_services_failure(monkeypatch):
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="boom")
    )
    services, err = ops._resolve_app_services()
    assert services is None
    assert err is not None
    assert "boom" in err.details


# --- _stop_observability_stack ---


@pytest.mark.unit
def test_stop_observability_stack_skips_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: False)
    results = ops._stop_observability_stack()
    assert results[0].ok is True
    assert "Skipped observability stack" in results[0].message


@pytest.mark.unit
def test_stop_observability_stack_no_services(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(
        ops,
        "_resolve_observability_services",
        lambda: ([], None),
    )
    results = ops._stop_observability_stack()
    assert results[0].ok is True
    assert "No observability services resolved" in results[0].message


@pytest.mark.unit
def test_stop_observability_stack_resolution_failure(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(
        ops,
        "_resolve_observability_services",
        lambda: (None, ops.OpsResult(False, "Failed to resolve observability services", "boom")),
    )
    results = ops._stop_observability_stack()
    assert results[0].ok is False
    assert "Failed to resolve observability services" in results[0].message


@pytest.mark.unit
def test_stop_observability_stack_success(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_resolve_observability_services", lambda: (["grafana"], None))
    calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    results = ops._stop_observability_stack()
    assert results[0].ok is True
    assert "stopped" in results[0].message.lower()
    assert calls[0][:3] == ["docker", "compose", "-f"]
    assert "stop" in calls[0]
    assert "grafana" in calls[0]


@pytest.mark.unit
def test_stop_observability_stack_failure(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_resolve_observability_services", lambda: (["grafana"], None))
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="boom")
    )
    results = ops._stop_observability_stack()
    assert results[0].ok is False
    assert "boom" in results[0].details


# --- _restart_observability_stack ---


@pytest.mark.unit
def test_restart_observability_stack_skips_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: False)
    results = ops._restart_observability_stack()
    assert results[0].ok is True
    assert "Skipped observability stack" in results[0].message


@pytest.mark.unit
def test_restart_observability_stack_no_services(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_resolve_observability_services", lambda: ([], None))
    results = ops._restart_observability_stack()
    assert results[0].ok is True
    assert "No observability services resolved" in results[0].message


@pytest.mark.unit
def test_restart_observability_stack_resolution_failure(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(
        ops,
        "_resolve_observability_services",
        lambda: (None, ops.OpsResult(False, "Failed to resolve observability services", "boom")),
    )
    results = ops._restart_observability_stack()
    assert results[0].ok is False
    assert "Failed to resolve observability services" in results[0].message


@pytest.mark.unit
def test_restart_observability_stack_skips_services_not_running(monkeypatch):
    # grafana/loki are defined by the profiles but not currently up -- restart must
    # report them as cleanly skipped rather than erroring or starting them.
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_resolve_observability_services", lambda: (["grafana", "loki"], None))
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    calls = []
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: calls.append(cmd))
    results = ops._restart_observability_stack()
    assert not calls  # no restart command issued when nothing is running
    messages = [r.message for r in results]
    assert any("grafana" in m and "not running (skipped)" in m for m in messages)
    assert any("loki" in m and "not running (skipped)" in m for m in messages)
    assert all(r.ok for r in results)
    assert "No running observability services to restart" in results[-1].message


@pytest.mark.unit
def test_restart_observability_stack_restarts_only_running_services(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_resolve_observability_services", lambda: (["grafana", "loki"], None))
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    results = ops._restart_observability_stack()
    assert calls[0][:3] == ["docker", "compose", "-f"]
    assert "restart" in calls[0]
    assert "grafana" in calls[0]
    assert "loki" not in calls[0]
    messages = [r.message for r in results]
    assert any("loki" in m and "not running (skipped)" in m for m in messages)
    assert any("Restarted observability services: grafana" in m for m in messages)
    assert all(r.ok for r in results)


@pytest.mark.unit
def test_restart_observability_stack_failure(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_resolve_observability_services", lambda: (["grafana"], None))
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="boom")
    )
    results = ops._restart_observability_stack()
    assert results[-1].ok is False
    assert "boom" in results[-1].details


# --- _compose_down ---


@pytest.mark.unit
def test_compose_down_skips_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: False)
    results = ops._compose_down(["api"], volumes=False)
    assert results[0].ok is True
    assert "Skipped Compose teardown" in results[0].message


@pytest.mark.unit
def test_compose_down_no_services(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    results = ops._compose_down([], volumes=False)
    assert results[0].ok is True
    assert "No Compose services" in results[0].message


@pytest.mark.unit
def test_compose_down_success_preserves_volumes_by_default(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    results = ops._compose_down(["api", "web"], volumes=False)
    assert results[0].ok is True
    assert "data directories preserved" in results[0].message
    assert "--volumes" not in calls[0]
    assert "down" in calls[0]
    assert "api" in calls[0] and "web" in calls[0]


@pytest.mark.unit
def test_compose_down_with_volumes(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path / "home")
    calls = []
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    results = ops._compose_down(["cassandra"], volumes=True)
    assert results[0].ok is True
    assert "their data directories" in results[0].message
    assert "--volumes" not in calls[0]
    data_dir = tmp_path / "home" / ".nyxGPT" / "volumes" / "cassandra"
    assert any(f"Removed data directory: {data_dir}" in r.message for r in results)
    assert not data_dir.exists()


@pytest.mark.unit
def test_compose_down_failure(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stderr="boom")
    )
    results = ops._compose_down(["api"], volumes=False)
    assert results[0].ok is False
    assert "boom" in results[0].details


# --- ops.stop() orchestration ---


def _mode(native=None, compose=None):
    return ops.DeploymentMode(native=native or {}, compose=compose or {}, conflicts=[])


@pytest.mark.unit
def test_stop_native_only_target_all(capsys):
    mode = _mode(
        native={"api": "started", "web": "started", "ollama": "started", "cassandra": "running"},
        compose={},
    )
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]) as sb,
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]) as sd,
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]) as sl,
        patch.object(ops, "_compose_stop_service") as cs,
    ):
        args = MagicMock()
        args.target = "all"
        rc = ops.stop(args)
        assert rc == 0

        assert sb.call_count == 3
        sd.assert_called_once_with("nyxgpt-cassandra")
        sl.assert_called_once_with("com.nyxgpt.cassandra-logs")
        cs.assert_not_called()
        assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_stop_compose_only_target_api(capsys):
    mode = _mode(native={"api": "none"}, compose={"api": "running"})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_stop_brew_service") as sb,
        patch.object(ops, "_compose_stop_service", return_value=[ops.OpsResult(True, "ok")]) as cs,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.stop(args)
        assert rc == 0

        sb.assert_not_called()
        cs.assert_called_once_with("api")


@pytest.mark.unit
def test_stop_mixed_mode_stops_both_and_reports_it(capsys):
    mode = _mode(native={"api": "started"}, compose={"api": "running"})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]) as sb,
        patch.object(ops, "_compose_stop_service", return_value=[ops.OpsResult(True, "ok")]) as cs,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.stop(args)
        assert rc == 0

        sb.assert_called_once_with("nyxgpt-api")
        cs.assert_called_once_with("api")
        out = capsys.readouterr().out
        assert "mixed mode" in out


@pytest.mark.unit
def test_stop_already_stopped_component_does_nothing(capsys):
    mode = _mode(native={"api": "none"}, compose={})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_stop_brew_service") as sb,
        patch.object(ops, "_compose_stop_service") as cs,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.stop(args)
        assert rc == 0

        sb.assert_not_called()
        cs.assert_not_called()
        assert "already stopped" in capsys.readouterr().out


@pytest.mark.unit
def test_stop_marks_component_intentionally_stopped(capsys):
    mode = _mode(native={"api": "started"}, compose={})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops.self_heal, "mark_intentionally_stopped") as mark_stopped,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.stop(args)
        assert rc == 0

    mark_stopped.assert_called_once_with("api")


@pytest.mark.unit
def test_stop_marks_already_stopped_component_too(capsys):
    """Marking happens even when nothing was actually running -- idempotent,
    and it correctly reflects operator intent regardless of prior state."""
    mode = _mode(native={"api": "none"}, compose={})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops.self_heal, "mark_intentionally_stopped") as mark_stopped,
    ):
        args = MagicMock()
        args.target = "api"
        ops.stop(args)

    mark_stopped.assert_called_once_with("api")


@pytest.mark.unit
def test_stop_all_marks_every_core_component(capsys):
    mode = _mode(
        native={"api": "started", "web": "started", "ollama": "started", "cassandra": "running"},
        compose={},
    )
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops.self_heal, "mark_intentionally_stopped") as mark_stopped,
    ):
        args = MagicMock()
        args.target = "all"
        ops.stop(args)

    assert mark_stopped.call_args_list == [
        call("api"),
        call("web"),
        call("ollama"),
        call("cassandra"),
    ]


@pytest.mark.unit
def test_stop_mark_intentional_stop_failure_does_not_block_stop(capsys):
    mode = _mode(native={"api": "started"}, compose={})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops.self_heal, "mark_intentionally_stopped", side_effect=RuntimeError("boom")),
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.stop(args)

    assert rc == 0
    assert "Could not mark api as intentionally stopped" in capsys.readouterr().out


@pytest.mark.unit
def test_stop_leaves_terraform_or_kubernetes_managed_component_unmarked(capsys):
    """`nyxgpt ops stop` (no --terraform/--kubernetes) never stops a
    Terraform/Kubernetes-managed container, so it must not mark that
    component intentionally stopped either -- doing so would blind
    self-heal to a component this call never touched (#3406)."""
    mode = _mode(native={"api": "none"}, compose={})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_terraform_or_kubernetes_managed_components", return_value={"api"}),
        patch.object(ops.self_heal, "mark_intentionally_stopped") as mark_stopped,
        patch.object(ops, "_stop_brew_service") as sb,
        patch.object(ops, "_compose_stop_service") as cs,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.stop(args)

    assert rc == 0
    mark_stopped.assert_not_called()
    sb.assert_not_called()
    cs.assert_not_called()
    assert "Terraform/Kubernetes" in capsys.readouterr().out


@pytest.mark.unit
def test_stop_observability_target_calls_stop_observability_stack():
    mode = _mode()
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(
            ops, "_stop_observability_stack", return_value=[ops.OpsResult(True, "ok")]
        ) as so,
    ):
        args = MagicMock()
        args.target = "observability"
        rc = ops.stop(args)
        assert rc == 0
        so.assert_called_once()


@pytest.mark.unit
def test_stop_all_target_does_not_touch_observability():
    mode = _mode(native={}, compose={})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_observability_stack") as so,
    ):
        args = MagicMock()
        args.target = "all"
        ops.stop(args)
        so.assert_not_called()


@pytest.mark.unit
def test_stop_returns_nonzero_on_failure(capsys):
    mode = _mode(native={"api": "started"}, compose={})
    with (
        patch.object(ops, "detect_deployment_mode", return_value=mode),
        patch.object(
            ops,
            "_stop_brew_service",
            return_value=[ops.OpsResult(False, "bad", "details")],
        ),
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.stop(args)
        assert rc == 2
        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "details" in out


# --- ops.down() orchestration ---


@pytest.mark.unit
def test_down_refuses_volumes_without_yes_really(capsys):
    args = MagicMock(
        app_only=False,
        observability_only=False,
        volumes=True,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    rc = ops.down(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "--yes-really" in err


@pytest.mark.unit
def test_down_all_scope_stops_native_and_composes_down(capsys):
    args = MagicMock(
        app_only=False,
        observability_only=False,
        volumes=False,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]) as sb,
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]) as sd,
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]) as sl,
        patch.object(ops, "_compose_available", return_value=True),
        patch.object(ops, "_resolve_app_services", return_value=(["api", "web"], None)) as ra,
        patch.object(
            ops, "_resolve_observability_services", return_value=(["grafana"], None)
        ) as ro,
        patch.object(ops, "_compose_down", return_value=[ops.OpsResult(True, "ok")]) as cd,
    ):
        rc = ops.down(args)
        assert rc == 0

        assert sb.call_count == 3
        sd.assert_called_once_with("nyxgpt-cassandra")
        sl.assert_called_once_with("com.nyxgpt.cassandra-logs")
        ra.assert_called_once()
        ro.assert_called_once()
        cd.assert_called_once()
        composed_services = cd.call_args.args[0]
        assert set(composed_services) == {"api", "web", "grafana"}
        assert cd.call_args.kwargs == {"volumes": False}


@pytest.mark.unit
def test_down_marks_core_components_intentionally_stopped_before_teardown():
    """`ops down` marks api/web/ollama/cassandra as intentionally stopped when
    tearing down the app tier, so self-heal leaves them alone instead of
    restarting what the teardown just stopped (acceptance failure: self-heal
    must honor an intentional down). This replaces the old global watchdog
    disable (commit f9967a72, #3406) -- the watchdog itself is untouched."""
    args = MagicMock(
        app_only=False,
        observability_only=False,
        volumes=False,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(ops.self_heal, "mark_intentionally_stopped") as mark_stopped,
        patch.object(ops.self_heal, "set_enabled") as set_enabled,
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_compose_available", return_value=False),
    ):
        rc = ops.down(args)

    assert rc == 0
    assert mark_stopped.call_args_list == [
        call("api"),
        call("web"),
        call("ollama"),
        call("cassandra"),
    ]
    # The watchdog's enabled/disabled state is never touched by `down` --
    # the SRE dashboard toggle is the only arm/disarm path (#3406).
    set_enabled.assert_not_called()


@pytest.mark.unit
def test_down_leaves_terraform_or_kubernetes_managed_components_unmarked():
    """A plain `nyxgpt ops down` (no --terraform/--kubernetes) only ever
    stops the native/Compose form of api/web/ollama/cassandra. If one of
    those service names is actually running under Terraform or Kubernetes,
    this call never touches it, so it must not mark it intentionally
    stopped either -- otherwise self-heal would stop guarding a component
    that never went down (#3406)."""
    args = MagicMock(
        app_only=False,
        observability_only=False,
        volumes=False,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(
            ops, "_terraform_or_kubernetes_managed_components", return_value={"api", "web"}
        ),
        patch.object(ops.self_heal, "mark_intentionally_stopped") as mark_stopped,
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_compose_available", return_value=False),
    ):
        rc = ops.down(args)

    assert rc == 0
    assert mark_stopped.call_args_list == [call("ollama"), call("cassandra")]


@pytest.mark.unit
def test_down_observability_only_does_not_touch_self_heal():
    """Tearing down only the observability tier must not mark any core
    component intentionally stopped -- the core app stack it guards is still
    meant to be running."""
    args = MagicMock(
        app_only=False,
        observability_only=True,
        volumes=False,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(ops.self_heal, "mark_intentionally_stopped") as mark_stopped,
        patch.object(ops.self_heal, "set_enabled") as set_enabled,
        patch.object(ops, "_compose_available", return_value=False),
    ):
        ops.down(args)

    mark_stopped.assert_not_called()
    set_enabled.assert_not_called()


@pytest.mark.unit
def test_down_app_only_scope_skips_observability(capsys):
    args = MagicMock(
        app_only=True,
        observability_only=False,
        volumes=False,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_compose_available", return_value=True),
        patch.object(ops, "_resolve_app_services", return_value=(["api"], None)),
        patch.object(ops, "_resolve_observability_services") as ro,
        patch.object(ops, "_compose_down", return_value=[ops.OpsResult(True, "ok")]) as cd,
    ):
        rc = ops.down(args)
        assert rc == 0
        ro.assert_not_called()
        assert cd.call_args.args[0] == ["api"]


@pytest.mark.unit
def test_down_observability_only_scope_skips_native_and_app(capsys):
    args = MagicMock(
        app_only=False,
        observability_only=True,
        volumes=False,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(ops, "_stop_brew_service") as sb,
        patch.object(ops, "_stop_docker_container") as sd,
        patch.object(ops, "_stop_launchagent") as sl,
        patch.object(ops, "_compose_available", return_value=True),
        patch.object(ops, "_resolve_app_services") as ra,
        patch.object(ops, "_resolve_observability_services", return_value=(["grafana"], None)),
        patch.object(ops, "_compose_down", return_value=[ops.OpsResult(True, "ok")]) as cd,
    ):
        rc = ops.down(args)
        assert rc == 0

        sb.assert_not_called()
        sd.assert_not_called()
        sl.assert_not_called()
        ra.assert_not_called()
        assert cd.call_args.args[0] == ["grafana"]


@pytest.mark.unit
def test_down_with_volumes_and_yes_really_passes_volumes_flag():
    args = MagicMock(
        app_only=True,
        observability_only=False,
        volumes=True,
        yes_really=True,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_compose_available", return_value=True),
        patch.object(ops, "_resolve_app_services", return_value=(["api"], None)),
        patch.object(ops, "_compose_down", return_value=[ops.OpsResult(True, "ok")]) as cd,
    ):
        rc = ops.down(args)
        assert rc == 0
        assert cd.call_args.kwargs == {"volumes": True}


@pytest.mark.unit
def test_down_skips_compose_teardown_without_docker():
    args = MagicMock(
        app_only=False,
        observability_only=False,
        volumes=False,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(ops, "_stop_brew_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_compose_available", return_value=False),
        patch.object(ops, "_compose_down") as cd,
    ):
        rc = ops.down(args)
        assert rc == 0
        cd.assert_not_called()


@pytest.mark.unit
def test_down_returns_nonzero_on_failure():
    args = MagicMock(
        app_only=False,
        observability_only=False,
        volumes=False,
        yes_really=False,
        terraform=False,
        kubernetes=False,
    )
    with (
        patch.object(
            ops, "_stop_brew_service", return_value=[ops.OpsResult(False, "bad", "details")]
        ),
        patch.object(ops, "_stop_docker_container", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_stop_launchagent", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_compose_available", return_value=False),
    ):
        rc = ops.down(args)
        assert rc == 2


def _mock_client(base_url, handler):
    return httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))


@pytest.mark.unit
def test_glitchtip_container_healthy_true(monkeypatch):
    status = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="running", health="healthy", healthy=True
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [status])
    assert ops._glitchtip_container_healthy() is True


@pytest.mark.unit
def test_glitchtip_container_healthy_false_when_unhealthy(monkeypatch):
    status = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="starting", health="starting", healthy=False
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [status])
    assert ops._glitchtip_container_healthy() is False


@pytest.mark.unit
def test_glitchtip_container_healthy_false_when_absent(monkeypatch):
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [])
    assert ops._glitchtip_container_healthy() is False


@pytest.mark.unit
def test_wait_for_glitchtip_healthy_absent_returns_false_without_sleeping(monkeypatch):
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [])
    sleeps = []
    monkeypatch.setattr(ops.time, "sleep", lambda s: sleeps.append(s))

    assert ops._wait_for_glitchtip_healthy(timeout=5, poll_interval=0.01) is False
    assert sleeps == []


@pytest.mark.unit
def test_wait_for_glitchtip_healthy_already_healthy_returns_true_immediately(monkeypatch):
    status = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="running", health="healthy", healthy=True
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [status])
    sleeps = []
    monkeypatch.setattr(ops.time, "sleep", lambda s: sleeps.append(s))

    assert ops._wait_for_glitchtip_healthy(timeout=5, poll_interval=0.01) is True
    assert sleeps == []


@pytest.mark.unit
def test_wait_for_glitchtip_healthy_polls_until_healthy(monkeypatch):
    unhealthy = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="starting", health="starting", healthy=False
    )
    healthy = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="running", health="healthy", healthy=True
    )
    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return [healthy] if calls["n"] >= 3 else [unhealthy]

    monkeypatch.setattr(ops.self_heal, "list_component_status", fake_status)
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)

    assert ops._wait_for_glitchtip_healthy(timeout=5, poll_interval=0.01) is True
    assert calls["n"] >= 3


@pytest.mark.unit
def test_wait_for_glitchtip_healthy_times_out(monkeypatch):
    unhealthy = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="starting", health="starting", healthy=False
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [unhealthy])
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)

    assert ops._wait_for_glitchtip_healthy(timeout=0.05, poll_interval=0.01) is False


@pytest.mark.unit
def test_wait_for_glitchtip_healthy_absent_but_enabled_returns_false_without_sleeping(
    monkeypatch,
):
    """Regression (#3356 review): error_tracking enabled + torn-down container.

    Exercises the real `self_heal.list_component_status()` (only `_run`,
    `_which`, and `load_config` are mocked) so this test observes the exact
    seam that broke: after the desired-state PR, a torn-down `glitchtip`
    container whose `[error_tracking]` flag is still enabled in config.ini is
    reported as `state="absent"`, not omitted -- so `_wait_for_glitchtip_healthy`
    must treat that the same as "not present" (fail fast) rather than polling
    out the full timeout for a container nothing in this call path starts.
    """
    cfg = ConfigParser()
    cfg.add_section("error_tracking")
    cfg.set("error_tracking", "enabled", "true")
    monkeypatch.setattr(ops.self_heal, "load_config", lambda: cfg)
    monkeypatch.setattr(ops.self_heal, "_which", lambda prog: "/usr/bin/docker")

    def fake_run(cmd, timeout=30.0, **_k):
        if "config" in cmd and "--services" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="glitchtip\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="")  # `ps -a`: nothing running

    monkeypatch.setattr(ops.self_heal, "_run", fake_run)

    sleeps = []
    monkeypatch.setattr(ops.time, "sleep", lambda s: sleeps.append(s))

    assert ops._wait_for_glitchtip_healthy(timeout=5, poll_interval=0.01) is False
    assert sleeps == []


# --- Grafana container health polling (#3538) ---


@pytest.mark.unit
def test_grafana_container_healthy_true(monkeypatch):
    status = self_heal.ComponentStatus(
        service="grafana", container="c", state="running", health="healthy", healthy=True
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [status])
    assert ops._grafana_container_healthy() is True


@pytest.mark.unit
def test_grafana_container_healthy_false_when_unhealthy(monkeypatch):
    status = self_heal.ComponentStatus(
        service="grafana", container="c", state="restarting", health="", healthy=False
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [status])
    assert ops._grafana_container_healthy() is False


@pytest.mark.unit
def test_grafana_container_healthy_false_when_absent(monkeypatch):
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [])
    assert ops._grafana_container_healthy() is False


@pytest.mark.unit
def test_wait_for_grafana_healthy_absent_returns_false_without_sleeping(monkeypatch):
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [])
    sleeps = []
    monkeypatch.setattr(ops.time, "sleep", lambda s: sleeps.append(s))

    assert ops._wait_for_grafana_healthy(timeout=5, poll_interval=0.01) is False
    assert sleeps == []


@pytest.mark.unit
def test_wait_for_grafana_healthy_already_healthy_returns_true_immediately(monkeypatch):
    status = self_heal.ComponentStatus(
        service="grafana", container="c", state="running", health="healthy", healthy=True
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [status])
    sleeps = []
    monkeypatch.setattr(ops.time, "sleep", lambda s: sleeps.append(s))

    assert ops._wait_for_grafana_healthy(timeout=5, poll_interval=0.01) is True
    assert sleeps == []


@pytest.mark.unit
def test_wait_for_grafana_healthy_polls_until_healthy(monkeypatch):
    unhealthy = self_heal.ComponentStatus(
        service="grafana", container="c", state="starting", health="starting", healthy=False
    )
    healthy = self_heal.ComponentStatus(
        service="grafana", container="c", state="running", health="healthy", healthy=True
    )
    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return [healthy] if calls["n"] >= 3 else [unhealthy]

    monkeypatch.setattr(ops.self_heal, "list_component_status", fake_status)
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)

    assert ops._wait_for_grafana_healthy(timeout=5, poll_interval=0.01) is True
    assert calls["n"] >= 3


@pytest.mark.unit
def test_wait_for_grafana_healthy_times_out_when_crash_looping(monkeypatch):
    """A container stuck `restarting` (#3538's crash loop) never reports
    healthy -- this must time out and return False, not hang forever."""
    restarting = self_heal.ComponentStatus(
        service="grafana", container="c", state="restarting", health="", healthy=False
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [restarting])
    monkeypatch.setattr(ops.time, "sleep", lambda s: None)

    assert ops._wait_for_grafana_healthy(timeout=0.05, poll_interval=0.01) is False


@pytest.mark.unit
def test_resolve_admin_credentials_generates_when_missing(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\n", encoding="utf-8")

    email, password, generated = ops._resolve_admin_credentials(cfg_path)

    assert email == ops.GLITCHTIP_DEFAULT_ADMIN_EMAIL
    assert generated is True
    assert len(password) > 10


@pytest.mark.unit
def test_resolve_admin_credentials_reads_existing(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text(
        "[error_tracking]\nadmin_email = owner@example.com\nadmin_password = s3cr3t\n",
        encoding="utf-8",
    )

    email, password, generated = ops._resolve_admin_credentials(cfg_path)

    assert email == "owner@example.com"
    assert password == "s3cr3t"
    assert generated is False


@pytest.mark.unit
def test_persist_admin_credentials_writes_and_chmods(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[nyxgpt]\ndefault_model = llama3.1:8b\n", encoding="utf-8")

    ops._persist_admin_credentials(cfg_path, "admin@nyxgpt.local", "generated-pw")

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("error_tracking", "admin_email") == "admin@nyxgpt.local"
    assert parser.get("error_tracking", "admin_password") == "generated-pw"
    assert parser.get("nyxgpt", "default_model") == "llama3.1:8b"
    assert oct(cfg_path.stat().st_mode)[-3:] == "600"


@pytest.mark.unit
def test_glitchtip_ensure_superuser_success(monkeypatch):
    monkeypatch.setattr(
        ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    )
    result = ops._glitchtip_ensure_superuser("admin@nyxgpt.local", "pw")
    assert result.ok
    assert "Created" in result.message


@pytest.mark.unit
def test_glitchtip_ensure_superuser_already_exists_is_ok(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="Error: That email address is already taken."
        ),
    )
    result = ops._glitchtip_ensure_superuser("admin@nyxgpt.local", "pw")
    assert result.ok
    assert "already exists" in result.message


@pytest.mark.unit
def test_glitchtip_ensure_superuser_other_failure(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )
    result = ops._glitchtip_ensure_superuser("admin@nyxgpt.local", "pw")
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_glitchtip_ensure_superuser_command_shape(monkeypatch):
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)
    ops._glitchtip_ensure_superuser("admin@nyxgpt.local", "pw")

    cmd = captured["cmd"]
    assert cmd[:4] == ["docker", "compose", "-f", str(ops.self_heal.COMPOSE_FILE)]
    assert "glitchtip" in cmd
    assert "createsuperuser" in cmd
    assert "--noinput" in cmd
    assert "DJANGO_SUPERUSER_EMAIL=admin@nyxgpt.local" in cmd
    assert "DJANGO_SUPERUSER_PASSWORD=pw" in cmd


@pytest.mark.unit
def test_glitchtip_login_success(monkeypatch):
    def handler(request):
        if request.url.path == "/_allauth/browser/v1/config" and request.method == "GET":
            return httpx.Response(200, headers={"set-cookie": "csrftoken=abc123; Path=/"})
        if request.url.path == "/_allauth/browser/v1/auth/login" and request.method == "POST":
            assert request.headers.get("x-csrftoken") == "abc123"
            return httpx.Response(200, json={"status": 200})
        return httpx.Response(404)

    monkeypatch.setattr(
        ops, "_glitchtip_http_client", lambda base_url, **k: _mock_client(base_url, handler)
    )

    client, result = ops._glitchtip_login("http://localhost:8080", "admin@nyxgpt.local", "pw")
    assert result.ok
    assert client is not None
    client.close()


@pytest.mark.unit
def test_glitchtip_login_falls_back_to_legacy_endpoint(monkeypatch):
    # Older GlitchTip images have no allauth headless routes; a 404 on the
    # headless login must fall through to the legacy /api/auth/login/ flow.
    def handler(request):
        if request.url.path.startswith("/_allauth/"):
            return httpx.Response(404)
        if request.url.path == "/api/auth/login/" and request.method == "GET":
            return httpx.Response(200, headers={"set-cookie": "csrftoken=abc123; Path=/"})
        if request.url.path == "/api/auth/login/" and request.method == "POST":
            assert request.headers.get("x-csrftoken") == "abc123"
            return httpx.Response(200, json={"detail": "ok"})
        return httpx.Response(404)

    monkeypatch.setattr(
        ops, "_glitchtip_http_client", lambda base_url, **k: _mock_client(base_url, handler)
    )

    client, result = ops._glitchtip_login("http://localhost:8080", "admin@nyxgpt.local", "pw")
    assert result.ok
    assert client is not None
    client.close()


@pytest.mark.unit
def test_glitchtip_login_failure(monkeypatch):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200)
        return httpx.Response(400, text="Invalid credentials")

    monkeypatch.setattr(
        ops, "_glitchtip_http_client", lambda base_url, **k: _mock_client(base_url, handler)
    )

    client, result = ops._glitchtip_login("http://localhost:8080", "admin@nyxgpt.local", "wrong")
    assert not result.ok
    assert client is None


@pytest.mark.unit
def test_glitchtip_ensure_api_token_creates_new():
    def handler(request):
        if request.method == "GET" and request.url.path == "/api/0/api-tokens/":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/api/0/api-tokens/":
            return httpx.Response(201, json={"token": "abc-token"})
        return httpx.Response(404)

    client = _mock_client("http://localhost:8080", handler)
    token, result = ops._glitchtip_ensure_api_token(client, "http://localhost:8080")
    assert token == "abc-token"
    assert result.ok
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_api_token_reuses_existing():
    def handler(request):
        if request.url.path == "/api/0/api-tokens/" and request.method == "GET":
            return httpx.Response(
                200, json=[{"name": ops.GLITCHTIP_TOKEN_NAME, "token": "existing-token"}]
            )
        return httpx.Response(404)

    client = _mock_client("http://localhost:8080", handler)
    token, result = ops._glitchtip_ensure_api_token(client, "http://localhost:8080")
    assert token == "existing-token"
    assert result.ok
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_api_token_failure():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(500, text="boom")

    client = _mock_client("http://localhost:8080", handler)
    token, result = ops._glitchtip_ensure_api_token(client, "http://localhost:8080")
    assert token is None
    assert not result.ok
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_organization_creates_new():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"slug": ops.GLITCHTIP_ORG_SLUG})

    client = _mock_client("http://localhost:8080", handler)
    slug, result = ops._glitchtip_ensure_organization(client)
    assert slug == ops.GLITCHTIP_ORG_SLUG
    assert result.ok
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_organization_reuses_existing():
    def handler(request):
        return httpx.Response(200, json=[{"slug": ops.GLITCHTIP_ORG_SLUG}])

    client = _mock_client("http://localhost:8080", handler)
    slug, result = ops._glitchtip_ensure_organization(client)
    assert slug == ops.GLITCHTIP_ORG_SLUG
    assert "existing" in result.message
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_organization_failure():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(500, text="boom")

    client = _mock_client("http://localhost:8080", handler)
    slug, result = ops._glitchtip_ensure_organization(client)
    assert slug is None
    assert not result.ok
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_team_creates_new():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        assert request.url.path == f"/api/0/organizations/{ops.GLITCHTIP_ORG_SLUG}/teams/"
        return httpx.Response(201, json={"slug": ops.GLITCHTIP_TEAM_SLUG})

    client = _mock_client("http://localhost:8080", handler)
    slug, result = ops._glitchtip_ensure_team(client, ops.GLITCHTIP_ORG_SLUG)
    assert slug == ops.GLITCHTIP_TEAM_SLUG
    assert result.ok
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_team_reuses_existing():
    def handler(request):
        return httpx.Response(200, json=[{"slug": ops.GLITCHTIP_TEAM_SLUG}])

    client = _mock_client("http://localhost:8080", handler)
    slug, result = ops._glitchtip_ensure_team(client, ops.GLITCHTIP_ORG_SLUG)
    assert slug == ops.GLITCHTIP_TEAM_SLUG
    assert "existing" in result.message
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_project_creates_new_via_team_route():
    posted_paths = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        posted_paths.append(request.url.path)
        return httpx.Response(201, json={"slug": ops.GLITCHTIP_PROJECT_SLUG})

    client = _mock_client("http://localhost:8080", handler)
    slug, result = ops._glitchtip_ensure_project(
        client, ops.GLITCHTIP_ORG_SLUG, ops.GLITCHTIP_TEAM_SLUG
    )
    assert slug == ops.GLITCHTIP_PROJECT_SLUG
    assert result.ok
    assert posted_paths == [
        f"/api/0/teams/{ops.GLITCHTIP_ORG_SLUG}/{ops.GLITCHTIP_TEAM_SLUG}/projects/"
    ]
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_project_falls_back_to_legacy_org_route():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path.startswith("/api/0/teams/"):
            return httpx.Response(405, text="Method not allowed")
        return httpx.Response(201, json={"slug": ops.GLITCHTIP_PROJECT_SLUG})

    client = _mock_client("http://localhost:8080", handler)
    slug, result = ops._glitchtip_ensure_project(
        client, ops.GLITCHTIP_ORG_SLUG, ops.GLITCHTIP_TEAM_SLUG
    )
    assert slug == ops.GLITCHTIP_PROJECT_SLUG
    assert result.ok
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_project_reuses_existing():
    def handler(request):
        return httpx.Response(200, json=[{"slug": ops.GLITCHTIP_PROJECT_SLUG}])

    client = _mock_client("http://localhost:8080", handler)
    slug, result = ops._glitchtip_ensure_project(
        client, ops.GLITCHTIP_ORG_SLUG, ops.GLITCHTIP_TEAM_SLUG
    )
    assert slug == ops.GLITCHTIP_PROJECT_SLUG
    assert "existing" in result.message
    client.close()


@pytest.mark.unit
def test_extract_dsn_variants():
    assert ops._extract_dsn({"dsn": {"public": "http://a"}}) == "http://a"
    assert ops._extract_dsn({"dsn": "http://b"}) == "http://b"
    assert ops._extract_dsn({"dsn": None}) == ""
    assert ops._extract_dsn({}) == ""
    assert ops._extract_dsn("not-a-dict") == ""


@pytest.mark.unit
def test_glitchtip_ensure_project_key_creates_new_with_dict_dsn():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"dsn": {"public": "http://key@localhost:8080/1"}})

    client = _mock_client("http://localhost:8080", handler)
    dsn, result = ops._glitchtip_ensure_project_key(client, "nyxgpt", "nyxgpt-backend")
    assert dsn == "http://key@localhost:8080/1"
    assert result.ok
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_project_key_reuses_existing_string_dsn():
    def handler(request):
        return httpx.Response(200, json=[{"dsn": "http://key@localhost:8080/1"}])

    client = _mock_client("http://localhost:8080", handler)
    dsn, result = ops._glitchtip_ensure_project_key(client, "nyxgpt", "nyxgpt-backend")
    assert dsn == "http://key@localhost:8080/1"
    assert "existing" in result.message
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_project_key_failure():
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(500, text="boom")

    client = _mock_client("http://localhost:8080", handler)
    dsn, result = ops._glitchtip_ensure_project_key(client, "nyxgpt", "nyxgpt-backend")
    assert dsn is None
    assert not result.ok
    client.close()


@pytest.mark.unit
def test_write_error_tracking_dsn_missing_file_is_noop(tmp_path):
    cfg_path = tmp_path / "missing.ini"
    result = ops._write_error_tracking_dsn(cfg_path, "http://key@localhost:8080/1", chmod_600=True)
    assert result.ok
    assert not cfg_path.exists()


@pytest.mark.unit
def test_write_error_tracking_dsn_writes_and_chmods(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[error_tracking]\nenabled = false\ndsn =\n", encoding="utf-8")

    result = ops._write_error_tracking_dsn(cfg_path, "http://key@localhost:8080/1", chmod_600=True)
    assert result.ok

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("error_tracking", "dsn") == "http://key@localhost:8080/1"
    assert parser.get("error_tracking", "enabled") == "true"
    assert oct(cfg_path.stat().st_mode)[-3:] == "600"


@pytest.mark.unit
def test_write_error_tracking_dsn_no_chmod_for_compose_path(tmp_path):
    cfg_path = tmp_path / "config.docker.ini"
    cfg_path.write_text("[error_tracking]\nenabled = false\ndsn =\n", encoding="utf-8")
    cfg_path.chmod(0o644)

    ops._write_error_tracking_dsn(cfg_path, "http://key@localhost:8080/1", chmod_600=False)

    assert oct(cfg_path.stat().st_mode)[-3:] == "644"


@pytest.mark.unit
def test_write_error_tracking_dsn_preserves_comments(tmp_path):
    """Regression test: a `ConfigParser` read/write round-trip silently drops
    comment lines, which would destroy the hand-written documentation
    comments in the git-tracked `docker/config.docker.ini`. The DSN/enabled
    write must patch only those two lines in place."""
    cfg_path = tmp_path / "config.docker.ini"
    original = (
        "[error_tracking]\n"
        "# Error tracking is local-only -- see docs/self-healing.md.\n"
        "enabled = false\n"
        "# Auto-filled by `nyxgpt ops glitchtip-init`.\n"
        "dsn =\n"
        "environment = docker\n"
        "\n"
        "[monitoring]\n"
        "# Monitoring section comment.\n"
        "enabled = false\n"
    )
    cfg_path.write_text(original, encoding="utf-8")

    result = ops._write_error_tracking_dsn(cfg_path, "http://key@localhost:8080/1", chmod_600=False)
    assert result.ok

    written = cfg_path.read_text(encoding="utf-8")
    assert "# Error tracking is local-only -- see docs/self-healing.md.\n" in written
    assert "# Auto-filled by `nyxgpt ops glitchtip-init`.\n" in written
    assert "# Monitoring section comment.\n" in written
    assert "dsn = http://key@localhost:8080/1\n" in written
    assert "enabled = true\n" in written
    assert "environment = docker\n" in written

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("error_tracking", "dsn") == "http://key@localhost:8080/1"
    assert parser.get("error_tracking", "enabled") == "true"
    assert parser.get("monitoring", "enabled") == "false"


@pytest.mark.unit
def test_write_grafana_glitchtip_token_writes_and_chmods(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    changed, result = ops._write_grafana_glitchtip_token("tok-123")

    assert changed is True
    assert result.ok
    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    assert token_path.read_text(encoding="utf-8") == "tok-123"
    assert oct(token_path.stat().st_mode)[-3:] == "600"
    assert oct(token_path.parent.stat().st_mode)[-3:] == "700"


@pytest.mark.unit
def test_write_grafana_glitchtip_token_is_a_noop_when_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    first_changed, _ = ops._write_grafana_glitchtip_token("tok-123")
    second_changed, second_result = ops._write_grafana_glitchtip_token("tok-123")

    assert first_changed is True
    assert second_changed is False
    assert second_result.ok


@pytest.mark.unit
def test_write_grafana_glitchtip_token_detects_a_rotated_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    ops._write_grafana_glitchtip_token("tok-old")
    changed, _ = ops._write_grafana_glitchtip_token("tok-new")

    assert changed is True
    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    assert token_path.read_text(encoding="utf-8") == "tok-new"


# --- Slack webhook secret for Grafana's alerting contact point (#3466) ---


@pytest.mark.unit
def test_write_grafana_slack_webhook_secret_writes_and_chmods(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    changed, result = ops._write_grafana_slack_webhook_secret("https://hooks.slack.com/x")

    assert changed is True
    assert result.ok
    secret_path = tmp_path / ".nyxGPT" / "secrets" / "slack-webhook-url"
    assert secret_path.read_text(encoding="utf-8") == "https://hooks.slack.com/x"
    assert oct(secret_path.stat().st_mode)[-3:] == "600"


@pytest.mark.unit
def test_write_grafana_slack_webhook_secret_writes_placeholder_when_unset(tmp_path, monkeypatch):
    """Never writes an empty string (#3538): Grafana's alerting-provisioning
    validator crash-loops the container on an empty `url`, so an unconfigured
    webhook gets a syntactically-valid but non-functional placeholder
    instead."""
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    changed, result = ops._write_grafana_slack_webhook_secret("")

    assert changed is True
    assert result.ok
    secret_path = tmp_path / ".nyxGPT" / "secrets" / "slack-webhook-url"
    assert secret_path.exists()
    assert secret_path.read_text(encoding="utf-8") == ops.GRAFANA_SLACK_WEBHOOK_PLACEHOLDER_URL


@pytest.mark.unit
def test_write_grafana_slack_webhook_secret_is_a_noop_when_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    first_changed, _ = ops._write_grafana_slack_webhook_secret("https://hooks.slack.com/x")
    second_changed, second_result = ops._write_grafana_slack_webhook_secret(
        "https://hooks.slack.com/x"
    )

    assert first_changed is True
    assert second_changed is False
    assert second_result.ok


@pytest.mark.unit
def test_write_grafana_slack_webhook_secret_detects_a_rotated_url(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    ops._write_grafana_slack_webhook_secret("https://hooks.slack.com/old")
    changed, _ = ops._write_grafana_slack_webhook_secret("https://hooks.slack.com/new")

    assert changed is True
    secret_path = tmp_path / ".nyxGPT" / "secrets" / "slack-webhook-url"
    assert secret_path.read_text(encoding="utf-8") == "https://hooks.slack.com/new"


@pytest.mark.unit
def test_write_grafana_slack_webhook_secret_reports_actionable_error_on_permission_denied(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    secrets_dir.chmod(0o500)
    try:
        changed, result = ops._write_grafana_slack_webhook_secret("https://hooks.slack.com/x")
    finally:
        secrets_dir.chmod(0o700)

    assert changed is False
    assert result.ok is False
    assert "Cannot write Slack webhook URL" in result.message
    assert "sudo chown" in result.details


@pytest.mark.unit
def test_sync_grafana_slack_webhook_secret_skips_without_config(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    results = ops._sync_grafana_slack_webhook_secret()

    assert len(results) == 1
    assert results[0].ok is True
    assert "no config.ini yet" in results[0].message


@pytest.mark.unit
def test_sync_grafana_slack_webhook_secret_writes_from_config_and_restarts(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[monitoring]\nslack_webhook_url = https://hooks.slack.com/x\n")
    monkeypatch.setattr(
        ops, "_restart_grafana_if_running", lambda reason="": ops.OpsResult(True, "restarted")
    )

    results = ops._sync_grafana_slack_webhook_secret()

    secret_path = tmp_path / ".nyxGPT" / "secrets" / "slack-webhook-url"
    assert secret_path.read_text(encoding="utf-8") == "https://hooks.slack.com/x"
    assert any("Wrote" in r.message for r in results)
    assert any(r.message == "restarted" for r in results)


@pytest.mark.unit
def test_sync_grafana_slack_webhook_secret_skips_restart_when_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[monitoring]\nslack_webhook_url = https://hooks.slack.com/x\n")
    ops._sync_grafana_slack_webhook_secret()

    restart_mock = MagicMock()
    monkeypatch.setattr(ops, "_restart_grafana_if_running", restart_mock)
    results = ops._sync_grafana_slack_webhook_secret()

    restart_mock.assert_not_called()
    assert len(results) == 1


# --- Grafana test-notification path: `nyxgpt ops alert-test` (#3466) ---


@pytest.mark.unit
def test_send_grafana_test_alert_posts_to_alertmanager_api(monkeypatch):
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json=None):
            calls.append((path, json))
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._send_grafana_test_alert("http://localhost:3001", "admin-pw")

    assert result.ok is True
    assert calls[0][0] == "/api/alertmanager/grafana/api/v2/alerts"
    alert = calls[0][1][0]
    assert alert["labels"]["alertname"] == "NyxGPTAlertTest"
    assert "startsAt" in alert and "endsAt" in alert


@pytest.mark.unit
def test_send_grafana_test_alert_reports_failure_on_http_error(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json=None):
            raise ops.httpx.ConnectError("connection refused")

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._send_grafana_test_alert("http://localhost:3001", "admin-pw")

    assert result.ok is False
    assert "Failed to send test alert" in result.message


@pytest.mark.unit
def test_alert_test_fails_without_config(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    rc = ops.alert_test(MagicMock())

    assert rc == 2
    assert "Missing config" in capsys.readouterr().out


@pytest.mark.unit
def test_alert_test_fails_when_monitoring_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[monitoring]\nenabled = false\n")

    rc = ops.alert_test(MagicMock())

    assert rc == 2
    assert "Monitoring is disabled" in capsys.readouterr().out


@pytest.mark.unit
def test_alert_test_sends_synthetic_alert_when_monitoring_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "[monitoring]\nenabled = true\ngrafana_ui_url = http://localhost:3001\n"
        "grafana_admin_password = admin-pw\n"
    )
    monkeypatch.setattr(
        ops, "_send_grafana_test_alert", lambda url, pw: ops.OpsResult(True, "sent")
    )

    rc = ops.alert_test(MagicMock())

    assert rc == 0
    assert "sent" in capsys.readouterr().out


# --- Linux bind-mount ownership: ~/.nyxGPT/secrets preflight (#3432) ---


@pytest.mark.unit
def test_write_grafana_glitchtip_token_reports_actionable_error_on_permission_denied(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    # r-x only -- no write bit -- simulates a root-owned dir left behind by
    # Docker auto-creating the bind-mount source on Linux.
    secrets_dir.chmod(0o500)
    try:
        changed, result = ops._write_grafana_glitchtip_token("tok-123")
    finally:
        secrets_dir.chmod(0o700)

    assert changed is False
    assert result.ok is False
    assert "Cannot write GlitchTip token" in result.message
    assert "sudo chown" in result.details
    assert "PermissionError" in result.details


@pytest.mark.unit
def test_ensure_glitchtip_secrets_dir_creates_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    results = ops._ensure_glitchtip_secrets_dir()

    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    assert len(results) == 1
    assert results[0].ok is True
    assert "Created" in results[0].message
    assert secrets_dir.is_dir()
    assert oct(secrets_dir.stat().st_mode)[-3:] == "700"


@pytest.mark.unit
def test_ensure_glitchtip_secrets_dir_ok_when_already_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True, mode=0o700)

    results = ops._ensure_glitchtip_secrets_dir()

    assert len(results) == 1
    assert results[0].ok is True
    assert "writable" in results[0].message


@pytest.mark.unit
def test_ensure_glitchtip_secrets_dir_reports_actionable_error_when_unwritable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    secrets_dir.chmod(0o500)
    try:
        results = ops._ensure_glitchtip_secrets_dir()
    finally:
        secrets_dir.chmod(0o700)

    assert len(results) == 1
    assert results[0].ok is False
    assert "not writable" in results[0].message
    assert "sudo chown" in results[0].details
    assert str(secrets_dir) in results[0].details


@pytest.mark.unit
def test_glitchtip_secrets_doctor_issues_empty_on_a_fresh_host(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    assert ops._glitchtip_secrets_doctor_issues() == []


@pytest.mark.unit
def test_glitchtip_secrets_doctor_issues_flags_unwritable_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    secrets_dir.chmod(0o500)
    try:
        issues = ops._glitchtip_secrets_doctor_issues()
    finally:
        secrets_dir.chmod(0o700)

    assert len(issues) == 1
    assert "not writable" in issues[0]


@pytest.mark.unit
def test_glitchtip_secrets_doctor_issues_flags_missing_token_when_grafana_running(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})

    issues = ops._glitchtip_secrets_doctor_issues()

    assert len(issues) == 1
    assert "glitchtip-grafana-token" in issues[0]
    assert "nyxgpt ops glitchtip-init" in issues[0]


@pytest.mark.unit
def test_glitchtip_secrets_doctor_issues_ok_when_token_present_and_grafana_running(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("tok", encoding="utf-8")

    assert ops._glitchtip_secrets_doctor_issues() == []


@pytest.mark.unit
def test_ops_doctor_flags_glitchtip_secrets_issues(monkeypatch, capsys, tmp_path):
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir.joinpath("config.ini").write_text(
        "[project]\nname=nyxGPT\n\n[tracing]\nenabled = false\n", encoding="utf-8"
    )
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})

    rc = ops.doctor(MagicMock())

    assert rc == 2
    out = capsys.readouterr().out
    assert "glitchtip-grafana-token" in out


@pytest.mark.unit
def test_restart_grafana_if_running_skips_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: False)
    result = ops._restart_grafana_if_running()
    assert result.ok
    assert "Docker not found" in result.message


@pytest.mark.unit
def test_restart_grafana_if_running_skips_when_not_running(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "exited"})
    result = ops._restart_grafana_if_running()
    assert result.ok
    assert "not running" in result.message


@pytest.mark.unit
def test_restart_grafana_if_running_restarts_when_running(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    monkeypatch.setattr(ops, "_wait_for_grafana_healthy", lambda: True)
    captured_cmd = {}

    def fake_run(cmd, check=False):
        captured_cmd["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)
    result = ops._restart_grafana_if_running()
    assert result.ok
    assert captured_cmd["cmd"][-2:] == ["restart", "grafana"]


@pytest.mark.unit
def test_restart_grafana_if_running_reports_failure(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, check=False: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    result = ops._restart_grafana_if_running()
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_restart_grafana_if_running_fails_when_never_healthy_again(monkeypatch):
    """A restart that leaves Grafana crash-looping (#3538) must surface as a
    failure, not a false "OK, restarted" while the container never actually
    comes back up."""
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "running"})
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=False: SimpleNamespace(returncode=0, stdout="", stderr="")
    )
    monkeypatch.setattr(ops, "_wait_for_grafana_healthy", lambda: False)

    result = ops._restart_grafana_if_running()

    assert result.ok is False
    assert "never became healthy" in result.message


@pytest.mark.unit
def test_patch_ini_value_appends_missing_key():
    text = "[error_tracking]\nenvironment = docker\n"
    patched = ops._patch_ini_value(text, "error_tracking", "dsn", "http://key@localhost:8080/1")
    assert "dsn = http://key@localhost:8080/1\n" in patched
    assert "environment = docker\n" in patched


@pytest.mark.unit
def test_patch_ini_value_appends_missing_section():
    text = "[monitoring]\nenabled = false\n"
    patched = ops._patch_ini_value(text, "error_tracking", "dsn", "http://key@localhost:8080/1")
    parser = ConfigParser()
    parser.read_string(patched)
    assert parser.get("error_tracking", "dsn") == "http://key@localhost:8080/1"
    assert parser.get("monitoring", "enabled") == "false"


@pytest.mark.unit
def test_provision_glitchtip_skips_without_docker(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: False)
    results = ops._provision_glitchtip()
    assert len(results) == 1
    assert results[0].ok
    assert "Docker not found" in results[0].message


@pytest.mark.unit
def test_provision_glitchtip_skips_when_not_healthy(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_wait_for_glitchtip_healthy", lambda: False)
    results = ops._provision_glitchtip()
    assert len(results) == 1
    assert results[0].ok
    assert "not up/healthy" in results[0].message


@pytest.mark.unit
def test_provision_glitchtip_fails_when_config_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_wait_for_glitchtip_healthy", lambda: True)
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    results = ops._provision_glitchtip()

    assert len(results) == 1
    assert not results[0].ok
    assert "Missing config" in results[0].message


@pytest.mark.unit
def test_provision_glitchtip_stops_after_superuser_failure(monkeypatch, tmp_path):
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[error_tracking]\n", encoding="utf-8")

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_wait_for_glitchtip_healthy", lambda: True)
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_superuser", lambda e, p: ops.OpsResult(False, "boom", "details")
    )
    login_calls = []

    def fake_login(*a, **k):
        login_calls.append(True)
        return None, ops.OpsResult(False, "should not be called")

    monkeypatch.setattr(ops, "_glitchtip_login", fake_login)

    results = ops._provision_glitchtip()

    assert not results[-1].ok
    assert login_calls == []


@pytest.mark.unit
def test_provision_glitchtip_stops_after_login_failure(monkeypatch, tmp_path):
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[error_tracking]\n", encoding="utf-8")

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_wait_for_glitchtip_healthy", lambda: True)
    monkeypatch.setattr(ops, "_glitchtip_ensure_superuser", lambda e, p: ops.OpsResult(True, "ok"))
    monkeypatch.setattr(
        ops,
        "_glitchtip_login",
        lambda base_url, e, p: (None, ops.OpsResult(False, "auth failed")),
    )
    token_calls = []

    def fake_token(*a, **k):
        token_calls.append(True)
        return None, ops.OpsResult(False, "should not be called")

    monkeypatch.setattr(ops, "_glitchtip_ensure_api_token", fake_token)

    results = ops._provision_glitchtip()

    assert not results[-1].ok
    assert token_calls == []


@pytest.mark.unit
def test_provision_glitchtip_stops_after_token_failure(monkeypatch, tmp_path):
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[error_tracking]\n", encoding="utf-8")

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_wait_for_glitchtip_healthy", lambda: True)
    monkeypatch.setattr(ops, "_glitchtip_ensure_superuser", lambda e, p: ops.OpsResult(True, "ok"))
    fake_client = MagicMock()
    monkeypatch.setattr(
        ops,
        "_glitchtip_login",
        lambda base_url, e, p: (fake_client, ops.OpsResult(True, "logged in")),
    )
    monkeypatch.setattr(
        ops,
        "_glitchtip_ensure_api_token",
        lambda client, base_url: (None, ops.OpsResult(False, "token failed")),
    )
    org_calls = []

    def fake_org(*a, **k):
        org_calls.append(True)
        return None, ops.OpsResult(False, "should not be called")

    monkeypatch.setattr(ops, "_glitchtip_ensure_organization", fake_org)

    results = ops._provision_glitchtip()

    assert not results[-1].ok
    assert org_calls == []
    fake_client.close.assert_called_once()


@pytest.mark.unit
def test_provision_glitchtip_full_happy_path(monkeypatch, tmp_path):
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[error_tracking]\nenabled = false\ndsn =\n", encoding="utf-8")

    (tmp_path / "docker").mkdir()
    compose_cfg = tmp_path / "docker" / "config.docker.ini"
    compose_cfg.write_text("[error_tracking]\nenabled = false\ndsn =\n", encoding="utf-8")

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", compose_cfg)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_wait_for_glitchtip_healthy", lambda: True)
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_superuser", lambda e, p: ops.OpsResult(True, "created")
    )

    fake_login_client = MagicMock()
    monkeypatch.setattr(
        ops,
        "_glitchtip_login",
        lambda base_url, e, p: (fake_login_client, ops.OpsResult(True, "logged in")),
    )
    monkeypatch.setattr(
        ops,
        "_glitchtip_ensure_api_token",
        lambda client, base_url: ("tok", ops.OpsResult(True, "token")),
    )
    fake_api_client = MagicMock()
    monkeypatch.setattr(ops, "_glitchtip_http_client", lambda base_url, **k: fake_api_client)
    monkeypatch.setattr(
        ops, "_glitchtip_ensure_organization", lambda client: ("nyxgpt", ops.OpsResult(True, "org"))
    )
    monkeypatch.setattr(
        ops,
        "_glitchtip_ensure_team",
        lambda client, org: ("nyxgpt", ops.OpsResult(True, "team")),
    )
    monkeypatch.setattr(
        ops,
        "_glitchtip_ensure_project",
        lambda client, org, team: ("nyxgpt-backend", ops.OpsResult(True, "project")),
    )
    monkeypatch.setattr(
        ops,
        "_glitchtip_ensure_project_key",
        lambda client, org, proj: ("http://key@localhost:8080/1", ops.OpsResult(True, "key")),
    )
    # Grafana isn't actually running in this test -- avoid a real `docker
    # compose ps` subprocess call from `_restart_grafana_if_running`.
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    results = ops._provision_glitchtip()

    assert all(r.ok for r in results)
    fake_login_client.close.assert_called_once()
    fake_api_client.close.assert_called_once()

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("error_tracking", "dsn") == "http://key@localhost:8080/1"
    assert parser.get("error_tracking", "enabled") == "true"

    compose_parser = ConfigParser()
    compose_parser.read(compose_cfg)
    assert compose_parser.get("error_tracking", "dsn") == "http://key@localhost:8080/1"
    assert compose_parser.get("error_tracking", "enabled") == "true"

    # The Infinity datasource's GlitchTip token is minted alongside the DSN
    # (#3411), never hand-pasted.
    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    assert token_path.read_text(encoding="utf-8") == "tok"
    assert oct(token_path.stat().st_mode)[-3:] == "600"


@pytest.mark.unit
def test_provision_glitchtip_persists_generated_password(monkeypatch, tmp_path):
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text("[error_tracking]\n", encoding="utf-8")

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_wait_for_glitchtip_healthy", lambda: True)

    captured_creds = {}

    def fake_superuser(email, password):
        captured_creds["email"] = email
        captured_creds["password"] = password
        return ops.OpsResult(False, "stop here so the test doesn't need the whole HTTP flow")

    monkeypatch.setattr(ops, "_glitchtip_ensure_superuser", fake_superuser)

    ops._provision_glitchtip()

    parser = ConfigParser()
    parser.read(cfg_path)
    assert parser.get("error_tracking", "admin_email") == ops.GLITCHTIP_DEFAULT_ADMIN_EMAIL
    assert parser.get("error_tracking", "admin_password") == captured_creds["password"]
    assert len(captured_creds["password"]) > 10


@pytest.mark.unit
def test_glitchtip_init_cli_entrypoint_returns_zero_on_success(capsys):
    with patch.object(ops, "_provision_glitchtip", return_value=[ops.OpsResult(True, "up")]):
        rc = ops.glitchtip_init(MagicMock())
        assert rc == 0
        assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_glitchtip_init_cli_entrypoint_returns_nonzero_on_failure(capsys):
    with patch.object(
        ops, "_provision_glitchtip", return_value=[ops.OpsResult(False, "down", "boom")]
    ):
        rc = ops.glitchtip_init(MagicMock())
        assert rc == 2
        assert "[FAIL]" in capsys.readouterr().out


# --- Terraform/Kubernetes local deployment wrappers (#3344) ---


class CP:
    """Minimal stand-in for subprocess.CompletedProcess used across the fakes below."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# --- _resolve_locality ---


@pytest.mark.unit
def test_resolve_locality_rejects_cloud(capsys):
    args = SimpleNamespace(local=False, cloud=True)
    assert ops._resolve_locality(args) is None
    assert "not yet implemented" in capsys.readouterr().err


@pytest.mark.unit
def test_resolve_locality_requires_local(capsys):
    args = SimpleNamespace(local=False, cloud=False)
    assert ops._resolve_locality(args) is None
    assert "--local is required" in capsys.readouterr().err


@pytest.mark.unit
def test_resolve_locality_accepts_local():
    args = SimpleNamespace(local=True, cloud=False)
    assert ops._resolve_locality(args) == "local"


# --- _resolve_api_key ---


@pytest.mark.unit
def test_resolve_api_key_prefers_explicit(monkeypatch):
    monkeypatch.setattr(ops.sys.stdin, "isatty", lambda: True)
    assert ops._resolve_api_key("explicit-key") == "explicit-key"


@pytest.mark.unit
def test_resolve_api_key_generates_random_when_not_interactive(monkeypatch):
    monkeypatch.setattr(ops.sys.stdin, "isatty", lambda: False)
    key = ops._resolve_api_key(None)
    assert len(key) == 64
    int(key, 16)  # random hex -- raises ValueError if not


@pytest.mark.unit
def test_resolve_api_key_uses_prompted_value_when_interactive(monkeypatch):
    monkeypatch.setattr(ops.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ops.getpass, "getpass", lambda *_a, **_k: "typed-key")
    assert ops._resolve_api_key(None) == "typed-key"


@pytest.mark.unit
def test_resolve_api_key_falls_back_to_random_on_blank_prompt(monkeypatch):
    monkeypatch.setattr(ops.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(ops.getpass, "getpass", lambda *_a, **_k: "")
    key = ops._resolve_api_key(None)
    assert len(key) == 64


# --- _refuse_port_collision ---


@pytest.mark.unit
def test_refuse_port_collision_none_when_nothing_running(monkeypatch):
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={"api": "none"}, compose={}, conflicts=[]),
    )
    assert ops._refuse_port_collision(["api"]) is None


@pytest.mark.unit
def test_refuse_port_collision_blocks_on_native(monkeypatch):
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={"api": "started"}, compose={}, conflicts=[]),
    )
    result = ops._refuse_port_collision(["api", "web"])
    assert result is not None
    assert result.ok is False
    assert "api" in result.details


@pytest.mark.unit
def test_refuse_port_collision_blocks_on_compose(monkeypatch):
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={"ollama": "running"}, conflicts=[]),
    )
    result = ops._refuse_port_collision(["ollama"])
    assert result is not None
    assert "ollama" in result.details


# --- Terraform: _ensure_terraform_binary ---


@pytest.mark.unit
def test_ensure_terraform_binary_already_installed(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/terraform")
    results = ops._ensure_terraform_binary()
    assert results == [ops.OpsResult(True, "terraform already installed")]


@pytest.mark.unit
def test_ensure_terraform_binary_no_brew(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    results = ops._ensure_terraform_binary()
    assert results[0].ok is False
    assert "Homebrew is unavailable" in results[0].message


@pytest.mark.unit
def test_ensure_terraform_binary_installs_via_hashicorp_tap(monkeypatch):
    which_calls = {"n": 0}

    def fake_which(prog):
        if prog == "brew":
            return "/opt/homebrew/bin/brew"
        if prog == "terraform":
            which_calls["n"] += 1
            return None if which_calls["n"] == 1 else "/opt/homebrew/bin/terraform"
        return None

    run_calls = []

    def fake_run(cmd, check=True, **_k):
        run_calls.append(cmd)
        return CP(returncode=0)

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)

    results = ops._ensure_terraform_binary()
    assert all(r.ok for r in results)
    assert ["brew", "tap", "hashicorp/tap"] in run_calls
    assert ["brew", "install", "hashicorp/tap/terraform"] in run_calls


@pytest.mark.unit
def test_ensure_terraform_binary_tap_failure(monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/opt/homebrew/bin/brew" if prog == "brew" else None
    )
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="tap failed")
    )
    results = ops._ensure_terraform_binary()
    assert results[0].ok is False
    assert "brew tap" in results[0].message


# --- Terraform: _ensure_terraform_tfvars ---


@pytest.mark.unit
def test_ensure_terraform_tfvars_already_exists(monkeypatch, tmp_path):
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir()
    (tf_dir / "terraform.tfvars").write_text("existing", encoding="utf-8")
    monkeypatch.setattr(ops, "TERRAFORM_DIR", tf_dir)
    results = ops._ensure_terraform_tfvars(None)
    assert results[0].ok is True
    assert "already exists" in results[0].message


@pytest.mark.unit
def test_ensure_terraform_tfvars_missing_example(monkeypatch, tmp_path):
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir()
    monkeypatch.setattr(ops, "TERRAFORM_DIR", tf_dir)
    results = ops._ensure_terraform_tfvars(None)
    assert results[0].ok is False
    assert "Missing" in results[0].message


@pytest.mark.unit
def test_ensure_terraform_tfvars_bootstraps_from_example(monkeypatch, tmp_path):
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir()
    (tf_dir / "terraform.tfvars.example").write_text(
        'repo_path    = "/absolute/path/to/nyxGPT"\n'
        'auth_api_key = "REPLACE_WITH_A_REAL_KEY"\n'  # pragma: allowlist secret
        'cors_origins = "http://localhost:3000"\n',
        encoding="utf-8",
    )
    repo_root = tmp_path / "repo"
    monkeypatch.setattr(ops, "TERRAFORM_DIR", tf_dir)
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)

    results = ops._ensure_terraform_tfvars("my-key")
    assert results[0].ok is True
    tfvars = tf_dir / "terraform.tfvars"
    content = tfvars.read_text(encoding="utf-8")
    assert str(repo_root) in content
    assert 'auth_api_key = "my-key"' in content  # pragma: allowlist secret


# --- Terraform: _terraform_init_plan_apply ---


def _fake_terraform_run(*, init_rc=0, plan_rc=0, apply_rc=0):
    def fake_run(cmd, check=True, **_k):
        assert cmd[0] == "terraform"
        assert cmd[1].startswith("-chdir=")
        if cmd[2] == "init":
            return CP(returncode=init_rc, stderr="init failed" if init_rc else "")
        if cmd[2] == "plan":
            return CP(returncode=plan_rc, stderr="plan failed" if plan_rc else "")
        if cmd[2] == "apply":
            return CP(returncode=apply_rc, stderr="apply failed" if apply_rc else "")
        raise AssertionError(f"unexpected terraform subcommand: {cmd}")

    return fake_run


@pytest.mark.unit
def test_terraform_init_plan_apply_all_succeed(monkeypatch):
    monkeypatch.setattr(ops, "_run", _fake_terraform_run())
    results = ops._terraform_init_plan_apply()
    assert [r.ok for r in results] == [True, True, True]


@pytest.mark.unit
def test_terraform_init_plan_apply_stops_on_init_failure(monkeypatch):
    monkeypatch.setattr(ops, "_run", _fake_terraform_run(init_rc=1))
    results = ops._terraform_init_plan_apply()
    assert len(results) == 1
    assert results[0].ok is False
    assert "init failed" in results[0].message


@pytest.mark.unit
def test_terraform_init_plan_apply_stops_on_plan_failure(monkeypatch):
    monkeypatch.setattr(ops, "_run", _fake_terraform_run(plan_rc=1))
    results = ops._terraform_init_plan_apply()
    assert [r.ok for r in results] == [True, False]


@pytest.mark.unit
def test_terraform_init_plan_apply_stops_on_apply_failure(monkeypatch):
    monkeypatch.setattr(ops, "_run", _fake_terraform_run(apply_rc=1))
    results = ops._terraform_init_plan_apply()
    assert [r.ok for r in results] == [True, True, False]


# --- Terraform: state/health ---


@pytest.mark.unit
def test_terraform_stack_state_maps_components(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_docker_container_state",
        lambda name: "running" if name == "nyxgpt-tf-api" else "absent",
    )
    state = ops.terraform_stack_state()
    assert state["api"] == "running"
    assert state["web"] == "absent"


@pytest.mark.unit
def test_terraform_stack_health_reports_outputs(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running"})
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, check=True, **_k: CP(stdout='{"api_url": {"value": "http://localhost:8000"}}'),
    )
    results = ops._terraform_stack_health()
    assert results[0].ok is True
    assert any("api_url" in r.message for r in results)


# --- Terraform: _install_terraform / _down_terraform ---


@pytest.mark.unit
def test_install_terraform_requires_locality(capsys):
    args = SimpleNamespace(local=False, cloud=False, api_key=None)
    assert ops._install_terraform(args) == 2
    assert "--local is required" in capsys.readouterr().err


@pytest.mark.unit
def test_install_terraform_refuses_port_collision(monkeypatch, capsys):
    args = SimpleNamespace(local=True, cloud=False, api_key=None)
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={"api": "started"}, compose={}, conflicts=[]),
    )
    assert ops._install_terraform(args) == 2
    assert "[FAIL]" in capsys.readouterr().out


@pytest.mark.unit
def test_install_terraform_success_runs_all_steps(monkeypatch, capsys):
    args = SimpleNamespace(local=True, cloud=False, api_key="k")
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_ensure_terraform_binary", return_value=ok) as b,
        patch.object(ops, "_ensure_terraform_tfvars", return_value=ok) as t,
        patch.object(ops, "_generate_compose_config", return_value=ok) as c,
        patch.object(ops, "_build_terraform_docker_images", return_value=ok),
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok) as a,
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_start_observability_stack_terraform", return_value=ok) as o,
        patch.object(ops, "_provision_glitchtip", return_value=ok) as g,
        patch.object(ops, "_terraform_stack_health", return_value=ok) as h,
    ):
        rc = ops._install_terraform(args)
    assert rc == 0
    b.assert_called_once()
    t.assert_called_once_with("k")
    # The Terraform path must derive docker/config.docker.ini before apply --
    # main.tf bind-mounts it, same as docker-compose.yml (regression: #3398).
    c.assert_called_once()
    a.assert_called_once()
    # ...and bring observability up on the terraform network + provision GlitchTip.
    o.assert_called_once()
    g.assert_called_once()
    h.assert_called_once()
    assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_install_terraform_stops_pipeline_on_step_failure(monkeypatch):
    args = SimpleNamespace(local=True, cloud=False, api_key=None)
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    with (
        patch.object(ops, "migrate_legacy_volumes", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_ensure_terraform_binary", return_value=[ops.OpsResult(False, "no terraform")]
        ),
        patch.object(ops, "_ensure_terraform_tfvars") as t,
        patch.object(ops, "_terraform_init_plan_apply") as a,
        patch.object(ops, "_terraform_stack_health") as h,
    ):
        rc = ops._install_terraform(args)
    assert rc == 2
    t.assert_not_called()
    a.assert_not_called()
    h.assert_not_called()


@pytest.mark.unit
def test_install_terraform_clears_intentional_stop_markers(monkeypatch, capsys):
    """Terraform install brings all four core components up under
    `nyxgpt-tf-*` -- their intentional-stop markers (from a prior `ops down`)
    must be cleared so self-heal resumes guarding them (#3406)."""
    args = SimpleNamespace(local=True, cloud=False, api_key="k")
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_ensure_terraform_binary", return_value=ok),
        patch.object(ops, "_ensure_terraform_tfvars", return_value=ok),
        patch.object(ops, "_generate_compose_config", return_value=ok),
        patch.object(ops, "_build_terraform_docker_images", return_value=ok),
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_start_observability_stack_terraform", return_value=ok),
        patch.object(ops, "_provision_glitchtip", return_value=ok),
        patch.object(ops, "_terraform_stack_health", return_value=ok),
        patch.object(ops.self_heal, "clear_intentionally_stopped") as clear_stopped,
    ):
        rc = ops._install_terraform(args)
    assert rc == 0
    assert clear_stopped.call_args_list == [
        call("api"),
        call("web"),
        call("ollama"),
        call("cassandra"),
    ]


@pytest.mark.unit
def test_down_terraform_no_binary(monkeypatch, capsys):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    rc = ops._down_terraform(SimpleNamespace())
    assert rc == 2
    assert "nothing to destroy" in capsys.readouterr().out


@pytest.mark.unit
def test_down_terraform_destroy_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/opt/homebrew/bin/terraform")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="Destroy complete")
    )
    rc = ops._down_terraform(SimpleNamespace())
    assert rc == 0


@pytest.mark.unit
def test_down_terraform_destroy_failure(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/opt/homebrew/bin/terraform")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="boom"))
    rc = ops._down_terraform(SimpleNamespace())
    assert rc == 2


@pytest.mark.unit
def test_down_terraform_prunes_docker_build_cache(monkeypatch):
    """`down --terraform` reclaims the BuildKit cache the deploy's image builds
    accumulate (17GB+ across repeated local deploys), AFTER terraform destroy."""
    calls = []

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        if cmd[:3] == ["docker", "builder", "prune"]:
            return CP(returncode=0, stdout="Total reclaimed space: 12.3GB")
        return CP(returncode=0, stdout="ok")

    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._down_terraform_steps()

    assert ["docker", "builder", "prune", "-f"] in calls
    assert any("build cache pruned" in r.message and "12.3GB" in r.message for r in results)
    destroy_i = next(i for i, c in enumerate(calls) if "destroy" in c)
    prune_i = next(i for i, c in enumerate(calls) if c[:3] == ["docker", "builder", "prune"])
    assert destroy_i < prune_i


@pytest.mark.unit
def test_down_terraform_cache_prune_failure_is_non_fatal(monkeypatch):
    """A build-cache prune failure is best-effort and never fails the teardown."""

    def fake_run(cmd, check=True, **_k):
        if cmd[:3] == ["docker", "builder", "prune"]:
            return CP(returncode=1, stderr="prune boom")
        return CP(returncode=0, stdout="ok")

    monkeypatch.setattr(ops, "_which", lambda prog: f"/usr/bin/{prog}")
    monkeypatch.setattr(ops, "_run", fake_run)
    rc = ops._down_terraform(SimpleNamespace())
    assert rc == 0


# --- Kubernetes: _ensure_kubectl_and_cluster ---


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_missing_kubectl(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    results = ops._ensure_kubectl_and_cluster()
    assert results[0].ok is False
    assert "kubectl not found" in results[0].message


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_unreachable(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="refused")
    )
    results = ops._ensure_kubectl_and_cluster()
    assert results[0].ok is False
    assert "No reachable" in results[0].message


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_reachable(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0))
    results = ops._ensure_kubectl_and_cluster()
    assert results[0].ok is True


# --- Kubernetes: _build_and_load_k8s_image ---


@pytest.mark.unit
def test_build_and_load_k8s_image_no_docker(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    results = ops._build_and_load_k8s_image()
    assert results[0].ok is False
    assert "docker not found" in results[0].message


@pytest.mark.unit
def test_build_and_load_k8s_image_build_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)

    def fake_run(cmd, check=True, **_k):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=1)
        if cmd[:2] == ["docker", "build"]:
            return CP(returncode=1, stderr="build boom")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_and_load_k8s_image()
    assert results[0].ok is False
    assert "docker build failed" in results[0].message


@pytest.mark.unit
def test_build_and_load_k8s_image_skips_load_on_docker_desktop(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)

    def fake_run(cmd, check=True, **_k):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=1)
        if cmd[:2] == ["docker", "build"]:
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="docker-desktop")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_and_load_k8s_image()
    assert all(r.ok for r in results)
    assert any("Docker Desktop" in r.message for r in results)


@pytest.mark.unit
def test_build_and_load_k8s_image_loads_into_kind(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/" + prog if prog in ("docker", "kind") else None
    )
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    run_calls = []

    def fake_run(cmd, check=True, **_k):
        run_calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=1)
        if cmd[:2] == ["docker", "build"]:
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="kind-nyxgpt")
        if cmd[:2] == ["kind", "load"]:
            return CP(returncode=0)
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_and_load_k8s_image()
    assert all(r.ok for r in results)
    assert ["kind", "load", "docker-image", ops.K8S_IMAGE, "--name", "nyxgpt"] in run_calls


@pytest.mark.unit
def test_build_and_load_k8s_image_unrecognized_context(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/docker" if prog == "docker" else None
    )
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)

    def fake_run(cmd, check=True, **_k):
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=1)
        if cmd[:2] == ["docker", "build"]:
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="some-other-cluster")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_and_load_k8s_image()
    assert all(r.ok for r in results)
    assert any("Unrecognized cluster context" in r.message for r in results)


@pytest.mark.unit
def test_build_and_load_k8s_image_skips_rebuild_when_source_unchanged(monkeypatch, tmp_path):
    """Repeated `nyxgpt ops install --kubernetes --local` runs against
    unchanged source should skip `docker build` entirely (#3414), mirroring
    the Homebrew reinstall-if-needed behavior from #3406."""
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    fingerprint = ops._hash_paths(ops._API_IMAGE_FINGERPRINT_PATHS)
    (tmp_path / ".nyxgpt-api_local.sha256").write_text(fingerprint, encoding="utf-8")

    run_calls = []

    def fake_run(cmd, check=True, **_k):
        run_calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="docker-desktop")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_and_load_k8s_image()
    assert all(r.ok for r in results)
    assert "skipped rebuild" in results[0].message
    assert not any(c[:2] == ["docker", "build"] for c in run_calls)


# --- Kubernetes: build_and_load_k8s_image() public wrapper kwargs (#3419) ---


@pytest.mark.unit
def test_build_and_load_k8s_image_public_wrapper_forwards_only_given_kwargs(monkeypatch):
    """The web component passes context/fingerprint_paths/excludes/build_args; the api
    component (default call) must keep forwarding zero kwargs (see the docstring and
    test_build_and_load_k8s_image_public_wrapper_uses_given_tag above)."""
    calls = []
    monkeypatch.setattr(
        ops,
        "_build_and_load_k8s_image",
        lambda image, **kwargs: calls.append((image, kwargs)) or [ops.OpsResult(True, "ok")],
    )

    ops.build_and_load_k8s_image("nyxgpt-api:1.2.3-abcd123")
    assert calls[-1] == ("nyxgpt-api:1.2.3-abcd123", {})

    web_context = ops.REPO_ROOT / "web"
    results = ops.build_and_load_k8s_image(
        "nyxgpt-web:1.2.3-abcd123",
        context=web_context,
        fingerprint_paths=[web_context],
        excludes=ops._WEB_VENDOR_EXCLUDES,
        build_args={"NEXT_PUBLIC_API_BASE_URL": "http://localhost:8000"},
    )
    assert calls[-1] == (
        "nyxgpt-web:1.2.3-abcd123",
        {
            "context": web_context,
            "fingerprint_paths": [web_context],
            "excludes": ops._WEB_VENDOR_EXCLUDES,
            "build_args": {"NEXT_PUBLIC_API_BASE_URL": "http://localhost:8000"},
        },
    )
    assert results == [ops.OpsResult(True, "ok")]


# --- Kubernetes: _build_and_load_k8s_web_image (#3419) ---


@pytest.mark.unit
def test_build_and_load_k8s_web_image_builds_web_context_with_build_arg(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/" + prog if prog == "docker" else None
    )
    monkeypatch.setattr(ops, "DOCKER_IMAGE_MARKER_DIR", tmp_path)
    run_calls = []

    def fake_run(cmd, check=True, **_k):
        run_calls.append(cmd)
        if cmd[:3] == ["docker", "image", "inspect"]:
            return CP(returncode=1)
        if cmd[:2] == ["docker", "build"]:
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="docker-desktop")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_and_load_k8s_web_image()

    assert all(r.ok for r in results)
    build_cmd = next(c for c in run_calls if c[:2] == ["docker", "build"])
    assert build_cmd[2:5] == ["-t", ops.TF_WEB_IMAGE, "--build-arg"]
    assert f"NEXT_PUBLIC_API_BASE_URL={ops.TF_WEB_API_BASE_URL_DEFAULT}" in build_cmd
    assert str(ops.REPO_ROOT / "web") in build_cmd


# --- Kubernetes: _ensure_k8s_secret ---


@pytest.mark.unit
def test_ensure_k8s_secret_already_exists(monkeypatch, tmp_path):
    k8s_dir = tmp_path / "k8s"
    k8s_dir.mkdir()
    (k8s_dir / "secret.yaml").write_text("existing", encoding="utf-8")
    monkeypatch.setattr(ops, "K8S_DIR", k8s_dir)
    results = ops._ensure_k8s_secret(None)
    assert results[0].ok is True
    assert "already exists" in results[0].message


@pytest.mark.unit
def test_ensure_k8s_secret_bootstraps_from_example(monkeypatch, tmp_path):
    k8s_dir = tmp_path / "k8s"
    k8s_dir.mkdir()
    (k8s_dir / "secret.example.yaml").write_text(
        'apiVersion: v1\nkind: Secret\nstringData:\n  api-key: "change-me"\n', encoding="utf-8"
    )
    monkeypatch.setattr(ops, "K8S_DIR", k8s_dir)
    results = ops._ensure_k8s_secret("real-key")
    assert results[0].ok is True
    content = (k8s_dir / "secret.yaml").read_text(encoding="utf-8")
    assert 'api-key: "real-key"' in content


# --- Kubernetes: _kubectl_apply_kustomization / _k8s_stack_health ---


@pytest.mark.unit
def test_kubectl_apply_kustomization_success(monkeypatch):
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="applied")
    )
    results = ops._kubectl_apply_kustomization()
    assert results[0].ok is True


@pytest.mark.unit
def test_kubectl_apply_kustomization_failure(monkeypatch):
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="boom"))
    results = ops._kubectl_apply_kustomization()
    assert results[0].ok is False


@pytest.mark.unit
def test_k8s_stack_health_reports_pods_service(monkeypatch):
    def fake_run(cmd, check=True, **_k):
        if cmd[4] == "pods":
            return CP(
                returncode=0,
                stdout="nyxgpt-api-stable-abc=Running;nyxgpt-api-canary-def=Pending;",
            )
        if cmd[4] == "svc":
            return CP(returncode=0, stdout="nyxgpt-api   ClusterIP\n")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._k8s_stack_health()
    pod_results = [r for r in results if r.message.startswith("pod ")]
    assert len(pod_results) == 2
    assert pod_results[0].ok is True
    assert pod_results[1].ok is False
    assert not any("HPA" in r.message for r in results)
    assert any("Service nyxgpt-api found" in r.message for r in results)


@pytest.mark.unit
def test_k8s_stack_health_reports_web_service_alongside_api(monkeypatch):
    """#3419: the post-apply health snapshot must check nyxgpt-web too, distinguishing
    found from not-found per service rather than reporting one combined result."""

    def fake_run(cmd, check=True, **_k):
        if cmd[4] == "pods":
            return CP(returncode=0, stdout="nyxgpt-web-stable-abc=Running;")
        if cmd[4] == "svc":
            svc_name = cmd[5]
            if svc_name == "nyxgpt-api":
                return CP(returncode=0, stdout="nyxgpt-api   ClusterIP\n")
            return CP(returncode=1)
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._k8s_stack_health()
    assert any(r.ok and "Service nyxgpt-api found" in r.message for r in results)
    assert any(not r.ok and "Service nyxgpt-web not found" in r.message for r in results)


# --- Kubernetes: _install_kubernetes / _down_kubernetes ---


@pytest.mark.unit
def test_install_kubernetes_requires_locality(capsys):
    args = SimpleNamespace(local=False, cloud=False, api_key=None)
    assert ops._install_kubernetes(args) == 2
    assert "--local is required" in capsys.readouterr().err


@pytest.mark.unit
def test_install_kubernetes_refuses_port_collision(monkeypatch, capsys):
    args = SimpleNamespace(local=True, cloud=False, api_key=None)
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={"api": "started"}, compose={}, conflicts=[]),
    )
    assert ops._install_kubernetes(args) == 2
    assert "[FAIL]" in capsys.readouterr().out


@pytest.mark.unit
def test_install_kubernetes_success_runs_all_steps(monkeypatch, capsys):
    args = SimpleNamespace(local=True, cloud=False, api_key="k")
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok) as c,
        patch.object(ops, "_build_and_load_k8s_image", return_value=ok) as b,
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok) as bw,
        patch.object(ops, "_ensure_k8s_secret", return_value=ok) as s,
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok) as a,
        patch.object(ops, "_k8s_stack_health", return_value=ok) as h,
    ):
        rc = ops._install_kubernetes(args)
    assert rc == 0
    c.assert_called_once()
    b.assert_called_once()
    bw.assert_called_once()
    s.assert_called_once_with("k")
    a.assert_called_once()
    h.assert_called_once()
    assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_install_kubernetes_clears_intentional_stop_markers_for_api_and_web(monkeypatch, capsys):
    """Kubernetes manages both `api` and `web` (#3419) -- both intentional-stop
    markers must be cleared, and only those two (no manifest for ollama/cassandra)."""
    args = SimpleNamespace(local=True, cloud=False, api_key="k")
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_image", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_k8s_stack_health", return_value=ok),
        patch.object(ops.self_heal, "clear_intentionally_stopped") as clear_stopped,
    ):
        rc = ops._install_kubernetes(args)
    assert rc == 0
    assert clear_stopped.call_args_list == [call("api"), call("web")]


@pytest.mark.unit
def test_install_kubernetes_stops_pipeline_on_step_failure(monkeypatch):
    args = SimpleNamespace(local=True, cloud=False, api_key=None)
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    with (
        patch.object(
            ops,
            "_ensure_kubectl_and_cluster",
            return_value=[ops.OpsResult(False, "no cluster")],
        ),
        patch.object(ops, "_build_and_load_k8s_image") as b,
        patch.object(ops, "_build_and_load_k8s_web_image") as bw,
        patch.object(ops, "_ensure_k8s_secret") as s,
        patch.object(ops, "_kubectl_apply_kustomization") as a,
        patch.object(ops, "_k8s_stack_health") as h,
    ):
        rc = ops._install_kubernetes(args)
    assert rc == 2
    b.assert_not_called()
    bw.assert_not_called()
    s.assert_not_called()
    a.assert_not_called()
    h.assert_not_called()


@pytest.mark.unit
def test_down_kubernetes_no_kubectl(monkeypatch, capsys):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    rc = ops._down_kubernetes(SimpleNamespace())
    assert rc == 2
    assert "nothing to tear down" in capsys.readouterr().out


@pytest.mark.unit
def test_down_kubernetes_delete_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="deleted")
    )
    rc = ops._down_kubernetes(SimpleNamespace())
    assert rc == 0


@pytest.mark.unit
def test_down_kubernetes_delete_failure(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="boom"))
    rc = ops._down_kubernetes(SimpleNamespace())
    assert rc == 2


# --- Structured (non-printing) Terraform/Kubernetes functions for the SRE/admin dashboard API ---


@pytest.mark.unit
def test_install_terraform_local_runs_steps_and_returns_results(monkeypatch):
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_ensure_terraform_binary", return_value=ok),
        patch.object(ops, "_ensure_terraform_tfvars", return_value=ok) as t,
        patch.object(ops, "_generate_compose_config", return_value=ok),
        patch.object(ops, "_build_terraform_docker_images", return_value=ok),
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_start_observability_stack_terraform", return_value=ok),
        patch.object(ops, "_provision_glitchtip", return_value=ok),
        patch.object(ops, "_terraform_stack_health", return_value=ok),
    ):
        results = ops.install_terraform_local(api_key="k")
    assert all(r.ok for r in results)
    t.assert_called_once_with("k")


@pytest.mark.unit
def test_install_terraform_local_reports_port_collision(monkeypatch):
    collision = ops.OpsResult(False, "Refusing to start: port collision")
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: collision)
    results = ops.install_terraform_local()
    assert results == [collision]


@pytest.mark.unit
def test_down_terraform_returns_results_without_printing(monkeypatch, capsys):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/terraform")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="destroyed")
    )
    # Observability teardown runs before the destroy; stub it so this test
    # exercises only the terraform-destroy result shape.
    monkeypatch.setattr(
        ops, "_stop_observability_stack_terraform", lambda: [ops.OpsResult(True, "obs down")]
    )
    results = ops.down_terraform()
    assert all(r.ok for r in results)
    assert any("terraform destroy" in r.message for r in results)
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_down_terraform_tears_down_observability_before_destroy(monkeypatch):
    """`ops down --terraform` must bring the observability stack down before
    `terraform destroy`, or the destroy can't remove the shared network
    (regression: containers left attached time out the network delete)."""
    order: list[str] = []
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/terraform")
    monkeypatch.setattr(
        ops,
        "_stop_observability_stack_terraform",
        lambda: (order.append("obs-down"), [ops.OpsResult(True, "obs down")])[1],
    )

    def fake_run(cmd, check=True, **_k):
        if "destroy" in cmd:
            order.append("destroy")
        return CP(returncode=0, stdout="ok")

    monkeypatch.setattr(ops, "_run", fake_run)
    ops.down_terraform()
    assert order == ["obs-down", "destroy"]


@pytest.mark.unit
def test_install_kubernetes_local_runs_steps_and_returns_results(monkeypatch):
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok) as s,
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_k8s_stack_health", return_value=ok),
    ):
        results = ops.install_kubernetes_local(api_key="k")
    assert all(r.ok for r in results)
    s.assert_called_once_with("k")


@pytest.mark.unit
def test_install_kubernetes_local_reports_port_collision(monkeypatch):
    collision = ops.OpsResult(False, "Refusing to start: port collision")
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: collision)
    results = ops.install_kubernetes_local()
    assert results == [collision]


@pytest.mark.unit
def test_down_kubernetes_returns_results_without_printing(monkeypatch, capsys):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="deleted")
    )
    results = ops.down_kubernetes()
    assert len(results) == 1
    assert results[0].ok is True
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_infra_status_reports_terraform_and_kubernetes(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running", "web": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )

    def fake_which(prog):
        return "/usr/local/bin/x" if prog in ("kubectl", "docker") else None

    def fake_run(cmd, check=True, **_k):
        return CP(returncode=0, stdout="nyxgpt-api-abc   1/1   Running\n")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops.infra_status()
    assert result["mode"] == "terraform"
    assert result["terraform"]["probe_available"] is True
    assert result["terraform"]["deployed"] is True
    assert result["terraform"]["containers"] == {"api": "running", "web": "absent"}
    assert result["kubernetes"]["available"] is True
    assert result["kubernetes"]["configured"] is True
    assert result["kubernetes"]["probe_available"] is True
    assert result["kubernetes"]["deployed"] is True
    assert result["kubernetes"]["namespace"] == "nyxgpt"
    assert result["kubernetes"]["pods"] == ["nyxgpt-api-abc   1/1   Running"]


@pytest.mark.unit
def test_infra_status_reports_nothing_deployed(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: None)

    result = ops.infra_status()
    assert result["mode"] == "none"
    assert result["terraform"]["probe_available"] is False
    assert result["terraform"]["deployed"] is False
    assert result["kubernetes"]["available"] is False
    # kubectl missing entirely means there's no context to read either --
    # folded into "not configured", which is a confident NOT DEPLOYED, not
    # CANNOT DETERMINE (#3468).
    assert result["kubernetes"]["configured"] is False
    assert result["kubernetes"]["probe_available"] is True
    assert result["kubernetes"]["deployed"] is False
    assert result["kubernetes"]["pods"] == []


@pytest.mark.unit
def test_infra_status_kubernetes_no_context_is_not_deployed(monkeypatch):
    """No kubeconfig/current-context configured -- #3468.

    A cluster that was never configured must read as a confidently
    determined NOT DEPLOYED, not CANNOT DETERMINE (reserved for a
    *configured* cluster the probe couldn't reach).
    """
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/kubectl" if prog == "kubectl" else None
    )

    def fake_run(cmd, check=True, **_k):
        if cmd[:2] == ["kubectl", "config"]:
            return CP(returncode=1, stdout="")
        raise AssertionError(f"kubectl should not be probed further: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops.infra_status()
    assert result["kubernetes"]["available"] is True
    assert result["kubernetes"]["configured"] is False
    assert result["kubernetes"]["probe_available"] is True
    assert result["kubernetes"]["deployed"] is False
    assert result["kubernetes"]["pods"] == []


@pytest.mark.unit
def test_infra_status_kubernetes_configured_but_unreachable_is_cannot_determine(monkeypatch):
    """A *configured* context the probe can't reach preserves #3410's protection."""
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/kubectl" if prog == "kubectl" else None
    )

    def fake_run(cmd, check=True, **_k):
        if cmd[:2] == ["kubectl", "config"]:
            return CP(returncode=0, stdout="kind-nyxgpt\n")
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"]:
            return CP(returncode=1, stdout="", stderr="connection refused")
        raise AssertionError(f"unexpected kubectl command: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops.infra_status()
    assert result["kubernetes"]["configured"] is True
    assert result["kubernetes"]["probe_available"] is False
    assert result["kubernetes"]["deployed"] is False
    assert result["kubernetes"]["pods"] == []


@pytest.mark.unit
def test_infra_status_kubernetes_configured_reachable_empty_namespace_is_not_deployed(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/kubectl" if prog == "kubectl" else None
    )

    def fake_run(cmd, check=True, **_k):
        if cmd[:2] == ["kubectl", "config"]:
            return CP(returncode=0, stdout="kind-nyxgpt\n")
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"]:
            return CP(returncode=0, stdout="")
        raise AssertionError(f"unexpected kubectl command: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops.infra_status()
    assert result["kubernetes"]["configured"] is True
    assert result["kubernetes"]["probe_available"] is True
    assert result["kubernetes"]["deployed"] is False
    assert result["kubernetes"]["pods"] == []


@pytest.mark.unit
def test_infra_status_reports_cannot_determine_when_docker_probe_unavailable(monkeypatch):
    """Docker absent from this vantage point must not render as 'not deployed' -- see #3410."""
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent", "web": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stdout=""))

    result = ops.infra_status()
    assert result["terraform"]["probe_available"] is False
    assert result["terraform"]["deployed"] is False


@pytest.mark.unit
def test_infra_status_detects_native_and_compose_modes(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(ops, "_which", lambda prog: None)

    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={"api": "started"}, compose={}, conflicts=[]),
    )
    assert ops.infra_status()["mode"] == "native"

    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={"api": "running"}, conflicts=[]),
    )
    assert ops.infra_status()["mode"] == "compose"


@pytest.mark.unit
def test_infra_status_serving_reports_single_instance_outside_kubernetes(monkeypatch):
    """Native/Compose/Terraform run one instance each -- serving must say so, not defer to canary."""
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/docker" if prog == "docker" else None
    )

    result = ops.infra_status()
    assert result["mode"] == "terraform"
    assert result["serving"] == {
        "supported": False,
        "message": (
            "Single instance serving 100% of traffic -- traffic splitting is a "
            "Kubernetes-mode feature (see the Canary page)."
        ),
    }


@pytest.mark.unit
def test_infra_status_serving_delegates_to_canary_status_in_kubernetes_mode(monkeypatch):
    """In kubernetes mode, serving must surface canary.status()'s weight/health -- see #3410."""
    from nyxgpt import canary

    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )

    def fake_which(prog):
        return "/usr/local/bin/kubectl" if prog == "kubectl" else None

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, check=True, **_k: CP(returncode=0, stdout="nyxgpt-api-abc   1/1   Running\n"),
    )

    fake_statuses = {
        "api": {
            "active": True,
            "weight_percent": 25,
            "stable": {"state": "healthy", "message": "stable healthy", "version": "1.0.0-aaa"},
            "canary": {"state": "healthy", "message": "canary healthy", "version": "1.0.1-bbb"},
        },
        "web": {
            "active": False,
            "weight_percent": 0,
            "stable": {"state": "healthy", "message": "web stable healthy", "version": "1.0.0-aaa"},
            "canary": {"state": "not_deployed", "message": "web canary idle", "version": ""},
        },
    }
    monkeypatch.setattr(canary, "status", lambda component="api": fake_statuses[component])

    result = ops.infra_status()
    assert result["mode"] == "kubernetes"
    # Per-component (#3419): every canary-capable component is broken out.
    assert result["serving"]["components"] == fake_statuses
    # Backward compatible: api's fields are still spread at the top level.
    assert result["serving"] == {
        "supported": True,
        "components": fake_statuses,
        **fake_statuses["api"],
    }


# --- install()/down() dispatch to the Terraform/Kubernetes paths ---


@pytest.mark.unit
def test_install_rejects_terraform_and_kubernetes_together(capsys):
    args = SimpleNamespace(terraform=True, kubernetes=True)
    assert ops.install(args) == 2
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.unit
def test_install_dispatches_to_terraform(monkeypatch):
    args = SimpleNamespace(terraform=True, kubernetes=False)
    with patch.object(ops, "_install_terraform", return_value=0) as it:
        assert ops.install(args) == 0
    it.assert_called_once_with(args)


@pytest.mark.unit
def test_install_dispatches_to_kubernetes(monkeypatch):
    args = SimpleNamespace(terraform=False, kubernetes=True)
    with patch.object(ops, "_install_kubernetes", return_value=0) as ik:
        assert ops.install(args) == 0
    ik.assert_called_once_with(args)


@pytest.mark.unit
def test_down_rejects_terraform_and_kubernetes_together(capsys):
    args = SimpleNamespace(terraform=True, kubernetes=True)
    assert ops.down(args) == 2
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.unit
def test_down_dispatches_to_terraform(monkeypatch):
    args = SimpleNamespace(terraform=True, kubernetes=False)
    with patch.object(ops, "_down_terraform", return_value=0) as dt:
        assert ops.down(args) == 0
    dt.assert_called_once_with(args)


@pytest.mark.unit
def test_down_dispatches_to_kubernetes(monkeypatch):
    args = SimpleNamespace(terraform=False, kubernetes=True)
    with patch.object(ops, "_down_kubernetes", return_value=0) as dk:
        assert ops.down(args) == 0
    dk.assert_called_once_with(args)


# --- status()/doctor() recognize the Terraform/Kubernetes deployment modes ---


@pytest.mark.unit
def test_status_shows_terraform_stack_when_present(monkeypatch, capsys):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(ops, "_run", lambda *a, **k: CP(stdout=""))
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running", "web": "absent"})

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Terraform-managed stack" in out
    assert "terraform api: running" in out


@pytest.mark.unit
def test_status_shows_kubernetes_pods_when_present(monkeypatch, capsys):
    def fake_which(prog):
        return "/usr/local/bin/kubectl" if prog == "kubectl" else None

    def fake_run(cmd, check=True, **_k):
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"] and "pods" in cmd:
            return CP(returncode=0, stdout="nyxgpt-api-stable-abc   1/1   Running\n")
        return CP(stdout="")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    # Isolate from real kubectl/canary probing -- this test only cares about the pod listing.
    monkeypatch.setattr(
        ops, "_serving_status", lambda running_mode: {"supported": False, "message": "n/a"}
    )

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Kubernetes (nyxgpt namespace" in out
    assert "nyxgpt-api-stable-abc" in out


@pytest.mark.unit
def test_status_shows_per_component_canary_when_kubernetes_pods_present(monkeypatch, capsys):
    """`nyxgpt ops status` surfaces every canary-capable component's rollout state (#3419)."""

    def fake_which(prog):
        return "/usr/local/bin/kubectl" if prog == "kubectl" else None

    def fake_run(cmd, check=True, **_k):
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"] and "pods" in cmd:
            return CP(returncode=0, stdout="nyxgpt-api-stable-abc   1/1   Running\n")
        return CP(stdout="")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(
        ops,
        "_serving_status",
        lambda running_mode: {
            "supported": True,
            "components": {
                "api": {
                    "active": True,
                    "weight_percent": 25,
                    "stable": {"state": "healthy", "message": "ok", "version": "1.0.0-aaa"},
                    "canary": {"state": "healthy", "message": "ok", "version": "1.0.1-bbb"},
                },
                "web": {
                    "active": False,
                    "weight_percent": 0,
                    "stable": {"state": "healthy", "message": "ok", "version": "1.0.0-aaa"},
                    "canary": {"state": "not_deployed", "message": "idle", "version": ""},
                },
            },
        },
    )

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Canary (per component" in out
    assert "api: rollout active -- 25%" in out
    assert "web: rollout idle" in out


@pytest.mark.unit
def test_requirement_distribution_name_strips_specifier_and_marker():
    assert (
        ops._requirement_distribution_name("opentelemetry-instrumentation-urllib>=0.45b0")
        == "opentelemetry-instrumentation-urllib"
    )
    assert ops._requirement_distribution_name("httpx>=0.27") == "httpx"
    assert ops._requirement_distribution_name('foo>=1.0; python_version >= "3.11"') == "foo"


@pytest.mark.unit
def test_stale_venv_doctor_issues_empty_when_no_pyproject(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)

    assert ops._stale_venv_doctor_issues() == []


@pytest.mark.unit
def test_stale_venv_doctor_issues_empty_when_all_dependencies_installed(monkeypatch, tmp_path):
    """#3487: doctor must report no issue when every declared dependency
    resolves via importlib.metadata (the common, healthy case)."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["json-fake-dep>=1.0"]\n', encoding="utf-8"
    )
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops.importlib.metadata, "version", lambda name: "1.0")

    assert ops._stale_venv_doctor_issues() == []


@pytest.mark.unit
def test_stale_venv_doctor_issues_flags_missing_declared_dependency(monkeypatch, tmp_path):
    """#3487 repro: a venv that wasn't refreshed after a pull added a new
    dependency (e.g. opentelemetry-instrumentation-urllib) must be flagged
    with the exact fix command, instead of surfacing only as a
    ModuleNotFoundError crash the next time something imports it."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["opentelemetry-instrumentation-urllib>=0.45b0", "httpx>=0.27"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)

    def fake_version(name):
        if name == "httpx":
            return "0.27.0"
        raise ops.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(ops.importlib.metadata, "version", fake_version)

    issues = ops._stale_venv_doctor_issues()

    assert len(issues) == 1
    assert "opentelemetry-instrumentation-urllib" in issues[0]
    assert "pip install -e ." in issues[0]


@pytest.mark.unit
def test_doctor_flags_stale_terraform_state(monkeypatch, tmp_path, capsys):
    """Genuinely stale: tfstate still records resources but no containers are running."""
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir()
    (tf_dir / "terraform.tfstate").write_text(
        json.dumps({"resources": [{"type": "docker_container", "name": "api"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "TERRAFORM_DIR", tf_dir)
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent", "web": "absent"})
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "Terraform state exists but no nyxgpt-tf-* containers are running" in out


@pytest.mark.unit
def test_doctor_does_not_flag_terraform_state_after_clean_destroy(monkeypatch, tmp_path, capsys):
    """terraform destroy leaves terraform.tfstate in place with an empty resources
    list -- that's a clean post-destroy state, not stale state, and must not FAIL.
    Repro from #3439: install --terraform --local, down --terraform, install
    (native), doctor should report PASS with no terraform finding."""
    # Pretend config exists at ~/.nyxGPT/config.ini (as ops.doctor expects)
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "config.ini"
    cfg.write_text("[project]\nname=nyxGPT\n\n[tracing]\nenabled = false\n", encoding="utf-8")

    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir()
    (tf_dir / "terraform.tfstate").write_text(
        json.dumps({"resources": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "TERRAFORM_DIR", tf_dir)
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent", "web": "absent"})
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(MagicMock())
    out = capsys.readouterr().out
    assert "Terraform state exists but no nyxgpt-tf-* containers are running" not in out
    assert rc == 0
    assert "doctor: OK" in out


@pytest.mark.unit
def test_generate_compose_config_derives_from_native(tmp_path, monkeypatch):
    """docker/config.docker.ini is derived from the native config: service
    endpoints are rewritten for the container network, auth is forced on, and
    browser-facing UI URLs plus user settings are preserved verbatim."""
    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    native = home / ".nyxGPT" / "config.ini"
    native.write_text(
        "[nyxgpt]\n"
        "default_model = llama3.1:8b\n"
        "sessions_dir = ~/.nyxGPT/sessions\n"
        "vectorstore_dir = ~/.nyxGPT/vectorstore\n"
        "[logging]\n"
        "dir = ~/.nyxGPT/logs\n"
        "[ollama]\n"
        "base_url = http://127.0.0.1:11434\n"
        "[api]\n"
        "host = 127.0.0.1\n"
        "[auth]\n"
        "enabled = false\n"
        "[rag]\n"
        "cassandra_hosts = 127.0.0.1\n"
        "[tracing]\n"
        "otlp_endpoint = http://127.0.0.1:4318/v1/traces\n"
        "jaeger_ui_url = http://localhost:16686\n",
        encoding="utf-8",
    )
    out = tmp_path / "config.docker.ini"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", out)

    results = ops._generate_compose_config()
    assert all(r.ok for r in results)

    text = out.read_text(encoding="utf-8")
    # service endpoints rewritten for the container network
    assert "base_url = http://ollama:11434" in text
    assert "cassandra_hosts = cassandra" in text
    assert "otlp_endpoint = http://otel-collector:4318/v1/traces" in text
    assert "host = 0.0.0.0" in text
    assert "sessions_dir = /root/.nyxGPT/sessions" in text
    assert "vectorstore_dir = /root/.nyxGPT/vectorstore" in text
    assert "dir = /root/.nyxGPT/logs" in text
    # no duplicated keys from a mis-targeted section rewrite
    assert text.count("sessions_dir =") == 1

    parser = ConfigParser()
    parser.read(out)
    # auth follows the native config (NOT forced on): the --local deploy is
    # loopback-only, so a native `enabled = false` carries over -- forcing it
    # true would reject the web's own keyless requests.
    assert parser.get("auth", "enabled") == "false"
    # preserved: user setting + browser-facing UI URL stays localhost
    assert parser.get("nyxgpt", "default_model") == "llama3.1:8b"
    assert "jaeger_ui_url = http://localhost:16686" in text


@pytest.mark.unit
def test_generate_compose_config_noop_without_native(tmp_path, monkeypatch):
    """Before `nyxgpt wizard` has created the native config, it no-ops cleanly."""
    home = tmp_path / "empty-home"
    home.mkdir()
    out = tmp_path / "config.docker.ini"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", out)

    results = ops._generate_compose_config()

    assert all(r.ok for r in results)
    assert not out.exists()


@pytest.mark.unit
def test_env_sync_generates_compose_config(tmp_path, monkeypatch):
    """`nyxgpt ops env-sync` regenerates docker/config.docker.ini from the
    native config -- the deploy paths that run env-sync get a fresh derived
    container config."""
    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    cfg_path = home / ".nyxGPT" / "config.ini"
    _write_config(cfg_path, api_key="cli-api-key")
    env_path = tmp_path / ".env"
    out = tmp_path / "config.docker.ini"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", out)

    args = MagicMock()
    args.config = str(cfg_path)
    args.env_file = str(env_path)

    assert not out.exists()
    rc = ops.env_sync(args)

    assert rc == 0
    assert out.exists()
    assert "host = 0.0.0.0" in out.read_text(encoding="utf-8")
