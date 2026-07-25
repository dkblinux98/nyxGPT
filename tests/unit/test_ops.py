import hashlib
import subprocess
import tarfile
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from nyxgpt import ops, self_heal


@pytest.mark.unit
def test_ops_install_returns_zero_when_all_ok(capsys):
    # Mock internal steps to all succeed
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
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
        patch.object(ops, "_persist_compose_file_path", return_value=ok_results),
        patch.object(ops, "_start_observability_stack", return_value=ok_results) as obs,
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
        patch.object(ops, "_install_homebrew_api", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_install_homebrew_web", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_ollama_service", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_log_symlinks", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "sync_env_from_config", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_persist_compose_file_path", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_start_observability_stack", return_value=[ops.OpsResult(True, "ok")]),
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
        patch.object(ops, "migrate_legacy_volumes", return_value=ok_results),
        patch.object(ops, "_reconcile_phantom_compose_app_containers", return_value=ok_results),
        patch.object(ops, "_install_scripts", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_ensure_mcp_deps", return_value=ok_results),
        patch.object(ops, "_ensure_cassandra_container", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_ollama_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_log_symlinks", return_value=ok_results),
        patch.object(ops, "sync_env_from_config", return_value=ok_results),
        patch.object(ops, "_persist_compose_file_path", return_value=ok_results),
        patch.object(ops, "_start_observability_stack") as obs,
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
        patch.object(ops, "_install_homebrew_api", side_effect=_record("homebrew api")),
        patch.object(ops, "_install_homebrew_web", side_effect=_record("homebrew web")),
        patch.object(ops, "_ensure_ollama_service", side_effect=_record("ollama service")),
        patch.object(ops, "_ensure_log_symlinks", side_effect=_record("log symlinks")),
        patch.object(ops, "sync_env_from_config", side_effect=_record("env sync")),
        patch.object(ops, "_persist_compose_file_path", side_effect=_record("compose file path")),
    ):
        rc = ops.install(MagicMock(skip_observability=True, terraform=False, kubernetes=False))
        assert rc == 0

    assert call_order[0] == "migrate volumes"
    assert call_order[1] == "reconcile"
    assert "cassandra container" in call_order
    assert "ollama service" in call_order
    assert "env sync" in call_order
    assert "compose file path" in call_order


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

    def fake_run(cmd, check=True):
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

    def fake_run(cmd, check=True):
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
def test_ops_status_smoke(monkeypatch, capsys):
    # Make status deterministic by stubbing _which and _run outputs
    class CP:
        def __init__(self, stdout=""):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(cmd, check=True):
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
    cfg.write_text("[project]\nname=nyxGPT\n", encoding="utf-8")

    # Make home dir resolve into tmp_path
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    # Tools exist
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")

    # Cassandra container is present and running
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")

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

    rc = ops.doctor(MagicMock())
    assert rc == 2

    out = capsys.readouterr().out
    assert "Missing web deps" in out


@pytest.mark.unit
def test_ops_doctor_fail_when_missing_config(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")

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

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "Missing local Cassandra container" in out
    assert "nyxgpt-cassandra" in out


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
def test_log_aggregation_wiring_issue_flags_missing_mount(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_log_aggregation_config(cfg_path)
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {"promtail": "running"})
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)

    log_dir = tmp_path / ".nyxGPT" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "nyxgpt.log").write_text("2026-07-18 00:00:00 INFO [-] nyxgpt: hi\n")

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    (tmp_path / "docker-compose.yml").write_text("services:\n  promtail:\n    image: x\n")

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

    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    (tmp_path / "docker-compose.yml").write_text(
        f"services:\n  promtail:\n    volumes:\n      - x:{ops.PROMTAIL_NATIVE_LOG_MOUNT_MARKER}:ro\n"
    )

    assert ops._log_aggregation_wiring_issue(cfg_path) is None


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
    (tmp_path / "docker-compose.yml").write_text("services:\n  promtail:\n    image: x\n")

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "not reaching Loki" in out


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
def test_env_sync_cli_wrapper_prints_result(tmp_path, capsys):
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="cli-api-key", grafana_password="cli-grafana-pw")
    env_path = tmp_path / ".env"

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
def test_which_finds_and_misses():
    assert ops._which("python3") is not None
    assert ops._which("definitely-not-a-real-binary-xyz") is None


@pytest.mark.unit
def test_read_project_version_missing_pyproject_returns_default(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    assert ops._read_project_version() == "1.0.0.md"


@pytest.mark.unit
def test_read_project_version_reads_from_pyproject(monkeypatch, tmp_path):
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "nyxGPT"\nversion = "9.9.9"\n', encoding="utf-8"
    )
    assert ops._read_project_version() == "9.9.9"


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
def test_brew_prefix_returns_run_output(monkeypatch):
    monkeypatch.setattr(
        ops,
        "_run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="/opt/homebrew\n"),
    )
    assert ops._brew_prefix() == Path("/opt/homebrew")


@pytest.mark.unit
def test_brew_prefix_falls_back_on_exception(monkeypatch):
    def raise_run(cmd, **k):
        raise FileNotFoundError("no brew")

    monkeypatch.setattr(ops, "_run", raise_run)
    assert ops._brew_prefix() == Path("/opt/homebrew")


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


@pytest.mark.unit
def test_ensure_ollama_service_no_brew():
    with patch.object(ops, "_which", lambda _: None):
        results = ops._ensure_ollama_service()
    assert results[0].ok is False
    assert "Homebrew not found" in results[0].message


@pytest.mark.unit
def test_ensure_ollama_service_already_running():
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {"ollama": "started"}),
    ):
        results = ops._ensure_ollama_service()
    assert results[0].ok is True
    assert "already running" in results[0].message


@pytest.mark.unit
def test_ensure_ollama_service_starts_stopped_service():
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {"ollama": "none"}),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        ),
    ):
        results = ops._ensure_ollama_service()
    assert [r.ok for r in results] == [True]
    assert "Started brew service: ollama" in results[0].message
    assert run_calls == [["brew", "services", "start", "ollama"]]


@pytest.mark.unit
def test_ensure_ollama_service_installs_formula_when_absent():
    run_calls = []
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {}),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: run_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
        ),
    ):
        results = ops._ensure_ollama_service()
    assert [r.ok for r in results] == [True, True]
    assert "Installed ollama formula" in results[0].message
    assert run_calls == [
        ["brew", "install", "ollama"],
        ["brew", "services", "start", "ollama"],
    ]


@pytest.mark.unit
def test_ensure_ollama_service_start_failure_reports_details():
    with (
        patch.object(ops, "_which", lambda _: "/opt/homebrew/bin/brew"),
        patch.object(ops, "_brew_services_snapshot", lambda: {"ollama": "none"}),
        patch.object(
            ops,
            "_run",
            lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
        ),
    ):
        results = ops._ensure_ollama_service()
    assert results[0].ok is False
    assert "Failed to start brew service: ollama" in results[0].message
    assert "boom" in results[0].details


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


# --- _ensure_log_symlinks ---


@pytest.mark.unit
def test_ensure_log_symlinks_creates_new_links(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_brew_prefix", lambda: tmp_path / "brew")

    results = ops._ensure_log_symlinks()
    assert all(r.ok for r in results)
    assert len(results) == 5
    for base in ("nyxgpt-api", "nyxgpt-web"):
        for ext in (".log", ".err.log"):
            link = home / ".nyxGPT" / "logs" / f"{base}{ext}"
            assert link.is_symlink()
    ollama_link = home / ".nyxGPT" / "logs" / "ollama.log"
    assert ollama_link.is_symlink()
    assert ollama_link.resolve() == (tmp_path / "brew" / "var" / "log" / "ollama.log").resolve()


@pytest.mark.unit
def test_ensure_log_symlinks_replaces_existing_link(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".nyxGPT" / "logs").mkdir(parents=True)
    stale_target = tmp_path / "stale.log"
    stale_target.write_text("stale", encoding="utf-8")
    (home / ".nyxGPT" / "logs" / "nyxgpt-api.log").symlink_to(stale_target)

    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_brew_prefix", lambda: tmp_path / "brew")

    results = ops._ensure_log_symlinks()
    assert all(r.ok for r in results)
    new_target = (home / ".nyxGPT" / "logs" / "nyxgpt-api.log").resolve()
    assert new_target != stale_target.resolve()


@pytest.mark.unit
def test_ensure_log_symlinks_reports_failure_on_exception(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_brew_prefix", lambda: tmp_path / "brew")

    def raise_symlink_to(self, target):
        raise OSError("cannot symlink")

    monkeypatch.setattr(ops.Path, "symlink_to", raise_symlink_to)

    results = ops._ensure_log_symlinks()
    assert all(r.ok is False for r in results)
    assert all("Failed to symlink" in r.message for r in results)


@pytest.mark.unit
def test_ensure_log_symlinks_skips_ollama_when_compose_container_present(monkeypatch, tmp_path):
    """Compose mode: `follow-ollama-logs.sh` owns ollama.log, so this must leave it alone.

    Regression test for the #3276 review finding: previously this
    unconditionally replaced ollama.log with a (dangling, in Compose mode)
    symlink, clobbering the file the log-follower LaunchAgent was actively
    appending to.
    """
    home = tmp_path / "home"
    (home / ".nyxGPT" / "logs").mkdir(parents=True)
    real_log = home / ".nyxGPT" / "logs" / "ollama.log"
    real_log.write_text("existing ollama container logs\n", encoding="utf-8")

    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_brew_prefix", lambda: tmp_path / "brew")
    monkeypatch.setattr(
        ops,
        "_docker_container_state",
        lambda name: "running" if name == ops.OLLAMA_CONTAINER_NAME else "absent",
    )

    results = ops._ensure_log_symlinks()
    assert all(r.ok for r in results)
    ollama_result = next(r for r in results if "ollama.log" in r.message)
    assert "Skipped" in ollama_result.message

    assert not real_log.is_symlink()
    assert real_log.read_text(encoding="utf-8") == "existing ollama container logs\n"


@pytest.mark.unit
def test_ensure_log_symlinks_symlinks_ollama_when_no_compose_container(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_brew_prefix", lambda: tmp_path / "brew")
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")

    results = ops._ensure_log_symlinks()
    assert all(r.ok for r in results)
    ollama_link = home / ".nyxGPT" / "logs" / "ollama.log"
    assert ollama_link.is_symlink()


@pytest.mark.unit
def test_install_step_order_does_not_clobber_compose_ollama_log(monkeypatch, tmp_path):
    """Integration-style check of the two steps in `install()`'s actual order.

    Runs `_install_ollama_launchagent()` followed by `_ensure_log_symlinks()`
    -- the order `install()` uses -- against a fake home dir with a
    pre-existing real ollama.log (simulating an already-running Compose
    follower), which is exactly the scenario the #3276 review found broken.
    """
    tpl = tmp_path / "com.nyxgpt.ollama-logs.plist"
    tpl.write_text("<plist/>", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".nyxGPT" / "logs").mkdir(parents=True)
    real_log = home / ".nyxGPT" / "logs" / "ollama.log"
    real_log.write_text("existing ollama container logs\n", encoding="utf-8")

    monkeypatch.setattr(ops, "_find_launchagent_template", lambda name: (tpl, [tpl]))
    monkeypatch.setattr(ops.Path, "home", lambda: home)
    monkeypatch.setattr(ops, "_brew_prefix", lambda: tmp_path / "brew")
    monkeypatch.setattr(ops, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(
        ops,
        "_docker_container_state",
        lambda name: "running" if name == ops.OLLAMA_CONTAINER_NAME else "absent",
    )

    ops._install_ollama_launchagent()
    ops._ensure_log_symlinks()

    assert not real_log.is_symlink()
    assert real_log.read_text(encoding="utf-8") == "existing ollama container logs\n"


# --- _create_dist_tarball ---


@pytest.mark.unit
def test_create_dist_tarball_creates_gzip_archive(tmp_path):
    tar_path = ops._create_dist_tarball(tmp_path, "nyxgpt-api", "1.2.3")
    assert tar_path == tmp_path / "dist" / "nyxgpt-api-1.2.3.tar.gz"
    assert tar_path.exists()
    with tarfile.open(tar_path, "r:gz") as tf:
        names = tf.getnames()
    assert any("README.txt" in n for n in names)
    # Temp staging dir must be cleaned up.
    assert not (tmp_path / "dist" / ".tmp-nyxgpt-api-1.2.3").exists()


@pytest.mark.unit
def test_create_dist_tarball_overwrites_existing_tarball_and_tmp(tmp_path):
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
    with tarfile.open(tar_path, "r:gz"):
        pass


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
    repo_root = tmp_path / "repo"
    (repo_root / "homebrew").mkdir(parents=True)
    (repo_root / "homebrew" / "nyxgpt-api.rb").write_text(
        'sha256 "0000000000000000000000000000000000000000000000000000000000000000"\n',
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
    assert "sha256" in formula.read_text(encoding="utf-8")
    assert any(cmd[:2] in (["brew", "install"], ["brew", "reinstall"]) for cmd in run_calls)
    assert any(cmd[:3] == ["brew", "services", "start"] for cmd in run_calls)


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
    repo_root = tmp_path / "repo"
    (repo_root / "homebrew").mkdir(parents=True)
    (repo_root / "homebrew" / "nyxgpt-web.rb").write_text(
        'url "__NYXGPT_WEB_URL__"\nsha256 "__NYXGPT_WEB_SHA256__"\n',
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
    assert any(cmd[:2] in (["brew", "install"], ["brew", "reinstall"]) for cmd in run_calls)
    assert any(cmd[:3] == ["brew", "services", "start"] for cmd in run_calls)


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
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_log_symlinks", return_value=ok_results),
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

    def fake_run(cmd, check=True):
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
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")

    web_dir = tmp_path / "web"
    (web_dir / "node_modules").mkdir(parents=True)

    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")
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
def test_env_sync_cli_wrapper_prints_details_on_failure(tmp_path, capsys):
    cfg_path = tmp_path / "missing-config.ini"
    env_path = tmp_path / ".env"

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
        patch.object(ops, "_install_homebrew_api", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_ollama_service", return_value=ok_results),
        patch.object(ops, "_ensure_log_symlinks", return_value=ok_results),
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
        patch.object(ops, "_install_homebrew_api", return_value=[]),
        patch.object(ops, "_install_homebrew_web", return_value=[]),
        patch.object(ops, "_ensure_log_symlinks", return_value=[]),
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
    (cfg_dir / "config.ini").write_text("[project]\nname=nyxGPT\n", encoding="utf-8")
    monkeypatch.setattr(ops.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(ops, "_which", lambda _: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "running")

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
def test_env_sync_logs_summary(caplog, tmp_path):
    cfg_path = tmp_path / "config.ini"
    _write_config(cfg_path, api_key="cli-api-key")
    env_path = tmp_path / ".env"

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
    def fake_run(cmd, check=True):
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


@pytest.mark.unit
def test_observability_cli_entrypoint_returns_zero_on_success(capsys):
    with patch.object(ops, "_start_observability_stack", return_value=[ops.OpsResult(True, "up")]):
        rc = ops.observability(MagicMock())
        assert rc == 0
        assert "[OK]" in capsys.readouterr().out


@pytest.mark.unit
def test_observability_cli_entrypoint_returns_nonzero_on_failure(capsys):
    with patch.object(
        ops,
        "_start_observability_stack",
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

    def fake_run(cmd, check=True):
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
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=1, stderr="tap failed"))
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
        'auth_api_key = "REPLACE_WITH_A_REAL_KEY"\n'
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
    assert 'auth_api_key = "my-key"' in content


# --- Terraform: _terraform_init_plan_apply ---


def _fake_terraform_run(*, init_rc=0, plan_rc=0, apply_rc=0):
    def fake_run(cmd, check=True):
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
        lambda cmd, check=True: CP(stdout='{"api_url": {"value": "http://localhost:8000"}}'),
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
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok) as a,
        patch.object(ops, "_terraform_stack_health", return_value=ok) as h,
    ):
        rc = ops._install_terraform(args)
    assert rc == 0
    b.assert_called_once()
    t.assert_called_once_with("k")
    a.assert_called_once()
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
def test_down_terraform_no_binary(monkeypatch, capsys):
    monkeypatch.setattr(ops, "_which", lambda prog: None)
    rc = ops._down_terraform(SimpleNamespace())
    assert rc == 2
    assert "nothing to destroy" in capsys.readouterr().out


@pytest.mark.unit
def test_down_terraform_destroy_success(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/opt/homebrew/bin/terraform")
    monkeypatch.setattr(
        ops, "_run", lambda cmd, check=True: CP(returncode=0, stdout="Destroy complete")
    )
    rc = ops._down_terraform(SimpleNamespace())
    assert rc == 0


@pytest.mark.unit
def test_down_terraform_destroy_failure(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/opt/homebrew/bin/terraform")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=1, stderr="boom"))
    rc = ops._down_terraform(SimpleNamespace())
    assert rc == 2


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
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=1, stderr="refused"))
    results = ops._ensure_kubectl_and_cluster()
    assert results[0].ok is False
    assert "No reachable" in results[0].message


@pytest.mark.unit
def test_ensure_kubectl_and_cluster_reachable(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=0))
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
def test_build_and_load_k8s_image_build_fails(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/docker")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=1, stderr="build boom"))
    results = ops._build_and_load_k8s_image()
    assert results[0].ok is False
    assert "docker build failed" in results[0].message


@pytest.mark.unit
def test_build_and_load_k8s_image_skips_load_on_docker_desktop(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/docker")

    def fake_run(cmd, check=True):
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
def test_build_and_load_k8s_image_loads_into_kind(monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/" + prog if prog in ("docker", "kind") else None
    )
    run_calls = []

    def fake_run(cmd, check=True):
        run_calls.append(cmd)
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
def test_build_and_load_k8s_image_unrecognized_context(monkeypatch):
    monkeypatch.setattr(
        ops, "_which", lambda prog: "/usr/local/bin/docker" if prog == "docker" else None
    )

    def fake_run(cmd, check=True):
        if cmd[:2] == ["docker", "build"]:
            return CP(returncode=0)
        if cmd[:2] == ["kubectl", "config"]:
            return CP(stdout="some-other-cluster")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._build_and_load_k8s_image()
    assert all(r.ok for r in results)
    assert any("Unrecognized cluster context" in r.message for r in results)


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
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=0, stdout="applied"))
    results = ops._kubectl_apply_kustomization()
    assert results[0].ok is True


@pytest.mark.unit
def test_kubectl_apply_kustomization_failure(monkeypatch):
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=1, stderr="boom"))
    results = ops._kubectl_apply_kustomization()
    assert results[0].ok is False


@pytest.mark.unit
def test_k8s_stack_health_reports_pods_hpa_service(monkeypatch):
    def fake_run(cmd, check=True):
        if cmd[4] == "pods":
            return CP(
                returncode=0,
                stdout="nyxgpt-api-blue-abc=Running;nyxgpt-api-green-def=Pending;",
            )
        if cmd[4] == "hpa":
            return CP(returncode=0, stdout="nyxgpt-api-blue   Deployment/nyxgpt-api-blue\n")
        if cmd[4] == "svc":
            return CP(returncode=0, stdout="nyxgpt-api   ClusterIP\n")
        raise AssertionError(f"unexpected: {cmd}")

    monkeypatch.setattr(ops, "_run", fake_run)
    results = ops._k8s_stack_health()
    pod_results = [r for r in results if r.message.startswith("pod ")]
    assert len(pod_results) == 2
    assert pod_results[0].ok is True
    assert pod_results[1].ok is False
    assert any("HPA" in r.message and r.ok for r in results)
    assert any("Service nyxgpt-api found" in r.message for r in results)


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
        patch.object(ops, "_ensure_k8s_secret", return_value=ok) as s,
        patch.object(ops, "_kubectl_apply_kustomization", return_value=ok) as a,
        patch.object(ops, "_k8s_stack_health", return_value=ok) as h,
    ):
        rc = ops._install_kubernetes(args)
    assert rc == 0
    c.assert_called_once()
    b.assert_called_once()
    s.assert_called_once_with("k")
    a.assert_called_once()
    h.assert_called_once()
    assert "[OK]" in capsys.readouterr().out


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
        patch.object(ops, "_ensure_k8s_secret") as s,
        patch.object(ops, "_kubectl_apply_kustomization") as a,
        patch.object(ops, "_k8s_stack_health") as h,
    ):
        rc = ops._install_kubernetes(args)
    assert rc == 2
    b.assert_not_called()
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
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=0, stdout="deleted"))
    rc = ops._down_kubernetes(SimpleNamespace())
    assert rc == 0


@pytest.mark.unit
def test_down_kubernetes_delete_failure(monkeypatch):
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/kubectl")
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=1, stderr="boom"))
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
        patch.object(ops, "_terraform_init_plan_apply", return_value=ok),
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
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=0, stdout="destroyed"))
    results = ops.down_terraform()
    assert len(results) == 1
    assert results[0].ok is True
    assert capsys.readouterr().out == ""


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
    monkeypatch.setattr(ops, "_run", lambda cmd, check=True: CP(returncode=0, stdout="deleted"))
    results = ops.down_kubernetes()
    assert len(results) == 1
    assert results[0].ok is True
    assert capsys.readouterr().out == ""


@pytest.mark.unit
def test_infra_status_reports_terraform_and_kubernetes(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "running", "web": "absent"})

    def fake_which(prog):
        return "/usr/local/bin/kubectl" if prog == "kubectl" else None

    def fake_run(cmd, check=True):
        return CP(returncode=0, stdout="nyxgpt-api-abc   1/1   Running\n")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)

    result = ops.infra_status()
    assert result["terraform"]["deployed"] is True
    assert result["terraform"]["containers"] == {"api": "running", "web": "absent"}
    assert result["kubernetes"]["available"] is True
    assert result["kubernetes"]["deployed"] is True
    assert result["kubernetes"]["namespace"] == "nyxgpt"
    assert result["kubernetes"]["pods"] == ["nyxgpt-api-abc   1/1   Running"]


@pytest.mark.unit
def test_infra_status_reports_nothing_deployed(monkeypatch):
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent"})
    monkeypatch.setattr(ops, "_which", lambda prog: None)

    result = ops.infra_status()
    assert result["terraform"]["deployed"] is False
    assert result["kubernetes"]["available"] is False
    assert result["kubernetes"]["deployed"] is False
    assert result["kubernetes"]["pods"] == []


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

    def fake_run(cmd, check=True):
        if cmd[:4] == ["kubectl", "-n", "nyxgpt", "get"] and "pods" in cmd:
            return CP(returncode=0, stdout="nyxgpt-api-blue-abc   1/1   Running\n")
        return CP(stdout="")

    monkeypatch.setattr(ops, "_which", fake_which)
    monkeypatch.setattr(ops, "_run", fake_run)
    monkeypatch.setattr(ops, "_brew_services_snapshot", lambda: {})
    monkeypatch.setattr(ops, "_docker_container_state", lambda name: "absent")
    monkeypatch.setattr(ops, "_compose_stack_snapshot", lambda: {})

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Kubernetes (nyxgpt namespace" in out
    assert "nyxgpt-api-blue-abc" in out


@pytest.mark.unit
def test_doctor_flags_stale_terraform_state(monkeypatch, tmp_path, capsys):
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir()
    (tf_dir / "terraform.tfstate").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(ops, "TERRAFORM_DIR", tf_dir)
    monkeypatch.setattr(ops, "terraform_stack_state", lambda: {"api": "absent", "web": "absent"})
    monkeypatch.setattr(ops, "_which", lambda prog: "/usr/local/bin/fake")
    monkeypatch.setattr(ops, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "Terraform state exists but no nyxgpt-tf-* containers are running" in out
