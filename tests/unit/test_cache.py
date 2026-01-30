"""Unit tests for caching utilities."""

import pytest
from unittest.mock import patch, MagicMock
from nyxgpt.cache import (
    make_embedding_cache_key,
    get_cached_embedding,
    cache_embedding,
    make_response_cache_key,
    get_cached_response,
    cache_response,
)


class TestEmbeddingCaching:
    """Test embedding cache functionality."""

    def test_make_embedding_cache_key_deterministic(self):
        """Cache keys should be deterministic for same inputs."""
        text = "test text"
        model = "nomic-embed-text"
        dimension = 768

        key1 = make_embedding_cache_key(text, model, dimension)
        key2 = make_embedding_cache_key(text, model, dimension)

        assert key1 == key2
        assert len(key1) == 64  # SHA256 hex digest length

    def test_make_embedding_cache_key_unique(self):
        """Cache keys should differ for different inputs."""
        key1 = make_embedding_cache_key("text1", "model", 768)
        key2 = make_embedding_cache_key("text2", "model", 768)
        key3 = make_embedding_cache_key("text1", "model", 384)
        key4 = make_embedding_cache_key("text1", "different-model", 768)

        # All keys should be unique
        assert len({key1, key2, key3, key4}) == 4

    @patch("nyxgpt.cache._get_embedding_cache")
    def test_get_cached_embedding_disabled(self, mock_get_cache):
        """Should return None when cache is disabled."""
        mock_get_cache.return_value = None

        result = get_cached_embedding("test", "model", 768)

        assert result is None

    @patch("nyxgpt.cache._get_embedding_cache")
    @patch("nyxgpt.cache.load_config")
    def test_get_cached_embedding_hit(self, mock_config, mock_get_cache):
        """Should return cached embedding on hit."""
        embedding = [0.1, 0.2, 0.3]
        mock_cache = MagicMock()
        mock_cache.get.return_value = embedding
        mock_get_cache.return_value = mock_cache

        mock_cfg = MagicMock()
        mock_cfg.getint.return_value = 0  # No TTL
        mock_config.return_value = mock_cfg

        result = get_cached_embedding("test", "model", 768)

        assert result == embedding
        mock_cache.get.assert_called_once()

    @patch("nyxgpt.cache._get_embedding_cache")
    @patch("nyxgpt.cache.load_config")
    def test_cache_embedding_success(self, mock_config, mock_get_cache):
        """Should cache embedding successfully."""
        embedding = [0.1, 0.2, 0.3]
        mock_cache = MagicMock()
        mock_get_cache.return_value = mock_cache

        mock_cfg = MagicMock()
        mock_cfg.getint.return_value = 0  # No TTL
        mock_config.return_value = mock_cfg

        cache_embedding("test", "model", 768, embedding)

        mock_cache.set.assert_called_once()
        args = mock_cache.set.call_args
        assert args[0][1] == embedding


class TestResponseCaching:
    """Test response cache functionality."""

    def test_make_response_cache_key_deterministic(self):
        """Cache keys should be deterministic for same inputs."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        model = "llama3.1:8b"

        key1 = make_response_cache_key(messages, model)
        key2 = make_response_cache_key(messages, model)

        assert key1 == key2
        assert len(key1) == 64  # SHA256 hex digest length

    def test_make_response_cache_key_unique(self):
        """Cache keys should differ for different inputs."""
        messages1 = [{"role": "user", "content": "Hello"}]
        messages2 = [{"role": "user", "content": "Goodbye"}]

        key1 = make_response_cache_key(messages1, "model")
        key2 = make_response_cache_key(messages2, "model")
        key3 = make_response_cache_key(messages1, "different-model")

        # All keys should be unique
        assert len({key1, key2, key3}) == 3

    @patch("nyxgpt.cache.load_config")
    def test_make_response_cache_key_session_scope(self, mock_config):
        """Cache keys should include session when scope is 'session'."""
        messages = [{"role": "user", "content": "Hello"}]
        model = "model"

        mock_cfg = MagicMock()
        mock_cfg.get.return_value = "session"  # Session scope
        mock_config.return_value = mock_cfg

        key1 = make_response_cache_key(messages, model, session="session1")
        key2 = make_response_cache_key(messages, model, session="session2")

        # Same messages but different sessions should have different keys
        assert key1 != key2

    @patch("nyxgpt.cache.load_config")
    def test_make_response_cache_key_global_scope(self, mock_config):
        """Cache keys should ignore session when scope is 'global'."""
        messages = [{"role": "user", "content": "Hello"}]
        model = "model"

        mock_cfg = MagicMock()
        mock_cfg.get.return_value = "global"  # Global scope
        mock_config.return_value = mock_cfg

        key1 = make_response_cache_key(messages, model, session="session1")
        key2 = make_response_cache_key(messages, model, session="session2")

        # Same messages in different sessions should have same key for global scope
        assert key1 == key2

    @patch("nyxgpt.cache._get_response_cache")
    def test_get_cached_response_disabled(self, mock_get_cache):
        """Should return None when cache is disabled."""
        mock_get_cache.return_value = None

        result = get_cached_response([{"role": "user", "content": "Hi"}], "model")

        assert result is None

    @patch("nyxgpt.cache._get_response_cache")
    @patch("nyxgpt.cache.load_config")
    def test_get_cached_response_hit(self, mock_config, mock_get_cache):
        """Should return cached response on hit."""
        response = "This is a cached response"
        mock_cache = MagicMock()
        mock_cache.get.return_value = response
        mock_get_cache.return_value = mock_cache

        mock_cfg = MagicMock()
        mock_cfg.getint.return_value = 3600  # 1 hour TTL
        mock_config.return_value = mock_cfg

        result = get_cached_response([{"role": "user", "content": "Hi"}], "model")

        assert result == response
        mock_cache.get.assert_called_once()

    @patch("nyxgpt.cache._get_response_cache")
    @patch("nyxgpt.cache.load_config")
    def test_cache_response_success(self, mock_config, mock_get_cache):
        """Should cache response successfully."""
        response = "Test response"
        messages = [{"role": "user", "content": "Test"}]
        mock_cache = MagicMock()
        mock_get_cache.return_value = mock_cache

        mock_cfg = MagicMock()
        mock_cfg.getint.return_value = 3600  # 1 hour TTL
        mock_config.return_value = mock_cfg

        cache_response(messages, "model", response)

        mock_cache.set.assert_called_once()
        args = mock_cache.set.call_args
        assert args[0][1] == response
