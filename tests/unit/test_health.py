"""Unit tests for src/nyxgpt/health.py (health aggregation helpers).

Related: #2699
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nyxgpt import health

pytestmark = pytest.mark.unit


# --- check_ollama ---


def test_check_ollama_ok_when_reachable():
    with patch("nyxgpt.health.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = MagicMock()
        result = health.check_ollama("http://127.0.0.1:11434")

    assert result.ok is True
    assert result.name == "ollama"
    assert result.applicable is True


def test_check_ollama_fails_when_unreachable():
    with patch("nyxgpt.health.urllib.request.urlopen", side_effect=OSError("Connection refused")):
        result = health.check_ollama("http://127.0.0.1:11434")

    assert result.ok is False
    assert "Connection refused" in result.detail


# --- check_cassandra ---


def test_check_cassandra_not_applicable_when_rag_disabled():
    result = health.check_cassandra(rag_enabled=False)

    assert result.ok is True
    assert result.applicable is False


def test_check_cassandra_ok_when_query_succeeds():
    mock_session = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_session.return_value = mock_session

    with (
        patch("nyxgpt.rag.vectorstore_cassandra._cassandra_cfg", return_value=object()),
        patch("nyxgpt.rag.vectorstore_cassandra.get_connection_pool", return_value=mock_pool),
    ):
        result = health.check_cassandra(rag_enabled=True)

    assert result.ok is True
    assert result.applicable is True
    mock_session.execute.assert_called_once()


def test_check_cassandra_fails_when_connection_raises():
    with patch(
        "nyxgpt.rag.vectorstore_cassandra._cassandra_cfg",
        side_effect=RuntimeError("no hosts resolved"),
    ):
        result = health.check_cassandra(rag_enabled=True)

    assert result.ok is False
    assert "no hosts resolved" in result.detail


# --- compute_alerts ---


def test_compute_alerts_empty_when_healthy():
    metrics = {
        "memory": {"percent": 10.0},
        "cpu": {"process_percent": 5.0, "system_percent": 12.0},
        "errors": {"rate_percent": 0.0},
    }
    alerts = health.compute_alerts(metrics, [])

    assert alerts == []


def test_compute_alerts_warns_on_elevated_memory():
    metrics = {
        "memory": {"percent": 80.0},
        "cpu": {"process_percent": 5.0, "system_percent": 12.0},
        "errors": {"rate_percent": 0.0},
    }
    alerts = health.compute_alerts(metrics, [])

    assert len(alerts) == 1
    assert alerts[0].severity == "warning"
    assert "Memory usage" in alerts[0].message


def test_compute_alerts_critical_on_exhausted_memory():
    metrics = {
        "memory": {"percent": 95.0},
        "cpu": {"process_percent": 5.0, "system_percent": 12.0},
        "errors": {"rate_percent": 0.0},
    }
    alerts = health.compute_alerts(metrics, [])

    assert len(alerts) == 1
    assert alerts[0].severity == "critical"


def test_compute_alerts_uses_normalized_system_cpu_not_process_cpu():
    """Regression test for #3465: a multi-threaded process's own CPU usage
    (`cpu.process_percent`) is not normalized by core count and can read
    >100% on an idle multi-core machine. The alert must key off the
    system-wide, core-normalized `cpu.system_percent` instead -- the same
    field the Resource Metrics history panel already renders correctly.
    """
    metrics = {
        "memory": {"percent": 10.0},
        "cpu": {"process_percent": 108.0, "system_percent": 12.0},
        "errors": {"rate_percent": 0.0},
    }
    alerts = health.compute_alerts(metrics, [])

    assert alerts == []


def test_compute_alerts_warns_on_elevated_system_cpu():
    metrics = {
        "memory": {"percent": 10.0},
        "cpu": {"process_percent": 5.0, "system_percent": 85.0},
        "errors": {"rate_percent": 0.0},
    }
    alerts = health.compute_alerts(metrics, [])

    assert len(alerts) == 1
    assert alerts[0].severity == "warning"
    assert "CPU usage" in alerts[0].message


def test_compute_alerts_critical_on_high_system_cpu():
    metrics = {
        "memory": {"percent": 10.0},
        "cpu": {"process_percent": 5.0, "system_percent": 97.0},
        "errors": {"rate_percent": 0.0},
    }
    alerts = health.compute_alerts(metrics, [])

    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert "CPU usage" in alerts[0].message


def test_compute_alerts_cpu_clears_when_system_percent_drops_below_threshold():
    high = health.compute_alerts(
        {
            "memory": {"percent": 10.0},
            "cpu": {"process_percent": 5.0, "system_percent": 97.0},
            "errors": {"rate_percent": 0.0},
        },
        [],
    )
    low = health.compute_alerts(
        {
            "memory": {"percent": 10.0},
            "cpu": {"process_percent": 5.0, "system_percent": 12.0},
            "errors": {"rate_percent": 0.0},
        },
        [],
    )

    assert any(a.severity == "critical" and "CPU usage" in a.message for a in high)
    assert not any("CPU usage" in a.message for a in low)


def test_compute_alerts_includes_unreachable_dependency():
    dep = health.DependencyCheck(name="ollama", ok=False, detail="timed out")
    alerts = health.compute_alerts(None, [dep])

    assert len(alerts) == 1
    assert alerts[0].severity == "critical"
    assert "ollama" in alerts[0].message


def test_compute_alerts_ignores_inapplicable_dependency():
    dep = health.DependencyCheck(name="cassandra", ok=False, detail="not checked", applicable=False)
    alerts = health.compute_alerts(None, [dep])

    assert alerts == []


def test_compute_alerts_handles_missing_resource_metrics():
    alerts = health.compute_alerts(None, [])

    assert alerts == []


def test_compute_alerts_warns_on_elevated_disk_usage():
    metrics = {
        "memory": {"percent": 10.0},
        "cpu": {"process_percent": 5.0},
        "disk": {"percent": 85.0},
        "errors": {"rate_percent": 0.0},
    }
    alerts = health.compute_alerts(metrics, [])

    assert len(alerts) == 1
    assert alerts[0].severity == "warning"
    assert "Disk usage" in alerts[0].message


def test_compute_alerts_critical_on_exhausted_disk():
    metrics = {
        "memory": {"percent": 10.0},
        "cpu": {"process_percent": 5.0},
        "disk": {"percent": 95.0},
        "errors": {"rate_percent": 0.0},
    }
    alerts = health.compute_alerts(metrics, [])

    assert len(alerts) == 1
    assert alerts[0].severity == "critical"


def test_alert_to_dict_defaults_source_to_local():
    alert = health.Alert(severity="warning", message="test")

    assert alert.to_dict() == {"severity": "warning", "message": "test", "source": "local"}


# --- fetch_grafana_alerts ---


def _cfg_with_monitoring(**overrides):
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["monitoring"] = {
        "enabled": "true",
        "grafana_ui_url": "http://localhost:3001",
        "grafana_admin_password": "test-password",
        **overrides,
    }
    return cfg


def test_fetch_grafana_alerts_returns_none_when_monitoring_disabled():
    from configparser import ConfigParser

    cfg = ConfigParser()
    cfg["monitoring"] = {"enabled": "false"}

    assert health.fetch_grafana_alerts(cfg) is None


def test_fetch_grafana_alerts_returns_none_when_password_unset():
    cfg = _cfg_with_monitoring(grafana_admin_password="")

    assert health.fetch_grafana_alerts(cfg) is None


def test_fetch_grafana_alerts_returns_none_on_request_failure():
    import httpx

    cfg = _cfg_with_monitoring()

    with patch("nyxgpt.health.httpx.get", side_effect=httpx.ConnectError("connection refused")):
        assert health.fetch_grafana_alerts(cfg) is None


def test_fetch_grafana_alerts_maps_firing_alerts():
    cfg = _cfg_with_monitoring()
    raw_alerts = [
        {
            "labels": {"alertname": "NyxGPTHighCPU", "severity": "critical"},
            "annotations": {"summary": "CPU usage above 95%"},
        },
        {
            "labels": {"alertname": "NyxGPTHighLatency", "severity": "warning"},
            "annotations": {},
        },
    ]
    mock_response = MagicMock()
    mock_response.json.return_value = raw_alerts
    mock_response.raise_for_status.return_value = None

    with patch("nyxgpt.health.httpx.get", return_value=mock_response) as mock_get:
        alerts = health.fetch_grafana_alerts(cfg)

    assert mock_get.call_args.kwargs["auth"] == ("admin", "test-password")
    assert len(alerts) == 2
    assert alerts[0] == health.Alert(
        severity="critical", message="CPU usage above 95%", source="grafana"
    )
    assert alerts[1].source == "grafana"
    assert "NyxGPTHighLatency" in alerts[1].message


def test_fetch_grafana_alerts_returns_empty_list_when_nothing_firing():
    cfg = _cfg_with_monitoring()
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_response.raise_for_status.return_value = None

    with patch("nyxgpt.health.httpx.get", return_value=mock_response):
        alerts = health.fetch_grafana_alerts(cfg)

    assert alerts == []
