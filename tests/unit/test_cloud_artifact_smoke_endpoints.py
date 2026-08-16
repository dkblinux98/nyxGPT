"""Unit tests for the /api/v1/ops/cloud-artifact-smoke endpoints (#3784).

`nyxgpt.cloud_artifact_smoke` is mocked out, so nothing here builds an image or
boots a container. What they pin is the contract the dashboard panel depends
on: the same recorded run reaches both surfaces, a machine without Docker is
told so rather than starting a run that cannot work, and a second run is
refused while one is in flight.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.cloud import CloudCommandError

pytestmark = pytest.mark.unit

_STATUS = {
    "running": False,
    "current": {},
    "last_result": {"passed": True, "version": "3.0.0rc9", "detail": "verified"},
    "docker_available": True,
    "docker_detail": "28.0.4",
    "base_image": "amazonlinux:2023",
    "faults": [{"fault": "old-python", "description": "..."}],
    "coverage_gaps": ["EC2 provisioning is not exercised"],
    "commands": {"run": "nyxgpt cloud smoke --container"},
    "docs": "docs/cloud-artifact-smoke.md",
}


def test_status_endpoint_returns_the_recorded_run():
    with patch("nyxgpt.app.cloud_artifact_smoke_module.smoke_status", return_value=_STATUS) as mock:
        response = TestClient(app).get("/api/v1/ops/cloud-artifact-smoke")

    assert response.status_code == 200
    body = response.json()
    assert body["last_result"]["passed"] is True
    # The gaps are part of the payload, not decoration: the panel renders them
    # next to the verdict so green is never read as "the cloud path works".
    assert body["coverage_gaps"]
    mock.assert_called_once()


def test_run_endpoint_starts_a_background_run_with_the_requested_version():
    started = {"started": True, "started_at": "2026-08-15T05:00:00+00:00", "version": "3.0.0rc9"}

    with (
        patch(
            "nyxgpt.app.cloud_artifact_smoke_module.docker_available", return_value=(True, "28.0.4")
        ),
        patch(
            "nyxgpt.app.cloud_artifact_smoke_module.start_background_run", return_value=started
        ) as mock,
    ):
        response = TestClient(app).post(
            "/api/v1/ops/cloud-artifact-smoke",
            json={"version": "3.0.0rc9", "inject": ["old-python"]},
        )

    assert response.status_code == 200
    assert response.json()["started"] is True
    namespace = mock.call_args[0][0]
    assert namespace.container is True
    assert namespace.version == "3.0.0rc9"
    assert namespace.inject == ["old-python"]


def test_run_endpoint_refuses_when_docker_is_unusable():
    with patch(
        "nyxgpt.app.cloud_artifact_smoke_module.docker_available",
        return_value=(False, "the Docker daemon is not reachable"),
    ):
        response = TestClient(app).post("/api/v1/ops/cloud-artifact-smoke", json={})

    assert response.status_code == 409
    assert "Docker engine is required" in response.json()["error"]["message"]


def test_run_endpoint_refuses_a_second_concurrent_run():
    with (
        patch(
            "nyxgpt.app.cloud_artifact_smoke_module.docker_available", return_value=(True, "28.0.4")
        ),
        patch(
            "nyxgpt.app.cloud_artifact_smoke_module.start_background_run",
            side_effect=CloudCommandError("A containerized cloud smoke ... is still running"),
        ),
    ):
        response = TestClient(app).post("/api/v1/ops/cloud-artifact-smoke", json={})

    assert response.status_code == 409
    assert "still running" in response.json()["error"]["message"]
