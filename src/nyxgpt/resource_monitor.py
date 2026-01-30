"""Resource usage monitoring for nyxGPT.

Tracks memory, CPU, request latency, and queue depth metrics.
"""

from __future__ import annotations

import time
import psutil
from threading import Lock
from typing import Optional
from collections import deque
from dataclasses import dataclass, field


@dataclass
class ResourceMetrics:
    """Resource usage metrics snapshot."""

    # Memory metrics (bytes)
    memory_rss: int  # Resident Set Size (physical memory)
    memory_vms: int  # Virtual Memory Size
    memory_percent: float  # Percentage of system memory

    # CPU metrics
    cpu_percent: float  # Process CPU usage percentage

    # Request metrics
    request_count: int  # Total requests processed
    active_requests: int  # Currently active requests
    avg_latency_ms: float  # Average request latency in milliseconds
    p95_latency_ms: float  # 95th percentile latency
    p99_latency_ms: float  # 99th percentile latency

    # Queue metrics
    queue_depth: int  # Current queue depth (active requests)
    max_queue_depth: int  # Maximum observed queue depth

    # Timestamp
    timestamp: float = field(default_factory=time.time)


class ResourceMonitor:
    """Monitor and report resource usage."""

    def __init__(self, latency_window: int = 1000):
        """Initialize resource monitor.

        Args:
            latency_window: Number of recent latencies to track for percentile calculation
        """
        self._lock = Lock()
        self._process = psutil.Process()

        # Request counters
        self._request_count = 0
        self._active_requests = 0
        self._max_queue_depth = 0

        # Latency tracking (circular buffer)
        self._latency_window = latency_window
        self._latencies: deque[float] = deque(maxlen=latency_window)

    def start_request(self) -> float:
        """Mark the start of a request.

        Returns:
            Start timestamp for latency calculation
        """
        with self._lock:
            self._active_requests += 1
            if self._active_requests > self._max_queue_depth:
                self._max_queue_depth = self._active_requests

        return time.time()

    def end_request(self, start_time: float) -> None:
        """Mark the end of a request and record latency.

        Args:
            start_time: Request start timestamp from start_request()
        """
        latency = (time.time() - start_time) * 1000  # Convert to milliseconds

        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._request_count += 1
            self._latencies.append(latency)

    def get_metrics(self) -> ResourceMetrics:
        """Get current resource usage metrics.

        Returns:
            ResourceMetrics snapshot
        """
        with self._lock:
            # Memory metrics
            mem_info = self._process.memory_info()
            memory_rss = mem_info.rss
            memory_vms = mem_info.vms
            memory_percent = self._process.memory_percent()

            # CPU metrics (non-blocking, 0.0 interval)
            cpu_percent = self._process.cpu_percent(interval=0.0)

            # Request metrics
            request_count = self._request_count
            active_requests = self._active_requests
            queue_depth = self._active_requests
            max_queue_depth = self._max_queue_depth

            # Latency percentiles
            latencies = sorted(self._latencies)
            if latencies:
                avg_latency = sum(latencies) / len(latencies)
                p95_idx = int(len(latencies) * 0.95)
                p99_idx = int(len(latencies) * 0.99)
                p95_latency = latencies[p95_idx] if p95_idx < len(latencies) else latencies[-1]
                p99_latency = latencies[p99_idx] if p99_idx < len(latencies) else latencies[-1]
            else:
                avg_latency = 0.0
                p95_latency = 0.0
                p99_latency = 0.0

        return ResourceMetrics(
            memory_rss=memory_rss,
            memory_vms=memory_vms,
            memory_percent=memory_percent,
            cpu_percent=cpu_percent,
            request_count=request_count,
            active_requests=active_requests,
            avg_latency_ms=avg_latency,
            p95_latency_ms=p95_latency,
            p99_latency_ms=p99_latency,
            queue_depth=queue_depth,
            max_queue_depth=max_queue_depth,
        )

    def reset_counters(self) -> None:
        """Reset request counters and latency history."""
        with self._lock:
            self._request_count = 0
            self._max_queue_depth = self._active_requests  # Keep current as max
            self._latencies.clear()


# Global singleton instance
_monitor: Optional[ResourceMonitor] = None
_monitor_lock = Lock()


def get_monitor() -> ResourceMonitor:
    """Get or create the global resource monitor instance.

    Returns:
        Global ResourceMonitor singleton
    """
    global _monitor
    if _monitor is None:
        with _monitor_lock:
            if _monitor is None:
                _monitor = ResourceMonitor()
    return _monitor
