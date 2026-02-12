"""Request batching for chat and RAG queries.

This module provides a batching system for improving throughput of chat and RAG requests
by grouping multiple independent requests together for processing.

The batching system supports:
- Configurable batch size and wait time
- Priority-based request handling (interactive vs batch)
- Metrics collection for batch efficiency monitoring
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


class RequestPriority(Enum):
    """Priority levels for batch requests."""

    INTERACTIVE = 1  # High priority - process quickly for interactive users
    BATCH = 2  # Low priority - can wait for better batching efficiency


@dataclass
class BatchRequest(Generic[T, R]):
    """A single request in the batch queue.

    Attributes:
        request_id: Unique identifier for this request
        data: Request payload data
        priority: Request priority level
        result_future: Future to store the result or error
        submit_time: Timestamp when request was submitted
    """

    request_id: str
    data: T
    priority: RequestPriority
    result_future: threading.Event
    submit_time: float = field(default_factory=time.time)
    result: R | None = None
    error: Exception | None = None


@dataclass
class BatchMetrics:
    """Metrics for batch processing efficiency.

    Attributes:
        total_requests: Total number of requests processed
        total_batches: Total number of batches processed
        avg_batch_size: Average number of requests per batch
        avg_wait_time_ms: Average time requests wait in queue (milliseconds)
        avg_process_time_ms: Average time to process a batch (milliseconds)
        requests_per_second: Throughput in requests per second
        interactive_requests: Number of high-priority interactive requests
        batch_requests: Number of low-priority batch requests
    """

    total_requests: int = 0
    total_batches: int = 0
    avg_batch_size: float = 0.0
    avg_wait_time_ms: float = 0.0
    avg_process_time_ms: float = 0.0
    requests_per_second: float = 0.0
    interactive_requests: int = 0
    batch_requests: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary for serialization."""
        return {
            "total_requests": self.total_requests,
            "total_batches": self.total_batches,
            "avg_batch_size": round(self.avg_batch_size, 2),
            "avg_wait_time_ms": round(self.avg_wait_time_ms, 2),
            "avg_process_time_ms": round(self.avg_process_time_ms, 2),
            "requests_per_second": round(self.requests_per_second, 2),
            "interactive_requests": self.interactive_requests,
            "batch_requests": self.batch_requests,
        }


class BatchProcessor(Generic[T, R]):
    """Batch processor for grouping and processing requests.

    This processor collects incoming requests in a queue and processes them
    in batches to improve throughput. Requests are grouped by:
    - Maximum batch size (process when batch is full)
    - Maximum wait time (process after timeout even if batch not full)
    - Priority (interactive requests get processed faster)

    Example:
        >>> def process_batch(requests):
        ...     # Process all requests together
        ...     return [result for req in requests]
        ...
        >>> processor = BatchProcessor(
        ...     batch_size=10,
        ...     wait_time_ms=100,
        ...     process_fn=process_batch
        ... )
        >>> processor.start()
        >>> result = processor.submit("request data", priority=RequestPriority.INTERACTIVE)
        >>> processor.stop()
    """

    def __init__(
        self,
        batch_size: int,
        wait_time_ms: int,
        process_fn: Callable[[list[BatchRequest[T, R]]], list[R]],
    ):
        """Initialize batch processor.

        Args:
            batch_size: Maximum number of requests per batch
            wait_time_ms: Maximum time to wait for batch to fill (milliseconds)
            process_fn: Function that processes a batch of requests
                       Returns list of results in same order as requests
        """
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")
        if wait_time_ms < 1:
            raise ValueError(f"wait_time_ms must be >= 1, got {wait_time_ms}")

        self.batch_size = batch_size
        self.wait_time_s = wait_time_ms / 1000.0
        self.process_fn = process_fn

        # Queue uses priority for ordering
        # Use counter as tie-breaker to avoid comparing BatchRequest objects
        self._queue: queue.PriorityQueue[tuple[int, int, BatchRequest[T, R]]] = (
            queue.PriorityQueue()
        )
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._lock = threading.Lock()

        # Metrics tracking
        self._total_requests = 0
        self._total_batches = 0
        self._total_wait_time = 0.0
        self._total_process_time = 0.0
        self._interactive_count = 0
        self._batch_count = 0
        self._start_time: float | None = None

    def start(self) -> None:
        """Start the batch processor thread."""
        with self._lock:
            if self._running:
                logger.warning("Batch processor already running")
                return

            self._running = True
            self._start_time = time.time()
            self._thread = threading.Thread(target=self._process_loop, daemon=True)
            self._thread.start()
            logger.info(
                "Batch processor started (batch_size=%d, wait_time_ms=%.1f)",
                self.batch_size,
                self.wait_time_s * 1000,
            )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the batch processor and wait for thread to finish.

        Args:
            timeout: Maximum time to wait for thread shutdown (seconds)
        """
        with self._lock:
            if not self._running:
                return

            self._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("Batch processor thread did not stop cleanly")
            else:
                logger.info("Batch processor stopped")

    def submit(
        self, data: T, priority: RequestPriority = RequestPriority.BATCH, timeout: float = 30.0
    ) -> R:
        """Submit a request for batch processing.

        Args:
            data: Request data to process
            priority: Request priority (INTERACTIVE or BATCH)
            timeout: Maximum time to wait for result (seconds)

        Returns:
            Processed result

        Raises:
            RuntimeError: If processor is not running
            TimeoutError: If request times out
            Exception: Any exception raised during processing
        """
        if not self._running:
            raise RuntimeError("Batch processor is not running")

        # Create request with event for synchronization
        request = BatchRequest[T, R](
            request_id=str(time.time_ns()),  # Unique ID
            data=data,
            priority=priority,
            result_future=threading.Event(),
            submit_time=time.time(),
        )

        # Add to queue with priority (lower value = higher priority)
        # Use counter as tie-breaker to maintain FIFO order for same priority
        priority_value = priority.value
        with self._counter_lock:
            counter = self._counter
            self._counter += 1
        self._queue.put((priority_value, counter, request))

        # Wait for result
        if not request.result_future.wait(timeout=timeout):
            raise TimeoutError(f"Request timed out after {timeout}s")

        # Return result or raise error
        if request.error:
            raise request.error
        return request.result  # type: ignore

    def get_metrics(self) -> BatchMetrics:
        """Get current batch processing metrics.

        Returns:
            BatchMetrics with current statistics
        """
        with self._lock:
            total_reqs = self._total_requests or 1  # Avoid division by zero
            total_batches = self._total_batches or 1

            # Calculate requests per second
            elapsed = time.time() - self._start_time if self._start_time else 1.0
            rps = self._total_requests / max(elapsed, 0.001)

            return BatchMetrics(
                total_requests=self._total_requests,
                total_batches=self._total_batches,
                avg_batch_size=self._total_requests / total_batches,
                avg_wait_time_ms=(self._total_wait_time / total_reqs) * 1000,
                avg_process_time_ms=(self._total_process_time / total_batches) * 1000,
                requests_per_second=rps,
                interactive_requests=self._interactive_count,
                batch_requests=self._batch_count,
            )

    def _process_loop(self) -> None:
        """Main processing loop (runs in background thread)."""
        while self._running:
            try:
                batch = self._collect_batch()
                if batch:
                    self._process_batch(batch)
            except Exception as e:
                logger.error("Error in batch processing loop: %s", e, exc_info=True)

    def _collect_batch(self) -> list[BatchRequest[T, R]]:
        """Collect a batch of requests from the queue.

        Returns:
            List of requests to process (may be empty if timeout)
        """
        batch: list[BatchRequest[T, R]] = []
        deadline = time.time() + self.wait_time_s

        while len(batch) < self.batch_size and time.time() < deadline:
            remaining = deadline - time.time()
            if remaining <= 0:
                break

            try:
                # Get next request with timeout
                _priority, _counter, request = self._queue.get(timeout=min(remaining, 0.1))
                batch.append(request)

                # If batch is full, process immediately
                if len(batch) >= self.batch_size:
                    break

            except queue.Empty:
                # Queue is empty, continue waiting until deadline
                continue

        return batch

    def _process_batch(self, batch: list[BatchRequest[T, R]]) -> None:
        """Process a batch of requests.

        Args:
            batch: List of requests to process
        """
        if not batch:
            return

        start_time = time.time()

        try:
            # Call process function with all requests
            results = self.process_fn(batch)

            # Validate results
            if len(results) != len(batch):
                raise ValueError(
                    f"Process function returned {len(results)} results "
                    f"but expected {len(batch)} (one per request)"
                )

            # Assign results to requests
            for request, result in zip(batch, results, strict=True):
                request.result = result
                request.error = None
                request.result_future.set()

        except Exception as e:
            logger.error("Error processing batch: %s", e, exc_info=True)
            # Mark all requests as failed with the same error
            for request in batch:
                request.result = None
                request.error = e
                request.result_future.set()

        # Update metrics
        process_time = time.time() - start_time
        total_wait_time = sum(start_time - req.submit_time for req in batch)

        with self._lock:
            self._total_requests += len(batch)
            self._total_batches += 1
            self._total_wait_time += total_wait_time
            self._total_process_time += process_time

            for req in batch:
                if req.priority == RequestPriority.INTERACTIVE:
                    self._interactive_count += 1
                else:
                    self._batch_count += 1

        logger.debug(
            "Processed batch: size=%d, wait_time=%.2fms, process_time=%.2fms",
            len(batch),
            (total_wait_time / len(batch)) * 1000,
            process_time * 1000,
        )
