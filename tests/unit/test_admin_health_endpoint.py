"""Unit tests for GET /api/v1/admin/health (src/nyxgpt/app.py::admin_health).

Related: #2699
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.health import DependencyCheck

pytestmark = pytest.mark.unit


def test_admin_health_aggregates_status():
    resource_metrics = {
        "memory": {"rss_mb": 100, "vms_mb": 200, "percent": 10, "available_mb": 8000},
        "cpu": {"process_percent": 3.0, "system_percent": 12.0},
        "latency": {"avg_ms": 5, "p50_ms": 4, "p95_ms": 10, "p99_ms": 20},
        "queue": {"depth": 0, "total_requests": 10},
        "errors": {"count": 0, "rate_percent": 0.0},
    }

    class _Monitor:
        def get_metrics(self):
            class _M:
                def to_dict(self_inner):
                    return resource_metrics

            return _M()

        def record_request_latency(self, *args, **kwargs):
            pass

    ollama_check = DependencyCheck(name="ollama", ok=True, detail="Reachable")
    cassandra_check = DependencyCheck(
        name="cassandra", ok=True, detail="RAG disabled", applicable=False
    )

    with (
        patch("nyxgpt.app.get_resource_monitor", return_value=_Monitor()),
        patch("nyxgpt.app.health_module.check_ollama", return_value=ollama_check),
        patch("nyxgpt.app.health_module.check_cassandra", return_value=cassandra_check),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/health")

    assert response.status_code == 200
    body = response.json()
    assert body["service"]["status"] == "ok"
    assert isinstance(body["service"]["uptime_s"], (int, float))
    assert body["dependencies"] == [ollama_check.to_dict(), cassandra_check.to_dict()]
    assert body["resource_metrics"] == resource_metrics
    assert body["alerts"] == []


def test_admin_health_surfaces_dependency_and_resource_alerts():
    resource_metrics = {
        "memory": {"rss_mb": 100, "vms_mb": 200, "percent": 96.0, "available_mb": 50},
        "cpu": {"process_percent": 3.0, "system_percent": 12.0},
        "latency": {"avg_ms": 5, "p50_ms": 4, "p95_ms": 10, "p99_ms": 20},
        "queue": {"depth": 0, "total_requests": 10},
        "errors": {"count": 0, "rate_percent": 0.0},
    }

    class _Monitor:
        def get_metrics(self):
            class _M:
                def to_dict(self_inner):
                    return resource_metrics

            return _M()

        def record_request_latency(self, *args, **kwargs):
            pass

    ollama_check = DependencyCheck(name="ollama", ok=False, detail="Connection refused")
    cassandra_check = DependencyCheck(
        name="cassandra", ok=True, detail="RAG disabled", applicable=False
    )

    with (
        patch("nyxgpt.app.get_resource_monitor", return_value=_Monitor()),
        patch("nyxgpt.app.health_module.check_ollama", return_value=ollama_check),
        patch("nyxgpt.app.health_module.check_cassandra", return_value=cassandra_check),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/health")

    assert response.status_code == 200
    body = response.json()
    severities = {a["severity"] for a in body["alerts"]}
    assert "critical" in severities
    assert any("ollama" in a["message"] for a in body["alerts"])
    assert any("Memory usage" in a["message"] for a in body["alerts"])


def test_admin_health_handles_missing_resource_monitor():
    with (
        patch("nyxgpt.app.get_resource_monitor", return_value=None),
        patch(
            "nyxgpt.app.health_module.check_ollama",
            return_value=DependencyCheck(name="ollama", ok=True, detail="ok"),
        ),
        patch(
            "nyxgpt.app.health_module.check_cassandra",
            return_value=DependencyCheck(
                name="cassandra", ok=True, detail="RAG disabled", applicable=False
            ),
        ),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/health")

    assert response.status_code == 200
    body = response.json()
    assert body["resource_metrics"] is None
    assert body["alerts"] == []
