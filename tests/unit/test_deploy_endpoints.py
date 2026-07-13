"""Unit tests for the /api/v1/deploy/* endpoints (blue/green deployment).

These exercise src/nyxgpt/app.py's deploy_status/deploy_switch/deploy_rollback
route handlers with nyxgpt.deploy mocked out, so no kubectl/cluster is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.deploy import DeployResult

pytestmark = pytest.mark.unit


def test_deploy_status_endpoint_returns_module_status():
    expected = {
        "namespace": "nyxgpt",
        "active": "blue",
        "inactive": "green",
        "colors": {
            "blue": {"healthy": True, "message": "blue healthy (1/1 ready)"},
            "green": {"healthy": True, "message": "green healthy (1/1 ready)"},
        },
        "history": [],
    }

    with patch("nyxgpt.app.deploy_module.status", return_value=expected) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/deploy/status")

    assert response.status_code == 200
    assert response.json() == expected
    mock_status.assert_called_once()


def test_deploy_switch_endpoint_success():
    with patch(
        "nyxgpt.app.deploy_module.switch",
        return_value=DeployResult(True, "Switched traffic from blue to green"),
    ) as mock_switch:
        client = TestClient(app)
        response = client.post("/api/v1/deploy/switch", json={"to": "green"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Switched traffic from blue to green"
    _, kwargs = mock_switch.call_args
    assert kwargs["target"] == "green"


def test_deploy_switch_endpoint_rejects_invalid_color():
    client = TestClient(app)
    response = client.post("/api/v1/deploy/switch", json={"to": "red"})

    assert response.status_code == 400


def test_deploy_switch_endpoint_returns_409_on_failure():
    with patch(
        "nyxgpt.app.deploy_module.switch",
        return_value=DeployResult(False, "Refusing to switch: green not ready"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/deploy/switch", json={"to": "green"})

    assert response.status_code == 409
    assert "not ready" in response.json()["error"]["message"]


def test_deploy_rollback_endpoint_success():
    with patch(
        "nyxgpt.app.deploy_module.rollback",
        return_value=DeployResult(True, "Switched traffic from green to blue"),
    ) as mock_rollback:
        client = TestClient(app)
        response = client.post("/api/v1/deploy/rollback")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    mock_rollback.assert_called_once()


def test_deploy_rollback_endpoint_returns_409_when_no_history():
    with patch(
        "nyxgpt.app.deploy_module.rollback",
        return_value=DeployResult(False, "No deployment history to roll back to"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/deploy/rollback")

    assert response.status_code == 409
