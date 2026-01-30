"""Resource usage monitoring for nyxGPT.

Lightweight monitoring of memory, CPU, and request metrics.
"""

from __future__ import annotations

import logging
import time
import psutil
from dataclasses import dataclass, field
from typing import Dict
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class ResourceMetrics:
    """Current resource usage metrics."""

    memory_mb: float
    memory_percent: float
    cpu_percent: float
    active_requests: int
    avg_latency_ms: float


@dataclass
class RequestTracker:
    """Track request latency and queue depth."""

    active: int = 0
    latencies: deque = field(default_factory=lambda: deque(maxlen=100))

    def start_request(self):
        """Increment active request counter."""
        self.active += 1
        return time.perf_counter()

    def end_request(self, start_time: float):
        """Record request completion."""
        self.active -= 1
        latency_ms = (time.perf_counter() - start_time) * 1000.0
        self.latencies.append(latency_ms)

    def get_avg_latency(self) -> float:
        """Get average latency over recent requests."""
        if not self.latencies:
            return 0.0
        return sum(self.latencies) / len(self.latencies)


# Global request tracker
_request_tracker = RequestTracker()


def get_metrics() -> ResourceMetrics:
    """Get current resource usage metrics.

    Returns:
        ResourceMetrics with current system state
    """
    process = psutil.Process()

    mem_info = process.memory_info()
    memory_mb = mem_info.rss / (1024 * 1024)
    memory_percent = process.memory_percent()

    # CPU percent over a short interval
    cpu_percent = process.cpu_percent(interval=0.1)

    return ResourceMetrics(
        memory_mb=memory_mb,
        memory_percent=memory_percent,
        cpu_percent=cpu_percent,
        active_requests=_request_tracker.active,
        avg_latency_ms=_request_tracker.get_avg_latency(),
    )


def start_request() -> float:
    """Mark the start of a request. Returns start timestamp."""
    return _request_tracker.start_request()


def end_request(start_time: float):
    """Mark the end of a request."""
    _request_tracker.end_request(start_time)
