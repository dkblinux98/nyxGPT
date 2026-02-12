"""Tests for request batching functionality."""

import asyncio

import pytest

from nyxgpt.request_batcher import (
    BatcherManager,
    BatchMetrics,
    BatchRequest,
    RequestBatcher,
    RequestPriority,
)


class TestBatchMetrics:
    """Test BatchMetrics dataclass."""

    def test_to_dict(self):
        """Test converting metrics to dictionary."""
        metrics = BatchMetrics(
            batch_id="batch_1",
            batch_size=5,
            wait_time_ms=50.123456,
            process_time_ms=100.987654,
            total_time_ms=151.111110,
            timestamp=1234567890.0,
        )

        result = metrics.to_dict()

        assert result["batch_id"] == "batch_1"
        assert result["batch_size"] == 5
        assert result["wait_time_ms"] == 50.12
        assert result["process_time_ms"] == 100.99
        assert result["total_time_ms"] == 151.11
        assert result["timestamp"] == 1234567890.0


class TestBatchRequest:
    """Test BatchRequest dataclass."""

    def test_creation(self):
        """Test creating a batch request."""
        future = asyncio.Future()
        request = BatchRequest(
            request_id="req_1",
            priority=RequestPriority.BATCH,
            data={"test": "data"},
            future=future,
        )

        assert request.request_id == "req_1"
        assert request.priority == RequestPriority.BATCH
        assert request.data == {"test": "data"}
        assert request.future == future
        assert request.enqueue_time > 0


class TestRequestBatcher:
    """Test RequestBatcher class."""

    @pytest.fixture
    async def processor(self):
        """Mock async processor."""

        async def mock_processor(batch):
            # Simulate processing by returning doubled values
            await asyncio.sleep(0.01)
            return [f"processed_{item}" for item in batch]

        return mock_processor

    @pytest.fixture
    async def batcher(self, processor):
        """Create a batcher instance."""
        batcher = RequestBatcher(
            batch_size=3,
            batch_timeout_ms=100,
            max_queue_size=10,
            processor=processor,
        )
        await batcher.start()
        yield batcher
        await batcher.stop()

    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test batcher initialization."""
        batcher = RequestBatcher(
            batch_size=5,
            batch_timeout_ms=200,
            max_queue_size=50,
        )

        assert batcher.batch_size == 5
        assert batcher.batch_timeout_ms == 200
        assert batcher.max_queue_size == 50
        assert not batcher._running

    @pytest.mark.asyncio
    async def test_start_stop(self, processor):
        """Test starting and stopping batcher."""
        batcher = RequestBatcher(processor=processor)

        await batcher.start()
        assert batcher._running
        assert batcher._task is not None

        await batcher.stop()
        assert not batcher._running

    @pytest.mark.asyncio
    async def test_submit_batch_request(self, batcher):
        """Test submitting a batch request."""
        result = await batcher.submit("req_1", "test_data", RequestPriority.BATCH)

        assert result == "processed_test_data"

    @pytest.mark.asyncio
    async def test_submit_interactive_request(self):
        """Test interactive requests bypass batching."""

        async def processor(batch):
            return [f"processed_{item}" for item in batch]

        batcher = RequestBatcher(processor=processor)
        await batcher.start()

        try:
            result = await batcher.submit("req_1", "test_data", RequestPriority.INTERACTIVE)
            assert result == "processed_test_data"
        finally:
            await batcher.stop()

    @pytest.mark.asyncio
    async def test_batch_accumulation_by_size(self, processor):
        """Test batching triggers when batch_size is reached."""
        batcher = RequestBatcher(
            batch_size=3,
            batch_timeout_ms=5000,  # Long timeout so size triggers first
            processor=processor,
        )
        await batcher.start()

        try:
            # Submit 3 requests concurrently
            tasks = [
                batcher.submit(f"req_{i}", f"data_{i}", RequestPriority.BATCH) for i in range(3)
            ]

            results = await asyncio.gather(*tasks)

            assert len(results) == 3
            assert results[0] == "processed_data_0"
            assert results[1] == "processed_data_1"
            assert results[2] == "processed_data_2"

            # Check metrics
            stats = await batcher.get_stats()
            assert stats["total_batches"] == 1
            assert stats["total_requests"] == 3
        finally:
            await batcher.stop()

    @pytest.mark.asyncio
    async def test_batch_accumulation_by_timeout(self, processor):
        """Test batching triggers when timeout expires."""
        batcher = RequestBatcher(
            batch_size=10,  # Large size so timeout triggers first
            batch_timeout_ms=50,
            processor=processor,
        )
        await batcher.start()

        try:
            # Submit 2 requests (less than batch_size)
            tasks = [
                batcher.submit(f"req_{i}", f"data_{i}", RequestPriority.BATCH) for i in range(2)
            ]

            results = await asyncio.gather(*tasks)

            assert len(results) == 2
            assert results[0] == "processed_data_0"
            assert results[1] == "processed_data_1"

            # Check metrics
            stats = await batcher.get_stats()
            assert stats["total_batches"] == 1
            assert stats["total_requests"] == 2
        finally:
            await batcher.stop()

    @pytest.mark.asyncio
    async def test_multiple_batches(self, processor):
        """Test processing multiple batches."""
        batcher = RequestBatcher(
            batch_size=2,
            batch_timeout_ms=100,
            processor=processor,
        )
        await batcher.start()

        try:
            # Submit 5 requests (should create 3 batches: 2, 2, 1)
            tasks = [
                batcher.submit(f"req_{i}", f"data_{i}", RequestPriority.BATCH) for i in range(5)
            ]

            results = await asyncio.gather(*tasks)

            assert len(results) == 5
            for i, result in enumerate(results):
                assert result == f"processed_data_{i}"

            # Give time for final batch to process
            await asyncio.sleep(0.2)

            # Check metrics
            stats = await batcher.get_stats()
            assert stats["total_batches"] == 3
            assert stats["total_requests"] == 5
        finally:
            await batcher.stop()

    @pytest.mark.asyncio
    async def test_queue_size_limit(self):
        """Test queue has a size limit."""
        batcher = RequestBatcher(max_queue_size=10)

        assert batcher.max_queue_size == 10
        assert batcher._queue.maxsize == 10

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """Test error handling in batch processing."""

        async def failing_processor(batch):
            raise ValueError("Processing failed")

        batcher = RequestBatcher(
            batch_size=2,
            batch_timeout_ms=100,
            processor=failing_processor,
        )
        await batcher.start()

        try:
            # Submit requests
            with pytest.raises(ValueError, match="Processing failed"):
                await batcher.submit("req_1", "data_1", RequestPriority.BATCH)
        finally:
            await batcher.stop()

    @pytest.mark.asyncio
    async def test_get_metrics(self, batcher):
        """Test retrieving batch metrics."""
        # Submit and process a batch
        tasks = [batcher.submit(f"req_{i}", f"data_{i}", RequestPriority.BATCH) for i in range(3)]
        await asyncio.gather(*tasks)

        metrics = await batcher.get_metrics(limit=10)

        assert len(metrics) >= 1
        assert "batch_id" in metrics[0]
        assert "batch_size" in metrics[0]
        assert "wait_time_ms" in metrics[0]
        assert "process_time_ms" in metrics[0]

    @pytest.mark.asyncio
    async def test_get_stats(self, batcher):
        """Test retrieving aggregate statistics."""
        # Submit and process batches
        tasks = [batcher.submit(f"req_{i}", f"data_{i}", RequestPriority.BATCH) for i in range(5)]
        await asyncio.gather(*tasks)

        # Give time for batches to process
        await asyncio.sleep(0.3)

        stats = await batcher.get_stats()

        assert "total_batches" in stats
        assert "total_requests" in stats
        assert "avg_batch_size" in stats
        assert "avg_wait_time_ms" in stats
        assert "avg_process_time_ms" in stats
        assert "queue_size" in stats
        assert stats["total_requests"] == 5


class TestBatcherManager:
    """Test BatcherManager class."""

    @pytest.fixture
    async def manager(self):
        """Create a manager instance."""
        manager = BatcherManager()
        yield manager
        await manager.stop_all()

    @pytest.mark.asyncio
    async def test_get_batcher(self, manager):
        """Test getting or creating a batcher."""

        async def processor(batch):
            return batch

        batcher = await manager.get_batcher(
            name="test_batcher",
            batch_size=5,
            batch_timeout_ms=100,
            processor=processor,
        )

        assert isinstance(batcher, RequestBatcher)
        assert batcher.batch_size == 5

        # Getting same batcher returns existing instance
        batcher2 = await manager.get_batcher(name="test_batcher")
        assert batcher2 is batcher

    @pytest.mark.asyncio
    async def test_multiple_batchers(self, manager):
        """Test managing multiple batchers."""

        async def processor(batch):
            return batch

        batcher1 = await manager.get_batcher("batcher1", processor=processor)
        batcher2 = await manager.get_batcher("batcher2", processor=processor)

        assert batcher1 is not batcher2

    @pytest.mark.asyncio
    async def test_stop_all(self, manager):
        """Test stopping all batchers."""

        async def processor(batch):
            return batch

        await manager.get_batcher("batcher1", processor=processor)
        await manager.get_batcher("batcher2", processor=processor)

        await manager.stop_all()

        # Batchers should be cleared
        assert len(manager._batchers) == 0

    @pytest.mark.asyncio
    async def test_get_all_stats(self, manager):
        """Test getting stats for all batchers."""

        async def processor(batch):
            return [f"processed_{item}" for item in batch]

        batcher1 = await manager.get_batcher("batcher1", batch_size=2, processor=processor)
        batcher2 = await manager.get_batcher("batcher2", batch_size=2, processor=processor)

        # Submit requests to both batchers
        await batcher1.submit("req_1", "data_1", RequestPriority.BATCH)
        await batcher2.submit("req_2", "data_2", RequestPriority.BATCH)

        # Give time for processing
        await asyncio.sleep(0.2)

        stats = await manager.get_all_stats()

        assert "batcher1" in stats
        assert "batcher2" in stats
        assert stats["batcher1"]["total_requests"] >= 1
        assert stats["batcher2"]["total_requests"] >= 1
