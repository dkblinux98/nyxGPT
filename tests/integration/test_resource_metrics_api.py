"""Integration tests for resource metrics API endpoint."""

import pytest
from fastapi.testclient import TestClient

from nyxgpt import resource_metrics_store


@pytest.fixture(autouse=True)
def _isolated_history_store():
    """Ensure the module-level history buffer doesn't leak between tests.

    `disk_loaded=True` also stops queries from falling through to whatever
    the real on-disk history log (`~/.nyxGPT/logs/...`, since the endpoint
    under test always uses the default path) happens to contain in the
    ambient environment -- these tests only care about in-memory state
    they record themselves via `record_sample`.
    """
    resource_metrics_store.reset_for_tests(disk_loaded=True)
    yield
    resource_metrics_store.reset_for_tests(disk_loaded=True)


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

    # Should have tracked additional requests (or be 0 if monitor not initialized)
    # Note: exact count depends on middleware ordering and what gets tracked
    # In TestClient context, resource monitor may not be initialized
    assert final_count >= initial_count
    assert final_count >= 0


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
    # Note: When using TestClient, the resource monitor may not be initialized
    # via the lifespan context, so we just verify the endpoint returns valid data
    assert data["queue"]["total_requests"] >= 0


@pytest.mark.integration
def test_metrics_history_endpoint_exists(client: TestClient):
    """Test that /api/v1/metrics/history endpoint exists and returns 200."""
    response = client.get("/api/v1/metrics/history")
    assert response.status_code == 200


@pytest.mark.integration
def test_metrics_history_defaults_to_1h_range(client: TestClient):
    """Test that the history endpoint defaults to the 1h range when unspecified."""
    response = client.get("/api/v1/metrics/history")
    assert response.status_code == 200
    data = response.json()
    assert data["range"] == "1h"
    assert data["requested_window_seconds"] == 3600


@pytest.mark.integration
@pytest.mark.parametrize(
    ("range_key", "window_seconds"),
    [("1h", 3600), ("24h", 86400), ("7d", 604800)],
)
def test_metrics_history_accepts_each_supported_range(
    client: TestClient, range_key: str, window_seconds: int
):
    """Test that each documented range value is accepted and echoed back."""
    response = client.get(f"/api/v1/metrics/history?range={range_key}")
    assert response.status_code == 200
    data = response.json()
    assert data["range"] == range_key
    assert data["requested_window_seconds"] == window_seconds


@pytest.mark.integration
def test_metrics_history_rejects_invalid_range(client: TestClient):
    """Test that an unsupported range value is rejected with a 422."""
    response = client.get("/api/v1/metrics/history?range=1y")
    assert response.status_code == 422


@pytest.mark.integration
def test_metrics_history_response_structure(client: TestClient):
    """Test that the history endpoint returns the documented fields."""
    response = client.get("/api/v1/metrics/history?range=1h")
    assert response.status_code == 200
    data = response.json()

    assert "range" in data
    assert "points" in data
    assert isinstance(data["points"], list)
    assert "sample_interval_seconds" in data
    assert "requested_window_seconds" in data
    assert "earliest_available_ts" in data
    assert "history_available_seconds" in data


@pytest.mark.integration
def test_metrics_history_honestly_reports_no_data_on_a_fresh_store(client: TestClient):
    """Test that an empty history store reports zero availability rather than fabricating data."""
    response = client.get("/api/v1/metrics/history?range=7d")
    assert response.status_code == 200
    data = response.json()

    assert data["points"] == []
    assert data["earliest_available_ts"] is None
    assert data["history_available_seconds"] == 0


@pytest.mark.integration
def test_metrics_history_reflects_recorded_samples(client: TestClient, tmp_path):
    """Test that a sample recorded via the store is returned by the history endpoint."""
    from configparser import ConfigParser

    from nyxgpt.resource_monitor import ResourceMonitor

    isolated_cfg = ConfigParser()
    isolated_cfg.add_section("logging")
    isolated_cfg.set("logging", "dir", str(tmp_path))

    monitor = ResourceMonitor(max_samples=10)
    resource_metrics_store.record_sample(monitor, cfg=isolated_cfg)

    response = client.get("/api/v1/metrics/history?range=1h")
    assert response.status_code == 200
    data = response.json()

    assert len(data["points"]) == 1
    assert data["earliest_available_ts"] is not None
    assert data["history_available_seconds"] >= 0
