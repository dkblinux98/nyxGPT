"""Unit tests for the /api/v1/infra/* endpoints.

These exercise src/nyxgpt/app.py's infra_* route handlers (Terraform/
Kubernetes install & teardown, per issue #3344) with nyxgpt.ops mocked
out, so no terraform/kubectl/docker is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.ops import OpsResult

pytestmark = pytest.mark.unit


def test_infra_status_endpoint_returns_module_status():
    expected = {
        "terraform": {"deployed": False, "containers": {"api": "absent"}},
        "kubernetes": {"available": False, "deployed": False, "namespace": "nyxgpt", "pods": []},
    }
    with patch("nyxgpt.app.ops_module.infra_status", return_value=expected) as mock_status:
        client = TestClient(app)
        response = client.get("/api/v1/infra/status")

    assert response.status_code == 200
    assert response.json() == expected
    mock_status.assert_called_once()


def test_infra_terraform_install_endpoint_success():
    results = [
        OpsResult(True, "terraform binary already installed"),
        OpsResult(True, "terraform apply"),
    ]
    with patch(
        "nyxgpt.app.ops_module.install_terraform_local", return_value=results
    ) as mock_install:
        client = TestClient(app)
        response = client.post("/api/v1/infra/terraform/install", json={"api_key": "k"})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["results"] == [
        {"ok": True, "message": "terraform binary already installed", "details": ""},
        {"ok": True, "message": "terraform apply", "details": ""},
    ]
    mock_install.assert_called_once_with(api_key="k")


def test_infra_terraform_install_endpoint_reports_step_failure():
    results = [OpsResult(False, "terraform init failed", "some error")]
    with patch("nyxgpt.app.ops_module.install_terraform_local", return_value=results):
        client = TestClient(app)
        response = client.post("/api/v1/infra/terraform/install", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][0]["message"] == "terraform init failed"


def test_infra_terraform_install_endpoint_defaults_api_key_to_none():
    with patch(
        "nyxgpt.app.ops_module.install_terraform_local", return_value=[OpsResult(True, "ok")]
    ) as mock_install:
        client = TestClient(app)
        response = client.post("/api/v1/infra/terraform/install", json={})

    assert response.status_code == 200
    mock_install.assert_called_once_with(api_key=None)


def test_infra_terraform_down_endpoint_success():
    with patch(
        "nyxgpt.app.ops_module.down_terraform", return_value=[OpsResult(True, "terraform destroy")]
    ) as mock_down:
        client = TestClient(app)
        response = client.post("/api/v1/infra/terraform/down")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    mock_down.assert_called_once()


def test_infra_kubernetes_install_endpoint_success():
    results = [
        OpsResult(True, "Kubernetes cluster reachable"),
        OpsResult(True, "kubectl apply -k k8s/"),
    ]
    with patch(
        "nyxgpt.app.ops_module.install_kubernetes_local", return_value=results
    ) as mock_install:
        client = TestClient(app)
        response = client.post("/api/v1/infra/kubernetes/install", json={"api_key": "k"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    mock_install.assert_called_once_with(api_key="k")


def test_infra_kubernetes_install_endpoint_reports_step_failure():
    results = [OpsResult(False, "No reachable Kubernetes cluster", "details")]
    with patch("nyxgpt.app.ops_module.install_kubernetes_local", return_value=results):
        client = TestClient(app)
        response = client.post("/api/v1/infra/kubernetes/install", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["results"][0]["message"] == "No reachable Kubernetes cluster"


def test_infra_kubernetes_down_endpoint_success():
    with patch(
        "nyxgpt.app.ops_module.down_kubernetes",
        return_value=[OpsResult(True, "kubectl delete -k k8s/")],
    ) as mock_down:
        client = TestClient(app)
        response = client.post("/api/v1/infra/kubernetes/down")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    mock_down.assert_called_once()


def test_admin_overview_does_not_break_with_infra_endpoints_added():
    client = TestClient(app)
    response = client.get("/api/v1/admin/overview")
    assert response.status_code == 200
