"""Unit tests for the /api/v1/cloud/infra* endpoints (P6-8, #3509).

These exercise src/nyxgpt/app.py's cloud_infra_* route handlers with
nyxgpt.cloud_infra mocked out, so no Terraform binary and no AWS account are
needed. The dashboard surface exists because CLAUDE.md's Definition of Done
requires ops features to be operable from the SRE/admin dashboard, not only
from the CLI -- these tests pin that it drives the same code path the CLI
does.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.cloud import CloudCommandError

pytestmark = pytest.mark.unit

_SETTINGS = {
    "aws_region": "us-east-1",
    "owner_ip_cidr": "198.51.100.7/32",
    "ssh_key_name": "owner-pair",
    "instance_type": "m5.large",
}


def test_status_endpoint_returns_module_status():
    expected = {
        "provisioned": True,
        "instance_id": "i-123",
        "security_group_id": "sg-456",
        "access_model": {"open_ports": [22], "ssh_only": True, "world_open_ingress": False},
    }

    with patch("nyxgpt.app.cloud_infra_module.infra_status", return_value=expected) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/cloud/infra")

    assert response.status_code == 200
    assert response.json() == expected
    mock_status.assert_called_once()


def test_plan_endpoint_passes_dashboard_inputs_through():
    result = {"action": "plan", "settings": _SETTINGS, "plan_file": "/tmp/tfplan"}

    with patch("nyxgpt.app.cloud_infra_module.plan_infra", return_value=result) as mock_plan:
        client = TestClient(app)
        response = client.post(
            "/api/v1/cloud/infra/plan",
            json={"region": "eu-west-2", "ssh_key_name": "owner-pair"},
        )

    assert response.status_code == 200
    assert response.json()["action"] == "plan"
    args = mock_plan.call_args.args[0]
    assert args.region == "eu-west-2"
    assert args.ssh_key_name == "owner-pair"
    # Omitted fields arrive as None so the module falls back to saved settings.
    assert args.owner_ip is None
    assert args.instance_type is None


def test_apply_endpoint_returns_outputs():
    result = {
        "action": "apply",
        "settings": _SETTINGS,
        "outputs": {"instance_id": "i-123", "security_group_id": "sg-456"},
        "state": {"instance_id": "i-123"},
    }

    with patch("nyxgpt.app.cloud_infra_module.apply_infra", return_value=result):
        client = TestClient(app)
        response = client.post("/api/v1/cloud/infra/apply", json={})

    assert response.status_code == 200
    assert response.json()["outputs"]["instance_id"] == "i-123"


def test_apply_endpoint_reports_failures_as_409():
    with patch(
        "nyxgpt.app.cloud_infra_module.apply_infra",
        side_effect=CloudCommandError("terraform apply failed"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/cloud/infra/apply", json={})

    assert response.status_code == 409
    assert "terraform apply failed" in response.json()["error"]["message"]


def test_destroy_endpoint_requires_explicit_confirmation():
    with patch("nyxgpt.app.cloud_infra_module.destroy_infra") as mock_destroy:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/infra/destroy", json={})

    assert response.status_code == 400
    assert "confirm" in response.json()["error"]["message"]
    mock_destroy.assert_not_called()


def test_destroy_endpoint_proceeds_when_confirmed():
    with patch(
        "nyxgpt.app.cloud_infra_module.destroy_infra",
        return_value={"action": "destroy", "settings": _SETTINGS},
    ) as mock_destroy:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/infra/destroy", json={"confirm": True})

    assert response.status_code == 200
    assert response.json()["action"] == "destroy"
    mock_destroy.assert_called_once()


def test_provisioning_actions_are_recorded_as_admin_activity():
    result = {
        "action": "apply",
        "settings": _SETTINGS,
        "outputs": {"instance_id": "i-123"},
        "state": {},
    }

    with (
        patch("nyxgpt.app.cloud_infra_module.apply_infra", return_value=result),
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        client.post("/api/v1/cloud/infra/apply", json={})

    assert mock_record.call_args.args[0] == "cloud_infra.apply"
    assert "i-123" in mock_record.call_args.args[1]
