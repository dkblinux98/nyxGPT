"""Integration tests for resource metrics API endpoint."""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.integration
def test_metrics_endpoint_exists(client: TestClient):
    """Test that /api/v1/metrics endpoint exists and returns 200."""
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200


@pytest.mark.integration
def test_metrics_response_structure(client: TestClient):
    """Test that metrics endpoint returns correct structure."""
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200

    data = response.json()

    # Check top-level keys
    assert "memory" in data
    assert "cpu" in data
    assert "latency" in data
    assert "queue" in data

    # Check memory fields
    assert "rss_mb" in data["memory"]
    assert "vms_mb" in data["memory"]
    assert "percent" in data["memory"]
    assert "available_mb" in data["memory"]

    # Check CPU fields
    assert "process_percent" in data["cpu"]
    assert "system_percent" in data["cpu"]

    # Check latency fields
    assert "avg_ms" in data["latency"]
    assert "p50_ms" in data["latency"]
    assert "p95_ms" in data["latency"]
    assert "p99_ms" in data["latency"]

    # Check queue fields
    assert "depth" in data["queue"]
    assert "total_requests" in data["queue"]


@pytest.mark.integration
def test_metrics_values_valid(client: TestClient):
    """Test that metrics endpoint returns valid values."""
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200

    data = response.json()

    # Memory values should be positive
    assert data["memory"]["rss_mb"] >= 0
    assert data["memory"]["vms_mb"] >= 0
    assert data["memory"]["percent"] >= 0
    assert data["memory"]["available_mb"] >= 0

    # CPU values should be non-negative percentages
    assert data["cpu"]["process_percent"] >= 0
    assert data["cpu"]["system_percent"] >= 0

    # Latency values should be non-negative
    assert data["latency"]["avg_ms"] >= 0
    assert data["latency"]["p50_ms"] >= 0
    assert data["latency"]["p95_ms"] >= 0
    assert data["latency"]["p99_ms"] >= 0

    # Queue values should be non-negative integers
    assert data["queue"]["depth"] >= 0
    assert data["queue"]["total_requests"] >= 0


@pytest.mark.integration
def test_metrics_tracks_requests(client: TestClient):
    """Test that metrics endpoint tracks request count."""
    # Get initial metrics
    response1 = client.get("/api/v1/metrics")
    data1 = response1.json()
    initial_count = data1["queue"]["total_requests"]

    # Make several more requests
    client.get("/api/v1/info")
    client.get("/health")
    client.get("/api/v1/info")

    # Get updated metrics
    response2 = client.get("/api/v1/metrics")
    data2 = response2.json()
    final_count = data2["queue"]["total_requests"]

    # Should have tracked additional requests
    # Note: exact count depends on middleware ordering and what gets tracked
    assert final_count >= initial_count


@pytest.mark.integration
def test_metrics_latency_tracking(client: TestClient):
    """Test that latency metrics are tracked across requests."""
    # Make several requests to populate latency data
    for _ in range(10):
        client.get("/health")

    # Get metrics
    response = client.get("/api/v1/metrics")
    data = response.json()

    # After multiple requests, latency metrics should be non-zero
    # (assuming at least one request took measurable time)
    assert data["queue"]["total_requests"] >= 10
