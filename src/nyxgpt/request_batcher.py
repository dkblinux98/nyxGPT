"""Request batching for improved throughput.

This module provides batching capabilities for chat and RAG requests,
accumulating multiple independent requests and processing them together
to improve throughput and resource utilization.
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class RequestPriority(Enum):
    """Request priority levels."""

    INTERACTIVE = 1  # High priority, bypass batching
    BATCH = 2  # Normal priority, can be batched


@dataclass
class BatchMetrics:
    """Metrics for batch efficiency tracking."""

    batch_id: str
    batch_size: int
    wait_time_ms: float
    process_time_ms: float
    total_time_ms: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to dictionary."""
        return {
            "batch_id": self.batch_id,
            "batch_size": self.batch_size,
            "wait_time_ms": round(self.wait_time_ms, 2),
            "process_time_ms": round(self.process_time_ms, 2),
            "total_time_ms": round(self.total_time_ms, 2),
            "timestamp": self.timestamp,
        }


@dataclass
class BatchRequest:
    """A request waiting to be batched."""

    request_id: str
    priority: RequestPriority
    data: Any
    future: asyncio.Future
    enqueue_time: float = field(default_factory=time.time)


class RequestBatcher:
    """Batch multiple independent requests for improved throughput.

    Accumulates requests in a queue and processes them together after either:
    - Batch size is reached
    - Timeout expires

    High priority (interactive) requests bypass batching and are processed immediately.
    """

    def __init__(
        self,
        batch_size: int = 5,
        batch_timeout_ms: int = 100,
        max_queue_size: int = 100,
        processor: Callable[[list[Any]], Coroutine[Any, Any, list[Any]]] | None = None,
    ):
        """Initialize request batcher.

        Args:
            batch_size: Maximum number of requests per batch
            batch_timeout_ms: Maximum wait time in milliseconds before processing batch
            max_queue_size: Maximum queue size (overflow protection)
            processor: Async function to process batch of requests
        """
        self.batch_size = batch_size
        self.batch_timeout_ms = batch_timeout_ms
        self.max_queue_size = max_queue_size
        self.processor = processor

        self._queue: asyncio.Queue[BatchRequest] = asyncio.Queue(maxsize=max_queue_size)
        self._metrics: list[BatchMetrics] = []
        self._metrics_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._running = False
        self._batch_counter = 0

        logger.info(
            f"RequestBatcher initialized: batch_size={batch_size}, "
            f"timeout={batch_timeout_ms}ms, max_queue={max_queue_size}"
        )

    async def start(self):
        """Start the batch processor task."""
        if self._running:
            logger.warning("RequestBatcher already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._process_batches())
        logger.info("RequestBatcher started")

    async def stop(self):
        """Stop the batch processor task."""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        logger.info("RequestBatcher stopped")

    async def submit(
        self,
        request_id: str,
        data: Any,
        priority: RequestPriority = RequestPriority.BATCH,
    ) -> Any:
        """Submit a request for processing.

        Args:
            request_id: Unique request identifier
            data: Request data to process
            priority: Request priority (INTERACTIVE bypasses batching)

        Returns:
            Processed result

        Raises:
            asyncio.QueueFull: If queue is full
        """
        # Interactive requests bypass batching
        if priority == RequestPriority.INTERACTIVE:
            logger.debug(f"Request {request_id} bypassing batch (interactive)")
            if self.processor:
                results = await self.processor([data])
                return results[0] if results else None
            return None

        # Create future for result
        future: asyncio.Future = asyncio.Future()
        request = BatchRequest(
            request_id=request_id,
            priority=priority,
            data=data,
            future=future,
        )

        # Add to queue (non-blocking, raises QueueFull if full)
        self._queue.put_nowait(request)
        logger.debug(f"Request {request_id} queued for batching")

        # Wait for result
        return await future

    async def _process_batches(self):
        """Main batch processing loop."""
        while self._running:
            try:
                # Accumulate batch
                batch = await self._accumulate_batch()

                if not batch:
                    # No requests in timeout period, continue
                    continue

                # Process batch
                await self._process_batch(batch)

            except asyncio.CancelledError:
                # Drain remaining requests
                await self._drain_queue()
                raise
            except Exception as e:
                logger.error(f"Error in batch processor: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    async def _accumulate_batch(self) -> list[BatchRequest]:
        """Accumulate requests into a batch.

        Waits for either:
        - batch_size requests
        - batch_timeout_ms milliseconds

        Returns:
            List of accumulated requests
        """
        batch: list[BatchRequest] = []
        timeout_sec = self.batch_timeout_ms / 1000.0
        deadline = time.time() + timeout_sec

        while len(batch) < self.batch_size:
            remaining = max(0, deadline - time.time())

            if remaining <= 0:
                # Timeout expired
                break

            try:
                # Wait for next request with timeout
                request = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                batch.append(request)
            except TimeoutError:
                # Timeout expired
                break

        return batch

    async def _process_batch(self, batch: list[BatchRequest]):
        """Process a batch of requests.

        Args:
            batch: List of requests to process
        """
        if not batch:
            return

        self._batch_counter += 1
        batch_id = f"batch_{self._batch_counter}"
        batch_start = time.time()

        # Calculate wait times
        wait_times = [(batch_start - req.enqueue_time) * 1000 for req in batch]
        avg_wait_time = sum(wait_times) / len(wait_times)

        logger.debug(
            f"Processing {batch_id}: {len(batch)} requests, "
            f"avg_wait={avg_wait_time:.2f}ms"
        )

        try:
            # Extract data
            batch_data = [req.data for req in batch]

            # Process batch
            process_start = time.time()
            if self.processor:
                results = await self.processor(batch_data)
            else:
                results = batch_data  # No-op processor
            process_time = (time.time() - process_start) * 1000

            # Set results
            for req, result in zip(batch, results, strict=True):
                if not req.future.done():
                    req.future.set_result(result)

            # Record metrics
            total_time = (time.time() - batch_start) * 1000
            metrics = BatchMetrics(
                batch_id=batch_id,
                batch_size=len(batch),
                wait_time_ms=avg_wait_time,
                process_time_ms=process_time,
                total_time_ms=total_time,
            )
            await self._record_metrics(metrics)

            logger.info(
                f"Processed {batch_id}: {len(batch)} requests in {process_time:.2f}ms"
            )

        except Exception as e:
            logger.error(f"Error processing {batch_id}: {e}", exc_info=True)
            # Set exception on all futures
            for req in batch:
                if not req.future.done():
                    req.future.set_exception(e)

    async def _drain_queue(self):
        """Drain remaining requests from queue on shutdown."""
        drained = []
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
                drained.append(req)
            except asyncio.QueueEmpty:
                break

        if drained:
            logger.warning(f"Draining {len(drained)} requests from queue")
            for req in drained:
                if not req.future.done():
                    req.future.set_exception(
                        RuntimeError("Request batcher stopped before processing")
                    )

    async def _record_metrics(self, metrics: BatchMetrics):
        """Record batch metrics.

        Args:
            metrics: Metrics to record
        """
        async with self._metrics_lock:
            self._metrics.append(metrics)
            # Keep last 1000 metrics
            if len(self._metrics) > 1000:
                self._metrics = self._metrics[-1000:]

    async def get_metrics(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent batch metrics.

        Args:
            limit: Maximum number of metrics to return

        Returns:
            List of metric dictionaries
        """
        async with self._metrics_lock:
            recent = self._metrics[-limit:]
            return [m.to_dict() for m in recent]

    async def get_stats(self) -> dict[str, Any]:
        """Get aggregate statistics.

        Returns:
            Dictionary of statistics
        """
        async with self._metrics_lock:
            if not self._metrics:
                return {
                    "total_batches": 0,
                    "total_requests": 0,
                    "avg_batch_size": 0,
                    "avg_wait_time_ms": 0,
                    "avg_process_time_ms": 0,
                    "avg_total_time_ms": 0,
                }

            total_batches = len(self._metrics)
            total_requests = sum(m.batch_size for m in self._metrics)
            avg_batch_size = total_requests / total_batches
            avg_wait = sum(m.wait_time_ms for m in self._metrics) / total_batches
            avg_process = sum(m.process_time_ms for m in self._metrics) / total_batches
            avg_total = sum(m.total_time_ms for m in self._metrics) / total_batches

            return {
                "total_batches": total_batches,
                "total_requests": total_requests,
                "avg_batch_size": round(avg_batch_size, 2),
                "avg_wait_time_ms": round(avg_wait, 2),
                "avg_process_time_ms": round(avg_process, 2),
                "avg_total_time_ms": round(avg_total, 2),
                "queue_size": self._queue.qsize(),
                "max_queue_size": self.max_queue_size,
            }


class BatcherManager:
    """Manager for multiple request batchers."""

    def __init__(self):
        """Initialize batcher manager."""
        self._batchers: dict[str, RequestBatcher] = {}
        self._lock = asyncio.Lock()

    async def get_batcher(
        self,
        name: str,
        batch_size: int = 5,
        batch_timeout_ms: int = 100,
        max_queue_size: int = 100,
        processor: Callable[[list[Any]], Coroutine[Any, Any, list[Any]]] | None = None,
    ) -> RequestBatcher:
        """Get or create a batcher.

        Args:
            name: Batcher name
            batch_size: Maximum requests per batch
            batch_timeout_ms: Maximum wait time in milliseconds
            max_queue_size: Maximum queue size
            processor: Async batch processor function

        Returns:
            RequestBatcher instance
        """
        async with self._lock:
            if name not in self._batchers:
                batcher = RequestBatcher(
                    batch_size=batch_size,
                    batch_timeout_ms=batch_timeout_ms,
                    max_queue_size=max_queue_size,
                    processor=processor,
                )
                await batcher.start()
                self._batchers[name] = batcher
                logger.info(f"Created batcher: {name}")

            return self._batchers[name]

    async def stop_all(self):
        """Stop all batchers."""
        async with self._lock:
            for name, batcher in self._batchers.items():
                await batcher.stop()
                logger.info(f"Stopped batcher: {name}")
            self._batchers.clear()

    async def get_all_stats(self) -> dict[str, dict[str, Any]]:
        """Get statistics for all batchers.

        Returns:
            Dictionary mapping batcher names to their stats
        """
        async with self._lock:
            stats = {}
            for name, batcher in self._batchers.items():
                stats[name] = await batcher.get_stats()
            return stats
