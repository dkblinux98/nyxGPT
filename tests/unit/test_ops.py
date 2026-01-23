from unittest.mock import patch, MagicMock

import pytest

from nyxgpt import ops


@pytest.mark.unit
def test_ops_install_returns_zero_when_all_ok(capsys):
    # Mock internal steps to all succeed
    ok_results = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_install_scripts", return_value=ok_results),
        patch.object(ops, "_ensure_web_deps", return_value=ok_results),
        patch.object(ops, "_install_cassandra_launchagent", return_value=ok_results),
        patch.object(ops, "_install_homebrew_web", return_value=ok_results),
        patch.object(ops, "_ensure_log_symlinks", return_value=ok_results),
    ):
        rc = ops.install(MagicMock())
        assert rc == 0
        out = capsys.readouterr().out
        assert "[OK]" in out


@pytest.mark.unit
def test_ops_install_returns_nonzero_when_any_fail(capsys):
    mixed = [ops.OpsResult(True, "ok"), ops.OpsResult(False, "bad", "details")]
    with (
        patch.object(ops, "_install_scripts", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_ensure_web_deps", return_value=[ops.OpsResult(True, "ok")]),
        patch.object(ops, "_install_cassandra_launchagent", return_value=mixed),
        patch.object(
            ops, "_install_homebrew_web", return_value=[ops.OpsResult(True, "ok")]
        ),
        patch.object(
            ops, "_ensure_log_symlinks", return_value=[ops.OpsResult(True, "ok")]
        ),
    ):
        rc = ops.install(MagicMock())
        assert rc == 2
        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "details" in out


@pytest.mark.unit
def test_ops_restart_all_ok(capsys):
    ok = [ops.OpsResult(True, "ok")]
    with (
        patch.object(ops, "_restart_brew_service", return_value=ok) as rb,
        patch.object(ops, "_restart_docker_container", return_value=ok) as rd,
        patch.object(ops, "_restart_launchagent", return_value=ok) as rl,
    ):
        args = MagicMock()
        args.target = "all"
        rc = ops.restart(args)
        assert rc == 0

        # ensure we attempted expected components
        assert rb.call_count == 3  # api, web, ollama
        rd.assert_called_once_with("nyxgpt-cassandra")
        rl.assert_called_once_with("com.nyxgpt.cassandra-logs")

        out = capsys.readouterr().out
        assert "[OK]" in out


@pytest.mark.unit
def test_ops_restart_returns_nonzero_on_failure(capsys):
    ok = [ops.OpsResult(True, "ok")]
    bad = [ops.OpsResult(False, "bad", "details")]
    with (
        patch.object(ops, "_restart_brew_service", side_effect=[ok, bad, ok]),
        patch.object(ops, "_restart_docker_container", return_value=ok),
        patch.object(ops, "_restart_launchagent", return_value=ok),
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
        patch.object(ops, "_restart_brew_service", return_value=ok) as rb,
        patch.object(ops, "_restart_docker_container", return_value=ok) as rd,
        patch.object(ops, "_restart_launchagent", return_value=ok) as rl,
    ):
        args = MagicMock()
        args.target = "api"
        rc = ops.restart(args)
        assert rc == 0

        rb.assert_called_once_with("nyxgpt-api")
        rd.assert_not_called()
        rl.assert_not_called()

        out = capsys.readouterr().out
        assert "Restarted" in out or "[OK]" in out


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

    rc = ops.status(MagicMock())
    assert rc == 0
    out = capsys.readouterr().out
    assert "Homebrew services" in out
    assert "com.nyxgpt.cassandra-logs" in out
    assert "nyxgpt-cassandra" in out


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

    rc = ops.doctor(MagicMock())
    assert rc == 2
    out = capsys.readouterr().out
    assert "doctor: FAIL" in out
    assert "Missing config" in out
