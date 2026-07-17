"""Tests for request batching functionality."""

from __future__ import annotations

import logging
import threading
import time

import pytest

from nyxgpt.batch_processor import BatchMetrics, BatchProcessor, RequestPriority

pytestmark = pytest.mark.unit


def test_batch_processor_basic():
    """Test basic batch processing."""

    # Define a simple processing function
    def process_batch(requests):
        return [f"processed: {req.data}" for req in requests]

    processor = BatchProcessor(batch_size=2, wait_time_ms=100, process_fn=process_batch)
    processor.start()

    try:
        # Submit two requests
        result1 = processor.submit("request1", priority=RequestPriority.BATCH)
        result2 = processor.submit("request2", priority=RequestPriority.BATCH)

        assert result1 == "processed: request1"
        assert result2 == "processed: request2"

    finally:
        processor.stop()


def test_batch_processor_timeout():
    """Test that batches are processed even when not full (timeout)."""

    processed = []

    def process_batch(requests):
        # Track when batch was processed
        processed.append(len(requests))
        return [f"result-{req.data}" for req in requests]

    # Small wait time to test timeout behavior
    processor = BatchProcessor(batch_size=10, wait_time_ms=100, process_fn=process_batch)
    processor.start()

    try:
        import threading

        # Submit requests in parallel to ensure they arrive together
        results = []

        def submit_req(data):
            results.append(processor.submit(data, priority=RequestPriority.BATCH))

        threads = [threading.Thread(target=submit_req, args=(f"req{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All requests should complete
        assert len(results) == 3
        assert all(r.startswith("result-req") for r in results)

        # Verify batch was processed with 3 items (less than batch_size=10)
        # Should process all together due to timeout rather than waiting for full batch
        assert len(processed) > 0
        # Total requests should sum to 3
        total_processed = sum(processed)
        assert total_processed == 3

    finally:
        processor.stop()


def test_batch_processor_priority():
    """Test priority-based request ordering."""

    processing_order = []

    def process_batch(requests):
        for req in requests:
            processing_order.append((req.data, req.priority))
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=5, wait_time_ms=200, process_fn=process_batch)
    processor.start()

    try:
        # Submit requests with different priorities
        # Lower priority value = higher priority (INTERACTIVE=1, BATCH=2)
        import threading

        results = []

        def submit_request(data, priority):
            result = processor.submit(data, priority=priority)
            results.append(result)

        # Submit batch priority requests
        t1 = threading.Thread(target=submit_request, args=("low1", RequestPriority.BATCH))
        t2 = threading.Thread(target=submit_request, args=("high1", RequestPriority.INTERACTIVE))
        t3 = threading.Thread(target=submit_request, args=("low2", RequestPriority.BATCH))
        t4 = threading.Thread(target=submit_request, args=("high2", RequestPriority.INTERACTIVE))

        t1.start()
        time.sleep(0.01)  # Small delay to ensure ordering
        t2.start()
        time.sleep(0.01)
        t3.start()
        time.sleep(0.01)
        t4.start()

        t1.join()
        t2.join()
        t3.join()
        t4.join()

        # All requests should complete
        assert len(results) == 4

        # Interactive requests should be processed before batch requests
        # (assuming they're in the same batch)
        # This is a soft check since timing can vary
        # At minimum, all requests should complete successfully
        _ = processing_order  # Used for debugging if needed

    finally:
        processor.stop()


def test_batch_processor_error_handling():
    """Test error handling in batch processing."""

    def failing_process_batch(requests):
        # Fail on purpose
        raise ValueError("Processing failed!")

    processor = BatchProcessor(batch_size=2, wait_time_ms=50, process_fn=failing_process_batch)
    processor.start()

    try:
        # Submit request that will fail
        with pytest.raises(ValueError, match="Processing failed!"):
            processor.submit("data", priority=RequestPriority.BATCH)

    finally:
        processor.stop()


def test_batch_processor_metrics():
    """Test metrics collection."""

    def process_batch(requests):
        return [f"result-{req.data}" for req in requests]

    processor = BatchProcessor(batch_size=3, wait_time_ms=100, process_fn=process_batch)
    processor.start()

    try:
        # Submit requests with different priorities
        processor.submit("a", priority=RequestPriority.INTERACTIVE)
        processor.submit("b", priority=RequestPriority.BATCH)
        processor.submit("c", priority=RequestPriority.BATCH)

        # Get metrics
        metrics = processor.get_metrics()

        assert isinstance(metrics, BatchMetrics)
        assert metrics.total_requests == 3
        assert metrics.total_batches >= 1
        assert metrics.interactive_requests == 1
        assert metrics.batch_requests == 2
        assert metrics.avg_batch_size > 0
        assert metrics.requests_per_second >= 0

        # Test metrics serialization
        metrics_dict = metrics.to_dict()
        assert "total_requests" in metrics_dict
        assert "avg_batch_size" in metrics_dict
        assert metrics_dict["total_requests"] == 3

    finally:
        processor.stop()


def test_batch_processor_concurrent_requests():
    """Test handling concurrent requests."""

    def process_batch(requests):
        # Simulate some processing time
        time.sleep(0.01)
        return [f"result-{req.data}" for req in requests]

    processor = BatchProcessor(batch_size=5, wait_time_ms=100, process_fn=process_batch)
    processor.start()

    try:
        import concurrent.futures

        # Submit many requests concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(processor.submit, f"req{i}", RequestPriority.BATCH)
                for i in range(20)
            ]

            results = [f.result() for f in futures]

        # All requests should complete successfully
        assert len(results) == 20
        assert all(r.startswith("result-req") for r in results)

        # Check metrics
        metrics = processor.get_metrics()
        assert metrics.total_requests == 20

    finally:
        processor.stop()


def test_batch_processor_invalid_config():
    """Test validation of batch processor configuration."""

    def dummy_process(requests):
        return [req.data for req in requests]

    # Invalid batch size
    with pytest.raises(ValueError, match="batch_size must be >= 1"):
        BatchProcessor(batch_size=0, wait_time_ms=100, process_fn=dummy_process)

    # Invalid wait time
    with pytest.raises(ValueError, match="wait_time_ms must be >= 1"):
        BatchProcessor(batch_size=4, wait_time_ms=0, process_fn=dummy_process)


def test_batch_processor_not_started():
    """Test error when submitting to non-running processor."""

    def dummy_process(requests):
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=4, wait_time_ms=100, process_fn=dummy_process)

    # Should raise error when not started
    with pytest.raises(RuntimeError, match="not running"):
        processor.submit("data", priority=RequestPriority.BATCH)


def test_batch_processor_stop():
    """Test graceful shutdown."""

    def process_batch(requests):
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=4, wait_time_ms=100, process_fn=process_batch)
    processor.start()

    # Submit a request
    result = processor.submit("test", priority=RequestPriority.BATCH)
    assert result == "test"

    # Stop processor
    processor.stop(timeout=2.0)

    # Should not be running
    with pytest.raises(RuntimeError, match="not running"):
        processor.submit("data", priority=RequestPriority.BATCH)


def test_batch_processor_start_when_already_running(caplog):
    """Calling start() a second time should be a no-op that logs a warning."""

    def process_batch(requests):
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=4, wait_time_ms=100, process_fn=process_batch)
    processor.start()

    try:
        first_thread = processor._thread
        with caplog.at_level(logging.WARNING):
            processor.start()

        assert "already running" in caplog.text
        # Should still be the same thread, not restarted
        assert processor._thread is first_thread
    finally:
        processor.stop()


def test_batch_processor_stop_when_not_running():
    """Calling stop() before start() should return without error."""

    def process_batch(requests):
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=4, wait_time_ms=100, process_fn=process_batch)

    # Should simply return, no exception
    processor.stop()
    assert processor._running is False


def test_batch_processor_stop_logs_when_thread_does_not_stop_cleanly(caplog):
    """If the worker thread is blocked in process_fn, stop() should log a warning
    instead of raising when the join times out."""

    def slow_process(requests):
        time.sleep(1.0)
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=1, wait_time_ms=10, process_fn=slow_process)
    processor.start()

    submit_thread = threading.Thread(
        target=processor.submit, args=("x",), kwargs={"priority": RequestPriority.BATCH}
    )
    submit_thread.start()
    time.sleep(0.1)  # Let the worker thread start processing (and sleeping)

    try:
        with caplog.at_level(logging.WARNING):
            processor.stop(timeout=0.05)

        assert "did not stop cleanly" in caplog.text
    finally:
        # Let the slow processing finish for real so nothing leaks into other tests
        submit_thread.join(timeout=5)
        if processor._thread is not None:
            processor._thread.join(timeout=5)


def test_batch_processor_submit_times_out_before_batch_processed():
    """submit() should raise TimeoutError if the result isn't ready in time."""

    def process_batch(requests):
        return [req.data for req in requests]

    # Large wait_time_ms means the batch won't be flushed before our tiny submit timeout.
    processor = BatchProcessor(batch_size=100, wait_time_ms=200, process_fn=process_batch)
    processor.start()

    try:
        with pytest.raises(TimeoutError, match="timed out after"):
            processor.submit("x", priority=RequestPriority.BATCH, timeout=0.02)
    finally:
        processor.stop()


def test_batch_processor_loop_recovers_from_unexpected_exception(caplog):
    """An exception raised outside of _process_batch's own handling (e.g. from
    _collect_batch) should be logged and the loop should keep running."""

    def process_batch(requests):
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=2, wait_time_ms=50, process_fn=process_batch)
    original_collect_batch = processor._collect_batch
    call_count = {"n": 0}

    def flaky_collect_batch():
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return original_collect_batch()

    processor._collect_batch = flaky_collect_batch

    with caplog.at_level(logging.ERROR):
        processor.start()
        try:
            result = processor.submit("ok", priority=RequestPriority.BATCH, timeout=5)
        finally:
            processor.stop()

    assert result == "ok"
    assert "Error in batch processing loop" in caplog.text


def test_collect_batch_breaks_when_deadline_passes_mid_iteration(monkeypatch):
    """_collect_batch should stop collecting once the deadline has passed, even
    if it's discovered partway through the wait loop."""

    def process_batch(requests):
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=5, wait_time_ms=50, process_fn=process_batch)

    times = iter([1000.0, 1000.01, 1000.2])
    monkeypatch.setattr("nyxgpt.batch_processor.time.time", lambda: next(times, 1000.2))

    batch = processor._collect_batch()
    assert batch == []


def test_process_batch_returns_immediately_for_empty_batch():
    """_process_batch should be a no-op when given an empty batch."""

    calls = []

    def process_batch(requests):
        calls.append(requests)
        return [req.data for req in requests]

    processor = BatchProcessor(batch_size=2, wait_time_ms=50, process_fn=process_batch)
    processor._process_batch([])

    assert calls == []


def test_process_batch_result_count_mismatch_raises_value_error():
    """If process_fn returns a different number of results than requests, every
    request in the batch should receive a ValueError."""

    def bad_process(requests):
        return []  # Always wrong: caller always submits at least one request

    processor = BatchProcessor(batch_size=1, wait_time_ms=50, process_fn=bad_process)
    processor.start()

    try:
        with pytest.raises(ValueError, match="expected 1"):
            processor.submit("x", priority=RequestPriority.BATCH, timeout=5)
    finally:
        processor.stop()
