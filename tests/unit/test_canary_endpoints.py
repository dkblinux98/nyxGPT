"""Unit tests for the /api/v1/canary/* endpoints (canary deployment).

These exercise src/nyxgpt/app.py's canary_* route handlers with
nyxgpt.canary mocked out, so no kubectl/cluster is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.canary import CanaryResult

pytestmark = pytest.mark.unit


def test_canary_status_endpoint_returns_module_status():
    expected = {
        "namespace": "nyxgpt",
        "active": True,
        "weight_percent": 25,
        "stable": {"healthy": True, "message": "nyxgpt-api-stable healthy (3/3 ready)"},
        "canary": {"healthy": True, "message": "nyxgpt-api-canary healthy (1/1 ready)"},
        "metrics": {"total_requests": 100, "error_rate_percent": 1.0, "p95_latency_ms": 250.0},
        "history": [],
    }

    with patch("nyxgpt.app.canary_module.status", return_value=expected) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/canary/status")

    assert response.status_code == 200
    assert response.json() == expected
    mock_status.assert_called_once()


def test_canary_start_endpoint_success():
    with patch(
        "nyxgpt.app.canary_module.start",
        return_value=CanaryResult(True, "Started canary rollout at 10% (1/4 replicas)"),
    ) as mock_start:
        client = TestClient(app)
        response = client.post("/api/v1/canary/start", json={"weight_percent": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "Started canary rollout" in body["message"]
    _, kwargs = mock_start.call_args
    assert kwargs["weight_percent"] == 10


def test_canary_start_endpoint_returns_409_when_already_active():
    with patch(
        "nyxgpt.app.canary_module.start",
        return_value=CanaryResult(False, "Canary rollout already in progress at 25%"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/canary/start", json={})

    assert response.status_code == 409
    assert "already in progress" in response.json()["error"]["message"]


def test_canary_evaluate_endpoint_success():
    with patch(
        "nyxgpt.app.canary_module.evaluate",
        return_value=CanaryResult(True, "Metrics within thresholds; safe to promote"),
    ) as mock_evaluate:
        client = TestClient(app)
        response = client.post("/api/v1/canary/evaluate")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    mock_evaluate.assert_called_once()


def test_canary_evaluate_endpoint_returns_409_on_regression():
    with patch(
        "nyxgpt.app.canary_module.evaluate",
        return_value=CanaryResult(False, "Metrics regression detected; automatically rolled back"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/canary/evaluate")

    assert response.status_code == 409
    assert "automatically rolled back" in response.json()["error"]["message"]


def test_canary_promote_endpoint_success():
    with patch(
        "nyxgpt.app.canary_module.promote",
        return_value=CanaryResult(True, "Promoted canary to 35% (1/4 replicas)"),
    ) as mock_promote:
        client = TestClient(app)
        response = client.post("/api/v1/canary/promote", json={"step_percent": 25})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    _, kwargs = mock_promote.call_args
    assert kwargs["step_percent"] == 25


def test_canary_promote_endpoint_returns_409_when_no_rollout():
    with patch(
        "nyxgpt.app.canary_module.promote",
        return_value=CanaryResult(False, "No canary rollout in progress"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/canary/promote", json={})

    assert response.status_code == 409


def test_canary_rollback_endpoint_success():
    with patch(
        "nyxgpt.app.canary_module.rollback",
        return_value=CanaryResult(True, "Rolled back canary rollout from 25% to 0%"),
    ) as mock_rollback:
        client = TestClient(app)
        response = client.post("/api/v1/canary/rollback")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    mock_rollback.assert_called_once()


def test_canary_rollback_endpoint_returns_409_when_no_rollout():
    with patch(
        "nyxgpt.app.canary_module.rollback",
        return_value=CanaryResult(False, "No canary rollout in progress"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/canary/rollback")

    assert response.status_code == 409
