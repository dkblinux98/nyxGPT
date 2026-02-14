"""Resource usage monitoring for nyxGPT API.

This module provides utilities for tracking system resource usage including:
- Memory tracking (RSS, VMS, available system memory)
- CPU utilization (process and system-wide)
- Request latency tracking
- Queue depth monitoring (for batch processing)
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any

import psutil

logger = logging.getLogger(__name__)


@dataclass
class ResourceMetrics:
    """System and process resource usage metrics.

    Attributes:
        memory_rss_mb: Resident Set Size (physical memory) in MB
        memory_vms_mb: Virtual Memory Size in MB
        memory_percent: Percentage of system memory used by process
        memory_available_mb: Available system memory in MB
        cpu_percent_process: CPU usage percentage for this process
        cpu_percent_system: Overall system CPU usage percentage
        avg_request_latency_ms: Average request latency in milliseconds
        p50_request_latency_ms: 50th percentile (median) request latency
        p95_request_latency_ms: 95th percentile request latency
        p99_request_latency_ms: 99th percentile request latency
        queue_depth: Current number of requests in batch queue
        total_requests: Total number of requests tracked
    """

    memory_rss_mb: float
    memory_vms_mb: float
    memory_percent: float
    memory_available_mb: float
    cpu_percent_process: float
    cpu_percent_system: float
    avg_request_latency_ms: float
    p50_request_latency_ms: float
    p95_request_latency_ms: float
    p99_request_latency_ms: float
    queue_depth: int
    total_requests: int

    def to_dict(self) -> dict:
        """Convert metrics to dictionary for JSON serialization."""
        return {
            "memory": {
                "rss_mb": round(self.memory_rss_mb, 2),
                "vms_mb": round(self.memory_vms_mb, 2),
                "percent": round(self.memory_percent, 2),
                "available_mb": round(self.memory_available_mb, 2),
            },
            "cpu": {
                "process_percent": round(self.cpu_percent_process, 2),
                "system_percent": round(self.cpu_percent_system, 2),
            },
            "latency": {
                "avg_ms": round(self.avg_request_latency_ms, 2),
                "p50_ms": round(self.p50_request_latency_ms, 2),
                "p95_ms": round(self.p95_request_latency_ms, 2),
                "p99_ms": round(self.p99_request_latency_ms, 2),
            },
            "queue": {
                "depth": self.queue_depth,
                "total_requests": self.total_requests,
            },
        }


class ResourceMonitor:
    """Monitor system and process resource usage.

    This class tracks resource metrics including memory, CPU, request latency,
    and queue depth. It maintains a sliding window of request latency samples
    for percentile calculations.

    Example:
        >>> monitor = ResourceMonitor(max_samples=1000)
        >>> monitor.record_request_latency(45.2)
        >>> metrics = monitor.get_metrics()
        >>> print(f"CPU: {metrics.cpu_percent_process}%")
    """

    def __init__(self, max_samples: int = 1000, batch_processor: Any = None):
        """Initialize resource monitor.

        Args:
            max_samples: Maximum number of latency samples to keep in memory
            batch_processor: Optional BatchProcessor instance for queue metrics
        """
        self._process = psutil.Process()
        self._max_samples = max_samples
        self._latency_samples: deque[float] = deque(maxlen=max_samples)
        self._total_requests = 0
        self._lock = threading.Lock()
        self._batch_processor = batch_processor

        # Initialize CPU percent (first call always returns 0.0)
        self._process.cpu_percent()

    def record_request_latency(self, latency_ms: float) -> None:
        """Record a request latency sample.

        Args:
            latency_ms: Request latency in milliseconds
        """
        with self._lock:
            self._latency_samples.append(latency_ms)
            self._total_requests += 1

    def get_metrics(self) -> ResourceMetrics:
        """Get current resource usage metrics.

        Returns:
            ResourceMetrics with current system and process metrics
        """
        # Get memory info
        mem_info = self._process.memory_info()
        mem_percent = self._process.memory_percent()
        system_mem = psutil.virtual_memory()

        # Get CPU info (interval=None uses cached value from previous call)
        cpu_percent_process = self._process.cpu_percent()
        cpu_percent_system = psutil.cpu_percent()

        # Calculate latency percentiles
        with self._lock:
            samples = sorted(self._latency_samples)
            total_requests = self._total_requests

        if samples:
            avg_latency = sum(samples) / len(samples)
            p50_latency = self._percentile(samples, 50)
            p95_latency = self._percentile(samples, 95)
            p99_latency = self._percentile(samples, 99)
        else:
            avg_latency = p50_latency = p95_latency = p99_latency = 0.0

        # Get queue depth from batch processor if available
        queue_depth = 0
        if self._batch_processor is not None:
            # Access the private queue size (this is safe since we own both modules)
            try:
                queue_depth = self._batch_processor._queue.qsize()
            except Exception:
                # Fallback if queue access fails
                queue_depth = 0

        return ResourceMetrics(
            memory_rss_mb=mem_info.rss / (1024 * 1024),
            memory_vms_mb=mem_info.vms / (1024 * 1024),
            memory_percent=mem_percent,
            memory_available_mb=system_mem.available / (1024 * 1024),
            cpu_percent_process=cpu_percent_process,
            cpu_percent_system=cpu_percent_system,
            avg_request_latency_ms=avg_latency,
            p50_request_latency_ms=p50_latency,
            p95_request_latency_ms=p95_latency,
            p99_request_latency_ms=p99_latency,
            queue_depth=queue_depth,
            total_requests=total_requests,
        )

    @staticmethod
    def _percentile(sorted_samples: list[float], percentile: int) -> float:
        """Calculate percentile from sorted samples.

        Args:
            sorted_samples: Sorted list of samples
            percentile: Percentile to calculate (0-100)

        Returns:
            Percentile value
        """
        if not sorted_samples:
            return 0.0

        if len(sorted_samples) == 1:
            return sorted_samples[0]

        # Use nearest-rank method
        rank = (percentile / 100) * (len(sorted_samples) - 1)
        lower = int(rank)
        upper = min(lower + 1, len(sorted_samples) - 1)
        weight = rank - lower

        return sorted_samples[lower] * (1 - weight) + sorted_samples[upper] * weight


# Global resource monitor instance (initialized at startup)
_resource_monitor: ResourceMonitor | None = None


def init_resource_monitor(batch_processor: Any = None) -> ResourceMonitor:
    """Initialize the global resource monitor.

    Args:
        batch_processor: Optional BatchProcessor instance for queue metrics

    Returns:
        Initialized ResourceMonitor instance
    """
    global _resource_monitor
    _resource_monitor = ResourceMonitor(max_samples=1000, batch_processor=batch_processor)
    logger.info("Resource monitor initialized")
    return _resource_monitor


def get_resource_monitor() -> ResourceMonitor | None:
    """Get the global resource monitor instance.

    Returns:
        ResourceMonitor instance or None if not initialized
    """
    return _resource_monitor
