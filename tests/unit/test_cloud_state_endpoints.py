"""Unit tests for the /api/v1/cloud/state* endpoints (P6-9, #3510).

These exercise src/nyxgpt/app.py's cloud_state_* route handlers with
nyxgpt.cloud_state mocked out, so no Terraform binary and no AWS account are
needed. The dashboard surface exists because CLAUDE.md's Definition of Done
requires ops features to be operable from the SRE/admin dashboard, not only
from the CLI -- these tests pin that it drives the same functions the CLI
does, and that the two destructive operations (breaking a lock, rolling state
back) cannot be triggered by an empty request body.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.cloud import CloudCommandError

pytestmark = pytest.mark.unit

_BACKEND = {
    "bucket": "nyxgpt-tfstate-123456789012-us-west-2",
    "table": "nyxgpt-tfstate-locks",
    "key": "nyxgpt/aws/terraform.tfstate",
    "region": "us-west-2",
    "enabled": True,
}


def test_status_endpoint_is_offline_by_default():
    expected = {"backend": "local", "remote_enabled": False, "verified": False}

    with patch("nyxgpt.app.cloud_state_module.state_status", return_value=expected) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/cloud/state")

    assert response.status_code == 200
    assert response.json() == expected
    mock_status.assert_called_once_with(verify=False)


def test_status_endpoint_forwards_the_verify_flag():
    with patch("nyxgpt.app.cloud_state_module.state_status", return_value={}) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/cloud/state?verify=true")

    assert response.status_code == 200
    mock_status.assert_called_once_with(verify=True)


def test_migrate_endpoint_passes_dashboard_inputs_through():
    result = {"action": "migrate", "backend": _BACKEND, "actions": [], "migrated_local_state": True}

    with patch(
        "nyxgpt.app.cloud_state_module.migrate_to_remote", return_value=result
    ) as mock_migrate:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/state/migrate", json={"region": "us-west-2"})

    assert response.status_code == 200
    assert response.json() == result
    assert mock_migrate.call_args.args[0].region == "us-west-2"


def test_migrate_endpoint_reports_a_backend_failure_as_a_conflict():
    with patch(
        "nyxgpt.app.cloud_state_module.migrate_to_remote",
        side_effect=CloudCommandError("bucket name is already taken"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/cloud/state/migrate", json={})

    assert response.status_code == 409
    assert "already taken" in response.json()["error"]["message"]


def test_unlock_endpoint_requires_a_lock_id():
    """Breaking "whatever lock is held" would clobber a live apply's lock."""
    with patch("nyxgpt.app.cloud_state_module.unlock_state") as mock_unlock:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/state/unlock", json={})

    assert response.status_code == 400
    assert "lock_id is required" in response.json()["error"]["message"]
    mock_unlock.assert_not_called()


def test_unlock_endpoint_releases_the_named_lock():
    result = {"action": "unlock", "lock_id": "abc-123", "unlocked": True}

    with patch("nyxgpt.app.cloud_state_module.unlock_state", return_value=result) as mock_unlock:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/state/unlock", json={"lock_id": "abc-123"})

    assert response.status_code == 200
    assert response.json() == result
    mock_unlock.assert_called_once_with("abc-123")


def test_versions_endpoint_forwards_the_limit():
    result = {"action": "versions", "bucket": "b", "key": "k", "versions": [], "truncated": False}

    with patch(
        "nyxgpt.app.cloud_state_module.list_state_versions", return_value=result
    ) as mock_versions:
        client = TestClient(app)
        response = client.get("/api/v1/cloud/state/versions?limit=5")

    assert response.status_code == 200
    mock_versions.assert_called_once_with(limit=5)


def test_versions_endpoint_explains_that_there_is_no_remote_backend_yet():
    with patch(
        "nyxgpt.app.cloud_state_module.list_state_versions",
        side_effect=CloudCommandError("Remote state is not configured."),
    ):
        client = TestClient(app)
        response = client.get("/api/v1/cloud/state/versions")

    assert response.status_code == 409
    assert "not configured" in response.json()["error"]["message"]


def test_restore_endpoint_requires_a_version_id():
    with patch("nyxgpt.app.cloud_state_module.restore_state_version") as mock_restore:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/state/restore", json={"confirm": True})

    assert response.status_code == 400
    assert "version_id is required" in response.json()["error"]["message"]
    mock_restore.assert_not_called()


def test_restore_endpoint_requires_explicit_confirmation():
    """A later apply against a wrong restore can destroy live resources."""
    with patch("nyxgpt.app.cloud_state_module.restore_state_version") as mock_restore:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/state/restore", json={"version_id": "older"})

    assert response.status_code == 400
    assert "confirm" in response.json()["error"]["message"]
    mock_restore.assert_not_called()


def test_restore_endpoint_restores_a_confirmed_version():
    result = {"action": "restore", "version_id": "older", "bytes": 42}

    with patch(
        "nyxgpt.app.cloud_state_module.restore_state_version", return_value=result
    ) as mock_restore:
        client = TestClient(app)
        response = client.post(
            "/api/v1/cloud/state/restore", json={"version_id": "older", "confirm": True}
        )

    assert response.status_code == 200
    assert response.json() == result
    mock_restore.assert_called_once_with("older")


def test_state_operations_are_recorded_in_the_admin_activity_log():
    """SRE surfaces record who changed infrastructure state and how."""
    result = {"action": "migrate", "backend": _BACKEND, "actions": [], "migrated_local_state": True}

    with (
        patch("nyxgpt.app.cloud_state_module.migrate_to_remote", return_value=result),
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        client.post("/api/v1/cloud/state/migrate", json={})

    mock_record.assert_called_once()
    assert mock_record.call_args.args[0] == "cloud_state.migrate"
    assert _BACKEND["bucket"] in mock_record.call_args.args[1]
