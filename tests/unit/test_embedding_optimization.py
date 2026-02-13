"""Tests for embedding generation optimizations."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from nyxgpt.rag.embeddings import (
    GPUInfo,
    _detect_gpu,
    _embed_batch_sync,
    _estimate_memory_usage,
    _get_optimal_batch_size,
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


if __name__ == "__main__":
    unittest.main()
