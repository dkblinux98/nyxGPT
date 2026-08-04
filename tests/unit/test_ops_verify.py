"""Unit tests for `nyxgpt ops verify`'s orchestration in `ops.py` (#3555 / P6-18).

`nyxgpt.verify`'s own pure logic (traffic generation, Prometheus/Grafana
assertions, screenshots) is covered in `tests/unit/test_verify.py`; these
tests cover `ops.verify()`'s boot/teardown/gating decisions, mocking
`nyxgpt.verify` entirely so they never touch a network or subprocess.
"""

from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nyxgpt import ops, self_heal
from nyxgpt.verify import VerifyCheck


def _cfg_with_monitoring_enabled(tmp_path: Path) -> Path:
    cfg_path = tmp_path / "config.ini"
    parser = ConfigParser()
    parser["monitoring"] = {"enabled": "true"}
    parser["auth"] = {"api_key": "test-key"}
    with cfg_path.open("w") as f:
        parser.write(f)
    return cfg_path


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_verify_missing_config_returns_2(capsys):
    rc = ops.verify(SimpleNamespace())
    assert rc == 2
    assert "Missing config" in capsys.readouterr().out


def test_verify_monitoring_disabled_returns_2(tmp_path, capsys):
    cfg_path = tmp_path / ".nyxGPT" / "config.ini"
    cfg_path.parent.mkdir(parents=True)
    parser = ConfigParser()
    parser["monitoring"] = {"enabled": "false"}
    with cfg_path.open("w") as f:
        parser.write(f)

    rc = ops.verify(SimpleNamespace())
    assert rc == 2
    assert "Monitoring is disabled" in capsys.readouterr().out


def test_verify_boot_failure_returns_early_without_teardown(tmp_path, capsys):
    (tmp_path / ".nyxGPT").mkdir()
    _cfg_with_monitoring_enabled(tmp_path / ".nyxGPT")

    with (
        patch.object(
            ops,
            "_boot_verify_stack",
            return_value=[ops.OpsResult(False, "docker not found")],
        ) as boot,
        patch.object(ops, "_teardown_verify_stack") as teardown,
        patch.object(ops, "_wait_for_verify_stack_healthy") as wait_healthy,
    ):
        rc = ops.verify(SimpleNamespace())

    assert rc == 2
    boot.assert_called_once()
    wait_healthy.assert_not_called()
    teardown.assert_not_called()
    assert "docker not found" in capsys.readouterr().out


def test_verify_skip_boot_never_boots_or_tears_down(tmp_path, capsys):
    (tmp_path / ".nyxGPT").mkdir()
    _cfg_with_monitoring_enabled(tmp_path / ".nyxGPT")

    with (
        patch.object(ops, "_boot_verify_stack") as boot,
        patch.object(ops, "_teardown_verify_stack") as teardown,
        patch.object(ops, "detect_deployment_mode", return_value=ops.DeploymentMode({}, {}, [])),
        patch.object(ops, "_verify_repo_index_path", return_value="/root/.nyxGPT/fixture"),
        patch.object(ops, "_grafana_admin_password", return_value="pw"),
        patch.object(ops.verify_mod, "snapshot_counters", return_value={}),
        patch.object(
            ops.verify_mod,
            "generate_traffic",
            return_value=(SimpleNamespace(), [VerifyCheck(True, "chat ok")]),
        ),
        patch.object(ops.verify_mod, "assert_counter_deltas", return_value=[]),
        patch.object(ops.verify_mod, "resolve_touched_dashboards", return_value=[]),
    ):
        rc = ops.verify(SimpleNamespace(skip_boot=True))

    assert rc == 0
    boot.assert_not_called()
    teardown.assert_not_called()
    assert "chat ok" in capsys.readouterr().out


def test_verify_tears_down_by_default_after_a_successful_boot(tmp_path):
    (tmp_path / ".nyxGPT").mkdir()
    _cfg_with_monitoring_enabled(tmp_path / ".nyxGPT")

    with (
        patch.object(ops, "_boot_verify_stack", return_value=[ops.OpsResult(True, "booted")]),
        patch.object(
            ops, "_wait_for_verify_stack_healthy", return_value=[ops.OpsResult(True, "healthy")]
        ),
        patch.object(ops, "_reconcile_grafana_provisioning", return_value=[]),
        patch.object(ops, "detect_deployment_mode", return_value=ops.DeploymentMode({}, {}, [])),
        patch.object(ops, "_verify_repo_index_path", return_value="/root/.nyxGPT/fixture"),
        patch.object(ops, "_grafana_admin_password", return_value="pw"),
        patch.object(ops.verify_mod, "snapshot_counters", return_value={}),
        patch.object(
            ops.verify_mod,
            "generate_traffic",
            return_value=(SimpleNamespace(), [VerifyCheck(True, "chat ok")]),
        ),
        patch.object(ops.verify_mod, "assert_counter_deltas", return_value=[]),
        patch.object(ops.verify_mod, "resolve_touched_dashboards", return_value=[]),
        patch.object(
            ops, "_teardown_verify_stack", return_value=[ops.OpsResult(True, "torn down")]
        ) as teardown,
    ):
        rc = ops.verify(SimpleNamespace())

    assert rc == 0
    teardown.assert_called_once()


def test_verify_keep_up_skips_teardown_after_a_successful_boot(tmp_path):
    (tmp_path / ".nyxGPT").mkdir()
    _cfg_with_monitoring_enabled(tmp_path / ".nyxGPT")

    with (
        patch.object(ops, "_boot_verify_stack", return_value=[ops.OpsResult(True, "booted")]),
        patch.object(
            ops, "_wait_for_verify_stack_healthy", return_value=[ops.OpsResult(True, "healthy")]
        ),
        patch.object(ops, "_reconcile_grafana_provisioning", return_value=[]),
        patch.object(ops, "detect_deployment_mode", return_value=ops.DeploymentMode({}, {}, [])),
        patch.object(ops, "_verify_repo_index_path", return_value="/root/.nyxGPT/fixture"),
        patch.object(ops, "_grafana_admin_password", return_value="pw"),
        patch.object(ops.verify_mod, "snapshot_counters", return_value={}),
        patch.object(
            ops.verify_mod,
            "generate_traffic",
            return_value=(SimpleNamespace(), [VerifyCheck(True, "chat ok")]),
        ),
        patch.object(ops.verify_mod, "assert_counter_deltas", return_value=[]),
        patch.object(ops.verify_mod, "resolve_touched_dashboards", return_value=[]),
        patch.object(ops, "_teardown_verify_stack") as teardown,
    ):
        rc = ops.verify(SimpleNamespace(keep_up=True))

    assert rc == 0
    teardown.assert_not_called()


def test_verify_health_wait_failure_still_tears_down_and_returns_2(tmp_path):
    (tmp_path / ".nyxGPT").mkdir()
    _cfg_with_monitoring_enabled(tmp_path / ".nyxGPT")

    with (
        patch.object(ops, "_boot_verify_stack", return_value=[ops.OpsResult(True, "booted")]),
        patch.object(
            ops,
            "_wait_for_verify_stack_healthy",
            return_value=[ops.OpsResult(False, "timed out")],
        ),
        patch.object(ops, "_reconcile_grafana_provisioning", return_value=[]),
        patch.object(
            ops, "_teardown_verify_stack", return_value=[ops.OpsResult(True, "torn down")]
        ) as teardown,
        patch.object(ops.verify_mod, "generate_traffic") as generate_traffic,
    ):
        rc = ops.verify(SimpleNamespace())

    assert rc == 2
    teardown.assert_called_once()
    generate_traffic.assert_not_called()


def test_verify_reports_prometheus_and_panel_check_failures(tmp_path, capsys):
    (tmp_path / ".nyxGPT").mkdir()
    _cfg_with_monitoring_enabled(tmp_path / ".nyxGPT")
    dashboard = tmp_path / "dash.json"
    dashboard.write_text("{}")

    with (
        patch.object(ops, "detect_deployment_mode", return_value=ops.DeploymentMode({}, {}, [])),
        patch.object(ops, "_verify_repo_index_path", return_value="/root/.nyxGPT/fixture"),
        patch.object(ops, "_grafana_admin_password", return_value="pw"),
        patch.object(ops, "_grafana_admin_client") as admin_client,
        patch.object(ops.verify_mod, "snapshot_counters", return_value={}),
        patch.object(
            ops.verify_mod,
            "generate_traffic",
            return_value=(SimpleNamespace(), [VerifyCheck(True, "chat ok")]),
        ),
        patch.object(
            ops.verify_mod,
            "assert_counter_deltas",
            return_value=[VerifyCheck(False, "counter delta FAILED: chat requests")],
        ),
        patch.object(ops.verify_mod, "resolve_touched_dashboards", return_value=[dashboard]),
        patch.object(
            ops.verify_mod,
            "assert_panel_queries",
            return_value=[VerifyCheck(False, "panel query FAILED: RAG / Panel A")],
        ),
        patch.object(ops.verify_mod, "dashboard_uid", return_value="dash-uid"),
        patch.object(ops.verify_mod, "capture_dashboard_screenshots", return_value=[]),
    ):
        admin_client.return_value.__enter__.return_value = object()
        rc = ops.verify(SimpleNamespace(skip_boot=True))

    out = capsys.readouterr().out
    assert rc == 2
    assert "counter delta FAILED: chat requests" in out
    assert "panel query FAILED: RAG / Panel A" in out


def test_verify_repo_index_path_translates_for_compose_mode(tmp_path):
    with patch.object(ops.verify_mod, "write_verify_repo_fixture") as write_fixture:
        mode = ops.DeploymentMode(native={}, compose={"api": "running"}, conflicts=[])
        path = ops._verify_repo_index_path(mode)

    write_fixture.assert_called_once()
    assert path == "/root/.nyxGPT/volumes/nyxgpt-data/rag-verify-repo"


def test_verify_repo_index_path_uses_host_path_for_native_mode(tmp_path):
    with patch.object(ops.verify_mod, "write_verify_repo_fixture") as write_fixture:
        mode = ops.DeploymentMode(native={"api": "running"}, compose={}, conflicts=[])
        path = ops._verify_repo_index_path(mode)

    write_fixture.assert_called_once()
    assert path == str(tmp_path / ".nyxGPT" / "volumes" / "nyxgpt-data" / "rag-verify-repo")


def _status(service: str, healthy: bool) -> self_heal.ComponentStatus:
    return self_heal.ComponentStatus(
        service=service,
        container=f"nyxgpt-{service}",
        state="running" if healthy else "starting",
        health="healthy" if healthy else "starting",
        healthy=healthy,
    )


def test_wait_for_verify_stack_healthy_returns_ok_once_all_healthy(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [_status(s, True) for s in ("api", "web", "ollama", "cassandra")],
    )
    results = ops._wait_for_verify_stack_healthy(timeout=5.0, poll_interval=0.01)
    assert len(results) == 1
    assert results[0].ok is True


def test_wait_for_verify_stack_healthy_times_out_and_names_pending_services(monkeypatch):
    monkeypatch.setattr(
        self_heal,
        "list_component_status",
        lambda: [_status("api", True), _status("web", False)],
    )
    results = ops._wait_for_verify_stack_healthy(
        services=("api", "web"), timeout=0.05, poll_interval=0.01
    )
    assert len(results) == 1
    assert results[0].ok is False
    assert "web" in results[0].message
