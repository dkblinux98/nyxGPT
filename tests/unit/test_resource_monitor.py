from __future__ import annotations

import time
import threading

import pytest

from nyxgpt.resource_monitor import ResourceMonitor, ResourceMetrics, get_monitor

pytestmark = pytest.mark.unit


def test_resource_monitor_initialization():
    """Test that ResourceMonitor initializes correctly."""
    monitor = ResourceMonitor(latency_window=100)
    assert monitor._request_count == 0
    assert monitor._active_requests == 0
    assert monitor._max_queue_depth == 0
    assert len(monitor._latencies) == 0


def test_start_request_increments_counters():
    """Test that start_request increments active request counter."""
    monitor = ResourceMonitor()
    initial_active = monitor._active_requests

    start_time = monitor.start_request()
    assert isinstance(start_time, float)
    assert monitor._active_requests == initial_active + 1


def test_end_request_records_latency():
    """Test that end_request records latency and updates counters."""
    monitor = ResourceMonitor()

    start_time = monitor.start_request()
    time.sleep(0.01)  # 10ms delay
    monitor.end_request(start_time)

    assert monitor._request_count == 1
    assert monitor._active_requests == 0
    assert len(monitor._latencies) == 1
    # Latency should be approximately 10ms
    assert 8 < monitor._latencies[0] < 20


def test_queue_depth_tracking():
    """Test that queue depth is tracked correctly."""
    monitor = ResourceMonitor()

    # Start multiple requests without ending them
    monitor.start_request()
    monitor.start_request()
    monitor.start_request()

    assert monitor._active_requests == 3
    assert monitor._max_queue_depth == 3

    # End one request
    monitor.end_request(time.time())
    assert monitor._active_requests == 2
    assert monitor._max_queue_depth == 3  # Max should remain


def test_get_metrics_returns_valid_data():
    """Test that get_metrics returns valid ResourceMetrics."""
    monitor = ResourceMonitor()

    # Simulate some requests
    start1 = monitor.start_request()
    time.sleep(0.01)
    monitor.end_request(start1)

    start2 = monitor.start_request()
    time.sleep(0.02)
    monitor.end_request(start2)

    metrics = monitor.get_metrics()

    # Check types and basic validity
    assert isinstance(metrics, ResourceMetrics)
    assert metrics.memory_rss > 0
    assert metrics.memory_vms > 0
    assert metrics.memory_percent >= 0
    assert metrics.cpu_percent >= 0
    assert metrics.request_count == 2
    assert metrics.active_requests == 0
    assert metrics.avg_latency_ms > 0
    assert metrics.p95_latency_ms > 0
    assert metrics.p99_latency_ms > 0
    assert metrics.queue_depth == 0
    assert metrics.max_queue_depth == 1
    assert isinstance(metrics.timestamp, float)


def test_latency_percentiles_calculation():
    """Test that latency percentiles are calculated correctly."""
    monitor = ResourceMonitor()

    # Add known latencies
    latencies = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    for latency_ms in latencies:
        monitor._latencies.append(latency_ms)

    metrics = monitor.get_metrics()

    # Average should be 55
    assert 54 < metrics.avg_latency_ms < 56

    # P95 should be around 95 (95th percentile of 10 values = index 9)
    assert 90 <= metrics.p95_latency_ms <= 100

    # P99 should be around 99 (99th percentile of 10 values = index 9)
    assert 90 <= metrics.p99_latency_ms <= 100


def test_latency_window_circular_buffer():
    """Test that latency buffer respects window size."""
    monitor = ResourceMonitor(latency_window=5)

    # Add more latencies than window size
    for i in range(10):
        start = monitor.start_request()
        monitor.end_request(start)

    # Should only keep last 5
    assert len(monitor._latencies) == 5
    assert monitor._request_count == 10


def test_reset_counters():
    """Test that reset_counters clears metrics correctly."""
    monitor = ResourceMonitor()

    # Simulate some activity
    start = monitor.start_request()
    monitor.end_request(start)
    monitor.start_request()  # Leave one active

    assert monitor._request_count > 0
    assert len(monitor._latencies) > 0

    # Reset counters
    monitor.reset_counters()

    assert monitor._request_count == 0
    assert len(monitor._latencies) == 0
    # Active requests and max_queue_depth should be preserved
    assert monitor._active_requests == 1
    assert monitor._max_queue_depth == 1


def test_concurrent_request_tracking():
    """Test that resource monitor handles concurrent requests correctly."""
    monitor = ResourceMonitor()
    results = []

    def simulate_request():
        start = monitor.start_request()
        time.sleep(0.01)
        monitor.end_request(start)
        results.append(True)

    # Start 10 concurrent requests
    threads = [threading.Thread(target=simulate_request) for _ in range(10)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 10
    assert monitor._request_count == 10
    assert monitor._active_requests == 0
    assert monitor._max_queue_depth <= 10


def test_get_monitor_singleton():
    """Test that get_monitor returns singleton instance."""
    monitor1 = get_monitor()
    monitor2 = get_monitor()

    assert monitor1 is monitor2


def test_metrics_with_no_requests():
    """Test that metrics work correctly with no request history."""
    monitor = ResourceMonitor()
    metrics = monitor.get_metrics()

    # Should still return valid metrics
    assert metrics.request_count == 0
    assert metrics.active_requests == 0
    assert metrics.avg_latency_ms == 0.0
    assert metrics.p95_latency_ms == 0.0
    assert metrics.p99_latency_ms == 0.0
    assert metrics.queue_depth == 0
    assert metrics.max_queue_depth == 0


def test_metrics_with_single_request():
    """Test that percentiles work correctly with single request."""
    monitor = ResourceMonitor()

    start = monitor.start_request()
    time.sleep(0.01)
    monitor.end_request(start)

    metrics = monitor.get_metrics()

    # All percentiles should be approximately equal with one data point
    assert metrics.avg_latency_ms > 0
    assert metrics.p95_latency_ms > 0
    assert metrics.p99_latency_ms > 0
    assert abs(metrics.avg_latency_ms - metrics.p95_latency_ms) < 1


def test_memory_metrics_are_positive():
    """Test that memory metrics are positive values."""
    monitor = ResourceMonitor()
    metrics = monitor.get_metrics()

    assert metrics.memory_rss > 0
    assert metrics.memory_vms > 0
    assert metrics.memory_percent > 0


def test_cpu_metrics_are_valid():
    """Test that CPU metrics are within valid range."""
    monitor = ResourceMonitor()

    # Make some requests to potentially generate CPU activity
    for _ in range(5):
        start = monitor.start_request()
        monitor.end_request(start)

    metrics = monitor.get_metrics()

    # CPU percent should be >= 0 and <= 100 (per core can exceed 100, but should be reasonable)
    assert metrics.cpu_percent >= 0
    assert metrics.cpu_percent <= 1000  # Allow multi-core systems


def test_max_queue_depth_persistence():
    """Test that max_queue_depth persists across resets."""
    monitor = ResourceMonitor()

    # Create peak queue depth
    monitor.start_request()
    monitor.start_request()
    monitor.start_request()
    assert monitor._max_queue_depth == 3

    # End all requests
    for _ in range(3):
        monitor.end_request(time.time())

    # Reset should preserve max_queue_depth as current active count
    monitor.reset_counters()
    assert monitor._max_queue_depth == 0  # No active requests after reset


def test_active_requests_never_negative():
    """Test that active_requests counter never goes negative."""
    monitor = ResourceMonitor()

    # Try to end a request without starting one
    monitor.end_request(time.time())

    # Should be clamped to 0, not negative
    assert monitor._active_requests == 0


def test_timestamp_in_metrics():
    """Test that metrics include current timestamp."""
    monitor = ResourceMonitor()
    before = time.time()
    metrics = monitor.get_metrics()
    after = time.time()

    # Timestamp should be between before and after
    assert before <= metrics.timestamp <= after


def test_thread_safe_metrics_access():
    """Test that concurrent access to metrics is thread-safe."""
    monitor = ResourceMonitor()
    results = []

    def get_metrics_repeatedly():
        for _ in range(10):
            metrics = monitor.get_metrics()
            results.append(metrics)

    # Start multiple threads reading metrics
    threads = [threading.Thread(target=get_metrics_repeatedly) for _ in range(5)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Should have 50 metric snapshots with no errors
    assert len(results) == 50
    for metric in results:
        assert isinstance(metric, ResourceMetrics)
