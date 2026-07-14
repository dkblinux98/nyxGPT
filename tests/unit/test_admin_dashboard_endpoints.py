"""Unit tests for the /api/v1/admin/* endpoints (admin dashboard).

Covers src/nyxgpt/app.py's admin_overview/admin_activity_list/admin_access_*
route handlers. External dependencies (deploy/canary status, the resource
monitor, and config file writes) are mocked so no cluster or real config
mutation is required.

Related: #2698
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def _auth_cfg(enabled=False, header="X-API-Key", api_key=""):
    return {"enabled": enabled, "header": header, "api_key": api_key}


# --- GET /admin/overview ---


def test_admin_overview_aggregates_status():
    deploy_status = {"namespace": "nyxgpt", "active": "blue", "inactive": "green"}
    canary_status = {"namespace": "nyxgpt", "active": False}

    with (
        patch("nyxgpt.app.deploy_module.status", return_value=deploy_status),
        patch("nyxgpt.app.canary_module.status", return_value=canary_status),
        patch("nyxgpt.app.get_resource_monitor", return_value=None),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["deploy"] == deploy_status
    assert body["canary"] == canary_status
    assert body["resource_metrics"] is None
    assert "info" in body
    assert "observability" in body
    assert set(body["observability"]) == {
        "monitoring",
        "tracing",
        "error_tracking",
        "log_aggregation",
    }
    assert "auth_enabled" in body


def test_admin_overview_degrades_gracefully_when_deploy_status_fails():
    with (
        patch("nyxgpt.app.deploy_module.status", side_effect=RuntimeError("kubectl not found")),
        patch("nyxgpt.app.canary_module.status", return_value={}),
        patch("nyxgpt.app.get_resource_monitor", return_value=None),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/overview")

    assert response.status_code == 200
    body = response.json()
    assert "error" in body["deploy"]
    assert "kubectl not found" in body["deploy"]["error"]


# --- GET /admin/activity ---


def test_admin_activity_returns_recent_events():
    events = [
        {"ts": 1.0, "action": "config.updated", "detail": "log_level=DEBUG"},
        {"ts": 2.0, "action": "deploy.switch", "detail": "blue -> green"},
    ]

    with patch("nyxgpt.app.admin_activity_module.recent", return_value=events) as mock_recent:
        client = TestClient(app)
        response = client.get("/api/v1/admin/activity")

    assert response.status_code == 200
    assert response.json() == {"events": events}
    mock_recent.assert_called_once()


def test_admin_activity_clamps_limit_to_valid_range():
    with patch("nyxgpt.app.admin_activity_module.recent", return_value=[]) as mock_recent:
        client = TestClient(app)
        response = client.get("/api/v1/admin/activity?limit=10000")

    assert response.status_code == 200
    args, kwargs = mock_recent.call_args
    assert args[0] == 500


# --- GET /admin/access ---


def test_admin_access_get_masks_api_key():
    with patch(
        "nyxgpt.app._auth_cfg",
        return_value=_auth_cfg(enabled=True, header="X-API-Key", api_key="supersecretvalue"),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/admin/access", headers={"X-API-Key": "supersecretvalue"})

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["api_key_set"] is True
    assert body["api_key_masked"] == "supe********alue"
    assert "api_key" not in body


def test_admin_access_get_reports_no_key_set():
    with patch("nyxgpt.app._auth_cfg", return_value=_auth_cfg(enabled=False, api_key="")):
        client = TestClient(app)
        response = client.get("/api/v1/admin/access")

    assert response.status_code == 200
    body = response.json()
    assert body["api_key_set"] is False
    assert body["api_key_masked"] is None


# --- POST /admin/access ---


def test_admin_access_update_rejects_empty_payload():
    with patch("nyxgpt.app._auth_cfg", return_value=_auth_cfg()):
        client = TestClient(app)
        response = client.post("/api/v1/admin/access", json={})

    assert response.status_code == 400


def test_admin_access_update_toggles_enabled():
    with (
        patch(
            "nyxgpt.app._auth_cfg",
            return_value=_auth_cfg(enabled=True, api_key="existing-key"),
        ),
        patch("nyxgpt.app._apply_auth_config_updates") as mock_apply,
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.post(
            "/api/v1/admin/access",
            json={"enabled": True},
            headers={"X-API-Key": "existing-key"},
        )

    assert response.status_code == 200
    mock_apply.assert_called_once_with({"enabled": True})
    mock_record.assert_called_once()
    assert response.json()["enabled"] is True


def test_admin_access_update_rotate_returns_new_key_once():
    with (
        patch(
            "nyxgpt.app._auth_cfg",
            return_value=_auth_cfg(enabled=True, api_key="brand-new-generated-key"),
        ),
        patch("nyxgpt.app._apply_auth_config_updates") as mock_apply,
        patch("nyxgpt.app.admin_activity_module.record"),
    ):
        client = TestClient(app)
        response = client.post(
            "/api/v1/admin/access",
            json={"rotate": True},
            headers={"X-API-Key": "brand-new-generated-key"},
        )

    assert response.status_code == 200
    body = response.json()
    # The freshly generated key is returned once in the response body...
    assert isinstance(body["api_key"], str) and len(body["api_key"]) > 16
    assert body["api_key"] != "brand-new-generated-key"  # not the pre-rotation key
    assert body["api_key_masked"] != body["api_key"]

    # ...and the same generated value is what got persisted to config.
    (call_updates,), _ = mock_apply.call_args
    assert call_updates["api_key"] == body["api_key"]


def test_admin_access_update_records_activity_event():
    with (
        patch("nyxgpt.app._auth_cfg", return_value=_auth_cfg(enabled=False)),
        patch("nyxgpt.app._apply_auth_config_updates"),
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.post("/api/v1/admin/access", json={"header": "X-Custom-Key"})

    assert response.status_code == 200
    mock_record.assert_called_once()
    args, _ = mock_record.call_args
    assert args[0] == "access.updated"
