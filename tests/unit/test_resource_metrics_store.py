"""Unit tests for nyxgpt.resource_metrics_store (server-side resource history).

Related: #3352
"""

from __future__ import annotations

import json
from configparser import ConfigParser

import pytest

from nyxgpt import resource_metrics_store
from nyxgpt.resource_monitor import ResourceMonitor

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_store():
    """Ensure each test starts with an empty in-memory sample buffer."""
    resource_metrics_store.reset_for_tests()
    yield
    resource_metrics_store.reset_for_tests()


@pytest.fixture
def cfg(tmp_path):
    parser = ConfigParser()
    parser.add_section("logging")
    parser.set("logging", "dir", str(tmp_path))
    return parser


@pytest.fixture
def monitor():
    return ResourceMonitor(max_samples=10)


def _make_point(ts: float, value: float = 100.0) -> dict:
    return {
        "ts": ts,
        "memory_rss_mb": value,
        "memory_percent": value,
        "cpu_process_percent": value,
        "cpu_system_percent": value,
        "avg_latency_ms": value,
        "p99_latency_ms": value,
        "queue_depth": value,
    }


def test_record_sample_appends_to_buffer_and_disk(monitor, cfg):
    sample = resource_metrics_store.record_sample(monitor, cfg=cfg)

    assert sample["ts"] > 0
    assert "memory_rss_mb" in sample
    assert "queue_depth" in sample

    log_path = resource_metrics_store._history_log_path(cfg)
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["ts"] == sample["ts"]


def test_query_history_rejects_invalid_range():
    with pytest.raises(ValueError):
        resource_metrics_store.query_history("invalid")


def test_query_history_empty_reports_zero_availability(cfg):
    # Passing an isolated (empty) log dir keeps this assertion independent of
    # whatever the real on-disk history log happens to contain in the
    # ambient environment.
    result = resource_metrics_store.query_history("1h", now=1_000_000.0, cfg=cfg)

    assert result["range"] == "1h"
    assert result["points"] == []
    assert result["earliest_available_ts"] is None
    assert result["history_available_seconds"] == 0.0
    assert result["requested_window_seconds"] == 3600


def test_query_history_filters_points_outside_window():
    now = 1_000_000.0
    with resource_metrics_store._lock:
        resource_metrics_store._samples.append(_make_point(now - 7200))  # outside 1h window
        resource_metrics_store._samples.append(_make_point(now - 1800))  # inside 1h window
        resource_metrics_store._disk_loaded = True

    result = resource_metrics_store.query_history("1h", now=now)

    assert len(result["points"]) == 1
    assert result["points"][0]["ts"] == now - 1800


def test_query_history_reports_partial_availability_honestly():
    now = 1_000_000.0
    with resource_metrics_store._lock:
        resource_metrics_store._samples.append(_make_point(now - 300))
        resource_metrics_store._disk_loaded = True

    result = resource_metrics_store.query_history("1h", now=now)

    assert result["earliest_available_ts"] == now - 300
    assert result["history_available_seconds"] == 300.0
    assert result["history_available_seconds"] < result["requested_window_seconds"]


def test_query_history_caps_coverage_at_requested_window():
    now = 1_000_000.0
    with resource_metrics_store._lock:
        resource_metrics_store._samples.append(_make_point(now - 10 * 86400))  # 10 days old
        resource_metrics_store._disk_loaded = True

    result = resource_metrics_store.query_history("7d", now=now)

    # Even though the oldest sample is far outside the window, availability
    # is capped at the requested window rather than reporting a nonsensical
    # 10 days of "availability" for a 7-day request.
    assert result["history_available_seconds"] == 7 * 86400


def test_query_history_downsamples_when_points_exceed_max():
    now = 1_000_000.0
    with resource_metrics_store._lock:
        for i in range(50):
            resource_metrics_store._samples.append(_make_point(now - i, value=float(i)))
        resource_metrics_store._disk_loaded = True

    result = resource_metrics_store.query_history("1h", now=now, max_points=10)

    assert len(result["points"]) <= 10


def test_query_history_returns_all_points_under_max():
    now = 1_000_000.0
    with resource_metrics_store._lock:
        for i in range(5):
            resource_metrics_store._samples.append(_make_point(now - i))
        resource_metrics_store._disk_loaded = True

    result = resource_metrics_store.query_history("1h", now=now, max_points=300)

    assert len(result["points"]) == 5


def test_sampler_start_and_stop_records_samples(monitor, cfg):
    sampler = resource_metrics_store.Sampler(
        monitor_getter=lambda: monitor, interval_seconds=0.05, cfg=cfg
    )
    sampler.start()
    try:
        import time

        time.sleep(0.2)
    finally:
        sampler.stop()

    with resource_metrics_store._lock:
        assert len(resource_metrics_store._samples) >= 1


def test_sampler_start_twice_is_a_noop(monitor):
    sampler = resource_metrics_store.Sampler(monitor_getter=lambda: monitor, interval_seconds=10)
    sampler.start()
    try:
        first_thread = sampler._thread
        sampler.start()
        assert sampler._thread is first_thread
    finally:
        sampler.stop()


def test_sampler_swallows_monitor_getter_exceptions():
    def _raise():
        raise RuntimeError("boom")

    sampler = resource_metrics_store.Sampler(monitor_getter=_raise, interval_seconds=0.05)
    sampler.start()
    try:
        import time

        time.sleep(0.2)
    finally:
        sampler.stop()  # must not raise / hang despite the getter always failing


def test_load_from_disk_seeds_buffer_once(cfg):
    log_path = resource_metrics_store._history_log_path(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_make_point(123.0)) + "\n")
        f.write(json.dumps(_make_point(456.0)) + "\n")

    loaded = resource_metrics_store._load_from_disk(cfg)

    assert len(loaded) == 2
    assert resource_metrics_store._disk_loaded is True
