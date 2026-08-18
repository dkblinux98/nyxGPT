"""Unit tests for the /api/v1/cloud/deploy* endpoints (P6-11, #3513; #3804).

These exercise src/nyxgpt/app.py's cloud_deploy_* route handlers with
nyxgpt.cloud_deploy mocked out, so no AWS account, no Terraform binary and no
SSH connection are needed.

Opening and closing the SSH access tunnel used to be a dashboard control and
an endpoint here. The owner removed both (2026-08-16, #3804): the dashboard
observes the cloud deployment and `nyxgpt cloud tunnel` operates it. Its
absence is pinned below.
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
    # Default is the cheap, side-effect-free read the dashboard can poll.
    mock.assert_called_once_with(probe_health=False)


def test_status_endpoint_forwards_an_explicit_health_probe():
    """The Cloud Deployment page asks for a real health answer on refresh (#3514)."""
    with patch("nyxgpt.app.cloud_deploy_module.deploy_status", return_value={}) as mock:
        client = TestClient(app)
        response = client.get("/api/v1/cloud/deploy?probe_health=true")

    assert response.status_code == 200
    mock.assert_called_once_with(probe_health=True)


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


def test_tunnel_is_not_reachable_over_http():
    """`nyxgpt cloud tunnel` / `--stop` own the access tunnel (#3804)."""
    with patch("nyxgpt.app.cloud_deploy_module.start_tunnel") as mock_start:
        client = TestClient(app)
        response = client.post("/api/v1/cloud/deploy/tunnel", json={})

    assert response.status_code == 404
    mock_start.assert_not_called()


def test_the_only_deploy_read_is_status():
    """The surviving GET surface is the information the dashboard renders."""
    paths = {path for path in app.openapi()["paths"] if path.startswith("/api/v1/cloud/deploy")}

    assert paths == {"/api/v1/cloud/deploy", "/api/v1/cloud/deploy/destroy"}
