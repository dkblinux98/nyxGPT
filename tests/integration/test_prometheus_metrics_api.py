"""Integration tests for the Prometheus /metrics endpoint."""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
def test_prometheus_metrics_endpoint_exists(client: TestClient):
    """Test that /metrics endpoint exists, returns 200, and is unauthenticated."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


@pytest.mark.integration
def test_prometheus_metrics_tracks_requests(client: TestClient):
    """Test that hitting the API is reflected in the exposed request counter."""
    client.get("/health")

    response = client.get("/metrics")
    assert response.status_code == 200
    assert "nyxgpt_http_requests_total" in response.text
    assert "nyxgpt_http_request_duration_seconds" in response.text
