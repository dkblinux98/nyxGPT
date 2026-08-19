import contextlib
import hashlib
import json
import logging
import os
import signal
import subprocess
import sys
import tarfile
import time
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


@pytest.fixture(autouse=True)
def _no_required_model_check(monkeypatch):
    """Default `ops._missing_required_models_issue()` to "nothing missing".

    `doctor()` asks the configured Ollama whether it holds the required models
    (#3824). On a developer machine (or a runner that has one from another
    test) that is a live HTTP call whose answer depends on which models happen
    to be in that store -- so a `doctor` test asserting rc == 0 would pass or
    fail based on the host, not the code under test. The check's own behavior
    is covered by tests/unit/test_required_model_bootstrap.py, which calls it
    directly with the model list stubbed.
    """
    monkeypatch.setattr(ops, "_missing_required_models_issue", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _force_macos_native_path(monkeypatch):
    """Pin `platform.system()` to "Darwin" for this file's tests.

    This file predates the Linux/systemd native path (#3508) and its ~90
    `ops.install`/`restart`/`stop`/`status`/`doctor` call sites assume the
    original macOS-only (Homebrew/launchd) code path -- they patch
    `_install_homebrew_api`, `_restart_brew_service`, etc. directly. Without
    this, those tests would silently exercise the Linux dispatch branch
    instead whenever the suite runs on a real Linux host (CI runs on
    ubuntu-latest), since `ops.py` now branches on the real OS. The Linux
    path has its own tests in test_ops_systemd.py.
    """
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")


@pytest.fixture(autouse=True)
def _no_registered_native_services(monkeypatch):
    """Report an empty machine to the install-identity check, for this whole file.

    Pinned to Darwin above, every `doctor` here reaches that check (#3861),
    which reads what the service managers actually have registered. These
    tests stub `_which` truthy for every tool but leave `_run` real, so
    without this the read shells out to a `brew` the Linux runner does not
    have. Empty is the right default: none of them is about services left
    behind by an earlier install -- the ones that are live in
    tests/unit/test_install_identity.py, which sets its own answer.
    """
    monkeypatch.setattr(ops, "_discover_native_services", lambda: [])


@pytest.mark.unit
def test_ops_install_returns_zero_when_all_ok(capsys):
    # Mock internal steps to all succeed
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_install_config", return_value=ok_results),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_sync_packaged_resources", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok_results),
        patch.object(ops, "_reconcile_grafana_provisioning", return_value=ok_results) as obs,
        patch.object(ops, "_provision_glitchtip", return_value=ok_results),
    ):
        rc = ops.install(
            MagicMock(dev=False, skip_observability=False, terraform=False, kubernetes=False)
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "[OK]" in out
        obs.assert_called_once()


@pytest.mark.unit
def test_ops_install_returns_nonzero_when_any_fail(capsys):
    mixed = [ops.OpsResult(True, "ok"), ops.OpsResult(False, "bad", "details")]
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_install_config", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "migrate_legacy_volumes", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops,
            "_reconcile_phantom_compose_app_containers",
            return_value=[ops.OpsResult(True, "ok")],
        ),
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "ok")]),
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
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "sync_env_from_config", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_ensure_glitchtip_secrets_dir", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(
            ops, "_reconcile_grafana_provisioning", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(ops, "_provision_glitchtip", return_value=[ops.OpsResult(True, "ok")]),
    ):
        rc = ops.install(
            MagicMock(dev=False, skip_observability=False, terraform=False, kubernetes=False)
        )
        assert rc == 2
        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "details" in out


@pytest.mark.unit
def test_ops_install_skip_observability_flag_skips_the_step(capsys):
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_install_config", return_value=ok_results),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_sync_packaged_resources", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
        patch.object(ops, "_reconcile_grafana_provisioning") as obs,
    ):
        rc = ops.install(
            MagicMock(dev=False, skip_observability=True, terraform=False, kubernetes=False)
        )
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
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_clear_intentional_stops", side_effect=_record("clear intentional stops")
        ),
        patch.object(ops, "_install_config", side_effect=_record("config")),
        patch.object(ops, "migrate_legacy_volumes", side_effect=_record("migrate volumes")),
        patch.object(
            ops, "_reconcile_phantom_compose_app_containers", side_effect=_record("reconcile")
        ),
        patch.object(
            ops, "_sync_packaged_resources", side_effect=_record("sync packaged ops resources")
        ),
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
        patch.object(ops, "_ensure_required_models", side_effect=_record("required models")),
        patch.object(
            ops, "_cleanup_stale_log_symlinks", side_effect=_record("stale log symlink cleanup")
        ),
        patch.object(ops, "sync_env_from_config", side_effect=_record("env sync")),
    ):
        rc = ops.install(
            MagicMock(dev=False, skip_observability=True, terraform=False, kubernetes=False)
        )
        assert rc == 0

    # Syncing packaged ops resources comes first (everything else assumes the
    # packaged Compose file/templates/scripts are already synced to
    # NYXGPT_HOME -- #3621), then clearing intentional-stop markers, then
    # config (a fresh machine needs config.ini before any other step can act
    # on it), then reconciliation before anything creates.
    assert call_order[0] == "sync packaged ops resources"
    assert call_order[1] == "clear intentional stops"
    assert call_order[2] == "config"
    assert call_order[3] == "migrate volumes"
    assert call_order[4] == "reconcile"
    assert "cassandra container" in call_order
    assert "ollama service" in call_order
    assert "env sync" in call_order
    # The model pull targets the server the ollama step just started, so it
    # cannot run before it (#3824).
    assert call_order.index("required models") > call_order.index("ollama service")


@pytest.mark.unit
def test_ops_install_clears_intentional_stop_markers_for_core_components():
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_install_config", return_value=ok_results),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_sync_packaged_resources", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
        patch.object(ops.self_heal, "clear_intentionally_stopped") as clear_stopped,
    ):
        rc = ops.install(
            MagicMock(dev=False, skip_observability=True, terraform=False, kubernetes=False)
        )
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
def test_detect_deployment_mode_flags_terraform_conflict(monkeypatch):
    """#3565 round 5 acceptance failure: after an `ops down` (no `--terraform`)
    followed by `ops install`, the owner had a full native stack AND a full
    Terraform stack running simultaneously, but `nyxgpt ops status` logged
    `conflicts=[]` -- the pre-fix `conflicts` field only ever compared native
    vs. Compose, with no way to represent a native-vs-Terraform collision."""
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-web": "stopped", "ollama": "stopped"},
    )

    def fake_docker_state(name):
        if name == "nyxgpt-cassandra":
            return "exited"  # native cassandra lost its port to the tf one
        if name == ops.TERRAFORM_CONTAINERS["api"]:
            return "running"
        return "absent"

    monkeypatch.setattr(ops, "_docker_container_state", fake_docker_state)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    mode = ops.detect_deployment_mode()
    assert mode.terraform["api"] == "running"
    assert mode.conflicts == []  # native vs. Compose only -- unaffected
    assert mode.terraform_conflicts == ["api"]


@pytest.mark.unit
def test_detect_deployment_mode_no_terraform_conflict_when_terraform_absent(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-web": "stopped", "ollama": "stopped"},
    )
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    mode = ops.detect_deployment_mode()
    assert mode.terraform_conflicts == []


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
def test_ops_doctor_names_the_line_of_an_unparseable_config(monkeypatch, capsys, tmp_path):
    """A user whose config.ini is damaged needs the line, not "Failed to parse" (#3944).

    This is the recovery path: config.ini being unreadable takes the whole
    API down, so `nyxgpt ops doctor` is the only surface left that can say
    what is wrong with it. Reporting it as a warning in a log the user never
    sees -- which is what it did -- is not a recovery path.
    """
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.ini").write_text(
        "[monitoring]\nSLACK_BOT_TOKEN = a\nslack_bot_token = b\n", encoding="utf-8"
    )

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(MagicMock())

    assert rc == 2
    out = capsys.readouterr().out
    assert "doctor: FAIL" in out
    assert "Cannot parse" in out
    assert "DuplicateOptionError" in out
    assert "line 3" in out


@pytest.mark.unit
def test_ops_doctor_shows_the_offending_line_text(monkeypatch, capsys, tmp_path):
    """Doctor is the one caller that opts into quoting the file (#3944 review).

    The API's rendering of the same fault is redacted, because
    `config_unreadable_guard` answers before auth can be enforced and the
    offending line can be a credential. Doctor has neither problem: it is a
    local command run by the owner of the file, and it is where the recovery
    documentation sends a user whose API is down. If this ever redacts too,
    the recovery path loses the only thing that makes it actionable.
    """
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.ini").write_text(
        "api_key = sk-live-VERYSECRETVALUE\n[auth]\nenabled = true\n", encoding="utf-8"
    )

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.doctor(MagicMock())

    assert rc == 2
    out = capsys.readouterr().out
    assert "MissingSectionHeaderError" in out
    assert "line 1" in out
    assert "sk-live-VERYSECRETVALUE" in out


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


# --- doctor: prometheus -> native API scrape health (#3721) ---


def _write_monitoring_config(cfg_path: Path, enabled: bool = True) -> None:
    cfg_path.write_text(
        "[monitoring]\n"
        f"enabled = {'true' if enabled else 'false'}\n"
        "prometheus_ui_url = http://localhost:9090\n",
        encoding="utf-8",
    )


def _patch_prometheus_targets(monkeypatch, targets, *, raises: Exception | None = None):
    def fake_get(url, params=None, timeout=None):
        if raises is not None:
            raise raises
        assert url == "http://localhost:9090/api/v1/targets"
        assert params == {"state": "active"}
        return httpx.Response(
            200,
            json={"status": "success", "data": {"activeTargets": targets}},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(ops.httpx, "get", fake_get)


@pytest.mark.unit
def test_prometheus_api_scrape_issue_none_when_no_config(tmp_path):
    assert ops._prometheus_api_scrape_issue(tmp_path / "missing.ini") is None


@pytest.mark.unit
def test_prometheus_api_scrape_issue_none_when_monitoring_disabled(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_monitoring_config(cfg_path, enabled=False)
    monkeypatch.setattr(
        ops.httpx, "get", lambda *a, **k: pytest.fail("must not query a disabled stack")
    )

    assert ops._prometheus_api_scrape_issue(cfg_path) is None


@pytest.mark.unit
def test_prometheus_api_scrape_issue_none_when_target_is_up(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_monitoring_config(cfg_path)
    _patch_prometheus_targets(
        monkeypatch, [{"labels": {"job": "nyxgpt-api"}, "health": "up", "lastError": ""}]
    )

    assert ops._prometheus_api_scrape_issue(cfg_path) is None


@pytest.mark.unit
def test_prometheus_api_scrape_issue_flags_a_down_target(tmp_path, monkeypatch):
    """The #3721 symptom: prometheus itself is perfectly healthy, so nothing else
    in doctor notices that every dashboard is rendering empty."""
    cfg_path = tmp_path / "config.ini"
    _write_monitoring_config(cfg_path)
    _patch_prometheus_targets(
        monkeypatch,
        [
            {
                "labels": {"job": "nyxgpt-api"},
                "health": "down",
                "lastError": "dial tcp 172.17.0.1:8000: connect: connection refused",
            }
        ],
    )
    monkeypatch.setattr(ops, "_is_linux", lambda: True)

    issue = ops._prometheus_api_scrape_issue(cfg_path)

    assert issue is not None
    assert "connection refused" in issue
    assert "host-api-relay" in issue


@pytest.mark.unit
def test_prometheus_api_scrape_issue_omits_the_linux_hint_elsewhere(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_monitoring_config(cfg_path)
    _patch_prometheus_targets(
        monkeypatch, [{"labels": {"job": "nyxgpt-api"}, "health": "down", "lastError": "boom"}]
    )
    monkeypatch.setattr(ops, "_is_linux", lambda: False)

    issue = ops._prometheus_api_scrape_issue(cfg_path)

    assert issue is not None
    assert "host-api-relay" not in issue


@pytest.mark.unit
def test_prometheus_api_scrape_issue_ignores_other_jobs(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_monitoring_config(cfg_path)
    _patch_prometheus_targets(
        monkeypatch, [{"labels": {"job": "prometheus"}, "health": "down", "lastError": "boom"}]
    )

    assert ops._prometheus_api_scrape_issue(cfg_path) is None


@pytest.mark.unit
def test_prometheus_api_scrape_issue_silent_when_prometheus_is_unreachable(tmp_path, monkeypatch):
    """A stack that isn't up yet is the existing "stack is down" story -- this
    check must not add a second, confusing issue line on top of it."""
    cfg_path = tmp_path / "config.ini"
    _write_monitoring_config(cfg_path)
    _patch_prometheus_targets(monkeypatch, [], raises=httpx.ConnectError("no route"))

    assert ops._prometheus_api_scrape_issue(cfg_path) is None


# --- #3509: the pre-#3721 `[api] host` workaround left in place ---


def _write_bind_posture_config(
    cfg_path: Path, api_host: str = "0.0.0.0", monitoring_enabled: bool = True
) -> None:
    cfg_path.write_text(
        f"[api]\nhost = {api_host}\nport = 8000\n"
        "\n[monitoring]\n"
        f"enabled = {'true' if monitoring_enabled else 'false'}\n"
        "prometheus_ui_url = http://localhost:9090\n",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_insecure_api_bind_issue_flags_the_leftover_workaround(tmp_path, monkeypatch):
    """The #3509 acceptance failure's residue: the reporter widened `[api] host`
    to 0.0.0.0 to make Grafana work before the relay existed. The scrape is now
    *up*, so _prometheus_api_scrape_issue stays quiet and nothing else notices
    the API is still published on every interface."""
    cfg_path = tmp_path / "config.ini"
    _write_bind_posture_config(cfg_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    issue = ops._insecure_api_bind_issue(cfg_path)

    assert issue is not None
    assert "0.0.0.0" in issue
    # Must name the way back, not just the problem.
    assert "127.0.0.1" in issue
    assert "nyxgpt ops observability" in issue


@pytest.mark.unit
def test_insecure_api_bind_issue_silent_on_a_loopback_bind(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_bind_posture_config(cfg_path, api_host="127.0.0.1")
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    assert ops._insecure_api_bind_issue(cfg_path) is None


@pytest.mark.unit
def test_insecure_api_bind_issue_silent_off_linux(tmp_path, monkeypatch):
    """Docker Desktop never had the container->host-loopback gap, so a widened
    bind there is not attributable to this workaround."""
    cfg_path = tmp_path / "config.ini"
    _write_bind_posture_config(cfg_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(
        ops,
        "_docker_bridge_gateway_ip",
        lambda: pytest.fail("must not probe docker on a non-Linux host"),
    )

    assert ops._insecure_api_bind_issue(cfg_path) is None


@pytest.mark.unit
def test_insecure_api_bind_issue_silent_when_monitoring_is_disabled(tmp_path, monkeypatch):
    """Without observability in use the widening can't be blamed on the scrape
    gap -- doctor must not nag a deliberate, auth-gated LAN bind."""
    cfg_path = tmp_path / "config.ini"
    _write_bind_posture_config(cfg_path, monitoring_enabled=False)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(
        ops,
        "_docker_bridge_gateway_ip",
        lambda: pytest.fail("must not probe docker when monitoring is off"),
    )

    assert ops._insecure_api_bind_issue(cfg_path) is None


@pytest.mark.unit
def test_insecure_api_bind_issue_silent_when_the_relay_could_not_work(tmp_path, monkeypatch):
    """No resolvable bridge gateway means reverting would trade a bind-posture
    finding for genuinely empty dashboards. Advising it would be wrong."""
    cfg_path = tmp_path / "config.ini"
    _write_bind_posture_config(cfg_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: None)

    assert ops._insecure_api_bind_issue(cfg_path) is None


@pytest.mark.unit
def test_insecure_api_bind_issue_silent_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    assert ops._insecure_api_bind_issue(tmp_path / "missing.ini") is None


@pytest.mark.unit
def test_sync_host_relay_env_explains_how_to_revert_a_widened_bind(tmp_path, monkeypatch):
    """`Host API relay disabled (... already listens beyond loopback)` read as an
    approval, so every reconcile silently re-affirmed the insecure posture."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[api]\nhost = 0.0.0.0\nport = 8000\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("NYXGPT_HOST_RELAY_PROFILE=monitoring\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    result = ops._sync_host_relay_env(cfg_path=cfg_path, env_path=env_path)

    assert result.ok is True
    assert "disabled" in result.message
    assert "127.0.0.1" in result.details


@pytest.mark.unit
def test_sync_host_relay_env_stays_quiet_when_disabled_for_other_reasons(tmp_path, monkeypatch):
    """A loopback-bound macOS host isn't carrying the workaround -- attaching the
    revert advice there would be noise pointing at a setting already correct."""
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[api]\nhost = 127.0.0.1\nport = 8000\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("NYXGPT_HOST_RELAY_PROFILE=disabled\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_is_linux", lambda: False)

    result = ops._sync_host_relay_env(cfg_path=cfg_path, env_path=env_path)

    assert result.ok is True
    assert "disabled" in result.message
    assert result.details == ""


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


@pytest.mark.unit
def test_loki_recent_volume_by_logger_http_error_includes_body_in_log(
    monkeypatch, tmp_path, caplog
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "grafana-doctor-token").write_text("a-token")

    class _FakeResponse:
        status_code = 502
        text = "bad gateway: loki datasource proxy unreachable"

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

    with caplog.at_level("WARNING", logger="nyxgpt.ops"):
        volumes, issue = ops._loki_recent_volume_by_logger("http://localhost:3001")

    assert volumes is None
    assert issue is None
    records = [r for r in caplog.records if "Failed to query Loki log volumes" in r.getMessage()]
    assert len(records) == 1
    assert "HTTP 502" in records[0].getMessage()
    assert "bad gateway: loki datasource proxy unreachable" in records[0].getMessage()


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
    # No *secret* line is written -- but the Compose ollama service still has
    # to be told which models to pre-pull, and those are not secrets (#3824).
    assert "NYXGPT_AUTH_API_KEY" not in env_path.read_text(encoding="utf-8")
    assert "NYXGPT_DEFAULT_MODEL=" in env_path.read_text(encoding="utf-8")


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
    # The FAIL is about secrets only: the model vars were written anyway, so
    # the detail has to say so rather than leave the operator thinking `.env`
    # was left untouched (#3824).
    assert "NYXGPT_DEFAULT_MODEL" in results[0].details
    assert "NYXGPT_DEFAULT_MODEL=" in env_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_sync_env_from_config_creates_env_from_example(tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="real-api-key", grafana_password="real-grafana-pw")

    example_path = tmp_path / ".env.example"
    example_path.write_text(
        "NYXGPT_API_PORT=8000\n"
        "NYXGPT_AUTH_API_KEY=change-me\n"
        "GRAFANA_ADMIN_PASSWORD=change-me\n",
        encoding="utf-8",
    )

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
def test_sync_env_from_config_syncs_only_the_secret_that_is_set(tmp_path):
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
    monkeypatch.setattr(ops, "_sync_packaged_resources", lambda: [ops.OpsResult(True, "synced")])

    args = MagicMock()
    args.config = str(cfg_path)
    args.env_file = str(env_path)

    rc = ops.env_sync(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert env_path.exists()


@pytest.mark.unit
def test_env_sync_cli_wrapper_seeds_env_from_packaged_example_without_prior_install(
    tmp_path, capsys, monkeypatch
):
    # Regression test (#3621): `nyxgpt ops env-sync` is documented (docs/ops.md,
    # _sync_grafana_slack_webhook_secret's docstring) as runnable as the very
    # first command -- e.g. the Compose-only Quickstart's `nyxgpt wizard` then
    # `nyxgpt ops env-sync`, with no `nyxgpt ops install` beforehand. That means
    # NYXGPT_HOME/.env.example doesn't exist yet unless env_sync() syncs the
    # packaged resources itself; without that, sync_env_from_config()'s
    # .env.example fallback silently finds nothing and .env ends up containing
    # only the secret lines, dropping every non-secret default (ports, image
    # tags, etc.).
    src_root = tmp_path / "packaged"
    (src_root / "docker").mkdir(parents=True)
    (src_root / "ops").mkdir(parents=True)
    (src_root / "scripts").mkdir(parents=True)
    (src_root / "k8s").mkdir(parents=True)
    (src_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (src_root / ".env.example").write_text(
        "NYXGPT_API_PORT=8000\nNYXGPT_AUTH_API_KEY=change-me\n", encoding="utf-8"
    )

    home = tmp_path / "home"
    nyxgpt_home = home / ".nyxGPT"
    monkeypatch.setattr(ops, "_packaged_resources_root", lambda: src_root)
    monkeypatch.setattr(ops, "NYXGPT_HOME", nyxgpt_home)
    monkeypatch.setattr(ops, "OPS_COMPOSE_FILE", nyxgpt_home / "docker-compose.yml")
    monkeypatch.setattr(ops, "OPS_DOCKER_DIR", nyxgpt_home / "docker")
    monkeypatch.setattr(ops, "OPS_SCRIPTS_SRC_DIR", nyxgpt_home / "scripts")
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", nyxgpt_home / "docker" / "config.docker.ini")

    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="fresh-api-key")

    args = MagicMock()
    args.config = str(cfg_path)
    args.env_file = None

    assert not nyxgpt_home.exists()
    rc = ops.env_sync(args)
    assert rc == 0

    env_path = nyxgpt_home / ".env"
    assert env_path.exists()
    content = env_path.read_text(encoding="utf-8")
    assert "NYXGPT_AUTH_API_KEY=fresh-api-key" in content
    # The non-secret default from the packaged .env.example must survive --
    # this is the line that silently vanished before env_sync() synced the
    # packaged resources first.
    assert "NYXGPT_API_PORT=8000" in content


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
        run.assert_called_once_with(
            ["echo", "hi"],
            check=True,
            text=True,
            capture_output=True,
            input=None,
            env=None,
            # Bounded by default since #3858 -- a call that can be reached from
            # an HTTP handler must not be able to block a thread forever.
            timeout=ops.DEFAULT_RUN_TIMEOUT_SECONDS,
        )
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


# --- #3783: a failed subprocess's own output must reach the log message ---


@pytest.mark.unit
def test_run_includes_subprocess_stderr_in_failure_message(caplog):
    # #3783: the rc9 cloud round lost pip's "requires a different Python"
    # refusal because ops logged only the exit code and the argv.
    pip_error = "ERROR: Package 'nyxgpt-api' requires a different Python: " "3.9.16 not in '>=3.11'"
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["pip", "install", "nyxgpt-api.tar.gz"],
            returncode=1,
            stdout="Processing nyxgpt-api.tar.gz\n",
            stderr=pip_error + "\n",
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            ops._run(["pip", "install", "nyxgpt-api.tar.gz"], check=False)

    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit"
    message = records[0].getMessage()
    assert pip_error in message, "the diagnostic stderr must be in the message, not only in extra"
    assert "Processing nyxgpt-api.tar.gz" in message, "stdout belongs in the excerpt too"
    assert pip_error in records[0].output_excerpt


@pytest.mark.unit
def test_run_failure_message_bounds_long_output_with_head_tail_marker(caplog):
    # Bounded, not unbounded: head + tail + an explicit elision marker, and
    # never zero -- the stderr that says why is the last thing emitted.
    stdout = "\n".join(f"progress line {i}" for i in range(500))
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["npm", "ci"], returncode=1, stdout=stdout, stderr="EBADENGINE unsupported\n"
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            ops._run(["npm", "ci"], check=False)

    message = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()][
        0
    ].getMessage()
    assert "progress line 0" in message, "head of the output is kept"
    assert "progress line 499" in message, "tail of the output is kept"
    assert "progress line 250" not in message, "the middle is elided"
    assert "lines omitted" in message, "the elision is marked, never silent"
    assert "EBADENGINE unsupported" in message, "stderr is never crowded out by stdout volume"
    assert message.count("\n") < 60, "the excerpt stays bounded"


@pytest.mark.unit
def test_bounded_output_keeps_short_output_verbatim():
    assert ops._bounded_output("only line") == "only line"
    assert ops._bounded_output("a\nb\nc") == "a\nb\nc"
    assert ops._bounded_output(None) == ""
    assert ops._bounded_output("   \n  ") == ""


@pytest.mark.unit
def test_bounded_output_clips_a_single_pathological_line():
    excerpt = ops._bounded_output("x" * 5000)
    assert len(excerpt) < 5000
    assert excerpt.endswith("... [line truncated]")


@pytest.mark.unit
def test_output_excerpt_combines_both_streams():
    cp = subprocess.CompletedProcess(args=["x"], returncode=1, stdout="out\n", stderr="err\n")
    assert ops._output_excerpt(cp) == "out\nerr"
    assert (
        ops._output_excerpt(
            subprocess.CompletedProcess(args=["x"], returncode=1, stdout="", stderr="err\n")
        )
        == "err"
    )


@pytest.mark.unit
def test_emit_results_inlines_failure_details_into_the_warning_message(caplog):
    # #3783: "ops: install failed: Failed to pip install nyxgpt-api" named the
    # step and dropped the reason, which sat unread in the structured extra.
    pip_error = "ERROR: Package requires a different Python: 3.9.16 not in '>=3.11'"
    results = [ops.OpsResult(False, "Failed to pip install nyxgpt-api", pip_error)]

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        ops._emit_results("install", results)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert pip_error in warnings[0].getMessage()


@pytest.mark.unit
def test_emit_results_bounds_a_huge_failure_detail(caplog):
    results = [
        ops.OpsResult(
            False, "Failed to install web deps", "\n".join(f"line {i}" for i in range(400))
        )
    ]

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        ops._emit_results("install", results)

    message = [r for r in caplog.records if r.levelname == "WARNING"][0].getMessage()
    assert "line 0" in message and "line 399" in message
    assert "lines omitted" in message
    assert "line 200" not in message


@pytest.mark.unit
def test_run_steps_step_raising_calledprocesserror_reports_the_output(capsys, caplog):
    # str(CalledProcessError) is "Command ... returned non-zero exit status 1."
    # -- on its own it says nothing about what went wrong (#3783).
    def _boom():
        raise subprocess.CalledProcessError(
            1, ["pip", "install", "x"], output="Processing x\n", stderr="ERROR: no such package\n"
        )

    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        results, _slow = ops._run_steps("install", [("pip step", _boom)], quiet=True)

    assert len(results) == 1 and results[0].ok is False
    assert "ERROR: no such package" in (results[0].details or "")
    assert "ERROR: no such package" in capsys.readouterr().out


@pytest.mark.unit
def test_emit_results_leaves_a_successful_result_message_alone(caplog):
    with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
        ops._emit_results("install", [ops.OpsResult(True, "installed", "/some/path")])

    info = [r for r in caplog.records if r.levelname == "INFO"][0]
    assert info.getMessage() == "ops: install ok: installed"


@pytest.mark.unit
def test_run_expected_returncode_message_stays_clean(caplog):
    # The #3574 "expected exit" wording is a success path -- it must not grow
    # a subprocess-output block.
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["manage", "createsuperuser"], returncode=1, stdout="", stderr="already exists\n"
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            ops._run(["manage", "createsuperuser"], check=False, expected_returncodes={1})

    records = [r for r in caplog.records if r.levelname == "INFO"]
    assert records
    assert "--- subprocess output ---" not in records[0].getMessage()


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
def test_redact_cmd_masks_secret_flag_values():
    # A secret passed as the value after a secret-named flag is masked.
    assert ops._redact_cmd(["kubectl", "--api-key", "s3cr3t", "get"]) == [
        "kubectl",
        "--api-key",
        "***",
        "get",
    ]
    # Inline --flag=value form is masked too.
    assert ops._redact_cmd(["nyxgpt", "--glitchtip-dsn=https://abc@host/1"]) == [
        "nyxgpt",
        "--glitchtip-dsn=***",
    ]
    # Env-style NAME=value with a secret-looking name (docker `-e` forwarding)
    # is masked even without a leading dash (CodeQL #105/#106 regression).
    assert ops._redact_cmd(["docker", "exec", "-e", "DJANGO_SUPERUSER_PASSWORD=pw"]) == [
        "docker",
        "exec",
        "-e",
        "DJANGO_SUPERUSER_PASSWORD=***",
    ]
    # Non-secret arguments pass through untouched.
    assert ops._redact_cmd(["docker", "compose", "up", "-d"]) == [
        "docker",
        "compose",
        "up",
        "-d",
    ]


@pytest.mark.unit
def test_run_redacts_secret_cmd_values_on_nonzero_exit(caplog):
    # CodeQL py/clear-text-logging-sensitive-data: an api-key/password/DSN on
    # the argv must never reach the log message or the structured `cmd` field.
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["tool", "--password", "hunter2"], returncode=1, stdout="", stderr="boom\n"
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            ops._run(["tool", "--password", "hunter2"], check=False)

    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the non-zero exit"
    record = records[0]
    assert "hunter2" not in record.getMessage()
    assert "***" in record.getMessage()
    assert "hunter2" not in record.cmd
    assert record.cmd == ["tool", "--password", "***"]


@pytest.mark.unit
def test_run_expected_returncodes_logs_info_not_warning_with_expected_wording(caplog):
    # #3574: a declared-expected exit code (e.g. createsuperuser --noinput's
    # rc=1 when the account already exists) must never read as scary WARNING.
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["false"], returncode=1, stdout="", stderr="boom\n"
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            cp = ops._run(
                ["false"],
                check=False,
                expected_returncodes={1},
                expected_message="superuser already exists -- expected rc=1, treated as success",
            )

    assert cp.returncode == 1
    assert not any(r.levelno == logging.WARNING for r in caplog.records)
    records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert records, "Expected _run to log the declared-expected exit at INFO"
    assert "expected" in records[0].getMessage().lower()
    assert "superuser already exists" in records[0].getMessage()


@pytest.mark.unit
def test_run_expected_returncodes_mismatch_still_logs_warning(caplog):
    # A genuinely unexpected exit code (not in expected_returncodes) must
    # still log WARNING exactly as today, even when the caller declared some
    # other code as expected.
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["false"], returncode=2, stdout="", stderr="boom\n"
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            cp = ops._run(
                ["false"],
                check=False,
                expected_returncodes={1},
                expected_message="should not be used",
            )

    assert cp.returncode == 2
    records = [r for r in caplog.records if "Subprocess exited non-zero" in r.getMessage()]
    assert records, "Expected _run to log the unexpected non-zero exit"
    assert records[0].levelno == logging.WARNING
    assert not any(r.levelno == logging.INFO for r in caplog.records)


@pytest.mark.unit
def test_glitchtip_ensure_superuser_already_exists_logs_info_not_warning(caplog):
    # #3574 acceptance: a full down+install on an already-provisioned stack
    # must emit zero WARNING lines for the idempotent createsuperuser step.
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="",
            stderr="Error: That email address is already taken.",
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            result = ops._glitchtip_ensure_superuser("admin@nyxgpt.local", "pw")

    assert result.ok
    assert not any(r.levelno == logging.WARNING for r in caplog.records)
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert info_records, "Expected the expected-rc=1 exit to log at INFO"
    assert "expected" in info_records[0].getMessage().lower()


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
def test_find_launchagent_template_returns_the_synced_candidate(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "OPS_LAUNCHAGENTS_DIR", home / ".nyxGPT" / "ops" / "launchagents")
    target = ops.OPS_LAUNCHAGENTS_DIR / "com.nyxgpt.cassandra-logs.plist"
    target.parent.mkdir(parents=True)
    target.write_text("<plist/>", encoding="utf-8")

    tpl, candidates = ops._find_launchagent_template()
    assert tpl == target
    assert candidates == [target]


@pytest.mark.unit
def test_find_launchagent_template_returns_none_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "OPS_LAUNCHAGENTS_DIR", tmp_path / "nowhere")
    tpl, candidates = ops._find_launchagent_template()
    assert tpl is None
    assert len(candidates) == 1


@pytest.mark.unit
def test_find_launchagent_template_skips_candidate_that_errors(monkeypatch, tmp_path):
    launchagents_dir = tmp_path / ".nyxGPT" / "ops" / "launchagents"
    monkeypatch.setattr(ops, "OPS_LAUNCHAGENTS_DIR", launchagents_dir)
    bad_path = launchagents_dir / "com.nyxgpt.cassandra-logs.plist"
    real_exists = Path.exists

    def flaky_exists(self):
        if self == bad_path:
            raise OSError("permission denied")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", flaky_exists)
    tpl, candidates = ops._find_launchagent_template()
    assert tpl is None
    assert len(candidates) == 1


@pytest.mark.unit
def test_find_launchagent_template_accepts_a_different_plist_name(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops, "OPS_LAUNCHAGENTS_DIR", home / ".nyxGPT" / "ops" / "launchagents")
    target = ops.OPS_LAUNCHAGENTS_DIR / "com.nyxgpt.ollama-logs.plist"
    target.parent.mkdir(parents=True)
    target.write_text("<plist/>", encoding="utf-8")

    tpl, candidates = ops._find_launchagent_template("com.nyxgpt.ollama-logs.plist")
    assert tpl == target
    assert candidates == [target]


# --- _sync_packaged_resources ---


@pytest.mark.unit
def test_sync_packaged_resources_copies_compose_env_docker_ops_scripts(monkeypatch, tmp_path):
    src_root = tmp_path / "packaged"
    (src_root / "docker" / "grafana").mkdir(parents=True)
    (src_root / "docker" / "grafana" / "x.yml").write_text("x", encoding="utf-8")
    (src_root / "ops" / "launchagents").mkdir(parents=True)
    (src_root / "ops" / "launchagents" / "com.nyxgpt.cassandra-logs.plist").write_text(
        "<plist/>", encoding="utf-8"
    )
    (src_root / "scripts").mkdir(parents=True)
    (src_root / "scripts" / "run-web.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    # The Kubernetes manifests are packaged resources too (#3834) -- without
    # this sync a machine with no checkout has nothing to `kubectl apply -k`.
    (src_root / "k8s").mkdir(parents=True)
    (src_root / "k8s" / "kustomization.yaml").write_text("resources: []\n", encoding="utf-8")
    (src_root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (src_root / ".env.example").write_text("FOO=bar\n", encoding="utf-8")

    home = tmp_path / "home"
    monkeypatch.setattr(ops, "_packaged_resources_root", lambda: src_root)
    monkeypatch.setattr(ops, "NYXGPT_HOME", home / ".nyxGPT")
    monkeypatch.setattr(ops, "OPS_COMPOSE_FILE", home / ".nyxGPT" / "docker-compose.yml")
    monkeypatch.setattr(ops, "OPS_SCRIPTS_SRC_DIR", home / ".nyxGPT" / "scripts")

    results = ops._sync_packaged_resources()
    assert all(r.ok for r in results)
    assert (home / ".nyxGPT" / "docker-compose.yml").read_text(encoding="utf-8") == "services: {}\n"
    assert (home / ".nyxGPT" / ".env.example").read_text(encoding="utf-8") == "FOO=bar\n"
    assert (home / ".nyxGPT" / "docker" / "grafana" / "x.yml").exists()
    assert (home / ".nyxGPT" / "ops" / "launchagents" / "com.nyxgpt.cassandra-logs.plist").exists()
    assert (home / ".nyxGPT" / "k8s" / "kustomization.yaml").exists()
    script = home / ".nyxGPT" / "scripts" / "run-web.sh"
    assert script.exists()
    assert script.stat().st_mode & 0o777 == 0o755


@pytest.mark.unit
def test_sync_packaged_resources_is_idempotent_and_additive(monkeypatch, tmp_path):
    # Re-running (e.g. a second `nyxgpt ops install`) must overwrite the
    # synced copies with the current packaged content, without touching
    # unrelated files an operator already has under NYXGPT_HOME (notably the
    # separately generated docker/config.docker.ini -- #3621).
    src_root = tmp_path / "packaged"
    (src_root / "docker").mkdir(parents=True)
    (src_root / "docker" / "prometheus.yml").write_text("v1", encoding="utf-8")
    (src_root / "ops").mkdir(parents=True)
    (src_root / "scripts").mkdir(parents=True)
    (src_root / "k8s").mkdir(parents=True)
    (src_root / "docker-compose.yml").write_text("v1", encoding="utf-8")
    (src_root / ".env.example").write_text("v1", encoding="utf-8")

    home = tmp_path / "home"
    nyxgpt_home = home / ".nyxGPT"
    (nyxgpt_home / "docker").mkdir(parents=True)
    generated_config = nyxgpt_home / "docker" / "config.docker.ini"
    generated_config.write_text("operator's generated config", encoding="utf-8")

    monkeypatch.setattr(ops, "_packaged_resources_root", lambda: src_root)
    monkeypatch.setattr(ops, "NYXGPT_HOME", nyxgpt_home)
    monkeypatch.setattr(ops, "OPS_COMPOSE_FILE", nyxgpt_home / "docker-compose.yml")
    monkeypatch.setattr(ops, "OPS_SCRIPTS_SRC_DIR", nyxgpt_home / "scripts")

    assert all(r.ok for r in ops._sync_packaged_resources())

    (src_root / "docker" / "prometheus.yml").write_text("v2", encoding="utf-8")
    (src_root / "docker-compose.yml").write_text("v2", encoding="utf-8")

    results = ops._sync_packaged_resources()
    assert all(r.ok for r in results)
    assert (nyxgpt_home / "docker" / "prometheus.yml").read_text(encoding="utf-8") == "v2"
    assert (nyxgpt_home / "docker-compose.yml").read_text(encoding="utf-8") == "v2"
    assert generated_config.read_text(encoding="utf-8") == "operator's generated config"


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
@pytest.mark.parametrize("name", ["nyxgpt-api", "nyxgpt-web"])
def test_create_dist_tarball_never_vendors_pycache_or_ds_store(monkeypatch, tmp_path, name):
    """No dist tarball carries interpreter caches or Finder metadata from the
    checkout it was built from (#3757).

    A used checkout accumulates `__pycache__/*.pyc` from whatever Python last
    ran in it; vendoring those made api tarballs nondeterministic and, on a
    checkout in a paused-sync cloud-storage folder, made the copy fail
    outright (dehydrated `.pyc` placeholder -> `Errno 60`). One shared
    exclusion in `_vendor_tree` covers every `_create_dist_tarball` caller,
    so the web tree is held to it too.
    """
    if name == "nyxgpt-api":
        repo_root = _make_fake_api_repo_root(tmp_path)
        tree, arc_prefix = repo_root / "src" / "nyxgpt", f"{name}-1.2.3/src/nyxgpt"
    else:
        repo_root = _make_fake_web_repo_root(tmp_path)
        tree, arc_prefix = repo_root / "web", f"{name}-1.2.3"
    (tree / "resources" / "__pycache__").mkdir(parents=True)
    (tree / "resources" / "__pycache__" / "__init__.cpython-314.pyc").write_bytes(b"\x00")
    (tree / "stray.pyc").write_bytes(b"\x00")
    (tree / "stray.pyo").write_bytes(b"\x00")
    (tree / ".DS_Store").write_bytes(b"\x00")
    (tree / "resources" / "keep.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(ops, "REPO_ROOT", repo_root)

    tar_path = ops._create_dist_tarball(tmp_path, name, "1.2.3")
    with tarfile.open(tar_path, "r:gz") as tf:
        names = tf.getnames()

    assert f"{arc_prefix}/resources/keep.txt" in names
    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith((".pyc", ".pyo")) for n in names)
    assert not any(n.endswith(".DS_Store") for n in names)


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
def test_install_homebrew_api_without_a_checkout_uses_the_published_tap(monkeypatch, tmp_path):
    """No formula template means no checkout -- install the published formula (#3759).

    Full coverage of the remote-tap path lives in
    tests/unit/test_ops_artifact_install.py; this pins the dispatch.
    """
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_install_from_remote_tap", lambda name: [ops.OpsResult(True, name)])

    results = ops._install_homebrew_api()

    assert results == [ops.OpsResult(True, "nyxgpt-api")]


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
    # A fresh install/reinstall must restart (not merely start) the service so
    # the running uvicorn process actually picks up the newly built keg's
    # source instead of continuing to serve the old process's already-imported
    # (stale) code (#3472, mirrors the nyxgpt-web fix in #3445).
    assert any(cmd[:3] == ["brew", "services", "restart"] for cmd in run_calls)
    assert not any(cmd[:3] == ["brew", "services", "start"] for cmd in run_calls)


@pytest.mark.unit
def test_install_homebrew_api_already_up_to_date_uses_start_not_restart(monkeypatch, tmp_path):
    """When the vendored api source hasn't changed since the last install,
    `_brew_install_or_reinstall` skips the rebuild entirely -- and this must
    only `start` (idempotent) the service rather than bounce an
    already-healthy running process for no reason (#3472, mirrors #3445)."""
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

    results = ops._install_homebrew_api()
    assert all(r.ok for r in results)
    assert any(cmd[:3] == ["brew", "services", "start"] for cmd in run_calls)
    assert not any(cmd[:3] == ["brew", "services", "restart"] for cmd in run_calls)


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
def test_install_homebrew_web_without_a_checkout_uses_the_published_tap(monkeypatch, tmp_path):
    """Artifact install -- see the api twin above (#3759)."""
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_install_from_remote_tap", lambda name: [ops.OpsResult(True, name)])

    results = ops._install_homebrew_web()

    assert results == [ops.OpsResult(True, "nyxgpt-web")]


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


@pytest.mark.unit
def test_api_formula_wrapper_execs_uvicorn():
    """Regression test: the wrapper script must `exec` into uvicorn rather
    than run it as a child process. A plain (non-exec'd) foreground command
    leaves bash as the tracked launchd PID; bash's SIGTERM/SIGINT traps used
    to only log the signal without forwarding it or killing the child, so
    `brew services stop`/`restart` never actually stopped uvicorn -- it was
    silently orphaned, still bound to the port and still serving the *old*
    in-memory code, which is how a merged code fix could survive a
    `nyxgpt ops down && nyxgpt ops install` cycle without ever taking effect
    on the live stack (#3472). `exec` makes uvicorn the actual tracked
    process so a stop signal reaches it directly, mirroring nyxgpt-web.rb's
    wrapper (`exec npm run start`)."""
    formula = Path(__file__).resolve().parents[2] / "homebrew" / "nyxgpt-api.rb"
    text = formula.read_text(encoding="utf-8")
    assert 'exec "#{venv}/bin/python3" -m uvicorn nyxgpt.app:app' in text
    assert "trap '" not in text


@pytest.mark.unit
def test_wrapper_exec_forwards_sigterm_to_child_but_bare_trap_does_not(tmp_path):
    """OS-level proof of the #3472 root cause and fix, independent of the
    formula's literal text (see test_api_formula_wrapper_execs_uvicorn for
    that static check): a wrapper that runs its child as a plain foreground
    job with only a log-only `trap ... TERM` never forwards the signal, so
    the child outlives the wrapper (the exact orphaned-uvicorn bug). A
    wrapper that `exec`s into the child instead makes the child the actual
    signaled process, so it dies with the wrapper. Uses stand-in child
    scripts instead of uvicorn so the test has no port/readiness flakiness."""
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import pathlib, time\n"
        "pathlib.Path(pathlib.sys.argv[1]).write_text('running')\n"
        "time.sleep(30)\n"
    )

    old_wrapper = tmp_path / "old_wrapper.sh"
    old_wrapper.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "trap 'echo received TERM' TERM\n"
        f'{sys.executable} {child_script} "$1" &\n'
        "CHILD=$!\n"
        "wait $CHILD\n"
    )
    new_wrapper = tmp_path / "new_wrapper.sh"
    new_wrapper.write_text(
        "#!/bin/bash\n" "set -euo pipefail\n" f'exec {sys.executable} {child_script} "$1"\n'
    )

    def child_survives_wrapper_sigterm(wrapper: Path) -> bool:
        marker = tmp_path / f"marker-{wrapper.stem}"
        proc = subprocess.Popen(["/bin/bash", str(wrapper), str(marker)], start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert marker.exists(), "child never started"

            os.kill(proc.pid, signal.SIGTERM)
            proc.wait(timeout=5)

            # The wrapper's own PID (bash, or the exec'd child sharing it) is
            # gone either way -- what matters is whether a *different*,
            # still-running process is left holding the child's job. We
            # detect that by whether the process group the child was placed
            # in is still occupied under the wrapper's old (non-exec) PID.
            try:
                os.killpg(proc.pid, 0)
                return True  # process group still has a living member
            except ProcessLookupError:
                return False
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)

    assert child_survives_wrapper_sigterm(old_wrapper) is True
    assert child_survives_wrapper_sigterm(new_wrapper) is False


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


@pytest.mark.unit
def test_vendor_tree_always_excludes_caches_even_without_caller_excludes(tmp_path):
    """`__pycache__`/`.pyc`/`.pyo`/`.DS_Store` are dropped with no `excludes`
    argument at all -- the api branch of `_create_dist_tarball` passes none
    and used to vendor the checkout's bytecode caches (#3757)."""
    src = tmp_path / "src"
    (src / "pkg" / "__pycache__").mkdir(parents=True)
    (src / "pkg" / "__pycache__" / "mod.cpython-314.pyc").write_bytes(b"\x00")
    (src / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (src / "loose.pyc").write_bytes(b"\x00")
    (src / "loose.pyo").write_bytes(b"\x00")
    (src / ".DS_Store").write_bytes(b"\x00")

    dst = tmp_path / "dst"
    ops._vendor_tree(src, dst)

    assert (dst / "pkg" / "mod.py").exists()
    assert not (dst / "pkg" / "__pycache__").exists()
    assert not (dst / "loose.pyc").exists()
    assert not (dst / "loose.pyo").exists()
    assert not (dst / ".DS_Store").exists()


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
    """No `web/` means an artifact install, not a broken checkout (#3759)."""
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    results = ops._ensure_web_deps()
    assert results[0].ok is True
    assert "not applicable to an artifact install" in results[0].message


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
    """No root package.json means an artifact install (#3759)."""
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    results = ops._ensure_mcp_deps()
    assert results[0].ok is True
    assert "not applicable to an artifact install" in results[0].message


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
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_sync_packaged_resources", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", side_effect=RuntimeError("kaboom")),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
    ):
        rc = ops.install(MagicMock(dev=False, terraform=False, kubernetes=False))
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
    monkeypatch.setattr(
        ops, "_compose_stack_snapshot", lambda: {"grafana": "running", "api": "running"}
    )

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "compose grafana: running" in out
    assert "Compose components" in out


@pytest.mark.unit
def test_ops_status_omits_compose_config_hint_for_observability_only(monkeypatch, capsys):
    """`COMPOSE_CONFIG_HINT` names a file only a Compose *core* tier reads (#3855).

    The generality sweep's second site: this line used the same
    whole-snapshot truthiness test `infra_status` did, so a native install
    running the default observability stack was told to go edit
    `config.docker.ini` -- a file nothing on that host reads, since the
    observability containers take no nyxGPT config at all.
    """

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
    # The Compose rows are still listed -- only the config hint changed.
    assert "compose grafana: running" in out
    assert "Compose components" not in out
    assert ops.COMPOSE_CONFIG_HINT not in out


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
    # doctor now probes whether the docker daemon is actually reachable (#3632);
    # this test's narrow subprocess.run stub is only for the node resolve probe.
    monkeypatch.setattr(ops, "_docker_daemon_reachable", lambda: True)
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
    monkeypatch.setattr(ops, "_sync_packaged_resources", lambda: [ops.OpsResult(True, "synced")])

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
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_sync_packaged_resources", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        caplog.at_level("INFO", logger="nyxgpt.ops"),
    ):
        rc = ops.install(MagicMock(dev=False, terraform=False, kubernetes=False))

    assert rc == 0
    messages = [r.getMessage() for r in caplog.records]
    assert any("install starting" in m for m in messages)
    assert any("install succeeded" in m for m in messages)


@pytest.mark.unit
def test_ops_install_logs_error_when_step_raises(caplog):
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", side_effect=RuntimeError("boom")),
        patch.object(ops, "migrate_legacy_volumes", return_value=[]),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=[]),
        patch.object(ops, "_ensure_web_deps", return_value=[]),
        patch.object(ops, "_ensure_mcp_deps", return_value=[]),
        patch.object(ops, "_ensure_cassandra_container", return_value=[]),
        patch.object(ops, "_install_cassandra_launchagent", return_value=[]),
        patch.object(ops, "_install_ollama_launchagent", return_value=[]),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=[]),
        patch.object(ops, "_install_homebrew_api", return_value=[]),
        patch.object(ops, "_install_homebrew_web", return_value=[]),
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=[]),
        caplog.at_level("INFO", logger="nyxgpt.ops"),
    ):
        rc = ops.install(MagicMock(dev=False, terraform=False, kubernetes=False))

    assert rc == 2
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(error_records) == 1
    assert "sync packaged ops resources" in error_records[0].getMessage()
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
    # self_heal has its own `_which`, independent of ops's -- without this,
    # list_component_status()'s compose probe still finds the real `docker`
    # on PATH and tries `docker compose ps` against the (now ops-managed,
    # not-yet-synced -- #3621) default COMPOSE_FILE, which doesn't exist
    # here and logs an extra, unrelated self_heal warning.
    monkeypatch.setattr(ops.self_heal, "_which", lambda _: None)

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
    # Only the native cassandra container is "running" here -- a broader
    # stub (any name -> "running") would also mark every Terraform-managed
    # container "running", tripping the dual-stack conflict check this test
    # isn't exercising.
    monkeypatch.setattr(
        ops,
        "_docker_container_state",
        lambda name: "running" if name == "nyxgpt-cassandra" else "absent",
    )
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})

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


# --- host-api-relay: docker bridge -> host loopback (#3721) ---


def _fake_network_inspect(stdout: str, rc: int = 0):
    def fake_run(cmd, check=True, **_k):
        assert cmd[:4] == ["docker", "network", "inspect", "bridge"]
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout)

    return fake_run


def _write_relay_config(cfg_path: Path, api_host: str = "127.0.0.1") -> None:
    cfg_path.write_text(f"[api]\nhost = {api_host}\nport = 8000\n", encoding="utf-8")


@pytest.mark.unit
def test_docker_bridge_gateway_ip_parses_the_ipv4_gateway(monkeypatch):
    monkeypatch.setattr(ops, "_run", _fake_network_inspect("172.17.0.1 \n"))
    assert ops._docker_bridge_gateway_ip() == "172.17.0.1"


@pytest.mark.unit
def test_docker_bridge_gateway_ip_returns_none_when_docker_fails(monkeypatch):
    monkeypatch.setattr(ops, "_run", _fake_network_inspect("", rc=1))
    assert ops._docker_bridge_gateway_ip() is None


@pytest.mark.unit
def test_docker_bridge_gateway_ip_ignores_non_ipv4_gateways(monkeypatch):
    """socat's `bind=` in docker-compose.yml is an IPv4 listener, and
    host.docker.internal resolves to docker's IPv4 host-gateway -- an IPv6-only
    answer must read as "unknown" rather than be passed through."""
    monkeypatch.setattr(ops, "_run", _fake_network_inspect("fe80::1 not-an-ip \n"))
    assert ops._docker_bridge_gateway_ip() is None


@pytest.mark.unit
def test_host_relay_decision_enabled_on_linux_with_loopback_api(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.18.0.1")

    enabled, gateway, _reason = ops._host_relay_decision(cfg_path)

    assert enabled is True
    assert gateway == "172.18.0.1"


@pytest.mark.unit
def test_host_relay_decision_defaults_to_enabled_before_the_wizard_runs(tmp_path, monkeypatch):
    """No config.ini yet means the native wrapper's own 127.0.0.1 default
    applies, which is exactly the case that needs the relay."""
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    enabled, gateway, _reason = ops._host_relay_decision(tmp_path / "missing.ini")

    assert enabled is True
    assert gateway == "172.17.0.1"


@pytest.mark.unit
def test_host_relay_decision_disabled_off_linux(tmp_path, monkeypatch):
    """Docker Desktop proxies host.docker.internal to the host's loopback, so the
    relay is unnecessary there -- and a second listener on the API port would
    collide with uvicorn's own."""
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: False)
    monkeypatch.setattr(
        ops,
        "_docker_bridge_gateway_ip",
        lambda: pytest.fail("must not probe docker on a non-Linux host"),
    )

    enabled, gateway, _reason = ops._host_relay_decision(cfg_path)

    assert enabled is False
    assert gateway == "127.0.0.1"


@pytest.mark.unit
def test_host_relay_decision_disabled_when_api_already_listens_beyond_loopback(
    tmp_path, monkeypatch
):
    """An operator who deliberately set `[api] host = 0.0.0.0` already has a
    container-reachable API (and, per app.py's P6-1 gate, auth enabled). Starting
    the relay anyway would fail to bind against that wildcard socket."""
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path, api_host="0.0.0.0")
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    enabled, _gateway, reason = ops._host_relay_decision(cfg_path)

    assert enabled is False
    assert "already listens beyond loopback" in reason


@pytest.mark.unit
def test_host_relay_decision_disabled_when_gateway_unknown(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: None)

    enabled, gateway, reason = ops._host_relay_decision(cfg_path)

    assert enabled is False
    assert gateway == "127.0.0.1"
    assert "docker bridge gateway" in reason


@pytest.mark.unit
def test_sync_host_relay_env_enables_relay_and_preserves_other_lines(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "NYXGPT_BIND_ADDR=127.0.0.1\n"
        "NYXGPT_API_PORT=8000\n"
        "NYXGPT_HOST_RELAY_PROFILE=disabled\n"
        "NYXGPT_HOST_GATEWAY_IP=127.0.0.1\n"
        "GRAFANA_ADMIN_PASSWORD=secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    result = ops._sync_host_relay_env(cfg_path=cfg_path, env_path=env_path)

    assert result.ok is True
    content = env_path.read_text(encoding="utf-8")
    assert "NYXGPT_HOST_RELAY_PROFILE=monitoring" in content
    assert "NYXGPT_HOST_GATEWAY_IP=172.17.0.1" in content
    # Every variable this function doesn't own survives verbatim.
    assert "GRAFANA_ADMIN_PASSWORD=secret" in content
    assert "NYXGPT_BIND_ADDR=127.0.0.1" in content


@pytest.mark.unit
def test_sync_host_relay_env_seeds_from_example_when_env_missing(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path)
    (tmp_path / ".env.example").write_text(
        "NYXGPT_HOST_RELAY_PROFILE=disabled\nNYXGPT_HOST_GATEWAY_IP=127.0.0.1\n",
        encoding="utf-8",
    )
    env_path = tmp_path / ".env"
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    result = ops._sync_host_relay_env(cfg_path=cfg_path, env_path=env_path)

    assert result.ok is True
    assert "NYXGPT_HOST_RELAY_PROFILE=monitoring" in env_path.read_text(encoding="utf-8")


@pytest.mark.unit
def test_sync_host_relay_env_appends_variables_absent_from_an_older_env(tmp_path, monkeypatch):
    """Installs that predate #3721 have a `.env` with neither variable in it."""
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path)
    env_path = tmp_path / ".env"
    env_path.write_text("NYXGPT_API_PORT=8000\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    ops._sync_host_relay_env(cfg_path=cfg_path, env_path=env_path)

    content = env_path.read_text(encoding="utf-8")
    assert "NYXGPT_HOST_RELAY_PROFILE=monitoring" in content
    assert "NYXGPT_HOST_GATEWAY_IP=172.17.0.1" in content


@pytest.mark.unit
def test_sync_host_relay_env_reconciles_back_to_disabled(tmp_path, monkeypatch):
    """A host that stops needing the relay must have the profile written back, or
    the next `up` leaves a stale socat bound to the bridge gateway."""
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path, api_host="0.0.0.0")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "NYXGPT_HOST_RELAY_PROFILE=monitoring\nNYXGPT_HOST_GATEWAY_IP=172.17.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    result = ops._sync_host_relay_env(cfg_path=cfg_path, env_path=env_path)

    assert result.ok is True
    content = env_path.read_text(encoding="utf-8")
    assert "NYXGPT_HOST_RELAY_PROFILE=disabled" in content
    assert "NYXGPT_HOST_RELAY_PROFILE=monitoring" not in content


@pytest.mark.unit
def test_sync_host_relay_env_skips_when_no_env_files_exist(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path)
    env_path = tmp_path / ".env"
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    result = ops._sync_host_relay_env(cfg_path=cfg_path, env_path=env_path)

    assert result.ok is True
    assert "no Compose .env yet" in result.message
    assert not env_path.exists()


@pytest.mark.unit
def test_sync_host_relay_env_never_fails_the_caller_on_an_unwritable_env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.ini"
    _write_relay_config(cfg_path)
    env_path = tmp_path / ".env"
    env_path.write_text("NYXGPT_API_PORT=8000\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")

    def _boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", _boom)

    result = ops._sync_host_relay_env(cfg_path=cfg_path, env_path=env_path)

    # Degrades to the pre-#3721 behaviour (relay off), which is not a failed
    # install -- Compose still brings the rest of the stack up.
    assert result.ok is True
    assert "Could not update the host API relay settings" in result.message


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

    # OPS_DOCKER_DIR/NYXGPT_HOME are module-level constants computed once
    # from Path.home() at import time (#3621), so patching Path.home() here
    # (as before REPO_ROOT retirement) wouldn't retroactively change them --
    # patch the already-resolved constants directly instead.
    monkeypatch.setattr(ops, "OPS_DOCKER_DIR", tmp_path / "docker")
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
    monkeypatch.setattr(ops, "OPS_DOCKER_DIR", tmp_path / "docker")
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
        status_code = 200

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
        status_code = 200

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
def test_verify_grafana_datasources_resolve_fails_with_http_error_includes_body(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_provisioned_datasource_uids", lambda: ["prometheus"])
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)

    class FakeResponse:
        status_code = 500
        text = "internal server error: datasource registry unavailable"

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
    assert "Could not reach Grafana" in result.message
    assert "HTTP 500" in result.details
    assert "datasource registry unavailable" in result.details


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
            return {"type": "app", "enabled": True}

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
def test_verify_grafana_plugins_installed_fails_when_app_plugin_not_enabled(monkeypatch):
    monkeypatch.setattr(ops, "_grafana_expected_plugin_ids", lambda: ["grafana-lokiexplore-app"])
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)

    class FakeResponse:
        status_code = 200
        text = '{"type": "app", "enabled": false}'

        def json(self):
            return {"type": "app", "enabled": False}

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
    assert "not enabled" in result.details


@pytest.mark.unit
def test_verify_grafana_plugins_installed_ok_when_datasource_plugin_reports_disabled(
    monkeypatch,
):
    """#3560: `enabled` is an app-plugin-only concept -- a datasource plugin
    that is fully installed and loaded always reports `enabled: false`
    (Grafana has no on/off toggle for datasource plugins), so that must not
    be treated as a failure."""
    monkeypatch.setattr(
        ops, "_grafana_expected_plugin_ids", lambda: ["yesoreyeram-infinity-datasource"]
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "name": "Infinity",
                "type": "datasource",
                "id": "yesoreyeram-infinity-datasource",
                "enabled": False,
                "pinned": False,
                "autoEnabled": False,
                "module": "public/plugins/yesoreyeram-infinity-datasource/module.js",
            }

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
        text = '{"message": "Plugin not found"}'

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
    assert "HTTP 404" in result.details
    assert "Plugin not found" in result.details


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
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

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
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

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
def test_provision_grafana_doctor_token_fails_when_search_returns_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    class FakeResponse:
        status_code = 403
        text = "access denied: admin credentials rejected"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return FakeResponse()

        def post(self, path, json=None):
            raise AssertionError("should not reach POST when search fails")

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._provision_grafana_doctor_token("http://localhost:3001", "admin")

    assert result.ok is False
    assert "HTTP 403" in result.details
    assert "access denied: admin credentials rejected" in result.details
    assert not (tmp_path / ".nyxGPT" / "secrets" / "grafana-doctor-token").exists()


@pytest.mark.unit
def test_provision_grafana_doctor_token_fails_when_create_service_account_returns_http_error(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return FakeResponse(200, {"serviceAccounts": []})

        def post(self, path, json=None):
            assert path == "/api/serviceaccounts"
            return FakeResponse(409, text="service account name already exists")

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._provision_grafana_doctor_token("http://localhost:3001", "admin")

    assert result.ok is False
    assert "HTTP 409" in result.details
    assert "service account name already exists" in result.details
    assert not (tmp_path / ".nyxGPT" / "secrets" / "grafana-doctor-token").exists()


@pytest.mark.unit
def test_provision_grafana_doctor_token_fails_when_mint_token_returns_http_error(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return FakeResponse(200, {"serviceAccounts": [{"id": 3, "name": "nyxgpt-ops-doctor"}]})

        def post(self, path, json=None):
            assert path == "/api/serviceaccounts/3/tokens"
            return FakeResponse(500, text="token minting temporarily unavailable")

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._provision_grafana_doctor_token("http://localhost:3001", "admin")

    assert result.ok is False
    assert "HTTP 500" in result.details
    assert "token minting temporarily unavailable" in result.details
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
    captured_input = []

    def _fake_run(cmd, check=False, input=None):
        captured_cmd.extend(cmd)
        captured_input.append(input)
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
        "sh",
        "-c",
        'grafana cli admin reset-admin-password "$(cat)"',
    ]
    # The password must travel over stdin, never as an argv element -- argv
    # is visible to `ps`, shell history, and `_run`'s non-zero-exit logging
    # (#3644, CodeQL py/clear-text-logging-sensitive-data #105/#106).
    assert "new-secret" not in captured_cmd
    assert captured_input == ["new-secret"]


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
        lambda cmd, check=False, input=None: subprocess.CompletedProcess(
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
def test_reconcile_grafana_provisioning_syncs_the_host_relay_before_starting(tmp_path, monkeypatch):
    """#3721: `_start_observability_stack` enumerates services with `docker
    compose config --services`, which reads `.env` -- so the relay profile has to
    be written *before* the stack comes up, or the first `nyxgpt ops install` on
    a Linux host still leaves prometheus unable to reach the native API."""
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)

    order = []
    monkeypatch.setattr(
        ops,
        "_sync_host_relay_env",
        lambda: order.append("relay") or ops.OpsResult(True, "Host API relay enabled"),
    )
    monkeypatch.setattr(
        ops,
        "_start_observability_stack",
        lambda: order.append("start") or [ops.OpsResult(True, "stack up")],
    )

    results = ops._reconcile_grafana_provisioning()

    assert order == ["relay", "start"]
    assert all(r.ok for r in results)
    assert any("Host API relay" in r.message for r in results)


@pytest.mark.unit
def test_reconcile_grafana_provisioning_reconciles_volume_ownership_before_starting(
    tmp_path, monkeypatch
):
    """#3721: dockerd creates a missing bind-mount source root-owned, so the
    ownership reconcile has to happen before the containers first start -- once
    prometheus has crash-looped there is nothing to un-do, it just stays down."""
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)

    order = []
    monkeypatch.setattr(
        ops,
        "_ensure_observability_volume_dirs",
        lambda: order.append("volumes") or [ops.OpsResult(True, "/v/prometheus is owned by 65534")],
    )
    monkeypatch.setattr(
        ops,
        "_sync_host_relay_env",
        lambda: order.append("relay") or ops.OpsResult(True, "Host API relay enabled"),
    )
    monkeypatch.setattr(
        ops,
        "_start_observability_stack",
        lambda: order.append("start") or [ops.OpsResult(True, "stack up")],
    )

    results = ops._reconcile_grafana_provisioning()

    assert order == ["volumes", "relay", "start"]
    assert any("owned by 65534" in r.message for r in results)


@pytest.mark.unit
def test_reconcile_observability_reconciles_volume_ownership(tmp_path, monkeypatch):
    """The SRE dashboard's observability toggle goes through
    `reconcile_observability`, never through `install()`. Before #3721 that path
    had no ownership reconcile at all, so enabling monitoring from the dashboard
    on Linux brought up a prometheus that could not write /prometheus."""
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)
    monkeypatch.setattr(ops, "_sync_host_relay_env", lambda: ops.OpsResult(True, "relay"))
    monkeypatch.setattr(ops, "_record_ops_action", lambda *a, **k: None)

    called = []
    monkeypatch.setattr(
        ops,
        "_ensure_observability_volume_dirs",
        lambda: called.append(True) or [ops.OpsResult(True, "volumes ok")],
    )
    monkeypatch.setattr(
        ops, "_start_observability_stack", lambda: [ops.OpsResult(True, "stack up")]
    )

    ops.reconcile_observability(enable=True)

    assert called == [True]


@pytest.mark.unit
def test_reconcile_grafana_provisioning_surfaces_an_unfixable_volume_dir(tmp_path, monkeypatch):
    """A directory that could be neither chowned nor ACL-granted must reach the
    caller as a failure -- the whole point of #3632/#3721 is that this stops
    presenting as "Grafana panels are empty" with every service reporting OK."""
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)
    monkeypatch.setattr(ops, "_sync_host_relay_env", lambda: ops.OpsResult(True, "relay"))
    monkeypatch.setattr(
        ops,
        "_ensure_observability_volume_dirs",
        lambda: [ops.OpsResult(False, "/v/prometheus is owned by uid 0", "sudo chown -R ...")],
    )
    monkeypatch.setattr(
        ops, "_start_observability_stack", lambda: [ops.OpsResult(True, "stack up")]
    )

    results = ops._reconcile_grafana_provisioning()

    failures = [r for r in results if not r.ok]
    assert len(failures) == 1
    assert "owned by uid 0" in failures[0].message


@pytest.mark.unit
def test_reconcile_grafana_provisioning_writes_the_relay_env_next_to_the_compose_file(
    tmp_path, monkeypatch
):
    """End-to-end through the real `_sync_host_relay_env`: the generated `.env`
    must land in the directory Compose resolves it from (the compose file's own),
    otherwise the profile is written somewhere `docker compose -f` never reads."""
    _write_grafana_fixture(tmp_path, monkeypatch, datasource_yml=_SAMPLE_DATASOURCE_YML)
    (tmp_path / ".env").write_text("NYXGPT_API_PORT=8000\n", encoding="utf-8")
    monkeypatch.setattr(ops, "_recreate_grafana_if_provisioning_drifted", lambda: None)
    monkeypatch.setattr(ops, "_is_linux", lambda: True)
    monkeypatch.setattr(ops, "_docker_bridge_gateway_ip", lambda: "172.17.0.1")
    monkeypatch.setattr(
        ops, "_start_observability_stack", lambda: [ops.OpsResult(True, "stack up")]
    )

    ops._reconcile_grafana_provisioning()

    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "NYXGPT_HOST_RELAY_PROFILE=monitoring" in content
    assert "NYXGPT_HOST_GATEWAY_IP=172.17.0.1" in content


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
    with (
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "synced")]),
        patch.object(
            ops, "_reconcile_grafana_provisioning", return_value=[ops.OpsResult(True, "up")]
        ),
    ):
        rc = ops.observability(MagicMock(kubernetes=False))
        assert rc == 0
        assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_observability_cli_entrypoint_returns_nonzero_on_failure(capsys):
    with (
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "synced")]),
        patch.object(
            ops,
            "_reconcile_grafana_provisioning",
            return_value=[ops.OpsResult(False, "down", "boom")],
        ),
    ):
        rc = ops.observability(MagicMock(kubernetes=False))
        assert rc == 2
        assert "[FAIL]" in capsys.readouterr().out


@pytest.mark.unit
def test_observability_cli_entrypoint_skips_reconcile_when_sync_fails(capsys):
    with (
        patch.object(
            ops, "_sync_packaged_resources", return_value=[ops.OpsResult(False, "sync failed")]
        ),
        patch.object(ops, "_reconcile_grafana_provisioning") as reconcile,
    ):
        rc = ops.observability(MagicMock(kubernetes=False))
        assert rc == 2
        reconcile.assert_not_called()
        assert "[FAIL]" in capsys.readouterr().out


# --- _stop_brew_service ---


@pytest.mark.unit
def test_stop_brew_service_not_found(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    results = ops._stop_brew_service("nyxgpt-api")
    assert results[0].ok is False
    assert "brew not found" in results[0].message


@pytest.mark.unit
def test_stop_brew_service_success(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    # Nothing registered afterwards: no plist under this home, and `launchctl
    # print` cannot find the job. Both are pinned rather than left to the host,
    # because "is it still registered?" is now answered from those two facts
    # and a bare rc-0 stub would answer "loaded" (#3861).
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    _brew_list_stub(monkeypatch, ["nyxgpt-api none"])
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


# --- _stop_brew_service: the stop that exits 0 without de-registering (#3861) ---
#
# `brew services stop` exits 0 for a service that is registered but not
# running -- the state a crash-looping keg sits in -- and leaves its
# LaunchAgent plist in place, so launchd starts it again at the next login.
# Trusting the exit code reported "Stopped brew service: nyxgpt-api" on a
# machine where `brew services list` still showed it registered
# (macos-brew-smoke run 32222041921).


def _brew_list_stub(monkeypatch, listings, *, launchd_loaded=False):
    """Fake `_run` where `brew services list` returns each of `listings` in turn.

    `launchctl print` answers non-zero unless `launchd_loaded` -- "the job is
    not loaded" is the ordinary case, and it is the *other* half of the
    registration question the Status column cannot answer (#3861).
    """
    remaining = list(listings)

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["brew", "services", "list"]:
            out = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            return subprocess.CompletedProcess(cmd, 0, stdout=out)
        if cmd[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(
                cmd, 0 if launchd_loaded else 1, stderr="Could not find service"
            )
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(ops, "_run", fake_run)


@pytest.mark.unit
def test_stop_brew_service_deregisters_a_service_brew_left_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    la_dir = tmp_path / "Library" / "LaunchAgents"
    la_dir.mkdir(parents=True)
    plist = la_dir / "homebrew.mxcl.nyxgpt-api.plist"
    plist.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    booted: list[str] = []
    monkeypatch.setattr(
        ops,
        "_stop_launchagent",
        lambda label: booted.append(label) or [ops.OpsResult(True, label)],
    )
    _brew_list_stub(
        monkeypatch,
        [
            f"nyxgpt-api error 3 runner {plist}",  # after `brew services stop`
            f"nyxgpt-api error 3 runner {plist}",  # read again to find the plist
            "nyxgpt-api none",  # after the forced de-registration
        ],
    )

    results = ops._stop_brew_service("nyxgpt-api")

    assert all(r.ok for r in results), [r.message for r in results]
    assert booted == ["homebrew.mxcl.nyxgpt-api"]
    assert not plist.exists()
    assert any("de-registered brew service" in r.message for r in results)
    # The bare "Stopped brew service" claim is never made about a service
    # that was still registered when brew said it had stopped it.
    assert [r.message for r in results if r.message == "Stopped brew service: nyxgpt-api"] == []


@pytest.mark.unit
def test_stop_brew_service_reports_failure_when_it_stays_registered(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(ops, "_stop_launchagent", lambda label: [ops.OpsResult(True, label)])
    # The job is still loaded after the stop and after the escalation, which
    # is a registration whatever brew's column says -- and the caller must not
    # be told the port is free.
    _brew_list_stub(
        monkeypatch,
        ["nyxgpt-api error 3 runner ~/Library/LaunchAgents/x.plist"],
        launchd_loaded=True,
    )

    results = ops._stop_brew_service("nyxgpt-api")

    assert results[-1].ok is False
    assert "still registered" in results[-1].message
    assert "next login" in results[-1].details


@pytest.mark.unit
def test_stop_brew_service_leaves_a_clean_stop_alone(monkeypatch, tmp_path):
    """A stop that really de-registered reports exactly as it always did."""
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    boot = MagicMock()
    monkeypatch.setattr(ops, "_stop_launchagent", boot)
    _brew_list_stub(monkeypatch, ["nyxgpt-api none\nnyxgpt-web started 501 ~/x.plist"])

    results = ops._stop_brew_service("nyxgpt-api")

    assert [(r.ok, r.message) for r in results] == [(True, "Stopped brew service: nyxgpt-api")]
    boot.assert_not_called()


@pytest.mark.unit
def test_an_error_column_over_a_missing_plist_is_not_a_registration(monkeypatch, tmp_path):
    """An `error <code>` row whose plist is gone is not a registration (#3861).

    The observable measured on real runners (`macos-brew-smoke.yml` runs
    32222041921 and 32228088507): a column-based read reported the service
    registered while launchd said it was gone -- in the latter, the
    reconcile's escalation found no plist to remove and no loaded job. Run
    32233162053 traced that to ANSI-coloured state text rather than to a
    column outliving the registration, but the mechanism is not what this
    pins. What it pins is the rule that holds regardless: a state word says
    whether the service RUNS, and `error` is a state of a *registered* one, so
    the plist decides. Treating the column as "registered" reported
    a successful retire as a failure -- the mirror image of trusting `brew
    services stop`'s exit code, and just as wrong. Nothing will start this
    service again, so the stop is a stop.
    """
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    boot = MagicMock()
    monkeypatch.setattr(ops, "_stop_launchagent", boot)
    _brew_list_stub(monkeypatch, ["nyxgpt-api error 3 runner /gone/homebrew.mxcl.nyxgpt-api.plist"])

    assert ops._brew_service_is_registered("nyxgpt-api") is False
    results = ops._stop_brew_service("nyxgpt-api")

    assert [(r.ok, r.message) for r in results] == [(True, "Stopped brew service: nyxgpt-api")]
    boot.assert_not_called()


@pytest.mark.unit
def test_a_loaded_job_is_a_registration_even_with_no_plist(monkeypatch, tmp_path):
    """The other half: no file on disk, but launchd is still running the job."""
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    (tmp_path / "Library" / "LaunchAgents").mkdir(parents=True)
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    _brew_list_stub(monkeypatch, ["nyxgpt-api none"], launchd_loaded=True)

    assert ops._brew_service_is_registered("nyxgpt-api") is True


@pytest.mark.unit
def test_registration_is_not_probed_through_launchctl_off_macos(monkeypatch, tmp_path):
    """`brew services` drives systemd --user on Linux; there is no gui domain."""
    monkeypatch.setattr(ops, "_which", lambda _: "/home/linuxbrew/.linuxbrew/bin/brew")
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops.Path, "home", classmethod(lambda cls: tmp_path))
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="nyxgpt-api error 3 runner /x.plist")

    monkeypatch.setattr(ops, "_run", fake_run)

    assert ops._brew_service_is_registered("nyxgpt-api") is False
    assert not any(c[:1] == ["launchctl"] for c in calls), calls


@pytest.mark.unit
def test_brew_service_registration_reads_the_state_and_the_plist_brew_named(monkeypatch):
    """The File column *is* the registration, so it is read rather than guessed.

    `brew services list` pads the status of a crashed service with its exit
    code (`error  3`), so the file cannot be found by column index -- and the
    label scheme is brew's to change, so it cannot be derived from the
    formula name either.
    """
    monkeypatch.setattr(ops, "_which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd,
            0,
            stdout=(
                "Name               Status  User   File\n"
                "nyxgpt-api         error  3 runner ~/Library/LaunchAgents/"
                "homebrew.mxcl.nyxgpt-api.plist\n"
                "nyxgpt-api@3.0.0rc none\n"
            ),
        ),
    )

    state, plist = ops._brew_service_registration("nyxgpt-api")
    assert state == "error"
    assert plist is not None and plist.name == "homebrew.mxcl.nyxgpt-api.plist"
    assert "~" not in str(plist)

    # An unregistered keg names no file, and an unlisted formula is not there.
    assert ops._brew_service_registration("nyxgpt-api@3.0.0rc") == ("none", None)
    assert ops._brew_service_registration("nyxgpt-web") == ("none", None)


@pytest.mark.unit
def test_brew_service_registration_without_brew_is_not_registered(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda _: None)
    assert ops._brew_service_registration("nyxgpt-api") == ("none", None)
    assert ops._brew_service_is_registered("nyxgpt-api") is False


@pytest.mark.unit
def test_force_deregister_is_a_no_op_off_macos(monkeypatch):
    """brew services on Linux drive systemd; there is no plist to remove."""
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    assert ops._force_deregister_brew_service("nyxgpt-api") == []


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
def test_glitchtip_container_healthy_false_when_still_starting(monkeypatch):
    """Regression (#3588 review round 2): mirrors `_grafana_container_healthy`'s
    fix -- a freshly (re)started container reports `state=running,
    health=starting` throughout its healthcheck `start_period`, which
    `ComponentStatus.healthy` treats as healthy (intentionally, for the
    self-heal watchdog) but this caller must not.
    """
    status = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="running", health="starting", healthy=True
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [status])
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
def test_wait_for_glitchtip_healthy_polls_through_start_period(monkeypatch):
    """Regression (#3588 review round 2), mirrors the grafana version of this
    test: the initial check must not trust a `state=running, health=starting`
    snapshot as done -- keep polling until the healthcheck actually passes.
    """
    starting = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="running", health="starting", healthy=True
    )
    healthy = self_heal.ComponentStatus(
        service="glitchtip", container="c", state="running", health="healthy", healthy=True
    )
    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return [healthy] if calls["n"] >= 3 else [starting]

    monkeypatch.setattr(ops.self_heal, "list_component_status", fake_status)
    sleeps = []
    monkeypatch.setattr(ops.time, "sleep", lambda s: sleeps.append(s))

    assert ops._wait_for_glitchtip_healthy(timeout=5, poll_interval=0.01) is True
    assert calls["n"] >= 3
    assert sleeps, "must poll at least once instead of trusting the starting-state snapshot"


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
def test_grafana_container_healthy_false_when_still_starting(monkeypatch):
    """Regression (#3588 review round 2): a container freshly (re)started by
    `docker compose restart` immediately reports `state=running,
    health=starting` for its whole healthcheck `start_period` -- the exact
    shape `ComponentStatus.healthy` treats as healthy (by design, for the
    self-heal watchdog). `_grafana_container_healthy` must not: this was the
    root cause of `terraform-local-smoke`'s "grafana health -> 000000" CI
    failure -- `_wait_for_grafana_healthy` returned True instantly after the
    restart, before Grafana was actually reachable.
    """
    status = self_heal.ComponentStatus(
        service="grafana", container="c", state="running", health="starting", healthy=True
    )
    monkeypatch.setattr(ops.self_heal, "list_component_status", lambda: [status])
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
def test_wait_for_grafana_healthy_polls_through_start_period(monkeypatch):
    """Regression (#3588 review round 2): the initial check right after a
    `docker compose restart grafana` sees `state=running, health=starting`
    (the container's whole healthcheck `start_period`) -- must keep polling
    instead of returning True on that first look, or the caller reports the
    restart done while Grafana is still unreachable (the exact
    `terraform-local-smoke` "grafana health -> 000000" CI failure).
    """
    starting = self_heal.ComponentStatus(
        service="grafana", container="c", state="running", health="starting", healthy=True
    )
    healthy = self_heal.ComponentStatus(
        service="grafana", container="c", state="running", health="healthy", healthy=True
    )
    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return [healthy] if calls["n"] >= 3 else [starting]

    monkeypatch.setattr(ops.self_heal, "list_component_status", fake_status)
    sleeps = []
    monkeypatch.setattr(ops.time, "sleep", lambda s: sleeps.append(s))

    assert ops._wait_for_grafana_healthy(timeout=5, poll_interval=0.01) is True
    assert calls["n"] >= 3
    assert sleeps, "must poll at least once instead of trusting the starting-state snapshot"


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
        captured["env"] = k.get("env")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_run", fake_run)
    ops._glitchtip_ensure_superuser("admin@nyxgpt.local", "s3cr3t-pw")

    cmd = captured["cmd"]
    assert cmd[:4] == ["docker", "compose", "-f", str(ops.self_heal.COMPOSE_FILE)]
    assert "glitchtip" in cmd
    assert "createsuperuser" in cmd
    assert "--noinput" in cmd
    # The credentials are forwarded via bare `-e VAR` + the process
    # environment (CodeQL #105/#106): argv carries only the variable NAMES,
    # never the password value.
    assert "DJANGO_SUPERUSER_EMAIL" in cmd
    assert "DJANGO_SUPERUSER_PASSWORD" in cmd
    assert not any("s3cr3t-pw" in arg for arg in cmd)
    env = captured["env"]
    assert env["DJANGO_SUPERUSER_EMAIL"] == "admin@nyxgpt.local"
    assert env["DJANGO_SUPERUSER_PASSWORD"] == "s3cr3t-pw"
    assert env["DJANGO_SUPERUSER_USERNAME"] == "admin@nyxgpt.local"


def test_glitchtip_ensure_superuser_password_never_logged(caplog):
    # CodeQL #105/#106 regression: the idempotent rc=1 re-run logs at INFO
    # with the command in `extra` -- the password must not appear anywhere
    # in the log records (message or attributes) in any form.
    with patch.object(ops.subprocess, "run") as run:
        run.return_value = subprocess.CompletedProcess(
            args=["docker"],
            returncode=1,
            stdout="",
            stderr="Error: That email address is already taken.",
        )
        with caplog.at_level("DEBUG", logger="nyxgpt.ops"):
            result = ops._glitchtip_ensure_superuser("admin@nyxgpt.local", "s3cr3t-pw")

    assert result.ok
    for record in caplog.records:
        assert "s3cr3t-pw" not in record.getMessage()
        assert "s3cr3t-pw" not in repr(vars(record))


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
                200,
                json=[
                    {
                        "id": 1,
                        "label": ops.GLITCHTIP_TOKEN_NAME,
                        "token": "existing-token",
                        "scopes": ops.GLITCHTIP_TOKEN_SCOPES,
                    }
                ],
            )
        return httpx.Response(404)

    client = _mock_client("http://localhost:8080", handler)
    token, result = ops._glitchtip_ensure_api_token(client, "http://localhost:8080")
    assert token == "existing-token"
    assert result.ok
    assert "Reusing" in result.message
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_api_token_ignores_unlabeled_tokens():
    """A token whose `label` doesn't match (e.g. blank, from the pre-#3565-round-6
    bug where the code posted `name` instead of `label` and GlitchTip silently
    dropped it) must not be treated as a match -- it should mint a fresh,
    correctly labeled token instead of reusing an unrelated one."""

    posted = {}

    def handler(request):
        if request.url.path == "/api/0/api-tokens/" and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": 1, "label": "", "token": "unrelated-token", "scopes": []}]
            )
        if request.url.path == "/api/0/api-tokens/" and request.method == "POST":
            posted.update(json.loads(request.content))
            return httpx.Response(201, json={"token": "new-token"})
        return httpx.Response(404)

    client = _mock_client("http://localhost:8080", handler)
    token, result = ops._glitchtip_ensure_api_token(client, "http://localhost:8080")
    assert token == "new-token"
    assert result.ok
    assert posted.get("label") == ops.GLITCHTIP_TOKEN_NAME
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_api_token_replaces_stale_scopes():
    """A reused token missing a currently-required scope (e.g. minted before
    `team:write` was added) must be deleted and replaced -- GlitchTip's API
    has no token PUT/PATCH, so upgrading scopes means replacing it."""

    deleted_ids = []

    def handler(request):
        if request.url.path == "/api/0/api-tokens/" and request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "label": ops.GLITCHTIP_TOKEN_NAME,
                        "token": "stale-token",
                        "scopes": ["org:read"],
                    }
                ],
            )
        if request.url.path == "/api/0/api-tokens/7/" and request.method == "DELETE":
            deleted_ids.append(7)
            return httpx.Response(204)
        if request.url.path == "/api/0/api-tokens/" and request.method == "POST":
            return httpx.Response(201, json={"token": "fresh-token"})
        return httpx.Response(404)

    client = _mock_client("http://localhost:8080", handler)
    token, result = ops._glitchtip_ensure_api_token(client, "http://localhost:8080")
    assert token == "fresh-token"
    assert result.ok
    assert deleted_ids == [7]
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
def test_glitchtip_ensure_team_membership_joins_via_me_alias():
    """#3565 round 5 acceptance failure: a superuser who is an org member but
    not on any team sees "This organization has no projects" in the
    GlitchTip UI. `_glitchtip_ensure_team`'s own team-creation path already
    adds the creator, but a *pre-existing* team (created by an earlier/
    different admin) doesn't -- this must be called unconditionally to cover
    that case, using GlitchTip's `me` member-id alias so it never needs to
    look up an org-user id first."""
    posted_paths = []

    def handler(request):
        posted_paths.append((request.method, request.url.path))
        return httpx.Response(201, json={"slug": ops.GLITCHTIP_TEAM_SLUG, "isMember": True})

    client = _mock_client("http://localhost:8080", handler)
    result = ops._glitchtip_ensure_team_membership(
        client, ops.GLITCHTIP_ORG_SLUG, ops.GLITCHTIP_TEAM_SLUG
    )
    assert result.ok
    assert posted_paths == [
        (
            "POST",
            f"/api/0/organizations/{ops.GLITCHTIP_ORG_SLUG}/members/me/teams/"
            f"{ops.GLITCHTIP_TEAM_SLUG}/",
        )
    ]
    client.close()


@pytest.mark.unit
def test_glitchtip_ensure_team_membership_reports_failure():
    def handler(request):
        return httpx.Response(403, text="Permission denied")

    client = _mock_client("http://localhost:8080", handler)
    result = ops._glitchtip_ensure_team_membership(
        client, ops.GLITCHTIP_ORG_SLUG, ops.GLITCHTIP_TEAM_SLUG
    )
    assert not result.ok
    assert "403" in result.details
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
    # 644/755, not 600/700 (#3588): Grafana's container runs as non-root uid
    # 472, and a native Linux bind mount needs the file/dir world-readable
    # for that uid to stat/read it -- see _write_grafana_glitchtip_token.
    assert oct(token_path.stat().st_mode)[-3:] == "644"
    assert oct(token_path.parent.stat().st_mode)[-3:] == "755"


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
    # 644, not 600 (#3588): same cross-uid-readability reasoning as the
    # GlitchTip token -- see _write_grafana_glitchtip_token.
    assert oct(secret_path.stat().st_mode)[-3:] == "644"


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


# --- Grafana contact-point test path: `nyxgpt ops alert-test` (#3466, #3545) ---


@pytest.mark.unit
def test_grafana_receiver_k8s_name_matches_live_grafana_encoding():
    # Confirmed live against a booted grafana/grafana:13.1.1 (the pinned
    # image) provisioned with this repo's contact-points.yml (#3545): GET
    # /apis/notifications.alerting.grafana.app/v0alpha1/namespaces/default/receivers
    # lists the nyxgpt-slack contact point under this exact k8s resource name.
    assert ops._grafana_receiver_k8s_name("nyxgpt-slack") == "bnl4Z3B0LXNsYWNr"


@pytest.mark.unit
def test_send_grafana_test_alert_posts_to_receiver_test_api(monkeypatch):
    calls = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "success", "duration": "100ms"}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json=None):
            calls.append((path, json))
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._send_grafana_test_alert("http://localhost:3001", "admin-pw", True)

    assert result.ok is True
    path, body = calls[0]
    assert path == (
        "/apis/notifications.alerting.grafana.app/v0alpha1/namespaces/default/"
        "receivers/bnl4Z3B0LXNsYWNr/test"
    )
    assert body["integration"]["uid"] == "nyxgpt-slack-receiver"
    assert body["integration"]["type"] == "slack"
    assert body["integration"]["secureFields"] == {"url": True}
    assert body["alert"]["labels"]["alertname"] == "NyxGPTAlertTest"


@pytest.mark.unit
def test_send_grafana_test_alert_reports_failure_on_network_error(monkeypatch):
    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json=None):
            raise ops.httpx.ConnectError("connection refused")

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._send_grafana_test_alert("http://localhost:3001", "admin-pw", True)

    assert result.ok is False
    assert "Failed to reach Grafana's receiver-test API" in result.message


@pytest.mark.unit
def test_send_grafana_test_alert_includes_response_body_on_http_error(monkeypatch):
    class FakeResponse:
        status_code = 404
        text = '{"message":"Receiver not found"}'

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json=None):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._send_grafana_test_alert("http://localhost:3001", "admin-pw", True)

    assert result.ok is False
    assert "HTTP 404" in result.details
    assert "Receiver not found" in result.details


@pytest.mark.unit
def test_send_grafana_test_alert_fails_on_delivery_failure_when_webhook_configured(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {"status": "failure", "error": "failed to send Slack message: invalid_token"}

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json=None):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._send_grafana_test_alert("http://localhost:3001", "admin-pw", True)

    assert result.ok is False
    assert "Slack delivery test failed" in result.message
    assert "invalid_token" in result.details


@pytest.mark.unit
def test_send_grafana_test_alert_reports_pipeline_intact_when_webhook_unconfigured(monkeypatch):
    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "status": "failure",
                "error": "failed to send Slack message: failed incoming webhook: no_team",
            }

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, path, json=None):
            return FakeResponse()

    monkeypatch.setattr(ops.httpx, "Client", lambda **kwargs: FakeClient())

    result = ops._send_grafana_test_alert("http://localhost:3001", "admin-pw", False)

    assert result.ok is True
    assert "pipeline is intact" in result.message
    assert "no [monitoring] slack_webhook_url is configured" in result.message


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
def test_alert_test_sends_test_notification_when_monitoring_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "[monitoring]\nenabled = true\ngrafana_ui_url = http://localhost:3001\n"
        "grafana_admin_password = admin-pw\nslack_webhook_url = https://hooks.slack.com/x\n"
    )
    calls = []
    monkeypatch.setattr(
        ops,
        "_send_grafana_test_alert",
        lambda url, pw, webhook_configured: calls.append(webhook_configured)
        or ops.OpsResult(True, "sent"),
    )

    rc = ops.alert_test(MagicMock())

    assert rc == 0
    assert "sent" in capsys.readouterr().out
    assert calls == [True]


@pytest.mark.unit
def test_alert_test_passes_webhook_unconfigured_when_slack_webhook_url_unset(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "[monitoring]\nenabled = true\ngrafana_ui_url = http://localhost:3001\n"
        "grafana_admin_password = admin-pw\n"
    )
    calls = []
    monkeypatch.setattr(
        ops,
        "_send_grafana_test_alert",
        lambda url, pw, webhook_configured: calls.append(webhook_configured)
        or ops.OpsResult(True, "pipeline intact"),
    )

    rc = ops.alert_test(MagicMock())

    assert rc == 0
    assert calls == [False]


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
    # 755, not 700 (#3588): Grafana's non-root container uid needs to
    # traverse into this dir across a native Linux bind mount -- see
    # _ensure_glitchtip_secrets_dir.
    assert oct(secrets_dir.stat().st_mode)[-3:] == "755"


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
def test_ensure_glitchtip_secrets_dir_seeds_grafana_placeholders(tmp_path, monkeypatch):
    # #3538: Grafana 13.x crash-loops on a missing/empty $__file{} secret. The
    # preflight must seed valid, non-empty placeholders for both the GlitchTip
    # token and the Slack webhook so the observability bring-up doesn't take
    # Grafana down before the real values are written.
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    ops._ensure_glitchtip_secrets_dir()

    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    slack_path = tmp_path / ".nyxGPT" / "secrets" / "slack-webhook-url"
    assert token_path.exists()
    assert slack_path.exists()
    # Non-empty: an empty file also crashes Grafana (#3538).
    assert token_path.read_text(encoding="utf-8").strip()
    assert slack_path.read_text(encoding="utf-8").strip()


@pytest.mark.unit
def test_ensure_glitchtip_secrets_dir_does_not_clobber_existing_secrets(tmp_path, monkeypatch):
    # A real token/URL already on disk must survive the placeholder seeding.
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    secrets_dir = tmp_path / ".nyxGPT" / "secrets"
    secrets_dir.mkdir(parents=True, mode=0o700)
    (secrets_dir / "glitchtip-grafana-token").write_text("real-token", encoding="utf-8")
    (secrets_dir / "slack-webhook-url").write_text(
        "https://hooks.slack.com/services/REAL/REAL/REAL", encoding="utf-8"
    )

    ops._ensure_glitchtip_secrets_dir()

    assert (secrets_dir / "glitchtip-grafana-token").read_text(encoding="utf-8") == "real-token"
    assert "REAL" in (secrets_dir / "slack-webhook-url").read_text(encoding="utf-8")


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


def _write_error_tracking_config(path, *, enabled=True, dsn="http://key@localhost:8080/1"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[error_tracking]\nenabled = {'true' if enabled else 'false'}\ndsn = {dsn}\n"
        "glitchtip_ui_url = http://localhost:8080\n",
        encoding="utf-8",
    )


@pytest.mark.unit
def test_error_tracking_dsn_drift_issue_none_when_no_config(tmp_path):
    assert ops._error_tracking_dsn_drift_issue(tmp_path / "missing.ini") is None


@pytest.mark.unit
def test_error_tracking_dsn_drift_issue_none_when_disabled(tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_error_tracking_config(cfg_path, enabled=False)
    assert ops._error_tracking_dsn_drift_issue(cfg_path) is None


@pytest.mark.unit
def test_error_tracking_dsn_drift_issue_none_when_no_dsn(tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_error_tracking_config(cfg_path, dsn="")
    assert ops._error_tracking_dsn_drift_issue(cfg_path) is None


@pytest.mark.unit
def test_error_tracking_dsn_drift_issue_none_when_no_grafana_token(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / "config.ini"
    _write_error_tracking_config(cfg_path)
    assert ops._error_tracking_dsn_drift_issue(cfg_path) is None


@pytest.mark.unit
def test_error_tracking_dsn_drift_issue_none_when_key_still_live(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / "config.ini"
    _write_error_tracking_config(cfg_path, dsn="http://key@localhost:8080/1")
    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("tok", encoding="utf-8")

    def handler(request):
        return httpx.Response(200, json=[{"dsn": "http://key@localhost:8080/1"}])

    monkeypatch.setattr(
        ops, "_glitchtip_http_client", lambda base_url, **k: _mock_client(base_url, handler)
    )

    assert ops._error_tracking_dsn_drift_issue(cfg_path) is None


@pytest.mark.unit
def test_error_tracking_dsn_drift_issue_flags_stale_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / "config.ini"
    _write_error_tracking_config(cfg_path, dsn="http://stale-key@localhost:8080/1")
    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("tok", encoding="utf-8")

    def handler(request):
        return httpx.Response(200, json=[{"dsn": "http://current-key@localhost:8080/1"}])

    monkeypatch.setattr(
        ops, "_glitchtip_http_client", lambda base_url, **k: _mock_client(base_url, handler)
    )

    issue = ops._error_tracking_dsn_drift_issue(cfg_path)
    assert issue is not None
    assert "doesn't match any current GlitchTip key" in issue
    assert "nyxgpt ops glitchtip-init" in issue


@pytest.mark.unit
def test_error_tracking_dsn_drift_issue_none_when_glitchtip_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    cfg_path = tmp_path / "config.ini"
    _write_error_tracking_config(cfg_path)
    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    token_path.parent.mkdir(parents=True)
    token_path.write_text("tok", encoding="utf-8")

    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        ops, "_glitchtip_http_client", lambda base_url, **k: _mock_client(base_url, handler)
    )

    assert ops._error_tracking_dsn_drift_issue(cfg_path) is None


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
def test_restart_grafana_if_running_skips_when_absent(monkeypatch):
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    result = ops._restart_grafana_if_running()
    assert result.ok
    assert "not running" in result.message


@pytest.mark.unit
def test_restart_grafana_if_running_restarts_when_exited(monkeypatch):
    """Regression test (#3588): a crashed/exited Grafana container must still
    be restarted, not skipped -- `docker compose restart` handles a stopped
    container fine, and skipping here left Grafana dead after a from-scratch
    install crash-looped it before the GlitchTip token existed."""
    monkeypatch.setattr(ops, "_compose_available", lambda: True)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"grafana": "exited"})
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
def test_restart_native_api_if_running_skips_when_not_running(monkeypatch):
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {"nyxgpt-api": "none"})
    result = ops._restart_native_api_if_running()
    assert result.ok
    assert "not running natively" in result.message


@pytest.mark.unit
def test_restart_native_api_if_running_restarts_when_running(monkeypatch):
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {"nyxgpt-api": "started"})
    captured_cmd = {}

    def fake_run(cmd, check=False):
        captured_cmd["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(ops, "_run", fake_run)
    result = ops._restart_native_api_if_running("the new GlitchTip DSN")
    assert result.ok
    assert "Restarted nyxgpt-api" in result.message
    assert "the new GlitchTip DSN" in result.message
    assert captured_cmd["cmd"][-3:] == ["services", "restart", "nyxgpt-api"]


@pytest.mark.unit
def test_restart_native_api_if_running_reports_failure(monkeypatch):
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {"nyxgpt-api": "started"})
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/brew")
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, check=False: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    result = ops._restart_native_api_if_running()
    assert not result.ok
    assert "boom" in result.details


@pytest.mark.unit
def test_current_error_tracking_dsn_missing_file_returns_empty(tmp_path):
    assert ops._current_error_tracking_dsn(tmp_path / "missing.ini") == ""


@pytest.mark.unit
def test_current_error_tracking_dsn_reads_existing_value(tmp_path):
    cfg_path = tmp_path / "config.ini"
    cfg_path.write_text("[error_tracking]\ndsn = http://key@localhost:8080/1\n", encoding="utf-8")
    assert ops._current_error_tracking_dsn(cfg_path) == "http://key@localhost:8080/1"


@pytest.mark.unit
def test_containerized_error_tracking_dsn_rewrites_host_and_port():
    """#3565 round 5: GlitchTip mints DSNs from its browser-facing
    `GLITCHTIP_DOMAIN` (localhost) -- a containerized api can't reach that
    from inside the docker network, so the compose-facing copy must point at
    the `glitchtip` service instead, preserving the embedded key and project
    path."""
    dsn = "http://509fecaebca74ee68bcd4bd9d56dbe53@localhost:8080/1"
    assert (
        ops._containerized_error_tracking_dsn(dsn)
        == "http://509fecaebca74ee68bcd4bd9d56dbe53@glitchtip:8080/1"
    )


@pytest.mark.unit
def test_containerized_error_tracking_dsn_rewrites_nondefault_host_port():
    """The rewrite always targets the container-internal port (8080) even if
    the DSN's host-mapped port (`GLITCHTIP_UI_PORT`) differs -- the container
    network never sees the host port remap."""
    dsn = "http://key@localhost:19999/1"
    assert ops._containerized_error_tracking_dsn(dsn) == "http://key@glitchtip:8080/1"


@pytest.mark.unit
def test_containerized_error_tracking_dsn_empty_string_is_noop():
    assert ops._containerized_error_tracking_dsn("") == ""


@pytest.mark.unit
def test_containerized_error_tracking_dsn_unparseable_returns_unchanged():
    assert ops._containerized_error_tracking_dsn("not a url") == "not a url"


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
        "_glitchtip_ensure_team_membership",
        lambda client, org, team: ops.OpsResult(True, "membership"),
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

    restart_api_mock = MagicMock(return_value=ops.OpsResult(True, "Restarted nyxgpt-api"))
    monkeypatch.setattr(ops, "_restart_native_api_if_running", restart_api_mock)

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
    # #3565 round 5: the compose/container-facing copy gets its DSN host
    # rewritten to the `glitchtip` service's network alias -- a containerized
    # api can't reach the native config's browser-facing `localhost` DSN.
    assert compose_parser.get("error_tracking", "dsn") == "http://key@glitchtip:8080/1"
    assert compose_parser.get("error_tracking", "enabled") == "true"

    # The Infinity datasource's GlitchTip token is minted alongside the DSN
    # (#3411), never hand-pasted.
    token_path = tmp_path / ".nyxGPT" / "secrets" / "glitchtip-grafana-token"
    assert token_path.read_text(encoding="utf-8") == "tok"
    # 644, not 600 (#3588): see _write_grafana_glitchtip_token.
    assert oct(token_path.stat().st_mode)[-3:] == "644"

    # The native `nyxgpt-api` brew service reads config.ini's DSN once, at
    # process startup (#3470 acceptance failure) -- the DSN here changed
    # (empty -> "http://key@localhost:8080/1"), so a running nyxgpt-api must
    # be restarted to pick it up, mirroring the Grafana token restart above.
    restart_api_mock.assert_called_once()


@pytest.mark.unit
def test_provision_glitchtip_skips_native_api_restart_when_dsn_unchanged(monkeypatch, tmp_path):
    """#3470 acceptance failure: a `down`+`install` cycle against a preserved
    GlitchTip Postgres volume reuses the same org/project/key, so the DSN
    written to config.ini is unchanged -- restarting nyxgpt-api in that case
    would just be a needless bounce of a healthy process."""
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "[error_tracking]\nenabled = true\ndsn = http://key@localhost:8080/1\n",
        encoding="utf-8",
    )

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
        "_glitchtip_ensure_team_membership",
        lambda client, org, team: ops.OpsResult(True, "membership"),
    )
    monkeypatch.setattr(
        ops,
        "_glitchtip_ensure_project",
        lambda client, org, team: ("nyxgpt-backend", ops.OpsResult(True, "project")),
    )
    # Same DSN the project key already had -- an idempotent re-provisioning.
    monkeypatch.setattr(
        ops,
        "_glitchtip_ensure_project_key",
        lambda client, org, proj: ("http://key@localhost:8080/1", ops.OpsResult(True, "key")),
    )
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    restart_api_mock = MagicMock(return_value=ops.OpsResult(True, "Restarted nyxgpt-api"))
    monkeypatch.setattr(ops, "_restart_native_api_if_running", restart_api_mock)

    results = ops._provision_glitchtip()

    assert all(r.ok for r in results)
    restart_api_mock.assert_not_called()


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
    err = capsys.readouterr().err
    # Scoped to the flag, and carrying the pointer at the commands that DO
    # deploy to a cloud target -- never "cloud deployment is unimplemented"
    # (#3948).
    assert "not implemented for `ops install --terraform/--kubernetes`" in err
    assert ops.CLOUD_DEPLOY_POINTER in err


@pytest.mark.unit
def test_resolve_locality_defaults_to_local(capsys):
    """No locality flag is not an error: local is the default (#3948)."""
    args = SimpleNamespace(local=False, cloud=False)
    assert ops._resolve_locality(args) == "local"
    assert capsys.readouterr().err == ""


@pytest.mark.unit
def test_resolve_locality_accepts_explicit_local():
    """`--local` stays accepted, as a no-op, so existing scripts keep working."""
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
    assert 'auth_api_key = "my-key"' in content  # pragma: allowlist secret
    # The checkout path is NOT written here (#3835): repo_path is a dev-mode
    # `-var` on the apply, so the bootstrapped tfvars carries nothing that
    # ties the deployment to a repository.
    assert str(repo_root) not in content


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
def test_install_terraform_rejects_cloud_locality(capsys):
    args = SimpleNamespace(local=False, cloud=True, api_key=None)
    assert ops._install_terraform(args) == 2
    assert ops.CLOUD_DEPLOY_POINTER in capsys.readouterr().err


@pytest.mark.unit
def test_install_terraform_defaults_to_local_locality(monkeypatch):
    """No locality flag deploys locally rather than refusing (#3948)."""
    called = {}

    def fake_steps(api_key, dev=False):
        called["api_key"] = api_key
        return [ops.OpsResult(True, "install", "ok")]

    monkeypatch.setattr(ops, "_install_terraform_steps", fake_steps)
    args = SimpleNamespace(local=False, cloud=False, api_key="k", dev=False)
    assert ops._install_terraform(args) == 0
    assert called == {"api_key": "k"}


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
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_ensure_terraform_binary", return_value=ok) as b,
        patch.object(ops, "_ensure_terraform_tfvars", return_value=ok) as t,
        patch.object(ops, "_generate_compose_config", return_value=ok) as c,
        patch.object(ops, "_build_terraform_docker_images", return_value=ok),
        patch.object(ops, "_ensure_required_models", return_value=ok),
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok) as a,
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_sync_grafana_slack_webhook_secret", return_value=ok) as s,
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
    # ...and provision Grafana's Slack webhook secret before observability
    # starts, same as the native install() path (regression: #3588 round 4 --
    # Grafana's alerting provisioning crash-loops without this file present).
    s.assert_called_once()
    # ...and bring observability up on the terraform network + provision GlitchTip.
    o.assert_called_once()
    g.assert_called_once()
    h.assert_called_once()
    assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_install_terraform_syncs_slack_webhook_before_observability_starts(monkeypatch):
    """Regression (#3588 round 4): Grafana's alerting provisioning
    (docker/grafana/provisioning/alerting/contact-points.yml) unconditionally
    reads $__file{/etc/nyxgpt-secrets/slack-webhook-url} and refuses to boot
    if it's missing. The native install() path writes that secret via
    `_sync_grafana_slack_webhook_secret` before starting its observability
    stack; the terraform path must run the same step in the same order, or
    Grafana crash-loops on every from-scratch `nyxgpt ops install --terraform
    --local` (this is what turned the required `terraform-local-smoke` CI
    check red after the prior round's Grafana-restart fix unblocked it)."""
    args = SimpleNamespace(local=True, cloud=False, api_key="k")
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    call_order: list[str] = []
    with (
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_ensure_terraform_binary", return_value=ok),
        patch.object(ops, "_ensure_terraform_tfvars", return_value=ok),
        patch.object(ops, "_generate_compose_config", return_value=ok),
        patch.object(ops, "_build_terraform_docker_images", return_value=ok),
        patch.object(ops, "_ensure_required_models", return_value=ok),
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(
            ops,
            "_sync_grafana_slack_webhook_secret",
            side_effect=lambda: call_order.append("slack webhook secret") or ok,
        ),
        patch.object(
            ops,
            "_start_observability_stack_terraform",
            side_effect=lambda: call_order.append("observability stack") or ok,
        ),
        patch.object(ops, "_provision_glitchtip", return_value=ok),
        patch.object(ops, "_terraform_stack_health", return_value=ok),
    ):
        rc = ops._install_terraform(args)
    assert rc == 0
    assert call_order == ["slack webhook secret", "observability stack"]


@pytest.mark.unit
def test_install_terraform_stops_pipeline_on_step_failure(monkeypatch):
    args = SimpleNamespace(local=True, cloud=False, api_key=None)
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    with (
        patch.object(ops, "_sync_packaged_resources", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "migrate_legacy_volumes", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(
            ops, "_ensure_terraform_binary", return_value=[ops.OpsResult(False, "no terraform")]
        ),
        patch.object(ops, "_ensure_terraform_tfvars") as t,
        patch.object(ops, "_ensure_required_models") as m,
        patch.object(ops, "_terraform_init_plan_apply") as a,
        patch.object(ops, "_terraform_stack_health") as h,
    ):
        rc = ops._install_terraform(args)
    assert rc == 2
    t.assert_not_called()
    m.assert_not_called()
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
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_ensure_terraform_binary", return_value=ok),
        patch.object(ops, "_ensure_terraform_tfvars", return_value=ok),
        patch.object(ops, "_generate_compose_config", return_value=ok),
        patch.object(ops, "_build_terraform_docker_images", return_value=ok),
        patch.object(ops, "_ensure_required_models", return_value=ok),
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_sync_grafana_slack_webhook_secret", return_value=ok),
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


# --- Kubernetes: _ensure_kubectl_and_cluster (#3596: kind provisioning fallback) ---


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_installs_missing_kubectl(monkeypatch):
    """#3724: a missing kubectl is installed, not handed back to the operator."""
    installed = {"kubectl": False}

    def fake_which(prog):
        if prog == "kubectl":
            return "/tmp/bin/kubectl" if installed["kubectl"] else None
        return "/usr/local/bin/" + prog if prog in ("kind", "docker") else None

    def fake_ensure_kubectl():
        installed["kubectl"] = True
        return [ops.OpsResult(True, "Installed kubectl into /tmp/bin")]

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_ensure_kubectl_binary", fake_ensure_kubectl)
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0))
    results = ops._ensure_kubectl_and_cluster()
    assert all(r.ok for r in results)
    assert any("Installed kubectl" in r.message for r in results)
    assert any("Kubernetes cluster reachable" in r.message for r in results)


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_reports_kubectl_install_failure(monkeypatch):
    """When kubectl genuinely can't be installed, the failure is surfaced with a link."""
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(
        ops,
        "_ensure_kubectl_binary",
        lambda: [ops.OpsResult(False, "kubectl is missing and nyxgpt could not download it", "x")],
    )
    results = ops._ensure_kubectl_and_cluster()
    assert results[-1].ok is False
    assert "kubectl" in results[-1].message


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_reachable(monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/kubectl" if prog == "kubectl" else None
    )
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0))
    results = ops._ensure_kubectl_and_cluster()
    assert results[0].ok is True
    assert "Kubernetes cluster reachable" in results[0].message


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_installs_missing_kind(monkeypatch):
    """#3724: no cluster reachable and kind missing -- kind is installed and the
    cluster provisioned, instead of asking the operator to install kind first."""
    installed = {"kind": False}

    def fake_which(prog):
        if prog == "kind":
            return "/tmp/bin/kind" if installed["kind"] else None
        return "/usr/local/bin/" + prog if prog in ("kubectl", "docker") else None

    def fake_ensure_kind():
        installed["kind"] = True
        return [ops.OpsResult(True, "Installed kind into /tmp/bin")]

    calls = []

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        if cmd[:2] == ["kubectl", "cluster-info"]:
            return CP(returncode=1 if calls.count(cmd) == 1 else 0, stderr="refused")
        if cmd[:3] == ["kind", "get", "clusters"]:
            return CP(returncode=0, stdout="")
        if cmd[:3] == ["kind", "create", "cluster"]:
            return CP(returncode=0, stdout="created")
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return CP(returncode=0, stdout=ops.KIND_CONTEXT)
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_ensure_kind_binary", fake_ensure_kind)
    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._ensure_kubectl_and_cluster()
    assert all(r.ok for r in results)
    assert any("Installed kind" in r.message for r in results)
    assert ["kind", "create", "cluster", "--name", "nyxgpt-local", "--wait", "60s"] in calls


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_reports_kind_install_failure(monkeypatch):
    """If kind truly can't be installed, the error links to the installer -- no raw-tool
    instructions (CLAUDE.md's Operational Command Wrapping rule)."""
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/kubectl" if prog == "kubectl" else None
    )
    monkeypatch.setattr(
        ops,
        "_ensure_kind_binary",
        lambda: [
            ops.OpsResult(
                False,
                "kind is missing and nyxgpt could not download it",
                "Install it manually: https://kind.sigs.k8s.io/#installation",
            )
        ],
    )
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="refused")
    )
    results = ops._ensure_kubectl_and_cluster()
    assert results[-1].ok is False
    assert "could not download it" in results[-1].message
    assert "https://kind.sigs.k8s.io/#installation" in results[-1].details


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_unreachable_no_docker(monkeypatch):
    """kind is installed but Docker (which kind needs to create a cluster) is not."""
    monkeypatch.setattr(
        ops,
        "_which",
        lambda prog: "/usr/local/bin/" + prog if prog in ("kubectl", "kind") else None,
    )
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="refused")
    )
    results = ops._ensure_kubectl_and_cluster()
    assert results[0].ok is False
    assert "kind needs Docker" in results[0].message


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_provisions_kind_when_absent(monkeypatch):
    """No reachable cluster, kind+docker present, no existing nyxgpt-local cluster --
    provisions one from scratch (#3596, owner decision 2026-08-03)."""
    monkeypatch.setattr(
        ops,
        "_which",
        lambda prog: "/usr/local/bin/" + prog if prog in ("kubectl", "kind", "docker") else None,
    )
    calls = []

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        if cmd[:2] == ["kubectl", "cluster-info"]:
            # First call (before provisioning): unreachable. Second call (after
            # kind create): reachable.
            return CP(returncode=1 if calls.count(cmd) == 1 else 0, stderr="refused")
        if cmd[:3] == ["kind", "get", "clusters"]:
            return CP(returncode=0, stdout="")
        if cmd[:3] == ["kind", "create", "cluster"]:
            return CP(returncode=0, stdout="created")
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return CP(returncode=0, stdout=ops.KIND_CONTEXT)
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._ensure_kubectl_and_cluster()
    assert all(r.ok for r in results)
    assert any("Created local kind cluster: nyxgpt-local" in r.message for r in results)
    assert ["kind", "create", "cluster", "--name", "nyxgpt-local", "--wait", "60s"] in calls


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_reuses_existing_kind_cluster(monkeypatch):
    """A `nyxgpt-local` kind cluster from a previous run is reused, not recreated."""
    monkeypatch.setattr(
        ops,
        "_which",
        lambda prog: "/usr/local/bin/" + prog if prog in ("kubectl", "kind", "docker") else None,
    )
    calls = []

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        if cmd[:2] == ["kubectl", "cluster-info"]:
            return CP(returncode=1 if calls.count(cmd) == 1 else 0, stderr="refused")
        if cmd[:3] == ["kind", "get", "clusters"]:
            return CP(returncode=0, stdout="nyxgpt-local\n")
        if cmd[:3] == ["kubectl", "config", "use-context"]:
            return CP(returncode=0)
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return CP(returncode=0, stdout=ops.KIND_CONTEXT)
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._ensure_kubectl_and_cluster()
    assert all(r.ok for r in results)
    assert any("Reusing existing kind cluster" in r.message for r in results)
    assert not any(cmd[:3] == ["kind", "create", "cluster"] for cmd in calls)


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_provision_failure_reported(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_which",
        lambda prog: "/usr/local/bin/" + prog if prog in ("kubectl", "kind", "docker") else None,
    )

    def fake_run(cmd, check=True, **_k):
        if cmd[:2] == ["kubectl", "cluster-info"]:
            return CP(returncode=1, stderr="refused")
        if cmd[:3] == ["kind", "get", "clusters"]:
            return CP(returncode=0, stdout="")
        if cmd[:3] == ["kind", "create", "cluster"]:
            return CP(returncode=1, stderr="docker daemon not running")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._ensure_kubectl_and_cluster()
    assert results[-1].ok is False
    assert "kind create cluster" in results[-1].message


# --- Kubernetes: CLI tool auto-install (#3724) ---


@pytest.mark.unit
def test_tool_platform_maps_supported_hosts(monkeypatch):
    monkeypatch.setattr(ops.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(ops.platform, "machine", lambda: "arm64")
    assert ops._tool_platform() == ("darwin", "arm64")
    monkeypatch.setattr(ops.platform, "system", lambda: "Linux")
    monkeypatch.setattr(ops.platform, "machine", lambda: "x86_64")
    assert ops._tool_platform() == ("linux", "amd64")


@pytest.mark.unit
def test_tool_platform_none_on_unsupported_host(monkeypatch):
    monkeypatch.setattr(ops.platform, "system", lambda: "Windows")
    monkeypatch.setattr(ops.platform, "machine", lambda: "AMD64")
    assert ops._tool_platform() is None


@pytest.mark.unit
def test_ensure_nyxgpt_bin_on_path_is_idempotent(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(ops, "NYXGPT_BIN_DIR", bin_dir)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert ops._ensure_nyxgpt_bin_on_path() == bin_dir
    assert os.environ["PATH"].split(os.pathsep)[0] == str(bin_dir)
    ops._ensure_nyxgpt_bin_on_path()
    assert os.environ["PATH"].split(os.pathsep).count(str(bin_dir)) == 1


class _FakeStream:
    """Minimal stand-in for the context manager `httpx.stream` returns."""

    def __init__(self, chunks=(b"binary",), error=None):
        self._chunks = chunks
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def iter_bytes(self):
        yield from self._chunks


@pytest.mark.unit
def test_download_tool_binary_writes_executable(monkeypatch, tmp_path):
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(ops, "NYXGPT_BIN_DIR", bin_dir)
    monkeypatch.setattr(ops.httpx, "stream", lambda *a, **k: _FakeStream((b"ki", b"nd")))
    ok, details = ops._download_tool_binary("kind", "https://example.invalid/kind")
    assert ok is True
    dest = bin_dir / "kind"
    assert dest.read_bytes() == b"kind"
    assert os.access(dest, os.X_OK)
    assert "https://example.invalid/kind" in details


@pytest.mark.unit
def test_download_tool_binary_cleans_up_on_failure(monkeypatch, tmp_path):
    """A failed download leaves no partial (and never an executable) binary behind."""
    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(ops, "NYXGPT_BIN_DIR", bin_dir)
    monkeypatch.setattr(
        ops.httpx,
        "stream",
        lambda *a, **k: _FakeStream(error=httpx.HTTPError("404 not found")),
    )
    ok, details = ops._download_tool_binary("kind", "https://example.invalid/kind")
    assert ok is False
    assert "HTTPError" in details
    assert not (bin_dir / "kind").exists()
    assert list(bin_dir.glob(".*download")) == []


@pytest.mark.unit
def test_kubectl_download_url_uses_stable_version(monkeypatch):
    monkeypatch.setattr(
        ops.httpx,
        "get",
        lambda *a, **k: httpx.Response(200, text="v1.31.0\n", request=httpx.Request("GET", a[0])),
    )
    url = ops._kubectl_download_url("linux", "arm64")
    assert url == "https://dl.k8s.io/release/v1.31.0/bin/linux/arm64/kubectl"


@pytest.mark.unit
def test_ensure_cli_tool_noop_when_already_installed(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kind")
    monkeypatch.setattr(
        ops, "_run", lambda *a, **k: pytest.fail("must not install an already-present tool")
    )
    results = ops._ensure_kind_binary()
    assert results[0].ok is True
    assert "already installed" in results[0].message


@pytest.mark.unit
def test_ensure_cli_tool_installs_via_brew(monkeypatch):
    """Homebrew is preferred when present so the tool stays operator-upgradable."""
    state = {"installed": False}

    def fake_which(prog):
        if prog == "brew":
            return "/opt/homebrew/bin/brew"
        if prog == "kind":
            return "/opt/homebrew/bin/kind" if state["installed"] else None
        return None

    calls = []

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        state["installed"] = True
        return CP(returncode=0, stdout="poured")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._ensure_kind_binary()
    assert results[0].ok is True
    assert "via Homebrew" in results[0].message
    assert calls == [["brew", "install", "kind"]]


@pytest.mark.unit
def test_ensure_cli_tool_downloads_when_no_brew(monkeypatch, tmp_path):
    """The Linux/no-Homebrew path: fetch the official release binary into ~/.nyxGPT/bin."""
    state = {"installed": False}
    monkeypatch.setattr(
        ops,
        "_which",
        lambda prog: (
            (tmp_path / "kind").as_posix() if prog == "kind" and state["installed"] else None
        ),
    )
    monkeypatch.setattr(ops, "_tool_platform", lambda: ("linux", "amd64"))
    downloaded = {}

    def fake_download(name, url):
        downloaded["name"], downloaded["url"] = name, url
        state["installed"] = True
        return True, f"downloaded {url}"

    monkeypatch.setattr(ops, "_download_tool_binary", fake_download)
    results = ops._ensure_kind_binary()
    assert results[0].ok is True
    assert "Installed kind into" in results[0].message
    assert downloaded["name"] == "kind"
    assert downloaded["url"] == (
        "https://github.com/kubernetes-sigs/kind/releases/latest/download/kind-linux-amd64"
    )


@pytest.mark.unit
def test_ensure_cli_tool_falls_back_to_download_when_brew_fails(monkeypatch):
    """A broken/absent formula doesn't strand the install -- the direct download still runs."""
    state = {"installed": False}

    def fake_which(prog):
        if prog == "brew":
            return "/opt/homebrew/bin/brew"
        if prog == "kind":
            return "/tmp/bin/kind" if state["installed"] else None
        return None

    def fake_download(name, url):
        state["installed"] = True
        return True, f"downloaded {url}"

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="No available formula")
    )
    monkeypatch.setattr(ops, "_tool_platform", lambda: ("darwin", "arm64"))
    monkeypatch.setattr(ops, "_download_tool_binary", fake_download)
    results = ops._ensure_kind_binary()
    assert results[0].ok is True
    assert "No available formula" in results[0].details


@pytest.mark.unit
def test_ensure_cli_tool_reports_download_failure_with_manual_link(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(ops, "_tool_platform", lambda: ("linux", "amd64"))
    monkeypatch.setattr(ops, "_download_tool_binary", lambda name, url: (False, "boom"))
    results = ops._ensure_kubectl_binary()
    assert results[0].ok is False
    assert "could not download it" in results[0].message
    assert "https://kubernetes.io/docs/tasks/tools/" in results[0].details


@pytest.mark.unit
def test_ensure_cli_tool_reports_url_resolution_failure(monkeypatch):
    """kubectl's stable-version lookup failing is reported, not raised."""
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(ops, "_tool_platform", lambda: ("linux", "amd64"))

    def boom(system, arch):
        raise httpx.ConnectError("dns failure")

    monkeypatch.setattr(ops, "_kubectl_download_url", boom)
    results = ops._ensure_kubectl_binary()
    assert results[0].ok is False
    assert "ConnectError" in results[0].details


@pytest.mark.unit
def test_ensure_cli_tool_unsupported_platform(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(ops, "_tool_platform", lambda: None)
    monkeypatch.setattr(
        ops, "_download_tool_binary", lambda name, url: pytest.fail("no asset to download")
    )
    results = ops._ensure_kind_binary()
    assert results[0].ok is False
    assert "cannot install it here" in results[0].message
    assert "https://kind.sigs.k8s.io/#installation" in results[0].details


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
    # dev=True is the working-tree build -- what every k8s install did before
    # #3834, and what `--dev` now asks for explicitly.
    results = ops._build_and_load_k8s_web_image(dev=True)

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
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "nyxgpt-api-stable-abc"},
                                "status": {
                                    "phase": "Running",
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                },
                            },
                            {
                                "metadata": {"name": "nyxgpt-api-canary-def"},
                                "status": {
                                    "phase": "Failed",
                                    "message": "evicted",
                                },
                            },
                        ]
                    }
                ),
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
def test_k8s_stack_health_names_why_a_pod_is_pending(monkeypatch):
    """#3832: `pod x: Pending` told the operator nothing actionable.

    A Pod the scheduler refused must fail the snapshot *and* carry the
    scheduler's own message (via #3827's `_classify_k8s_pod` vocabulary), so
    the operator reads the cause instead of a bare phase.
    """

    def fake_run(cmd, check=True, **_k):
        if cmd[4] == "pods":
            return CP(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "prometheus-abc"},
                                "status": {
                                    "phase": "Pending",
                                    "conditions": [
                                        {
                                            "type": "PodScheduled",
                                            "status": "False",
                                            "reason": "Unschedulable",
                                            "message": "0/1 nodes are available: 1 "
                                            "Insufficient memory.",
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ),
            )
        return CP(returncode=0, stdout="")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._k8s_stack_health()
    pod_result = next(r for r in results if r.message.startswith("pod "))

    assert pod_result.ok is False
    assert "unschedulable" in pod_result.message.lower()
    assert "Insufficient memory" in pod_result.details


@pytest.mark.unit
def test_k8s_stack_health_reports_web_service_alongside_api(monkeypatch):
    """#3419: the post-apply health snapshot must check nyxgpt-web too, distinguishing
    found from not-found per service rather than reporting one combined result."""

    def fake_run(cmd, check=True, **_k):
        if cmd[4] == "pods":
            return CP(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "nyxgpt-web-stable-abc"},
                                "status": {
                                    "phase": "Running",
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                },
                            }
                        ]
                    }
                ),
            )
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


@pytest.mark.unit
def test_k8s_stack_health_checks_data_and_llm_services(monkeypatch):
    """#3786: api/web Services present while `cassandra`/`ollama` are missing is
    exactly the shape of the reported failure (Pods Running, no chat possible),
    so the snapshot must report each of the four separately."""
    checked: list[str] = []

    def fake_run(cmd, check=True, **_k):
        if cmd[4] == "pods":
            return CP(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "nyxgpt-api-stable-abc"},
                                "status": {
                                    "phase": "Running",
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                },
                            }
                        ]
                    }
                ),
            )
        if cmd[4] == "svc":
            checked.append(cmd[5])
            return CP(returncode=1) if cmd[5] in ("cassandra", "ollama") else CP(returncode=0)
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._k8s_stack_health()
    assert checked == ["nyxgpt-api", "nyxgpt-web", "cassandra", "ollama"]
    assert any(not r.ok and "Service cassandra not found" in r.message for r in results)
    assert any(not r.ok and "Service ollama not found" in r.message for r in results)


# --- Kubernetes: _wait_for_k8s_data_tier (#3786) ---


@pytest.mark.unit
def test_wait_for_k8s_data_tier_waits_for_both_statefulsets(monkeypatch):
    """The install must not report a healthy stack while Cassandra is still
    bootstrapping and Ollama is still pulling the default model."""
    calls: list[list[str]] = []

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        return CP(returncode=0, stdout="rolled out")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._wait_for_k8s_data_tier()
    # The wait also reads each workload's label selector (#3827), so filter to
    # the `rollout status` calls this test is about.
    rollouts = [c for c in calls if "rollout" in c]
    assert [c[5] for c in rollouts] == ["statefulset/cassandra", "statefulset/ollama"]
    assert all(c[1] == "-n" and c[2] == ops.K8S_NAMESPACE for c in rollouts)
    assert all(c[3:5] == ["rollout", "status"] for c in rollouts)
    assert all(any(a.startswith("--timeout=") for a in c) for c in rollouts)
    assert all(r.ok for r in results)


@pytest.mark.unit
def test_wait_for_k8s_data_tier_timeout_is_a_failure(monkeypatch):
    """A data tier that never came up cannot serve chat -- reporting that as a
    warning (or not at all) is what produced #3786."""
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="timed out")
    )
    results = ops._wait_for_k8s_data_tier()
    assert len(results) == 1  # stops at the first failing workload
    assert results[0].ok is False
    assert "Cassandra" in results[0].message
    assert "nyxgpt ops status" in (results[0].details or "")


@pytest.mark.unit
def test_wait_for_k8s_data_tier_reports_the_failing_workload(monkeypatch):
    """Ollama failing must name Ollama, not the tier in general."""

    def fake_run(cmd, check=True, **_k):
        if cmd[5] == "statefulset/ollama":
            return CP(returncode=1, stderr="timed out")
        return CP(returncode=0, stdout="rolled out")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._wait_for_k8s_data_tier()
    assert results[0].ok is True
    assert results[-1].ok is False
    assert "Ollama" in results[-1].message


# --- Kubernetes: _wait_for_k8s_app_tier (#3827) ---


@pytest.mark.unit
def test_wait_for_k8s_app_tier_waits_for_the_stable_deployments(monkeypatch):
    """The app tier had no wait at all: health was snapshotted seconds after
    `kubectl apply`, so its verdict described a rollout rather than a stack."""
    calls: list[list[str]] = []

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        return CP(returncode=0, stdout="rolled out")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._wait_for_k8s_app_tier()

    rollouts = [c for c in calls if "rollout" in c]
    assert [c[5] for c in rollouts] == ["deploy/nyxgpt-api-stable", "deploy/nyxgpt-web-stable"]
    assert all(r.ok for r in results)


@pytest.mark.unit
def test_wait_for_k8s_app_tier_skips_the_zero_replica_canaries(monkeypatch):
    """The canary halves ship at zero replicas until `nyxgpt canary start`
    scales them up -- waiting on them would wait for Pods nobody asked for."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: (calls.append(cmd), CP(returncode=0))[1]
    )
    ops._wait_for_k8s_app_tier()

    assert calls and not any("canary" in arg for c in calls for arg in c)


@pytest.mark.unit
def test_install_kubernetes_waits_for_the_app_tier_before_reading_health():
    """Ordering is the fix (#3827): every wait runs before the snapshot, so the
    install's exit status is about the settled stack, not a mid-rollout one."""
    order: list[str] = []
    ok = [ops.OpsResult(True, "ok")]

    def record(name):
        return lambda: (order.append(name), ok)[1]

    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_clear_intentional_stops", return_value=ok),
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_image", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_wait_for_k8s_data_tier", side_effect=record("data")),
        patch.object(ops, "_wait_for_k8s_app_tier", side_effect=record("app")),
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok),
        patch.object(ops, "_wait_for_k8s_observability", side_effect=record("observability")),
        patch.object(ops, "_k8s_stack_health", side_effect=record("health")),
        patch.object(ops, "_k8s_observability_health", return_value=ok),
        patch.object(ops, "_record_ops_action"),
    ):
        results = ops._install_kubernetes_steps(None)

    assert all(r.ok for r in results)
    assert order == ["data", "app", "observability", "health"]


@pytest.mark.unit
def test_install_kubernetes_does_not_fail_on_a_pod_that_is_merely_pending(monkeypatch):
    """End to end over the reported run (#3827): with the waits satisfied, the
    Pods still finishing their startup must not make the command exit 2."""
    ok = [ops.OpsResult(True, "ok")]
    pods = {
        "items": [
            {
                "metadata": {"name": "grafana-1"},
                "status": {
                    "phase": "Pending",
                    "containerStatuses": [{"state": {"waiting": {"reason": "ContainerCreating"}}}],
                },
            }
        ]
    }

    def fake_run(cmd, check=True, **_k):
        if "pods" in cmd:
            return CP(returncode=0, stdout=json.dumps(pods))
        return CP(returncode=0, stdout="")

    monkeypatch.setattr(ops, "_run", fake_run)
    with (
        patch.object(ops, "_refuse_port_collision", return_value=None),
        patch.object(ops, "_clear_intentional_stops", return_value=ok),
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_image", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=ok),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=ok),
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok),
        patch.object(ops, "_wait_for_k8s_observability", return_value=ok),
        patch.object(ops, "_k8s_observability_health", return_value=ok),
        patch.object(ops, "_record_ops_action"),
    ):
        results = ops._install_kubernetes_steps(None)

    assert all(r.ok for r in results), [r.message for r in results if not r.ok]
    assert any(ops._result_status_label(r) == "PENDING" for r in results)


# --- Kubernetes: _install_kubernetes / _down_kubernetes ---


@pytest.mark.unit
def test_install_kubernetes_rejects_cloud_locality(capsys):
    args = SimpleNamespace(local=False, cloud=True, api_key=None)
    assert ops._install_kubernetes(args) == 2
    assert ops.CLOUD_DEPLOY_POINTER in capsys.readouterr().err


@pytest.mark.unit
def test_install_kubernetes_defaults_to_local_locality(monkeypatch):
    """No locality flag deploys to the local cluster rather than refusing (#3948)."""
    called = {}

    def fake_steps(api_key, skip_observability=False, dev=False):
        called["api_key"] = api_key
        return [ops.OpsResult(True, "install", "ok")]

    monkeypatch.setattr(ops, "_install_kubernetes_steps", fake_steps)
    args = SimpleNamespace(
        local=False, cloud=False, api_key="k", dev=False, skip_observability=False
    )
    assert ops._install_kubernetes(args) == 0
    assert called == {"api_key": "k"}


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
        patch.object(ops, "_build_and_load_k8s_api_image", return_value=ok) as b,
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok) as bw,
        patch.object(ops, "_ensure_k8s_secret", return_value=ok) as s,
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok) as a,
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=ok) as w,
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=ok),
        patch.object(ops, "_k8s_stack_health", return_value=ok) as h,
        # The observability layer this mode now deploys too (#3787).
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok) as o,
        # Its rollout wait (#3826) shells out to kubectl for real otherwise.
        patch.object(ops, "_wait_for_k8s_observability", return_value=ok),
        patch.object(ops, "_k8s_observability_health", return_value=ok),
    ):
        rc = ops._install_kubernetes(args)
    assert rc == 0
    o.assert_called_once()
    c.assert_called_once()
    b.assert_called_once()
    bw.assert_called_once()
    s.assert_called_once_with("k")
    a.assert_called_once()
    w.assert_called_once()
    h.assert_called_once()
    assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_install_kubernetes_clears_intentional_stop_markers_for_api_and_web(monkeypatch, capsys):
    """Kubernetes manages both `api` and `web` (#3419) -- both intentional-stop
    markers must be cleared, and only those two: the markers track the *native*
    host services, and the Kubernetes deployment's Cassandra/Ollama run inside
    the cluster (#3786), not as host services this install ever started."""
    args = SimpleNamespace(local=True, cloud=False, api_key="k")
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_ensure_kubectl_and_cluster", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_api_image", return_value=ok),
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok),
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=ok),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=ok),
        patch.object(ops, "_k8s_stack_health", return_value=ok),
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok),
        # Its rollout wait (#3826) shells out to kubectl for real otherwise.
        patch.object(ops, "_wait_for_k8s_observability", return_value=ok),
        patch.object(ops, "_k8s_observability_health", return_value=ok),
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
        patch.object(ops, "_build_and_load_k8s_api_image") as b,
        patch.object(ops, "_build_and_load_k8s_web_image") as bw,
        patch.object(ops, "_ensure_k8s_secret") as s,
        patch.object(ops, "_kubectl_apply_kustomization") as a,
        patch.object(ops, "_wait_for_k8s_data_tier") as w,
        patch.object(ops, "_wait_for_k8s_app_tier"),
        patch.object(ops, "_k8s_stack_health") as h,
    ):
        rc = ops._install_kubernetes(args)
    assert rc == 2
    b.assert_not_called()
    bw.assert_not_called()
    s.assert_not_called()
    a.assert_not_called()
    w.assert_not_called()
    h.assert_not_called()


@pytest.mark.unit
def test_down_kubernetes_no_kubectl(monkeypatch, capsys):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    rc = ops._down_kubernetes(SimpleNamespace())
    assert rc == 2
    assert "nothing to tear down" in capsys.readouterr().out


def _bootstrap_k8s_dirs(monkeypatch, tmp_path):
    """Point `K8S_DIR`/`K8S_OBSERVABILITY_DIR` at bootstrapped tmp copies.

    Both teardown paths skip their `kubectl delete` when the (gitignored)
    `secret.yaml` is missing -- a real state since `ops observability
    --kubernetes` can deploy the layer with no app tier (#3787). A test that
    wants the delete to actually run therefore has to supply those files, or
    it silently asserts against the "nothing to do" branch and passes or
    fails depending on whether the developer's checkout happens to be
    bootstrapped.
    """
    app_dir = tmp_path / "k8s"
    observability_dir = app_dir / "observability"
    observability_dir.mkdir(parents=True)
    (app_dir / "secret.yaml").write_text("stringData: {}\n", encoding="utf-8")
    (observability_dir / "secret.yaml").write_text("stringData: {}\n", encoding="utf-8")
    monkeypatch.setattr(ops, "K8S_DIR", app_dir)
    monkeypatch.setattr(ops, "K8S_OBSERVABILITY_DIR", observability_dir)
    return app_dir, observability_dir


@pytest.mark.unit
def test_down_kubernetes_delete_success(monkeypatch, tmp_path):
    _bootstrap_k8s_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="deleted")
    )
    rc = ops._down_kubernetes(SimpleNamespace())
    assert rc == 0


@pytest.mark.unit
def test_down_kubernetes_delete_failure(monkeypatch, tmp_path):
    _bootstrap_k8s_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=1, stderr="boom"))
    rc = ops._down_kubernetes(SimpleNamespace())
    assert rc == 2


# --- Kubernetes: kind cluster provision/teardown helpers (#3596) ---


@pytest.mark.unit
def test_kind_cluster_exists_true(monkeypatch):
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="nyxgpt-local\nother\n")
    )
    assert ops._kind_cluster_exists() is True


@pytest.mark.unit
def test_kind_cluster_exists_false(monkeypatch):
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout=""))
    assert ops._kind_cluster_exists() is False


@pytest.mark.unit
def test_delete_kind_cluster_absent_is_a_noop_success(monkeypatch):
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout=""))
    results = ops._delete_kind_cluster()
    assert results[0].ok is True
    assert "already absent" in results[0].message


@pytest.mark.unit
def test_delete_kind_cluster_success(monkeypatch):
    def fake_run(cmd, check=True, **_k):
        if cmd[:3] == ["kind", "get", "clusters"]:
            return CP(returncode=0, stdout="nyxgpt-local\n")
        if cmd[:3] == ["kind", "delete", "cluster"]:
            return CP(returncode=0, stdout="deleted")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._delete_kind_cluster()
    assert results[0].ok is True
    assert "Deleted local kind cluster" in results[0].message


@pytest.mark.unit
def test_down_kubernetes_deletes_provisioned_kind_cluster(monkeypatch):
    """#3596: tearing down a deployment on the nyxgpt-provisioned kind cluster also
    deletes that cluster."""
    calls = []

    def fake_which(prog):
        return "/usr/local/bin/" + prog if prog in ("kubectl", "kind") else None

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        if cmd[:3] == ["kubectl", "delete", "-k"]:
            return CP(returncode=0, stdout="deleted")
        if cmd[:2] == ["kubectl", "config"]:
            return CP(returncode=0, stdout=ops.KIND_CONTEXT)
        if cmd[:3] == ["kind", "get", "clusters"]:
            return CP(returncode=0, stdout="nyxgpt-local\n")
        if cmd[:3] == ["kind", "delete", "cluster"]:
            return CP(returncode=0, stdout="deleted")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops.down_kubernetes()
    assert all(r.ok for r in results)
    assert any("Deleted local kind cluster" in r.message for r in results)
    assert ["kind", "delete", "cluster", "--name", "nyxgpt-local"] in calls


@pytest.mark.unit
def test_down_kubernetes_never_deletes_bring_your_own_cluster(monkeypatch):
    """A non-nyxgpt context (minikube, Docker Desktop, an operator's own kind cluster
    under a different name) must never be deleted by `nyxgpt ops down --kubernetes`."""
    calls = []

    def fake_which(prog):
        return "/usr/local/bin/" + prog if prog in ("kubectl", "kind") else None

    def fake_run(cmd, check=True, **_k):
        calls.append(cmd)
        if cmd[:3] == ["kubectl", "delete", "-k"]:
            return CP(returncode=0, stdout="deleted")
        if cmd[:2] == ["kubectl", "config"]:
            return CP(returncode=0, stdout="minikube")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops.down_kubernetes()
    assert all(r.ok for r in results)
    assert not any(cmd[:3] == ["kind", "delete", "cluster"] for cmd in calls)


# --- Structured (non-printing) Terraform/Kubernetes functions for the SRE/admin dashboard API ---


@pytest.mark.unit
def test_install_terraform_local_runs_steps_and_returns_results(monkeypatch):
    monkeypatch.setattr(ops, "_refuse_port_collision", lambda components: None)
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok),
        patch.object(ops, "_ensure_terraform_binary", return_value=ok),
        patch.object(ops, "_sync_local_terraform_config", return_value=ok),
        patch.object(ops, "_ensure_terraform_tfvars", return_value=ok) as t,
        patch.object(ops, "_generate_compose_config", return_value=ok),
        # The dashboard's bring-up is the artifact path (#3835): it pulls
        # published images and never builds from a checkout.
        patch.object(
            ops, "_pull_terraform_published_images", return_value=({"api": "i", "web": "i"}, ok)
        ),
        patch.object(ops, "_ensure_required_models", return_value=ok),
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok),
        patch.object(ops, "_ensure_glitchtip_secrets_dir", return_value=ok),
        patch.object(ops, "_sync_grafana_slack_webhook_secret", return_value=ok),
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
        patch.object(ops, "_build_and_load_k8s_api_image", return_value=ok),
        # Patched, not left real: these two shell out to `docker`/`kubectl`,
        # so an unpatched step makes this unit test pass or fail on what the
        # machine running it happens to have (and on the state of any cluster
        # it happens to reach).
        patch.object(ops, "_build_and_load_k8s_web_image", return_value=ok),
        patch.object(ops, "_ensure_k8s_secret", return_value=ok) as s,
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok),
        patch.object(ops, "_wait_for_k8s_data_tier", return_value=ok),
        patch.object(ops, "_wait_for_k8s_app_tier", return_value=ok),
        patch.object(ops, "_k8s_stack_health", return_value=ok),
        # The in-cluster observability layer is part of this bring-up now
        # (#3787) -- unpatched, these steps would shell out to kubectl.
        patch.object(ops, "_sync_packaged_resources", return_value=ok),
        patch.object(ops, "_apply_k8s_observability", return_value=ok),
        # Its rollout wait (#3826) shells out to kubectl for real otherwise.
        patch.object(ops, "_wait_for_k8s_observability", return_value=ok),
        patch.object(ops, "_k8s_observability_health", return_value=ok),
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
def test_down_kubernetes_returns_results_without_printing(monkeypatch, capsys, tmp_path):
    _bootstrap_k8s_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True, **_k: CP(returncode=0, stdout="deleted")
    )
    results = ops.down_kubernetes()
    # The app-tier kustomization, then the observability overlay (#3787),
    # then the install-mode record for the deployment just removed (#3834).
    assert [r.ok for r in results] == [True, True, True]
    assert "k8s/observability/" in results[1].message
    assert "install-mode record" in results[2].message
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_infra_status_compose_probe_available_true_when_probe_can_run(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(
        ops.self_heal, "compose_probe", lambda: ops.self_heal.ComposeProbe(available=True)
    )

    result = ops.infra_status()
    assert result["compose_probe_available"] is True
    assert result["compose_probe_reason"] == ""


@pytest.mark.unit
def test_infra_status_compose_probe_available_false_when_compose_file_unreachable(monkeypatch):
    # #3588: this is exactly what a Terraform-managed api container hit
    # before the docker-compose.yml bind mount was added -- the observability
    # tier's absence must be reported as "can't check", not "not running".
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running"})
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(native={}, compose={}, conflicts=[]),
    )
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/docker" if prog == "docker" else None
    )
    monkeypatch.setattr(
        ops.self_heal,
        "compose_probe",
        lambda: ops.self_heal.ComposeProbe(
            available=False,
            reason="`docker compose ps` exited 1: the Compose file is not reachable from here",
        ),
    )

    result = ops.infra_status()
    assert result["mode"] == "terraform"
    assert result["compose"] == {}
    assert result["compose_probe_available"] is False
    # #3812: the page must be able to say *why* it can't check, not just that
    # it can't -- the reason travels with the flag.
    assert "not reachable" in result["compose_probe_reason"]


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
        if "pods" in cmd:
            return CP(
                returncode=0,
                stdout=json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "nyxgpt-api-abc"},
                                "status": {
                                    "phase": "Running",
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                },
                            },
                            {
                                "metadata": {"name": "grafana-def"},
                                "status": {
                                    "phase": "Pending",
                                    "conditions": [
                                        {
                                            "type": "PodScheduled",
                                            "status": "False",
                                            "reason": "Unschedulable",
                                            "message": "0/1 nodes are available.",
                                        }
                                    ],
                                },
                            },
                        ]
                    }
                ),
            )
        # `_kubectl_context()` reads the current context; anything else is a
        # probe this test does not care about.
        return CP(returncode=0, stdout="kind-nyxgpt-local\n" if "config" in cmd else "")

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
    assert result["kubernetes"]["pods"] == [
        "nyxgpt-api-abc   Running",
        "grafana-def   Pending: unschedulable",
    ]
    # #3827: the Infrastructure page must be able to badge a Pod that will never
    # start differently from one that is merely starting -- the raw `kubectl get
    # pods` line says "Pending" for both.
    assert result["kubernetes"]["pod_states"] == [
        {"name": "nyxgpt-api-abc", "state": "ready", "summary": "Running", "details": ""},
        {
            "name": "grafana-def",
            "state": "failed",
            "summary": "Pending: unschedulable",
            "details": "0/1 nodes are available.",
        },
    ]


@pytest.mark.unit
def test_infra_status_reports_provisioned_kind_cluster(monkeypatch):
    """#3596: the Infrastructure page (and self-heal) must be able to tell a
    nyxgpt-provisioned kind cluster apart from a bring-your-own one."""
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
            return CP(returncode=0, stdout=f"{ops.KIND_CONTEXT}\n")
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"]:
            return CP(returncode=0, stdout="nyxgpt-api-abc   1/1   Running\n")
        raise AssertionError(f"unexpected kubectl command: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops.infra_status()
    assert result["kubernetes"]["context"] == ops.KIND_CONTEXT
    assert result["kubernetes"]["provisioned"] is True


@pytest.mark.unit
def test_infra_status_reports_bring_your_own_cluster_not_provisioned(monkeypatch):
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
            return CP(returncode=0, stdout="docker-desktop\n")
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"]:
            return CP(returncode=0, stdout="")
        raise AssertionError(f"unexpected kubectl command: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops.infra_status()
    assert result["kubernetes"]["context"] == "docker-desktop"
    assert result["kubernetes"]["provisioned"] is False


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


# The Compose-sourced observability tier a native install runs by default:
# `install()` starts it unless `--skip-observability` is passed, so this is
# what the *correctly configured* native install looks like (#3855).
_OBSERVABILITY_COMPOSE_SNAPSHOT = {
    "grafana": "running",
    "prometheus": "running",
    "loki": "running",
    "promtail": "running",
    "jaeger": "running",
    "otel-collector": "running",
    "glitchtip": "running",
    "glitchtip-postgres": "running",
    "glitchtip-redis": "running",
    "glitchtip-worker": "running",
}


@pytest.mark.unit
def test_infra_status_observability_only_compose_is_still_native_mode(monkeypatch):
    """Ten running observability containers are not a Compose *deployment* -- #3855.

    The owner's rc12 acceptance install: native/artifact api/web/ollama plus
    the ops-managed Cassandra container, with the observability stack up
    under Compose. Testing the whole Compose snapshot for truthiness took the
    `compose` branch before `native_running` was ever evaluated, so the
    Infrastructure page labelled a Homebrew stack "Docker Compose" and sent
    the operator down the Compose troubleshooting path.
    """
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={"api": "started", "web": "started", "ollama": "started"},
            compose=dict(_OBSERVABILITY_COMPOSE_SNAPSHOT),
            conflicts=[],
        ),
    )

    result = ops.infra_status()
    assert result["mode"] == "native"
    # The snapshot itself is unchanged -- the page still renders every
    # observability container under its Compose panel; only the *mode*
    # predicate stopped counting them as a core deployment.
    assert result["compose"] == _OBSERVABILITY_COMPOSE_SNAPSHOT


@pytest.mark.unit
def test_infra_status_compose_mode_survives_when_a_core_component_is_composed(monkeypatch):
    """A real Compose deployment still reports `compose`, observability alongside it."""
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={},
            compose={**_OBSERVABILITY_COMPOSE_SNAPSHOT, "api": "running", "web": "running"},
            conflicts=[],
        ),
    )
    assert ops.infra_status()["mode"] == "compose"


@pytest.mark.unit
def test_compose_core_components_ignores_the_observability_tier():
    """The core filter, asked directly -- #3855's generality sweep result."""
    mode = ops.DeploymentMode(
        native={},
        compose={**_OBSERVABILITY_COMPOSE_SNAPSHOT, "api": "running", "cassandra": "exited"},
        conflicts=[],
    )
    assert ops.compose_core_components(mode) == ["api", "cassandra"]
    assert (
        ops.compose_core_components(
            ops.DeploymentMode(
                native={}, compose=dict(_OBSERVABILITY_COMPOSE_SNAPSHOT), conflicts=[]
            )
        )
        == []
    )


@pytest.mark.unit
def test_infra_status_and_self_heal_agree_on_detected_mode(monkeypatch):
    """The Infrastructure page and the Self-Heal page must not contradict each other (#3855).

    Same host, same minute, same underlying probe: `list_component_status()`
    tags api/web/ollama/cassandra `native` and the observability containers
    `compose`. The disagreement between the two pages was the observable
    symptom, so it is asserted directly rather than only via `infra_status`.
    """
    components = [
        self_heal.ComponentStatus(
            service=service,
            container=service,
            state="started",
            health="healthy",
            healthy=True,
            source="native",
        )
        for service in ("api", "web", "ollama")
    ] + [
        self_heal.ComponentStatus(
            service=service,
            container=service,
            state=state,
            health="healthy",
            healthy=True,
            source="compose",
        )
        for service, state in _OBSERVABILITY_COMPOSE_SNAPSHOT.items()
    ]

    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    monkeypatch.setattr(
        ops,
        "detect_deployment_mode",
        lambda: ops.DeploymentMode(
            native={c.service: c.state for c in components if c.source == "native"},
            compose={c.service: c.state for c in components if c.source == "compose"},
            conflicts=[],
        ),
    )

    assert ops.infra_status()["mode"] == self_heal.detected_mode(components) == "native"


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
    pods = json.dumps(
        {
            "items": [
                {
                    "metadata": {"name": "nyxgpt-api-abc"},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "Ready", "status": "True"}],
                    },
                }
            ]
        }
    )
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, check=True, **_k: CP(
            returncode=0, stdout=pods if "pods" in cmd else "kind-nyxgpt-local\n"
        ),
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


def _status_pod(name: str, phase: str, **status) -> dict:
    """One `kubectl get pods -o json` item, as `nyxgpt ops status` now reads them."""
    return {"metadata": {"name": name}, "status": {"phase": phase, **status}}


_STATUS_READY_POD_LIST = {
    "items": [
        _status_pod(
            "nyxgpt-api-stable-abc",
            "Running",
            conditions=[{"type": "Ready", "status": "True"}],
        )
    ]
}


@pytest.mark.unit
def test_status_classifies_pending_and_blocked_pods_apart(monkeypatch, capsys):
    """`nyxgpt ops status` labels Pods, it does not echo `kubectl get pods` (#3827).

    The raw table renders a Pod pulling an image and a Pod no node will take
    identically -- both read `Pending` -- and `ops status` is the command
    every install failure message points the operator at, so it is exactly
    where that conflation does the most damage. Asserted together, because
    the defect is not "an unhelpful line" but *two different conditions
    reported the same way*.
    """
    pods = {
        "items": [
            _status_pod(
                "ready-pod",
                "Running",
                conditions=[{"type": "Ready", "status": "True"}],
            ),
            _status_pod(
                "pulling-pod",
                "Pending",
                conditions=[{"type": "PodScheduled", "status": "True"}],
                containerStatuses=[{"state": {"waiting": {"reason": "ContainerCreating"}}}],
            ),
            _status_pod(
                "unschedulable-pod",
                "Pending",
                conditions=[
                    {
                        "type": "PodScheduled",
                        "status": "False",
                        "reason": "Unschedulable",
                        "message": "0/1 nodes are available: 1 Insufficient memory.",
                    }
                ],
            ),
        ]
    }

    def fake_run(cmd, check=True, **_k):
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"] and "pods" in cmd:
            return CP(returncode=0, stdout=json.dumps(pods))
        return CP(stdout="")

    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/kubectl" if prog == "kubectl" else None
    )
    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(
        ops, "_serving_status", lambda running_mode: {"supported": False, "message": "n/a"}
    )
    monkeypatch.setattr(ops, "_k8s_observability_workload_state", lambda: {})

    assert ops.status(MagicMock()) == 0
    out = capsys.readouterr().out

    assert "[OK] pod ready-pod: Running" in out
    assert "[PENDING] pod pulling-pod: Pending: ContainerCreating" in out
    assert f"[FAIL] pod unschedulable-pod: {ops.K8S_SUMMARY_UNSCHEDULABLE}" in out
    # The scheduler's own words, which is what tells the operator the remedy
    # is a bigger node rather than a retry.
    assert "Insufficient memory" in out
    # The pre-fix behaviour: the raw table gave both Pending Pods the same
    # single word and no verdict at all.
    assert "[PENDING] pod unschedulable-pod" not in out
    assert "[FAIL] pod pulling-pod" not in out


@pytest.mark.unit
def test_status_classifies_observability_workloads(monkeypatch, capsys):
    """`0/1 ready` is PENDING in `ops status` too, not a bare count (#3827)."""

    def fake_run(cmd, check=True, **_k):
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"] and "pods" in cmd:
            return CP(returncode=0, stdout=json.dumps(_STATUS_READY_POD_LIST))
        return CP(stdout="")

    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/kubectl" if prog == "kubectl" else None
    )
    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(
        ops, "_serving_status", lambda running_mode: {"supported": False, "message": "n/a"}
    )
    monkeypatch.setattr(
        ops,
        "_k8s_observability_workload_state",
        lambda: {"grafana": "0/1 ready", "prometheus": "1/1 ready", "loki": "absent"},
    )

    assert ops.status(MagicMock()) == 0
    out = capsys.readouterr().out

    assert "[PENDING] grafana: 0/1 ready" in out
    assert "[OK] prometheus: 1/1 ready" in out
    assert "[FAIL] loki: absent" in out
    # This is the exact contradiction the issue reported: the install called a
    # zero-ready workload a failure while `status` printed it with a green tick.
    assert "[OK] grafana" not in out


@pytest.mark.unit
def test_status_shows_kubernetes_pods_when_present(monkeypatch, capsys):
    def fake_which(prog):
        return "/usr/local/bin/kubectl" if prog == "kubectl" else None

    def fake_run(cmd, check=True, **_k):
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"] and "pods" in cmd:
            return CP(returncode=0, stdout=json.dumps(_STATUS_READY_POD_LIST))
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
            return CP(returncode=0, stdout=json.dumps(_STATUS_READY_POD_LIST))
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
def test_doctor_flags_dual_stack(monkeypatch, tmp_path, capsys):
    """#3565 round 5 acceptance failure: after an incomplete mode switch left
    a native/Compose stack AND a Terraform stack running at once, `nyxgpt ops
    doctor` reported OK -- the conflicts detector it relies on
    (`detect_deployment_mode`) only ever compared native vs. Compose, so a
    native-vs-Terraform collision was invisible to it. Must FAIL now."""
    cfg_dir = tmp_path / ".nyxGPT"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.ini").write_text(
        "[project]\nname=nyxGPT\n\n[tracing]\nenabled = false\n", encoding="utf-8"
    )
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "TERRAFORM_DIR", tmp_path / "terraform")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: None)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})
    monkeypatch.setattr(
        ops,
        "_brew_services_snapshot",
        lambda: {"nyxgpt-api": "started", "nyxgpt-web": "stopped", "ollama": "stopped"},
    )

    def fake_docker_state(name):
        if name == ops.TERRAFORM_CONTAINERS["api"]:
            return "running"
        return "absent"

    monkeypatch.setattr(ops, "_docker_container_state", fake_docker_state)

    rc = ops.doctor(MagicMock())
    out = capsys.readouterr().out
    assert rc == 2
    assert "api" in out
    assert "BOTH native/Compose and Terraform" in out


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
        "jaeger_ui_url = http://localhost:16686\n"
        "[error_tracking]\n"
        "enabled = true\n"
        "glitchtip_ui_url = http://localhost:8080\n"
        "dsn = http://509fecaebca74ee68bcd4bd9d56dbe53@localhost:8080/1\n",
        encoding="utf-8",
    )
    out = tmp_path / "config.docker.ini"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", out)

    results = ops._generate_compose_config()
    assert all(r.ok for r in results)

    # Secrets ([auth] api_key, [monitoring] grafana_admin_password, ...) are
    # copied verbatim from the native config -- must land 0600, same as the
    # native file (#3500).
    assert oct(out.stat().st_mode & 0o777) == "0o600"

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
    # #3565 round 5: the error-tracking DSN is host-rewritten for the
    # container network (a containerized api can't reach the native config's
    # browser-facing `localhost` DSN), while glitchtip_ui_url -- opened from
    # the host browser -- stays localhost.
    assert (
        parser.get("error_tracking", "dsn")
        == "http://509fecaebca74ee68bcd4bd9d56dbe53@glitchtip:8080/1"
    )
    assert parser.get("error_tracking", "glitchtip_ui_url") == "http://localhost:8080"


@pytest.mark.unit
def test_generate_compose_config_no_error_tracking_section_is_noop(tmp_path, monkeypatch):
    """No `[error_tracking] dsn` in the native config (error tracking never
    provisioned) -- the DSN rewrite step must not blow up or fabricate a
    section."""
    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    native = home / ".nyxGPT" / "config.ini"
    native.write_text("[nyxgpt]\ndefault_model = llama3.1:8b\n", encoding="utf-8")
    out = tmp_path / "config.docker.ini"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", out)

    results = ops._generate_compose_config()
    assert all(r.ok for r in results)

    parser = ConfigParser()
    parser.read(out)
    assert not parser.has_section("error_tracking")


@pytest.mark.unit
def test_generate_compose_config_malformed_native_config_degrades_gracefully(
    tmp_path, monkeypatch, caplog
):
    """A native config.ini that isn't valid INI (e.g. a hand-edited duplicate
    section) must not crash `_generate_compose_config` -- the DSN rewrite step
    parses the text with `ConfigParser`, which raises `configparser.Error` on
    input the line-based `_patch_ini_value` rewrites tolerate fine. Regression
    test for #3565 round-5 review: this used to propagate uncaught out of
    `env_sync()`, which has no surrounding try/except."""
    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    native = home / ".nyxGPT" / "config.ini"
    native.write_text(
        "[nyxgpt]\n"
        "default_model = llama3.1:8b\n"
        "[error_tracking]\n"
        "enabled = true\n"
        "dsn = http://509fecaebca74ee68bcd4bd9d56dbe53@localhost:8080/1\n"
        "[error_tracking]\n"
        "dsn = http://duplicate@localhost:8080/1\n",
        encoding="utf-8",
    )
    out = tmp_path / "config.docker.ini"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", out)

    with caplog.at_level(logging.WARNING):
        results = ops._generate_compose_config()

    assert all(r.ok for r in results)
    assert out.exists()
    assert "Failed to parse" in caplog.text

    text = out.read_text(encoding="utf-8")
    # DSN rewrite skipped (unparseable), but the rest of the derived config
    # is still written -- no crash, no partial/missing output file.
    assert "dsn = http://509fecaebca74ee68bcd4bd9d56dbe53@localhost:8080/1" in text


@pytest.mark.unit
def test_env_sync_survives_malformed_native_config(tmp_path, monkeypatch):
    """`nyxgpt ops env-sync` must not raise when the native config.ini has a
    duplicate section -- `_generate_compose_config` is called directly with no
    surrounding try/except in `env_sync()`, unlike the `install()` call sites."""
    home = tmp_path / "home"
    (home / ".nyxGPT").mkdir(parents=True)
    native = home / ".nyxGPT" / "config.ini"
    _write_config(native, api_key="cli-api-key")
    with native.open("a", encoding="utf-8") as f:
        f.write(
            "[error_tracking]\ndsn = http://one@localhost:8080/1\n"
            "[error_tracking]\ndsn = http://two@localhost:8080/1\n"
        )
    out = tmp_path / "config.docker.ini"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "COMPOSE_CONFIG_FILE", out)
    monkeypatch.setattr(ops, "sync_env_from_config", lambda **kwargs: [])
    monkeypatch.setattr(ops, "_sync_grafana_slack_webhook_secret", lambda **kwargs: [])

    args = MagicMock()
    args.config = None
    args.env_file = None
    rc = ops.env_sync(args)

    assert rc == 0
    assert out.exists()


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


# --- Live step progress (#3558) ---


@pytest.mark.unit
def test_run_steps_announces_each_step_before_running_it(capsys):
    """Each step's "[n/m] name..." announcement must print before that step's
    function runs and before its outcome is printed -- the whole point of
    #3558 is that progress streams live instead of buffering to the end."""
    call_order = []

    def step_one():
        call_order.append("ran:one")
        return [ops.OpsResult(True, "one done")]

    def step_two():
        call_order.append("ran:two")
        return [ops.OpsResult(True, "two done")]

    steps = [("one", step_one), ("two", step_two)]
    results, slow_steps = ops._run_steps("test", steps, quiet=False)

    assert [r.message for r in results] == ["one done", "two done"]
    assert slow_steps == []
    assert call_order == ["ran:one", "ran:two"]

    lines = capsys.readouterr().out.splitlines()
    assert lines.index("[1/2] one...") < lines.index("[OK] one done")
    assert lines.index("[2/2] two...") < lines.index("[OK] two done")
    assert lines.index("[OK] one done") < lines.index("[2/2] two...")


@pytest.mark.unit
def test_run_steps_counter_reflects_total_step_count(capsys):
    steps = [(f"step{i}", lambda: [ops.OpsResult(True, "ok")]) for i in range(1, 4)]
    ops._run_steps("test", steps, quiet=False)
    out = capsys.readouterr().out
    assert "[1/3] step1..." in out
    assert "[2/3] step2..." in out
    assert "[3/3] step3..." in out


@pytest.mark.unit
def test_run_steps_quiet_suppresses_announcements_but_keeps_ok_fail(capsys):
    steps = [("one", lambda: [ops.OpsResult(True, "one done")])]
    ops._run_steps("test", steps, quiet=True)
    out = capsys.readouterr().out
    assert "[1/1]" not in out
    assert "[OK] one done" in out


@pytest.mark.unit
def test_run_steps_step_exception_names_step_shows_error_and_hint(capsys):
    def bad_step():
        raise RuntimeError("kaboom")

    steps = [("risky step", bad_step)]
    results, _slow_steps = ops._run_steps("test", steps, quiet=False)

    assert len(results) == 1
    assert results[0].ok is False
    assert "risky step" in results[0].message
    assert "RuntimeError: kaboom" in results[0].details
    assert "nyxgpt ops doctor" in results[0].details

    out = capsys.readouterr().out
    assert "[FAIL]" in out
    assert "risky step" in out


@pytest.mark.unit
def test_run_steps_one_bad_step_does_not_abort_the_rest():
    steps = [
        ("first", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ("second", lambda: [ops.OpsResult(True, "second ran")]),
    ]
    results, _ = ops._run_steps("test", steps, quiet=True)
    assert [r.ok for r in results] == [False, True]
    assert results[1].message == "second ran"


@pytest.mark.unit
def test_run_steps_flags_slow_step_for_summary(monkeypatch):
    # Fake a 10s-elapsed step without actually sleeping: two time.monotonic()
    # calls per step (start, then elapsed) -- quiet=True so no heartbeat
    # thread makes extra calls of its own.
    timestamps = iter([0.0, 10.0])
    monkeypatch.setattr(ops.time, "monotonic", lambda: next(timestamps))

    steps = [("slow step", lambda: [ops.OpsResult(True, "done")])]
    _results, slow_steps = ops._run_steps("test", steps, quiet=True)

    assert slow_steps == [("slow step", 10.0)]


@pytest.mark.unit
def test_run_steps_fast_step_is_not_flagged_as_slow(monkeypatch):
    timestamps = iter([0.0, 0.1])
    monkeypatch.setattr(ops.time, "monotonic", lambda: next(timestamps))

    steps = [("fast step", lambda: [ops.OpsResult(True, "done")])]
    _results, slow_steps = ops._run_steps("test", steps, quiet=True)

    assert slow_steps == []


@pytest.mark.unit
def test_print_slow_steps_summary_lists_step_and_duration(capsys):
    ops._print_slow_steps_summary([("slow step", 12.34)])
    out = capsys.readouterr().out
    assert "Slow steps" in out
    assert "slow step: 12.3s" in out


@pytest.mark.unit
def test_print_slow_steps_summary_prints_nothing_when_empty(capsys):
    ops._print_slow_steps_summary([])
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_result_status_label_ok_for_plain_success():
    assert ops._result_status_label(ops.OpsResult(True, "Started foo")) == "OK"


@pytest.mark.unit
def test_result_status_label_fail_for_failure():
    assert ops._result_status_label(ops.OpsResult(False, "bad")) == "FAIL"


@pytest.mark.unit
def test_result_status_label_skip_for_skipped_message():
    r = ops.OpsResult(True, "Skipped Compose teardown (Docker not found)")
    assert ops._result_status_label(r) == "SKIP"


@pytest.mark.unit
def test_emit_results_prints_skip_label_for_skipped_result(capsys):
    ops._emit_results("test", [ops.OpsResult(True, "Skipped thing (no docker)")])
    out = capsys.readouterr().out
    assert "[SKIP] Skipped thing (no docker)" in out


@pytest.mark.unit
def test_result_status_label_note_for_superseded_attempt():
    """#3762: an attempt the step recovered from is neither OK nor FAIL."""
    r = ops.OpsResult(True, "Superseded: could not add 'ec2-user' to the 'docker' group")
    assert ops._result_status_label(r) == "NOTE"


@pytest.mark.unit
def test_superseded_attempts_keeps_the_diagnostic_and_passes_successes_through():
    failed = ops.OpsResult(False, "Could not add 'x' to the 'docker' group", "run usermod")
    ok = ops.OpsResult(True, "Docker daemon enabled and started")

    settled = ops._superseded_attempts([failed, ok])

    assert [r.ok for r in settled] == [True, True]
    assert settled[0].message.startswith("Superseded: Could not add 'x'")
    assert settled[0].details == "run usermod"
    assert settled[1] is ok


@pytest.mark.unit
def test_run_steps_closes_a_mixed_step_with_one_verdict(capsys):
    """#3762: `[4/23] docker engine` printed a FAIL and then two OKs, with no
    answer to "did step 4 pass?". The verdict is now the step's last word."""
    steps = [
        (
            "docker engine",
            lambda: [
                ops.OpsResult(False, "Could not install Docker Compose automatically"),
                ops.OpsResult(True, "Docker daemon enabled and started"),
                ops.OpsResult(True, "Docker daemon is reachable"),
            ],
        )
    ]

    ops._run_steps("install", steps)

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("[")]
    assert lines[-1] == (
        "[FAIL] step 1/1 'docker engine' did not fully succeed: 1 of 3 checks failed "
        "(Could not install Docker Compose automatically)"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "results",
    [
        [ops.OpsResult(True, "all good"), ops.OpsResult(True, "also good")],
        [ops.OpsResult(False, "all bad"), ops.OpsResult(False, "also bad")],
    ],
)
def test_run_steps_adds_no_verdict_when_a_step_already_reads_coherently(capsys, results):
    ops._run_steps("install", [("a step", lambda: results)])

    assert "did not fully succeed" not in capsys.readouterr().out


@pytest.mark.unit
def test_step_heartbeat_prints_still_running_line_while_step_is_in_flight(monkeypatch):
    # Speed the heartbeat up so the test doesn't wait the real 5s interval.
    monkeypatch.setattr(ops, "_STEP_HEARTBEAT_INTERVAL_S", 0.01)
    hb = ops._StepHeartbeat("slow thing")
    hb.start()
    time.sleep(0.05)
    hb.stop()
    # No assertion on stdout content here (it's a background thread racing
    # capsys) -- this just proves start()/stop() don't hang or raise.
    assert True


@pytest.mark.unit
def test_ops_install_quiet_flag_suppresses_step_announcements(capsys):
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", return_value=ok_results),
        patch.object(ops, "_install_config", return_value=ok_results),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
    ):
        rc = ops.install(
            SimpleNamespace(skip_observability=True, terraform=False, kubernetes=False, quiet=True)
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "/17]" not in out  # no "[n/17] step..." announcements in quiet mode


@pytest.mark.unit
def test_ops_install_default_verbose_prints_step_announcements(capsys):
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_reconcile_install_mode", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_sync_packaged_resources", return_value=ok_results),
        patch.object(ops, "_install_config", return_value=ok_results),
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_env_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_required_models", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_cleanup_stale_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
    ):
        rc = ops.install(
            SimpleNamespace(skip_observability=True, terraform=False, kubernetes=False, quiet=False)
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[1/22] sync packaged ops resources..." in out


@pytest.mark.unit
def test_ops_env_sync_quiet_flag_suppresses_step_announcements(tmp_path, capsys):
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="cli-api-key")
    env_path = tmp_path / ".env"

    args = SimpleNamespace(config=str(cfg_path), env_file=str(env_path), quiet=True)
    rc = ops.env_sync(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "[1/3]" not in out
    assert "[OK]" in out


@pytest.mark.unit
def test_wait_for_stack_healthy_returns_true_when_all_healthy(monkeypatch):
    """`_wait_for_stack_healthy` returns True immediately once every desired
    component reports healthy, without needing to poll or sleep."""
    statuses = [
        self_heal.ComponentStatus(
            service="api", container="nyxgpt-api", state="running", health="healthy", healthy=True
        ),
        self_heal.ComponentStatus(
            service="web", container="nyxgpt-web", state="running", health="healthy", healthy=True
        ),
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: statuses)
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    assert ops._wait_for_stack_healthy(timeout=5.0, poll_interval=1.0) is True
    assert sleeps == []


@pytest.mark.unit
def test_wait_for_stack_healthy_ignores_non_desired_components(monkeypatch):
    """An unhealthy but `desired=False` component (disabled profile, or
    intentionally stopped -- see `list_component_status`) must not block the
    wait, matching `heal_now`'s automatic-pass semantics."""
    statuses = [
        self_heal.ComponentStatus(
            service="api", container="nyxgpt-api", state="running", health="healthy", healthy=True
        ),
        self_heal.ComponentStatus(
            service="grafana",
            container="grafana",
            state="absent",
            health="",
            healthy=False,
            desired=False,
        ),
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: statuses)

    assert ops._wait_for_stack_healthy(timeout=5.0, poll_interval=1.0) is True


@pytest.mark.unit
def test_wait_for_stack_healthy_returns_false_on_timeout(monkeypatch):
    """Times out (returns False) rather than polling forever when a desired
    component never reports healthy."""
    unhealthy = [
        self_heal.ComponentStatus(
            service="ollama", container="ollama", state="running", health="starting", healthy=False
        )
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: unhealthy)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert ops._wait_for_stack_healthy(timeout=0.0, poll_interval=0.0) is False


def _absent_observability_status(service: str) -> "self_heal.ComponentStatus":
    """The shape `_absent_desired_statuses` produces for an enabled-but-not-running
    observability profile: desired, absent, never healthy on its own."""
    return self_heal.ComponentStatus(
        service=service,
        container="",
        state="absent",
        health="",
        healthy=False,
        source="compose",
        desired=True,
    )


@pytest.mark.unit
def test_wait_for_stack_healthy_skips_observability_when_requested(monkeypatch):
    """`--skip-observability` deliberately doesn't start the observability
    profiles, but leaves their config.ini flags on -- so self-heal keeps
    reporting them desired-but-absent. The wait must exclude them, or `nyxgpt
    up --skip-observability` can never return 0 (#3508)."""
    statuses = [
        self_heal.ComponentStatus(
            service="api", container="nyxgpt-api", state="running", health="healthy", healthy=True
        ),
        _absent_observability_status("jaeger"),
        _absent_observability_status("otel-collector"),
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: statuses)
    monkeypatch.setattr(self_heal, "observability_services", lambda: {"jaeger", "otel-collector"})
    monkeypatch.setattr(time, "sleep", lambda s: pytest.fail("must not poll: nothing is pending"))

    assert ops._wait_for_stack_healthy(timeout=5.0, poll_interval=1.0, skip_observability=True)


@pytest.mark.unit
def test_wait_for_stack_healthy_waits_for_observability_by_default(monkeypatch):
    """The counter-case that makes the test above meaningful: without the flag,
    the very same desired-but-absent observability component still blocks."""
    statuses = [
        self_heal.ComponentStatus(
            service="api", container="nyxgpt-api", state="running", health="healthy", healthy=True
        ),
        _absent_observability_status("jaeger"),
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: statuses)
    monkeypatch.setattr(
        self_heal,
        "observability_services",
        lambda: pytest.fail("must not be consulted unless skipping"),
    )
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert ops._wait_for_stack_healthy(timeout=0.0, poll_interval=0.0) is False


@pytest.mark.unit
def test_wait_for_stack_healthy_skip_observability_still_waits_for_core(monkeypatch):
    """Skipping observability must not turn the health-wait off wholesale: an
    unhealthy core component still blocks."""
    statuses = [
        self_heal.ComponentStatus(
            service="api", container="nyxgpt-api", state="running", health="starting", healthy=False
        ),
        _absent_observability_status("jaeger"),
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: statuses)
    monkeypatch.setattr(self_heal, "observability_services", lambda: {"jaeger"})
    monkeypatch.setattr(time, "sleep", lambda s: None)

    assert (
        ops._wait_for_stack_healthy(timeout=0.0, poll_interval=0.0, skip_observability=True)
        is False
    )


@pytest.mark.unit
def test_wait_for_stack_healthy_resolves_observability_set_once(monkeypatch):
    """The exclusion set is resolved once, not per poll -- it shells out to
    `docker compose config` and the poll loop can run for minutes."""
    calls = []
    pending = [
        self_heal.ComponentStatus(
            service="api", container="nyxgpt-api", state="running", health="starting", healthy=False
        )
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: pending)
    monkeypatch.setattr(self_heal, "observability_services", lambda: calls.append(1) or set())
    monkeypatch.setattr(time, "sleep", lambda s: None)

    ops._wait_for_stack_healthy(timeout=0.0, poll_interval=0.0, skip_observability=True)

    assert calls == [1]


@pytest.mark.unit
def test_ops_up_timeout_names_the_pending_components(monkeypatch, capsys):
    """The timeout message names what is actually still unhealthy. Without
    this, `up` reported only that "not every component" was healthy, and
    working out which one meant re-running `ops status` by hand (#3508)."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: False)
    monkeypatch.setattr(ops, "_pending_components", lambda excluded: ["cassandra", "ollama"])

    rc = ops.up(
        SimpleNamespace(
            no_wait=False, timeout=1.0, kubernetes=False, skip_observability=False, quiet=False
        )
    )

    assert rc == 2
    assert "Still unhealthy: cassandra, ollama" in capsys.readouterr().err


@pytest.mark.unit
def test_ops_up_timeout_message_survives_an_empty_pending_list(monkeypatch, capsys):
    """A component that recovered between the last poll and the report leaves
    nothing to name -- the message must still be well-formed, not dangling."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: False)
    monkeypatch.setattr(ops, "_pending_components", lambda excluded: [])

    rc = ops.up(
        SimpleNamespace(
            no_wait=False, timeout=1.0, kubernetes=False, skip_observability=False, quiet=False
        )
    )

    assert rc == 2
    err = capsys.readouterr().err
    assert "Still unhealthy" not in err
    assert "timeout -- run `nyxgpt ops status`" in err


@pytest.mark.unit
def test_pending_components_excludes_and_sorts(monkeypatch):
    """`_pending_components` is the single definition of "pending" shared by
    the poll loop and the timeout message."""
    statuses = [
        self_heal.ComponentStatus(
            service="ollama", container="ollama", state="running", health="starting", healthy=False
        ),
        self_heal.ComponentStatus(
            service="api", container="nyxgpt-api", state="running", health="starting", healthy=False
        ),
        self_heal.ComponentStatus(
            service="web", container="nyxgpt-web", state="running", health="healthy", healthy=True
        ),
        _absent_observability_status("jaeger"),
    ]
    monkeypatch.setattr(self_heal, "list_component_status", lambda: statuses)

    assert ops._pending_components({"jaeger"}) == ["api", "ollama"]
    assert ops._pending_components(set()) == ["api", "jaeger", "ollama"]


@pytest.mark.unit
def test_ops_up_passes_skip_observability_to_health_wait(monkeypatch):
    """`up` threads `--skip-observability` into the wait, not just the install."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    waited: list[dict] = []
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: waited.append(kw) or True)

    rc = ops.up(
        SimpleNamespace(
            no_wait=False, timeout=180.0, kubernetes=False, skip_observability=True, quiet=False
        )
    )

    assert rc == 0
    assert waited == [{"timeout": 180.0, "skip_observability": True}]


@pytest.mark.unit
def test_ops_up_without_skip_observability_waits_for_it(monkeypatch):
    """And passes False through when the operator did want observability up."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    waited: list[dict] = []
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: waited.append(kw) or True)

    rc = ops.up(
        SimpleNamespace(
            no_wait=False, timeout=180.0, kubernetes=False, skip_observability=False, quiet=False
        )
    )

    assert rc == 0
    assert waited == [{"timeout": 180.0, "skip_observability": False}]


@pytest.mark.unit
def test_ops_up_returns_install_failure_without_waiting(monkeypatch):
    """`up` propagates a failing `install()` without ever calling the health-wait."""
    monkeypatch.setattr(ops, "install", lambda args: 2)
    waited = []
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: waited.append(kw) or True)

    rc = ops.up(MagicMock(dev=False, no_wait=False, timeout=180.0, kubernetes=False))

    assert rc == 2
    assert waited == []


@pytest.mark.unit
def test_ops_up_no_wait_skips_health_wait_and_url(monkeypatch, capsys):
    """`--no-wait` returns `install()`'s result as soon as it finishes."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    waited = []
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: waited.append(kw) or True)

    rc = ops.up(MagicMock(dev=False, no_wait=True, timeout=180.0, kubernetes=False))

    assert rc == 0
    assert waited == []
    assert ops.WEB_URL not in capsys.readouterr().out


@pytest.mark.unit
def test_ops_up_waits_then_prints_web_url(monkeypatch, capsys):
    """Once `install()` and the health-wait both succeed, `up` prints the web URL."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    calls = []
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: calls.append(kw) or True)

    # skip_observability is set explicitly: on a bare MagicMock the attribute
    # would be a truthy Mock, so the assertion below would pin an accident of
    # the test double rather than what `up` actually forwards.
    rc = ops.up(
        MagicMock(
            dev=False, no_wait=False, timeout=42.0, kubernetes=False, skip_observability=False
        )
    )

    assert rc == 0
    assert calls == [{"timeout": 42.0, "skip_observability": False}]
    assert ops.WEB_URL in capsys.readouterr().out


@pytest.mark.unit
def test_ops_up_times_out_returns_nonzero(monkeypatch, capsys):
    """A health-wait timeout is reported and fails the command, even though
    `install()` itself succeeded."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: False)

    rc = ops.up(MagicMock(dev=False, no_wait=False, timeout=180.0, kubernetes=False))

    assert rc == 2
    captured = capsys.readouterr()
    assert ops.WEB_URL not in captured.out
    assert "not every component reported healthy" in captured.err


@pytest.mark.unit
def test_ops_up_kubernetes_mode_prints_port_forward_instructions(monkeypatch, capsys):
    """Kubernetes Services are ClusterIP-only, so `up --kubernetes` must not
    claim the web URL is directly reachable -- it needs a manual
    `kubectl port-forward` first (see docs/kubernetes.md#4-verify)."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: True)

    rc = ops.up(MagicMock(dev=False, no_wait=False, timeout=180.0, kubernetes=True))

    assert rc == 0
    out = capsys.readouterr().out
    assert "port-forward" in out
    assert "kubectl -n nyxgpt port-forward" not in out
    assert ops.WEB_URL in out


@pytest.mark.unit
def test_ops_up_kubernetes_mode_wraps_kubectl_not_raw(monkeypatch, capsys):
    """The Operational Command Wrapping policy (CLAUDE.md) forbids instructing
    users to run a raw `kubectl` command -- `up --kubernetes` must point at
    the `nyxgpt ops port-forward` wrapper instead."""
    monkeypatch.setattr(ops, "install", lambda args: 0)
    monkeypatch.setattr(ops, "_wait_for_stack_healthy", lambda **kw: True)

    ops.up(MagicMock(dev=False, no_wait=False, timeout=180.0, kubernetes=True))

    out = capsys.readouterr().out
    assert "nyxgpt ops port-forward" in out


@pytest.mark.unit
def test_ops_port_forward_missing_kubectl(monkeypatch, capsys):
    """`nyxgpt ops port-forward` fails fast with a clear message if kubectl isn't installed."""
    monkeypatch.setattr(ops, "_which", lambda _: None)

    rc = ops.port_forward(MagicMock(port=3000))

    assert rc == 2
    assert "kubectl not found on PATH" in capsys.readouterr().err


@pytest.mark.unit
def test_ops_port_forward_invokes_kubectl(monkeypatch, capsys):
    """`nyxgpt ops port-forward` shells out to `kubectl port-forward` for the
    web Service, using the requested local port, and returns its exit code."""
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/kubectl")
    calls = []

    # One forward per target now (#3787's `--target observability` runs four
    # concurrently), so this spawns Popen rather than blocking in run().
    def fake_popen(cmd):
        calls.append(cmd)
        proc = MagicMock()
        proc.wait.return_value = 0
        return proc

    monkeypatch.setattr(ops.subprocess, "Popen", fake_popen)

    rc = ops.port_forward(MagicMock(target="web", port=3001))

    assert rc == 0
    assert calls == [
        ["kubectl", "-n", ops.K8S_NAMESPACE, "port-forward", "svc/nyxgpt-web", "3001:3000"]
    ]
    assert "3001" in capsys.readouterr().out


@pytest.mark.unit
def test_ops_port_forward_keyboard_interrupt_is_clean_exit(monkeypatch):
    """Stopping `port-forward` with Ctrl-C (the normal way to end it) is a
    clean exit, not a crash."""
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/kubectl")

    def raise_interrupt(cmd):
        raise KeyboardInterrupt

    monkeypatch.setattr(ops.subprocess, "Popen", raise_interrupt)

    rc = ops.port_forward(MagicMock(target="web", port=3000))

    assert rc == 0


@pytest.mark.unit
def test_compose_stack_snapshot_omits_undetermined_components(monkeypatch):
    """#3812: this map's values are docker states, compared against "running"
    by its callers. A component whose state could not be determined has no
    docker state to contribute, and putting a guess in here would push it into
    `detect_deployment_mode()`'s conflict checks."""
    monkeypatch.setattr(
        ops.self_heal,
        "list_component_status",
        lambda: [
            ops.self_heal.ComponentStatus(
                "prometheus", "nyxgpt-prometheus-1", "running", "healthy", True, source="compose"
            ),
            ops.self_heal.ComponentStatus(
                "grafana",
                "",
                "unknown",
                "",
                False,
                source="compose",
                note="`docker compose ps` exited 125",
                known=False,
            ),
        ],
    )

    assert ops._compose_stack_snapshot() == {"prometheus": "running"}
