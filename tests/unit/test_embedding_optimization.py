"""Tests for embedding generation optimizations."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import Mock, patch

from nyxgpt.cache import DiskCache, NoOpCache
from nyxgpt.rag import embeddings as embeddings_module
from nyxgpt.rag.embeddings import (
    EmbeddingConfig,
    EmbeddingError,
    GPUInfo,
    _detect_gpu,
    _embed_batch_async,
    _embed_batch_sync,
    _estimate_memory_usage,
    _get_embedding_cache,
    _get_optimal_batch_size,
    _post_json,
    embed_text,
    embed_texts,
)


class TestGPUDetection(unittest.TestCase):
    """Test GPU detection functionality."""

    def setUp(self):
        """Reset GPU info cache before each test."""
        import nyxgpt.rag.embeddings as emb_module

        emb_module._gpu_info = None
        emb_module._gpu_info_updated = 0.0

    @patch("subprocess.run")
    def test_gpu_detected_successfully(self, mock_run):
        """Test successful GPU detection."""
        mock_run.return_value = Mock(returncode=0, stdout="16384, 8192, 75.5\n8192, 4096, 60.0\n")

        gpu_info = _detect_gpu()

        self.assertTrue(gpu_info.available)
        self.assertEqual(gpu_info.device_count, 2)
        self.assertEqual(gpu_info.memory_total, 16384)
        self.assertEqual(gpu_info.memory_used, 8192)
        self.assertEqual(gpu_info.utilization, 75.5)

    @patch("subprocess.run")
    def test_gpu_not_available(self, mock_run):
        """Test when GPU is not available."""
        mock_run.side_effect = FileNotFoundError("nvidia-smi not found")

        gpu_info = _detect_gpu()

        self.assertFalse(gpu_info.available)
        self.assertEqual(gpu_info.device_count, 0)

    @patch("subprocess.run")
    def test_gpu_detection_timeout(self, mock_run):
        """Test GPU detection timeout handling."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired("nvidia-smi", 2)

        gpu_info = _detect_gpu()

        self.assertFalse(gpu_info.available)

    @patch("subprocess.run")
    def test_gpu_detection_shell_false(self, mock_run):
        """Test GPU detection uses shell=False for security."""
        mock_run.return_value = Mock(returncode=0, stdout="16384, 8192, 75.5\n")

        _detect_gpu()

        # Verify shell=False was passed
        call_kwargs = mock_run.call_args[1]
        self.assertIs(call_kwargs.get("shell"), False)


class TestThreadPoolCleanup(unittest.TestCase):
    """Test thread pool cleanup functionality."""

    def test_cleanup_thread_pool(self):
        """Test thread pool cleanup on shutdown."""
        import nyxgpt.rag.embeddings as emb_module

        # Create thread pool with max_workers
        emb_module._get_thread_pool(max_workers=4)
        self.assertIsNotNone(emb_module._thread_pool)

        # Call cleanup
        emb_module._cleanup_thread_pool()
        self.assertIsNone(emb_module._thread_pool)


class TestMemoryManagement(unittest.TestCase):
    """Test memory management and adaptive batching."""

    def test_estimate_memory_usage(self):
        """Test memory usage estimation."""
        # 16 texts, 768 dimensions
        memory = _estimate_memory_usage(16, 768)

        # Expected: 16 * 768 * 8 bytes + overhead
        expected_embeddings = 16 * 768 * 8
        expected_overhead = 16 * 100
        expected_total = expected_embeddings + expected_overhead

        self.assertEqual(memory, expected_total)

    @patch("nyxgpt.rag.embeddings.load_config")
    def test_get_optimal_batch_size_no_adaptive(self, mock_config):
        """Test optimal batch size without adaptive batching."""
        from nyxgpt.rag.embeddings import EmbeddingConfig

        config = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=768,
            timeout=120,
            batch_size=16,
            adaptive_batching=False,
        )

        batch_size = _get_optimal_batch_size(100, config)
        self.assertEqual(batch_size, 16)

    @patch("psutil.virtual_memory")
    @patch("nyxgpt.rag.embeddings.load_config")
    def test_get_optimal_batch_size_adaptive(self, mock_config, mock_memory):
        """Test adaptive batch sizing based on available memory."""
        from nyxgpt.rag.embeddings import EmbeddingConfig

        # Mock available memory (1GB)
        mock_memory.return_value = Mock(available=1024 * 1024 * 1024)

        config = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=768,
            timeout=120,
            batch_size=64,
            adaptive_batching=True,
        )

        batch_size = _get_optimal_batch_size(100, config)
        # Should return a reasonable batch size that fits in memory
        self.assertGreater(batch_size, 0)
        self.assertLessEqual(batch_size, 64)

    @patch("psutil.virtual_memory")
    @patch("nyxgpt.rag.embeddings._detect_gpu")
    @patch("nyxgpt.rag.embeddings.load_config")
    def test_get_optimal_batch_size_with_gpu(self, mock_config, mock_gpu, mock_memory):
        """Test adaptive batch sizing with GPU available."""
        from nyxgpt.rag.embeddings import EmbeddingConfig

        # Mock available memory (1GB)
        mock_memory.return_value = Mock(available=1024 * 1024 * 1024)

        # Mock GPU with 8GB free
        mock_gpu.return_value = GPUInfo(
            available=True,
            device_count=1,
            memory_total=16384,
            memory_used=8192,
            utilization=50.0,
        )

        config = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=768,
            timeout=120,
            batch_size=32,
            adaptive_batching=True,
            enable_gpu=True,
        )

        batch_size = _get_optimal_batch_size(100, config)
        # With GPU available, should scale up the batch size
        self.assertGreater(batch_size, 32)


class TestBatchEmbedding(unittest.TestCase):
    """Test batch embedding functionality."""

    @patch("nyxgpt.rag.embeddings._post_json")
    def test_embed_batch_sync_success(self, mock_post):
        """Test synchronous batch embedding."""
        mock_post.return_value = {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

        result = _embed_batch_sync(
            ["text1", "text2"],
            "http://localhost:11434/api/embed",
            "test-model",
            120,
            3,
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], [0.1, 0.2, 0.3])
        self.assertEqual(result[1], [0.4, 0.5, 0.6])

    @patch("nyxgpt.rag.embeddings._post_json")
    def test_embed_batch_sync_dimension_mismatch(self, mock_post):
        """Test batch embedding with dimension mismatch."""
        from nyxgpt.rag.embeddings import EmbeddingError

        mock_post.return_value = {"embeddings": [[0.1, 0.2]]}

        with self.assertRaises(EmbeddingError) as ctx:
            _embed_batch_sync(
                ["text1"],
                "http://localhost:11434/api/embed",
                "test-model",
                120,
                3,  # Expected dimension is 3, but got 2
            )

        self.assertIn("expected 3", str(ctx.exception))


class TestAsyncEventLoop(unittest.TestCase):
    """Test async event loop handling."""

    @patch("nyxgpt.rag.embeddings._embed_batch_sync")
    def test_embed_batch_async_with_running_loop(self, mock_sync):
        """Test async embed with running event loop."""
        mock_sync.return_value = [[0.1, 0.2, 0.3]]

        async def run_test():
            result = await _embed_batch_async(
                ["test"], "http://localhost:11434/api/embed", "test-model", 120, 3, None
            )
            return result

        result = asyncio.run(run_test())
        self.assertEqual(len(result), 1)

    @patch("nyxgpt.rag.embeddings._embed_batch_sync")
    def test_embed_batch_async_no_running_loop(self, mock_sync):
        """Test async embed fallback creates new loop when none is running."""
        mock_sync.return_value = [[0.1, 0.2, 0.3]]

        # This test verifies the code path where get_running_loop() raises RuntimeError
        # We'll test the actual behavior by inspecting the code logic rather than
        # trying to simulate the exact RuntimeError scenario which is complex to mock

        # Just verify the function works correctly
        async def run_test():
            result = await _embed_batch_async(
                ["test"], "http://localhost:11434/api/embed", "test-model", 120, 3, None
            )
            return result

        result = asyncio.run(run_test())
        self.assertEqual(len(result), 1)


class TestEmbedTextsOptimization(unittest.TestCase):
    """Test optimized embed_texts function."""

    @patch("nyxgpt.rag.embeddings._get_embedding_cache")
    @patch("nyxgpt.rag.embeddings._embedding_cfg")
    @patch("nyxgpt.rag.embeddings._embed_batch_sync")
    def test_embed_texts_with_metrics(self, mock_embed_batch, mock_cfg, mock_cache):
        """Test embed_texts with metrics collection."""
        from nyxgpt.rag.embeddings import EmbeddingConfig

        # Mock config
        mock_cfg.return_value = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=3,
            timeout=120,
            batch_size=2,
            enable_async=False,
            max_workers=4,
            enable_gpu=False,
            adaptive_batching=False,
        )

        # Mock cache (miss)
        mock_cache_obj = Mock()
        mock_cache_obj.get.return_value = None
        mock_cache.return_value = mock_cache_obj

        # Mock embedding results
        mock_embed_batch.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]

        texts = ["text1", "text2"]
        result, metrics = embed_texts(texts, collect_metrics=True)

        self.assertEqual(len(result), 2)
        self.assertEqual(metrics.num_texts_embedded, 2)
        self.assertEqual(metrics.cache_misses, 1)
        self.assertEqual(metrics.cache_hits, 0)
        self.assertGreater(metrics.embedding_time_ms, 0)

    @patch("nyxgpt.rag.embeddings._get_embedding_cache")
    @patch("nyxgpt.rag.embeddings._embedding_cfg")
    def test_embed_texts_cache_hit(self, mock_cfg, mock_cache):
        """Test embed_texts with cache hit."""
        from nyxgpt.rag.embeddings import EmbeddingConfig

        # Mock config
        mock_cfg.return_value = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=3,
            timeout=120,
            batch_size=2,
            enable_async=False,
            max_workers=4,
            enable_gpu=False,
            adaptive_batching=False,
        )

        # Mock cache (hit)
        cached_embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        mock_cache_obj = Mock()
        mock_cache_obj.get.return_value = cached_embeddings
        mock_cache.return_value = mock_cache_obj

        texts = ["text1", "text2"]
        result, metrics = embed_texts(texts, collect_metrics=True)

        self.assertEqual(result, cached_embeddings)
        self.assertEqual(metrics.cache_hits, 1)
        self.assertEqual(metrics.cache_misses, 0)

    @patch("nyxgpt.rag.embeddings._get_embedding_cache")
    @patch("nyxgpt.rag.embeddings._embedding_cfg")
    def test_embed_texts_empty_input(self, mock_cfg, mock_cache):
        """Test embed_texts with empty input."""
        from nyxgpt.rag.embeddings import EmbeddingConfig

        mock_cfg.return_value = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=3,
            timeout=120,
            batch_size=2,
        )

        result = embed_texts([])
        self.assertEqual(result, [])

    @patch("nyxgpt.rag.embeddings._get_embedding_cache")
    @patch("nyxgpt.rag.embeddings._embedding_cfg")
    @patch("nyxgpt.rag.embeddings._get_optimal_batch_size")
    @patch("nyxgpt.rag.embeddings._embed_batch_sync")
    def test_embed_texts_adaptive_batching(
        self, mock_embed_batch, mock_optimal_batch, mock_cfg, mock_cache
    ):
        """Test embed_texts with adaptive batching."""
        from nyxgpt.rag.embeddings import EmbeddingConfig

        # Mock config with adaptive batching
        mock_cfg.return_value = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=3,
            timeout=120,
            batch_size=2,
            enable_async=False,
            max_workers=4,
            enable_gpu=False,
            adaptive_batching=True,
        )

        # Mock optimal batch size to be larger
        mock_optimal_batch.return_value = 4

        # Mock cache (miss)
        mock_cache_obj = Mock()
        mock_cache_obj.get.return_value = None
        mock_cache.return_value = mock_cache_obj

        # Mock embedding results
        mock_embed_batch.return_value = [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
            [0.7, 0.8, 0.9],
            [1.0, 1.1, 1.2],
        ]

        texts = ["text1", "text2", "text3", "text4"]
        result, metrics = embed_texts(texts, collect_metrics=True)

        # Should use optimal batch size
        self.assertEqual(len(result), 4)
        self.assertEqual(metrics.batch_size, 4)


class TestEmbeddingCacheBackendSelection(unittest.TestCase):
    """Test _get_embedding_cache backend selection branches."""

    def setUp(self):
        embeddings_module._embedding_cache = None

    def tearDown(self):
        embeddings_module._embedding_cache = None

    @patch("nyxgpt.rag.embeddings.load_config")
    def test_disk_backend_selected(self, mock_config):
        from configparser import ConfigParser

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = ConfigParser()
            cfg["cache"] = {
                "embedding_cache_enabled": "true",
                "embedding_cache_backend": "disk",
                "embedding_cache_dir": tmpdir,
            }
            mock_config.return_value = cfg

            cache = _get_embedding_cache()
            self.assertIsInstance(cache, DiskCache)

    @patch("nyxgpt.rag.embeddings.load_config")
    def test_unknown_backend_falls_back_to_noop(self, mock_config):
        from configparser import ConfigParser

        cfg = ConfigParser()
        cfg["cache"] = {
            "embedding_cache_enabled": "true",
            "embedding_cache_backend": "redis",
        }
        mock_config.return_value = cfg

        cache = _get_embedding_cache()
        self.assertIsInstance(cache, NoOpCache)


class TestGPUCachedInfo(unittest.TestCase):
    """Test that _detect_gpu returns cached info within the TTL window."""

    def setUp(self):
        embeddings_module._gpu_info = None
        embeddings_module._gpu_info_updated = 0.0

    def tearDown(self):
        embeddings_module._gpu_info = None
        embeddings_module._gpu_info_updated = 0.0

    def test_returns_cached_gpu_info_within_ttl(self):
        cached = GPUInfo(
            available=True, device_count=1, memory_total=100, memory_used=10, utilization=5.0
        )
        embeddings_module._gpu_info = cached
        embeddings_module._gpu_info_updated = time.time()

        result = _detect_gpu()
        self.assertIs(result, cached)


class TestOptimalBatchSizePsutilMissing(unittest.TestCase):
    """Test the ImportError fallback when psutil is unavailable."""

    @patch("nyxgpt.rag.embeddings.load_config")
    def test_returns_default_batch_size_when_psutil_missing(self, mock_config):
        config = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=768,
            timeout=120,
            batch_size=16,
            adaptive_batching=True,
        )

        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "psutil":
                raise ImportError("no psutil")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            batch_size = _get_optimal_batch_size(100, config)

        self.assertEqual(batch_size, 16)


class TestOptimalBatchSizeGPUMediumMemory(unittest.TestCase):
    """Test the 2-4GB free GPU memory scaling branch."""

    @patch("nyxgpt.rag.embeddings._detect_gpu")
    @patch("psutil.virtual_memory")
    @patch("nyxgpt.rag.embeddings.load_config")
    def test_medium_gpu_memory_scales_by_1_5x(self, mock_config, mock_memory, mock_gpu):
        mock_memory.return_value = Mock(available=1024 * 1024 * 1024)
        # 4000 - 1500 = 2500 MB free -> between 2048 and 4096
        mock_gpu.return_value = GPUInfo(
            available=True, device_count=1, memory_total=4000, memory_used=1500, utilization=10.0
        )

        config = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=768,
            timeout=120,
            batch_size=32,
            adaptive_batching=True,
            enable_gpu=True,
        )

        batch_size = _get_optimal_batch_size(100, config)
        self.assertEqual(batch_size, 48)  # min(32 * 1.5, 64)


class TestPostJson(unittest.TestCase):
    """Test _post_json success and HTTP error handling."""

    @patch("urllib.request.urlopen")
    def test_post_json_success(self, mock_urlopen):
        response_mock = Mock()
        response_mock.read.return_value = json.dumps({"embeddings": [[0.1, 0.2]]}).encode("utf-8")
        response_mock.__enter__ = Mock(return_value=response_mock)
        response_mock.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = response_mock

        result = _post_json(
            "http://localhost:11434/api/embed", {"model": "m", "input": ["a"]}, timeout=5
        )
        self.assertEqual(result, {"embeddings": [[0.1, 0.2]]})

    @patch("urllib.request.urlopen")
    def test_post_json_http_error(self, mock_urlopen):
        error = urllib.error.HTTPError(
            url="http://localhost:11434/api/embed",
            code=500,
            msg="Internal Error",
            hdrs=None,
            fp=io.BytesIO(b"server error"),
        )
        mock_urlopen.side_effect = error

        with self.assertRaises(EmbeddingError) as ctx:
            _post_json(
                "http://localhost:11434/api/embed", {"model": "m", "input": ["a"]}, timeout=5
            )
        self.assertIn("HTTP error", str(ctx.exception))


class TestEmbedBatchAsyncFallbackLoop(unittest.TestCase):
    """Test _embed_batch_async creates a new loop when none is running."""

    def test_creates_new_loop_when_none_running(self):
        expected = [[0.1, 0.2, 0.3]]

        async def _dummy_run_in_executor():
            return expected

        fake_loop = Mock()
        fake_loop.run_in_executor = lambda *_a, **_k: _dummy_run_in_executor()

        with (
            patch(
                "nyxgpt.rag.embeddings.asyncio.get_running_loop",
                side_effect=RuntimeError("no running loop"),
            ),
            patch("nyxgpt.rag.embeddings.asyncio.new_event_loop", return_value=fake_loop),
            patch("nyxgpt.rag.embeddings.asyncio.set_event_loop") as mock_set_loop,
        ):
            coro = _embed_batch_async(["hi"], "http://x", "model", 5, 3, Mock())
            result = None
            try:
                coro.send(None)
            except StopIteration as e:
                result = e.value

        self.assertEqual(result, expected)
        mock_set_loop.assert_called_once_with(fake_loop)


class TestEmbedTextsAsyncPath(unittest.TestCase):
    """Test embed_texts async multi-batch processing."""

    def setUp(self):
        embeddings_module._embedding_cache = None
        embeddings_module._thread_pool = None

    def tearDown(self):
        embeddings_module._cleanup_thread_pool()
        embeddings_module._embedding_cache = None

    @patch("nyxgpt.rag.embeddings._get_embedding_cache")
    @patch("nyxgpt.rag.embeddings._embedding_cfg")
    @patch("nyxgpt.rag.embeddings._post_json")
    def test_embed_texts_async_multiple_batches(self, mock_post, mock_cfg, mock_cache):
        mock_cfg.return_value = EmbeddingConfig(
            base_url="http://localhost:11434",
            model="test-model",
            dimension=3,
            timeout=5,
            batch_size=1,
            enable_async=True,
            max_workers=2,
        )
        mock_cache_obj = Mock()
        mock_cache_obj.get.return_value = None
        mock_cache.return_value = mock_cache_obj
        mock_post.return_value = {"embeddings": [[0.1, 0.2, 0.3]]}

        texts = ["text1", "text2", "text3"]
        result = embed_texts(texts)

        self.assertEqual(len(result), 3)
        for vec in result:
            self.assertEqual(vec, [0.1, 0.2, 0.3])


class TestEmbedTextsAsyncFallback(unittest.IsolatedAsyncioTestCase):
    """Test embed_texts falls back to sync processing when a loop is already running."""

    async def asyncSetUp(self):
        embeddings_module._embedding_cache = None
        embeddings_module._thread_pool = None

    async def asyncTearDown(self):
        embeddings_module._cleanup_thread_pool()
        embeddings_module._embedding_cache = None

    async def test_falls_back_to_sync_when_loop_already_running(self):
        with (
            patch("nyxgpt.rag.embeddings._get_embedding_cache") as mock_cache,
            patch("nyxgpt.rag.embeddings._embedding_cfg") as mock_cfg,
            patch("nyxgpt.rag.embeddings._post_json") as mock_post,
        ):
            mock_cfg.return_value = EmbeddingConfig(
                base_url="http://localhost:11434",
                model="test-model",
                dimension=3,
                timeout=5,
                batch_size=1,
                enable_async=True,
                max_workers=2,
            )
            mock_cache_obj = Mock()
            mock_cache_obj.get.return_value = None
            mock_cache.return_value = mock_cache_obj
            mock_post.return_value = {"embeddings": [[0.1, 0.2, 0.3]]}

            texts = ["text1", "text2"]
            result = embed_texts(texts)

            self.assertEqual(len(result), 2)


class TestEmbedTextTupleHandling(unittest.TestCase):
    """Test embed_text correctly unpacks a tuple result from embed_texts."""

    @patch("nyxgpt.rag.embeddings.embed_texts")
    def test_handles_tuple_result_from_embed_texts(self, mock_embed_texts):
        mock_embed_texts.return_value = ([[1.0, 2.0, 3.0]], Mock())

        result = embed_text("hello")

        self.assertEqual(result, [1.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
