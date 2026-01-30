"""Unit tests for embedding and response caching."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from nyxgpt.rag.embeddings import EmbeddingCache
from nyxgpt.chat import ResponseCache


class TestEmbeddingCache:
    """Test embedding cache functionality."""

    def test_cache_put_and_get(self):
        """Test basic cache put and get operations."""
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)

        text = "Hello, world!"
        model = "nomic-embed-text"
        dimension = 768
        embedding = [0.1] * dimension

        # Store in cache
        cache.put(text, model, dimension, embedding)

        # Retrieve from cache
        result = cache.get(text, model, dimension)
        assert result == embedding
        assert cache.size() == 1

    def test_cache_miss(self):
        """Test cache miss for non-existent entry."""
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)

        result = cache.get("nonexistent", "model", 768)
        assert result is None

    def test_cache_different_models(self):
        """Test that different models produce different cache keys."""
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)

        text = "Same text"
        embedding1 = [0.1] * 768
        embedding2 = [0.2] * 768

        cache.put(text, "model1", 768, embedding1)
        cache.put(text, "model2", 768, embedding2)

        assert cache.get(text, "model1", 768) == embedding1
        assert cache.get(text, "model2", 768) == embedding2
        assert cache.size() == 2

    def test_cache_different_dimensions(self):
        """Test that different dimensions produce different cache keys."""
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)

        text = "Same text"
        model = "same-model"
        embedding1 = [0.1] * 384
        embedding2 = [0.2] * 768

        cache.put(text, model, 384, embedding1)
        cache.put(text, model, 768, embedding2)

        assert cache.get(text, model, 384) == embedding1
        assert cache.get(text, model, 768) == embedding2
        assert cache.size() == 2

    def test_cache_ttl_expiration(self):
        """Test that cache entries expire after TTL."""
        cache = EmbeddingCache(max_size=10, ttl_seconds=1)

        text = "Test text"
        model = "test-model"
        dimension = 768
        embedding = [0.1] * dimension

        cache.put(text, model, dimension, embedding)
        assert cache.get(text, model, dimension) == embedding

        # Wait for expiration
        time.sleep(1.1)

        # Should be expired now
        result = cache.get(text, model, dimension)
        assert result is None
        assert cache.size() == 0

    def test_cache_lru_eviction(self):
        """Test that least recently used entries are evicted when cache is full."""
        cache = EmbeddingCache(max_size=3, ttl_seconds=60)

        # Fill cache
        for i in range(3):
            cache.put(f"text{i}", "model", 768, [float(i)] * 768)

        assert cache.size() == 3

        # Access text0 to make it recently used
        cache.get("text0", "model", 768)

        # Add new entry - should evict text1 (least recently used)
        cache.put("text3", "model", 768, [3.0] * 768)

        assert cache.size() == 3
        assert cache.get("text0", "model", 768) is not None
        assert cache.get("text1", "model", 768) is None  # Evicted
        assert cache.get("text2", "model", 768) is not None
        assert cache.get("text3", "model", 768) is not None

    def test_cache_clear(self):
        """Test cache clear operation."""
        cache = EmbeddingCache(max_size=10, ttl_seconds=60)

        # Add some entries
        for i in range(5):
            cache.put(f"text{i}", "model", 768, [float(i)] * 768)

        assert cache.size() == 5

        # Clear cache
        cache.clear()

        assert cache.size() == 0
        assert cache.get("text0", "model", 768) is None

    def test_cache_update_access_order(self):
        """Test that accessing an entry updates its position in LRU order."""
        cache = EmbeddingCache(max_size=2, ttl_seconds=60)

        cache.put("text0", "model", 768, [0.0] * 768)
        cache.put("text1", "model", 768, [1.0] * 768)

        # Access text0 (makes it most recently used)
        cache.get("text0", "model", 768)

        # Add text2 - should evict text1
        cache.put("text2", "model", 768, [2.0] * 768)

        assert cache.get("text0", "model", 768) is not None
        assert cache.get("text1", "model", 768) is None
        assert cache.get("text2", "model", 768) is not None


class TestResponseCache:
    """Test response cache functionality."""

    def test_response_cache_put_and_get(self):
        """Test basic response cache operations."""
        cache = ResponseCache(max_size=10, ttl_seconds=60)

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello!"},
        ]
        model = "llama3.1:8b"
        response = "Hi there!"

        # Store in cache
        cache.put(messages, model, response)

        # Retrieve from cache
        result = cache.get(messages, model)
        assert result == response
        assert cache.size() == 1

    def test_response_cache_miss(self):
        """Test response cache miss."""
        cache = ResponseCache(max_size=10, ttl_seconds=60)

        messages = [{"role": "user", "content": "Hello!"}]
        result = cache.get(messages, "model")
        assert result is None

    def test_response_cache_different_messages(self):
        """Test that different messages produce different cache keys."""
        cache = ResponseCache(max_size=10, ttl_seconds=60)

        messages1 = [{"role": "user", "content": "Hello!"}]
        messages2 = [{"role": "user", "content": "Goodbye!"}]
        model = "llama3.1:8b"

        cache.put(messages1, model, "Response 1")
        cache.put(messages2, model, "Response 2")

        assert cache.get(messages1, model) == "Response 1"
        assert cache.get(messages2, model) == "Response 2"
        assert cache.size() == 2

    def test_response_cache_different_models(self):
        """Test that different models produce different cache keys."""
        cache = ResponseCache(max_size=10, ttl_seconds=60)

        messages = [{"role": "user", "content": "Hello!"}]

        cache.put(messages, "model1", "Response from model1")
        cache.put(messages, "model2", "Response from model2")

        assert cache.get(messages, "model1") == "Response from model1"
        assert cache.get(messages, "model2") == "Response from model2"
        assert cache.size() == 2

    def test_response_cache_ttl_expiration(self):
        """Test that response cache entries expire after TTL."""
        cache = ResponseCache(max_size=10, ttl_seconds=1)

        messages = [{"role": "user", "content": "Test"}]
        model = "test-model"
        response = "Test response"

        cache.put(messages, model, response)
        assert cache.get(messages, model) == response

        # Wait for expiration
        time.sleep(1.1)

        # Should be expired now
        result = cache.get(messages, model)
        assert result is None
        assert cache.size() == 0

    def test_response_cache_lru_eviction(self):
        """Test LRU eviction in response cache."""
        cache = ResponseCache(max_size=2, ttl_seconds=60)

        messages1 = [{"role": "user", "content": "Message 1"}]
        messages2 = [{"role": "user", "content": "Message 2"}]
        messages3 = [{"role": "user", "content": "Message 3"}]
        model = "test-model"

        cache.put(messages1, model, "Response 1")
        cache.put(messages2, model, "Response 2")

        # Access messages1 to make it recently used
        cache.get(messages1, model)

        # Add messages3 - should evict messages2
        cache.put(messages3, model, "Response 3")

        assert cache.get(messages1, model) == "Response 1"
        assert cache.get(messages2, model) is None  # Evicted
        assert cache.get(messages3, model) == "Response 3"

    def test_response_cache_clear(self):
        """Test response cache clear operation."""
        cache = ResponseCache(max_size=10, ttl_seconds=60)

        for i in range(5):
            messages = [{"role": "user", "content": f"Message {i}"}]
            cache.put(messages, "model", f"Response {i}")

        assert cache.size() == 5

        cache.clear()

        assert cache.size() == 0

    def test_response_cache_message_order_matters(self):
        """Test that message order affects cache key."""
        cache = ResponseCache(max_size=10, ttl_seconds=60)

        messages1 = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
        messages2 = [
            {"role": "assistant", "content": "B"},
            {"role": "user", "content": "A"},
        ]
        model = "test-model"

        cache.put(messages1, model, "Response 1")
        cache.put(messages2, model, "Response 2")

        # Different order = different cache key
        assert cache.get(messages1, model) == "Response 1"
        assert cache.get(messages2, model) == "Response 2"
        assert cache.size() == 2


@patch("nyxgpt.rag.embeddings.load_config")
def test_embedding_cache_config_integration(mock_load_config):
    """Test that embedding cache respects configuration."""
    from nyxgpt.rag.embeddings import _get_embedding_cache, _embedding_cache

    # Reset global cache
    import nyxgpt.rag.embeddings
    nyxgpt.rag.embeddings._embedding_cache = None

    # Mock config
    mock_cfg = MagicMock()
    mock_cfg.getboolean.return_value = True
    mock_cfg.getint.side_effect = lambda section, key, fallback: {
        "embedding_cache_max_size": 500,
        "embedding_cache_ttl_seconds": 7200,
    }.get(key, fallback)
    mock_load_config.return_value = mock_cfg

    # Get cache
    cache = _get_embedding_cache()

    assert cache is not None
    assert cache.max_size == 500
    assert cache.ttl_seconds == 7200


@patch("nyxgpt.chat.load_config")
def test_response_cache_config_integration(mock_load_config):
    """Test that response cache respects configuration."""
    from nyxgpt.chat import _get_response_cache, _response_cache

    # Reset global cache
    import nyxgpt.chat
    nyxgpt.chat._response_cache = None

    # Mock config
    mock_cfg = MagicMock()
    mock_cfg.getboolean.return_value = True
    mock_cfg.getint.side_effect = lambda section, key, fallback: {
        "response_cache_max_size": 200,
        "response_cache_ttl_seconds": 3600,
    }.get(key, fallback)
    mock_load_config.return_value = mock_cfg

    # Get cache
    cache = _get_response_cache()

    assert cache is not None
    assert cache.max_size == 200
    assert cache.ttl_seconds == 3600


@patch("nyxgpt.rag.embeddings.load_config")
def test_embedding_cache_disabled(mock_load_config):
    """Test that embedding cache can be disabled via config."""
    from nyxgpt.rag.embeddings import _get_embedding_cache

    # Reset global cache
    import nyxgpt.rag.embeddings
    nyxgpt.rag.embeddings._embedding_cache = None

    # Mock config with caching disabled
    mock_cfg = MagicMock()
    mock_cfg.getboolean.return_value = False
    mock_load_config.return_value = mock_cfg

    # Get cache - should be None
    cache = _get_embedding_cache()

    assert cache is None


@patch("nyxgpt.chat.load_config")
def test_response_cache_disabled(mock_load_config):
    """Test that response cache can be disabled via config."""
    from nyxgpt.chat import _get_response_cache

    # Reset global cache
    import nyxgpt.chat
    nyxgpt.chat._response_cache = None

    # Mock config with caching disabled
    mock_cfg = MagicMock()
    mock_cfg.getboolean.return_value = False
    mock_load_config.return_value = mock_cfg

    # Get cache - should be None
    cache = _get_response_cache()

    assert cache is None
