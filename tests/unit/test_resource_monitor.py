"""Unit tests for resource monitoring."""

import pytest

from nyxgpt.resource_monitor import ResourceMetrics, ResourceMonitor


@pytest.mark.unit
def test_resource_monitor_initialization():
    """Test resource monitor can be initialized."""
    monitor = ResourceMonitor(max_samples=100)
    assert monitor is not None
    assert monitor._max_samples == 100
    assert len(monitor._latency_samples) == 0
    assert monitor._total_requests == 0


@pytest.mark.unit
def test_record_request_latency():
    """Test recording request latency samples."""
    monitor = ResourceMonitor(max_samples=10)

    # Record some samples
    monitor.record_request_latency(10.0)
    monitor.record_request_latency(20.0)
    monitor.record_request_latency(30.0)

    assert len(monitor._latency_samples) == 3
    assert monitor._total_requests == 3


@pytest.mark.unit
def test_latency_samples_max_limit():
    """Test that latency samples respect max limit."""
    monitor = ResourceMonitor(max_samples=5)

    # Record more samples than max
    for i in range(10):
        monitor.record_request_latency(float(i))

    # Should only keep last 5 samples
    assert len(monitor._latency_samples) == 5
    assert monitor._total_requests == 10


@pytest.mark.unit
def test_get_metrics_basic():
    """Test getting basic resource metrics."""
    monitor = ResourceMonitor(max_samples=100)

    # Record some latency samples
    monitor.record_request_latency(10.0)
    monitor.record_request_latency(20.0)
    monitor.record_request_latency(30.0)

    metrics = monitor.get_metrics()

    # Check structure
    assert isinstance(metrics, ResourceMetrics)
    assert metrics.memory_rss_mb > 0
    assert metrics.memory_vms_mb > 0
    assert metrics.memory_percent >= 0
    assert metrics.memory_available_mb > 0
    assert metrics.cpu_percent_process >= 0
    assert metrics.cpu_percent_system >= 0
    assert metrics.total_requests == 3
    assert metrics.queue_depth == 0  # No batch processor

    # Check latency metrics
    assert metrics.avg_request_latency_ms == 20.0
    assert metrics.p50_request_latency_ms == 20.0
    assert metrics.p95_request_latency_ms > 0
    assert metrics.p99_request_latency_ms > 0


@pytest.mark.unit
def test_metrics_to_dict():
    """Test converting metrics to dictionary."""
    monitor = ResourceMonitor(max_samples=100)
    monitor.record_request_latency(15.0)

    metrics = monitor.get_metrics()
    metrics_dict = metrics.to_dict()

    # Check structure
    assert "memory" in metrics_dict
    assert "cpu" in metrics_dict
    assert "latency" in metrics_dict
    assert "queue" in metrics_dict

    # Check memory fields
    assert "rss_mb" in metrics_dict["memory"]
    assert "vms_mb" in metrics_dict["memory"]
    assert "percent" in metrics_dict["memory"]
    assert "available_mb" in metrics_dict["memory"]

    # Check CPU fields
    assert "process_percent" in metrics_dict["cpu"]
    assert "system_percent" in metrics_dict["cpu"]

    # Check latency fields
    assert "avg_ms" in metrics_dict["latency"]
    assert "p50_ms" in metrics_dict["latency"]
    assert "p95_ms" in metrics_dict["latency"]
    assert "p99_ms" in metrics_dict["latency"]

    # Check queue fields
    assert "depth" in metrics_dict["queue"]
    assert "total_requests" in metrics_dict["queue"]

    # Check error fields
    assert "errors" in metrics_dict
    assert "count" in metrics_dict["errors"]
    assert "rate_percent" in metrics_dict["errors"]


@pytest.mark.unit
def test_record_request_latency_defaults_to_no_error():
    monitor = ResourceMonitor(max_samples=100)
    monitor.record_request_latency(10.0)

    metrics = monitor.get_metrics()
    assert metrics.error_count == 0
    assert metrics.error_rate_percent == 0.0


@pytest.mark.unit
def test_error_rate_tracks_errors():
    monitor = ResourceMonitor(max_samples=100)
    monitor.record_request_latency(10.0, is_error=False)
    monitor.record_request_latency(20.0, is_error=True)
    monitor.record_request_latency(30.0, is_error=True)
    monitor.record_request_latency(40.0, is_error=False)

    metrics = monitor.get_metrics()
    assert metrics.error_count == 2
    assert metrics.error_rate_percent == 50.0


@pytest.mark.unit
def test_error_rate_respects_sliding_window():
    monitor = ResourceMonitor(max_samples=2)
    monitor.record_request_latency(10.0, is_error=True)
    monitor.record_request_latency(20.0, is_error=False)
    monitor.record_request_latency(30.0, is_error=False)

    metrics = monitor.get_metrics()
    # Only the last 2 samples (False, False) remain in the window.
    assert metrics.error_count == 0
    assert metrics.error_rate_percent == 0.0
    assert metrics.total_requests == 3


@pytest.mark.unit
def test_percentile_calculation():
    """Test percentile calculation accuracy."""
    monitor = ResourceMonitor(max_samples=100)

    # Record samples with known distribution
    samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    for sample in samples:
        monitor.record_request_latency(sample)

    metrics = monitor.get_metrics()

    # Check percentiles
    assert metrics.p50_request_latency_ms == 55.0  # Median
    assert metrics.p95_request_latency_ms >= 90.0
    assert metrics.p99_request_latency_ms >= 95.0


@pytest.mark.unit
def test_empty_samples():
    """Test metrics with no latency samples."""
    monitor = ResourceMonitor(max_samples=100)

    metrics = monitor.get_metrics()

    # Should have zero latency metrics
    assert metrics.avg_request_latency_ms == 0.0
    assert metrics.p50_request_latency_ms == 0.0
    assert metrics.p95_request_latency_ms == 0.0
    assert metrics.p99_request_latency_ms == 0.0
    assert metrics.total_requests == 0


@pytest.mark.unit
def test_single_sample():
    """Test metrics with only one sample."""
    monitor = ResourceMonitor(max_samples=100)
    monitor.record_request_latency(42.0)

    metrics = monitor.get_metrics()

    # All percentiles should equal the single sample
    assert metrics.avg_request_latency_ms == 42.0
    assert metrics.p50_request_latency_ms == 42.0
    assert metrics.p95_request_latency_ms == 42.0
    assert metrics.p99_request_latency_ms == 42.0
    assert metrics.total_requests == 1


@pytest.mark.unit
def test_thread_safety():
    """Test that recording samples is thread-safe."""
    import threading

    monitor = ResourceMonitor(max_samples=1000)

    def record_samples():
        for i in range(100):
            monitor.record_request_latency(float(i))

    # Create multiple threads recording samples
    threads = [threading.Thread(target=record_samples) for _ in range(5)]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    # Should have all 500 requests tracked
    metrics = monitor.get_metrics()
    assert metrics.total_requests == 500


@pytest.mark.unit
def test_percentile_edge_cases():
    """Test percentile calculation edge cases."""
    # Test with two samples
    monitor = ResourceMonitor(max_samples=100)
    monitor.record_request_latency(10.0)
    monitor.record_request_latency(90.0)

    metrics = monitor.get_metrics()
    assert metrics.p50_request_latency_ms == 50.0


@pytest.mark.unit
def test_batch_processor_queue_depth():
    """Test queue depth tracking with mock batch processor."""
    import queue
    from unittest.mock import Mock

    # Create mock batch processor with queue
    mock_processor = Mock()
    mock_queue = queue.Queue()
    mock_queue.put(1)
    mock_queue.put(2)
    mock_queue.put(3)
    mock_processor._queue = mock_queue

    monitor = ResourceMonitor(max_samples=100, batch_processor=mock_processor)
    metrics = monitor.get_metrics()

    # Should report queue depth
    assert metrics.queue_depth == 3
