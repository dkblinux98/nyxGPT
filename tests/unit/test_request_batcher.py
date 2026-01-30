"""Unit tests for request batching configuration."""

import pytest
from unittest.mock import patch, MagicMock
from nyxgpt.request_batcher import BatchConfig, get_batch_config, RequestBatcher


class TestBatchConfig:
    """Test batch configuration loading."""

    @patch("nyxgpt.request_batcher.load_config")
    def test_get_batch_config_default(self, mock_config):
        """Should use default values when config options not present."""
        mock_cfg = MagicMock()
        mock_cfg.getboolean.return_value = False
        mock_cfg.getint.side_effect = lambda section, key, fallback: fallback
        mock_config.return_value = mock_cfg

        config = get_batch_config()

        assert config.enabled is False
        assert config.max_batch_size == 10
        assert config.max_wait_ms == 50
        assert config.priority_threshold_ms == 100

    @patch("nyxgpt.request_batcher.load_config")
    def test_get_batch_config_custom(self, mock_config):
        """Should load custom config values."""
        mock_cfg = MagicMock()
        mock_cfg.getboolean.return_value = True
        mock_cfg.getint.side_effect = lambda section, key, fallback: {
            "max_batch_size": 20,
            "max_wait_ms": 100,
            "priority_threshold_ms": 200,
        }.get(key, fallback)
        mock_config.return_value = mock_cfg

        config = get_batch_config()

        assert config.enabled is True
        assert config.max_batch_size == 20
        assert config.max_wait_ms == 100
        assert config.priority_threshold_ms == 200


class TestRequestBatcher:
    """Test RequestBatcher functionality."""

    def test_batcher_disabled_by_default(self):
        """Batching should be disabled by default."""
        config = BatchConfig(
            enabled=False,
            max_batch_size=10,
            max_wait_ms=50,
            priority_threshold_ms=100,
        )
        batcher = RequestBatcher(config=config)

        assert not batcher.should_batch()

    def test_batcher_enabled(self):
        """Batching can be enabled via config."""
        config = BatchConfig(
            enabled=True,
            max_batch_size=10,
            max_wait_ms=50,
            priority_threshold_ms=100,
        )
        batcher = RequestBatcher(config=config)

        assert batcher.should_batch()

    @pytest.mark.asyncio
    async def test_batch_chat_placeholder(self):
        """batch_chat should be callable (even if not fully implemented)."""
        config = BatchConfig(
            enabled=True,
            max_batch_size=10,
            max_wait_ms=50,
            priority_threshold_ms=100,
        )
        batcher = RequestBatcher(config=config)

        # Should return empty list (placeholder implementation)
        result = await batcher.batch_chat([{"prompt": "test"}])
        assert result == []

    @pytest.mark.asyncio
    async def test_batch_rag_placeholder(self):
        """batch_rag should be callable (even if not fully implemented)."""
        config = BatchConfig(
            enabled=True,
            max_batch_size=10,
            max_wait_ms=50,
            priority_threshold_ms=100,
        )
        batcher = RequestBatcher(config=config)

        # Should return empty list (placeholder implementation)
        result = await batcher.batch_rag(["query1", "query2"])
        assert result == []
