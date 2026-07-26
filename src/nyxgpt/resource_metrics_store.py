"""Server-side persistence for resource usage metrics history.

`ResourceMonitor` (resource_monitor.py) only ever reflects the *current*
instant -- it has no memory of the past. The Settings -> Resource Usage page
needs real history (Last Hour / Last 24 Hours / Last 7 Days) that survives a
page reload and exists even if no browser tab was ever open, so this module
periodically samples the resource monitor from a background thread and keeps
the samples in an in-memory ring buffer, appending each sample to a JSONL
file under the log directory so history survives an API process restart too.

Mirrors the in-memory-ring-buffer-plus-JSONL-file pattern used by
`usage_analytics.py` for the same restart-durability tradeoff, and the
background-thread-with-stop-event pattern used by `self_heal.Watchdog`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from configparser import ConfigParser
from pathlib import Path
from typing import Any

from nyxgpt.logging import get_log_dir
from nyxgpt.resource_monitor import ResourceMonitor

logger = logging.getLogger(__name__)

# One sample per minute keeps the 7-day retention window's memory/disk
# footprint small (~10,080 samples) while still giving "Last Hour" a
# meaningful 60 points.
DEFAULT_SAMPLE_INTERVAL_SECONDS = 60.0

RANGE_WINDOW_SECONDS: dict[str, int] = {"1h": 3600, "24h": 86400, "7d": 604800}
RETENTION_SECONDS = RANGE_WINDOW_SECONDS["7d"]

# Cap points returned per request so the chart renders quickly regardless of
# how much history has accumulated; longer ranges are downsampled to fit.
MAX_POINTS_RETURNED = 300

_NUMERIC_FIELDS = (
    "memory_rss_mb",
    "memory_percent",
    "cpu_process_percent",
    "cpu_system_percent",
    "avg_latency_ms",
    "p99_latency_ms",
    "queue_depth",
)

_MAX_SAMPLES = int(RETENTION_SECONDS // DEFAULT_SAMPLE_INTERVAL_SECONDS) + 10
_MAX_LOG_BYTES = 2_000_000
_TAIL_CHUNK_SIZE = 65536

_lock = threading.Lock()
_samples: deque[dict[str, Any]] = deque(maxlen=_MAX_SAMPLES)
_disk_loaded = False


def _history_log_path(cfg: ConfigParser | None = None) -> Path:
    """Return the path of the JSONL file history samples are appended to."""
    return get_log_dir(cfg) / "resource_metrics_history.jsonl"


def _tail_lines(path: Path, limit: int) -> list[str]:
    """Return up to the last `limit` lines of `path` without loading the whole file into memory."""
    chunks: list[bytes] = []
    newline_count = 0
    with path.open("rb") as f:
        f.seek(0, 2)
        remaining = f.tell()
        while remaining > 0 and newline_count <= limit:
            read_size = min(_TAIL_CHUNK_SIZE, remaining)
            remaining -= read_size
            f.seek(remaining)
            data = f.read(read_size)
            newline_count += data.count(b"\n")
            chunks.append(data)
    text = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
    return text.splitlines()[-limit:]


def _truncate_log_if_large(path: Path) -> None:
    """Rewrite the history log to keep only the most recent `_MAX_SAMPLES` lines, if it has grown large."""
    if path.stat().st_size <= _MAX_LOG_BYTES:
        return
    lines = _tail_lines(path, _MAX_SAMPLES)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp_path.replace(path)


def _load_from_disk(cfg: ConfigParser | None = None) -> list[dict[str, Any]]:
    """Load persisted samples from the JSONL log into the in-memory buffer.

    Only ever runs once per process (guarded by `_disk_loaded`) since after
    that the in-memory buffer is authoritative and strictly newer.
    """
    global _disk_loaded
    path = _history_log_path(cfg)
    loaded: list[dict[str, Any]] = []
    if path.exists():
        try:
            for line in _tail_lines(path, _MAX_SAMPLES):
                if not line.strip():
                    continue
                try:
                    loaded.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            loaded = []
    with _lock:
        if not _disk_loaded:
            for sample in loaded:
                _samples.append(sample)
            _disk_loaded = True
        current = list(_samples)
    return current


def _sample_from_monitor(monitor: ResourceMonitor) -> dict[str, Any]:
    """Build a JSON-serializable history sample dict from `monitor`'s current metrics."""
    m = monitor.get_metrics()
    return {
        "ts": time.time(),
        "memory_rss_mb": round(m.memory_rss_mb, 2),
        "memory_percent": round(m.memory_percent, 2),
        "cpu_process_percent": round(m.cpu_percent_process, 2),
        "cpu_system_percent": round(m.cpu_percent_system, 2),
        "avg_latency_ms": round(m.avg_request_latency_ms, 2),
        "p99_latency_ms": round(m.p99_request_latency_ms, 2),
        "queue_depth": m.queue_depth,
    }


def record_sample(monitor: ResourceMonitor, cfg: ConfigParser | None = None) -> dict[str, Any]:
    """Sample `monitor` now, append it to the in-memory buffer and the on-disk log."""
    sample = _sample_from_monitor(monitor)
    with _lock:
        _samples.append(sample)
        try:
            path = _history_log_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(sample) + "\n")
            _truncate_log_if_large(path)
        except OSError:
            # History persistence is best-effort; never fail the sampler over it.
            logger.warning("resource metrics history: failed to persist sample", exc_info=True)
    return sample


def _downsample(points: list[dict[str, Any]], max_points: int) -> list[dict[str, Any]]:
    """Bucket-average `points` down to at most `max_points`, preserving chronological order."""
    if len(points) <= max_points:
        return points

    bucket_size = len(points) / max_points
    result: list[dict[str, Any]] = []
    for i in range(max_points):
        start = int(i * bucket_size)
        end = max(int((i + 1) * bucket_size), start + 1)
        bucket = points[start:end]
        if not bucket:
            continue
        averaged: dict[str, Any] = {"ts": bucket[-1]["ts"]}
        for field in _NUMERIC_FIELDS:
            averaged[field] = round(sum(p[field] for p in bucket) / len(bucket), 2)
        result.append(averaged)
    return result


def query_history(
    range_key: str,
    now: float | None = None,
    max_points: int = MAX_POINTS_RETURNED,
    cfg: ConfigParser | None = None,
) -> dict[str, Any]:
    """Return a downsampled series of history samples covering the requested window.

    Args:
        range_key: One of "1h", "24h", "7d".
        now: Reference time (defaults to `time.time()`); overridable for tests.
        max_points: Maximum number of points to return (downsampled if exceeded).
        cfg: Optional config override, for tests that use a non-default log directory.

    Returns:
        A dict with the requested range, the (possibly downsampled) points,
        the sample cadence, and an honest accounting of how much history is
        actually available so the UI never renders a misleadingly full chart.

    Raises:
        ValueError: If `range_key` is not one of the supported ranges.
    """
    if range_key not in RANGE_WINDOW_SECONDS:
        raise ValueError(
            f"invalid range {range_key!r}; expected one of {sorted(RANGE_WINDOW_SECONDS)}"
        )

    now = time.time() if now is None else now
    window_seconds = RANGE_WINDOW_SECONDS[range_key]
    cutoff = now - window_seconds

    with _lock:
        all_points = list(_samples)
    if not all_points and not _disk_loaded:
        all_points = _load_from_disk(cfg)

    windowed = [p for p in all_points if p["ts"] >= cutoff]
    earliest_ts = all_points[0]["ts"] if all_points else None
    history_available_seconds = 0.0
    if earliest_ts is not None:
        history_available_seconds = round(min(now - earliest_ts, window_seconds), 1)

    return {
        "range": range_key,
        "points": _downsample(windowed, max_points),
        "sample_interval_seconds": DEFAULT_SAMPLE_INTERVAL_SECONDS,
        "requested_window_seconds": window_seconds,
        "earliest_available_ts": earliest_ts,
        "history_available_seconds": history_available_seconds,
    }


class Sampler:
    """Background thread that periodically records a resource-metrics sample."""

    def __init__(
        self,
        monitor_getter: Callable[[], ResourceMonitor | None],
        interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        cfg: ConfigParser | None = None,
    ) -> None:
        """Configure the sampler's cadence and monitor source.

        Does not start the background thread; call `start()` for that.
        """
        self._monitor_getter = monitor_getter
        self.interval_seconds = interval_seconds
        self._cfg = cfg
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """Start the background sampling loop in a daemon thread.

        No-op (with a warning logged) if the loop is already running.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Resource metrics sampler already running")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(
            "Resource metrics sampler started (interval=%.0fs)",
            self.interval_seconds,
            extra={"component": "resource_metrics"},
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the background loop to stop and join it (waiting up to `timeout` seconds)."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("Resource metrics sampler stopped", extra={"component": "resource_metrics"})

    def _loop(self) -> None:
        """Background loop: record a sample on each interval while a monitor is available.

        Runs until `stop()` is called. Exceptions from a sampling pass are
        logged and swallowed so one failed pass doesn't kill the thread.
        """
        while not self._stop_event.is_set():
            try:
                monitor = self._monitor_getter()
                if monitor is not None:
                    record_sample(monitor, cfg=self._cfg)
            except Exception:
                logger.exception("resource metrics sampler: error recording sample")
            self._stop_event.wait(self.interval_seconds)


_sampler: Sampler | None = None


def get_sampler() -> Sampler:
    """Return the process-wide `Sampler` singleton, creating it on first call."""
    global _sampler
    if _sampler is None:
        from nyxgpt.resource_monitor import get_resource_monitor

        _sampler = Sampler(monitor_getter=get_resource_monitor)
    return _sampler


def reset_for_tests(disk_loaded: bool = False) -> None:
    """Clear the in-memory sample buffer. Test-only helper.

    `disk_loaded` defaults to False so `_load_from_disk` still runs as normal
    on the next empty-buffer query. Pass `disk_loaded=True` when a test wants
    queries to see only in-memory state (e.g. samples it records directly)
    without falling through to whatever the real on-disk history log
    happens to contain in the ambient environment.
    """
    global _disk_loaded
    with _lock:
        _samples.clear()
        _disk_loaded = disk_loaded
