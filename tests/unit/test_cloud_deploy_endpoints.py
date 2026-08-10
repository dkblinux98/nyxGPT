"""Unit tests for the /api/v1/cloud/deploy* endpoints (P6-11, #3513).

These exercise src/nyxgpt/app.py's cloud_deploy_* route handlers with
nyxgpt.cloud_deploy mocked out, so no AWS account, no Terraform binary and no
SSH connection are needed. The dashboard surface exists because CLAUDE.md's
Definition of Done requires ops features to be operable from the SRE/admin
dashboard, not only from the CLI -- these tests pin that it drives the same
code path the CLI does.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.cloud import CloudCommandError

pytestmark = pytest.mark.unit

_DEPLOY_RESULT = {
    "action": "deploy",
    "plan": {"version": "3.0.0", "profiles": ["monitoring"]},
    "target": {"host": "198.51.100.10", "instance_id": "i-0abc"},
    "steps": [],
    "tunnel": {"running": True, "pid": 4242},
    "health": {"healthy": True},
    "urls": {"api": "http://localhost:8000", "web": "http://localhost:3000"},
}


def test_status_endpoint_returns_module_status():
    expected = {
        "deployed": True,
        "version": "3.0.0",
        "tunnel": {"running": False},
        "urls": {"api": "http://localhost:8000"},
        "access_command": "nyxgpt cloud tunnel",
    }

    with patch("nyxgpt.app.cloud_deploy_module.deploy_status", return_value=expected) as mock:
        client = TestClient(app)
        response = client.get("/api/v1/cloud/deploy")

    assert response.status_code == 200
    assert response.json() == expected
    mock.assert_called_once()


def test_deploy_endpoint_passes_dashboard_inputs_through():
    with patch("nyxgpt.app.cloud_deploy_module.deploy", return_value=_DEPLOY_RESULT) as mock:
        client = TestClient(app)
        response = client.post(
            "/api/v1/cloud/deploy",
            json={"region": "eu-west-2", "version": "3.1.0", "skip_observability": True},
        )

    assert response.status_code == 200
    assert response.json()["urls"]["web"] == "http://localhost:3000"
    args = mock.call_args.args[0]
    # Deploy takes the provisioning inputs *and* the deploy-specific ones --
    # it applies the substrate before installing onto it.
    assert args.region == "eu-west-2"
    assert args.version == "3.1.0"
    assert args.skip_observability is True
    # Omitted fields arrive as None so the module falls back to saved settings.
    assert args.owner_ip is None
    assert args.host is None
    assert args.no_tunnel is False


def test_deploy_endpoint_surfaces_a_cloud_error_as_409():
    with patch(
        "nyxgpt.app.cloud_deploy_module.deploy",
        side_effect=CloudCommandError("instance never accepted SSH"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/cloud/deploy", json={})

    assert response.status_code == 409
    assert "never accepted SSH" in response.json()["error"]["message"]


def test_destroy_endpoint_requires_explicit_confirmation():
    with patch("nyxgpt.app.cloud_deploy_module.destroy") as mock:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/deploy/destroy", json={})

    assert response.status_code == 400
    assert "confirm" in response.json()["error"]["message"]
    mock.assert_not_called()


def test_destroy_endpoint_tears_down_when_confirmed():
    result = {
        "action": "destroy",
        "tunnel": {"stopped": True},
        "settings": {"aws_region": "us-east-1"},
    }

    with patch("nyxgpt.app.cloud_deploy_module.destroy", return_value=result) as mock:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/deploy/destroy", json={"confirm": True})

    assert response.status_code == 200
    assert response.json()["tunnel"]["stopped"] is True
    mock.assert_called_once()


def test_tunnel_endpoint_opens_a_background_tunnel():
    target = type("T", (), {"host": "198.51.100.10"})()
    started = {"action": "tunnel", "running": True, "pid": 4242, "urls": {}}

    with (
        patch("nyxgpt.app.cloud_deploy_module.resolve_target", return_value=target),
        patch(
            "nyxgpt.app.cloud_deploy_module.load_deploy_state",
            return_value={"profiles": ["tracing"]},
        ),
        patch("nyxgpt.app.cloud_deploy_module.start_tunnel", return_value=started) as mock_start,
    ):
        client = TestClient(app)
        response = client.post("/api/v1/cloud/deploy/tunnel", json={})

    assert response.status_code == 200
    assert response.json()["running"] is True
    # The tunnel forwards exactly the profiles the deploy enabled.
    assert mock_start.call_args.args[1] == ["tracing"]


def test_tunnel_endpoint_stops_on_request():
    with patch(
        "nyxgpt.app.cloud_deploy_module.stop_tunnel",
        return_value={"action": "tunnel-stop", "stopped": True, "pid": 4242},
    ) as mock_stop:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/deploy/tunnel", json={"action": "stop"})

    assert response.status_code == 200
    assert response.json()["stopped"] is True
    mock_stop.assert_called_once()


def test_tunnel_endpoint_surfaces_a_missing_deployment_as_409():
    with patch(
        "nyxgpt.app.cloud_deploy_module.resolve_target",
        side_effect=CloudCommandError("No provisioned instance found"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/cloud/deploy/tunnel", json={})

    assert response.status_code == 409
    assert "No provisioned instance" in response.json()["error"]["message"]
