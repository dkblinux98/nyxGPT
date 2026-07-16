"""Unit tests for the /api/v1/self-heal/* endpoints.

These exercise src/nyxgpt/app.py's self_heal_* route handlers with
nyxgpt.self_heal mocked out, so no docker daemon is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt import self_heal
from nyxgpt.app import app

pytestmark = pytest.mark.unit


def test_self_heal_status_endpoint_returns_module_status():
    expected = {
        "enabled": True,
        "components": [
            {
                "service": "api",
                "container": "nyxgpt-api-1",
                "state": "running",
                "health": "healthy",
                "healthy": True,
            }
        ],
        "unhealthy_count": 0,
        "events": [],
    }

    with patch("nyxgpt.app.self_heal_module.status", return_value=expected) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/self-heal/status")

    assert response.status_code == 200
    assert response.json() == expected
    mock_status.assert_called_once()


def test_self_heal_toggle_endpoint_enable():
    with patch("nyxgpt.app.self_heal_module.set_enabled", return_value=True) as mock_set:
        client = TestClient(app)
        response = client.post("/api/v1/self-heal/toggle", json={"enabled": True})

    assert response.status_code == 200
    assert response.json() == {"enabled": True}
    mock_set.assert_called_once_with(True)


def test_self_heal_toggle_endpoint_requires_enabled_field():
    client = TestClient(app)
    response = client.post("/api/v1/self-heal/toggle", json={})
    assert response.status_code == 400


def test_self_heal_heal_endpoint_heals_everything():
    result = {
        "checked": [{"service": "web", "state": "exited", "health": "", "healthy": False}],
        "healed": [
            {
                "service": "web",
                "reason": "state=exited health=n/a",
                "action": "restart",
                "ok": True,
                "restart_count": 1,
                "message": "Restarted web",
                "ts": 1234.0,
            }
        ],
    }
    with patch("nyxgpt.app.self_heal_module.heal_now", return_value=result) as mock_heal:
        client = TestClient(app)
        response = client.post("/api/v1/self-heal/heal", json={})

    assert response.status_code == 200
    assert response.json() == result
    mock_heal.assert_called_once_with(service=None)


def test_self_heal_heal_endpoint_targets_one_service():
    result = {"checked": [], "healed": []}
    with patch("nyxgpt.app.self_heal_module.heal_now", return_value=result) as mock_heal:
        client = TestClient(app)
        response = client.post("/api/v1/self-heal/heal", json={"service": "api"})

    assert response.status_code == 200
    mock_heal.assert_called_once_with(service="api")


def test_self_heal_heal_endpoint_returns_404_for_unknown_service():
    result = {"checked": [], "healed": [], "error": "Unknown or not-running component: nope"}
    with patch("nyxgpt.app.self_heal_module.heal_now", return_value=result):
        client = TestClient(app)
        response = client.post("/api/v1/self-heal/heal", json={"service": "nope"})

    assert response.status_code == 404
    assert "Unknown or not-running component" in response.json()["error"]["message"]


def test_self_heal_logs_endpoint_returns_service_logs():
    result = self_heal.HealResult(
        True, "Fetched last 100 log line(s) for glitchtip", "confirm: http://..."
    )
    with patch("nyxgpt.app.self_heal_module.component_logs", return_value=result) as mock_logs:
        client = TestClient(app)
        response = client.get(
            "/api/v1/self-heal/logs", params={"service": "glitchtip", "tail": 100}
        )

    assert response.status_code == 200
    assert response.json() == {"service": "glitchtip", "tail": 100, "logs": "confirm: http://..."}
    mock_logs.assert_called_once_with("glitchtip", tail=100)


def test_self_heal_logs_endpoint_defaults_tail_to_200():
    result = self_heal.HealResult(True, "Fetched last 200 log line(s) for api", "log output")
    with patch("nyxgpt.app.self_heal_module.component_logs", return_value=result) as mock_logs:
        client = TestClient(app)
        response = client.get("/api/v1/self-heal/logs", params={"service": "api"})

    assert response.status_code == 200
    mock_logs.assert_called_once_with("api", tail=200)


def test_self_heal_logs_endpoint_returns_502_on_failure():
    result = self_heal.HealResult(False, "Failed to fetch logs for nope", "no such service")
    with patch("nyxgpt.app.self_heal_module.component_logs", return_value=result):
        client = TestClient(app)
        response = client.get("/api/v1/self-heal/logs", params={"service": "nope"})

    assert response.status_code == 502
    assert "Failed to fetch logs for nope" in response.json()["error"]["message"]


def test_admin_overview_includes_self_heal_status():
    with patch(
        "nyxgpt.app.self_heal_module.status", return_value={"enabled": True, "components": []}
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/overview")

    assert response.status_code == 200
    assert response.json()["self_heal"] == {"enabled": True, "components": []}
