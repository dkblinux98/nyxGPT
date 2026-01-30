"""
Unit tests for embedding performance optimizations.

Tests async processing, concurrent batching, and configuration options.
"""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import AsyncMock, patch

import pytest

from nyxgpt.rag.embeddings import (
    EmbeddingConfig,
    _batched,
    _embed_batch_async,
    _embed_texts_async,
    _embed_texts_sync,
    embed_texts,
)


def test_batched_splits_correctly():
    """_batched should split iterable into fixed-size batches."""
    items = list(range(10))
    batches = list(_batched(items, 3))
    assert len(batches) == 4
    assert batches[0] == [0, 1, 2]
    assert batches[1] == [3, 4, 5]
    assert batches[2] == [6, 7, 8]
    assert batches[3] == [9]


def test_batched_empty_iterable():
    """_batched with empty iterable should return no batches."""
    batches = list(_batched([], 10))
    assert len(batches) == 0


def test_batched_single_batch():
    """_batched with items less than batch size should return one batch."""
    items = [1, 2, 3]
    batches = list(_batched(items, 10))
    assert len(batches) == 1
    assert batches[0] == [1, 2, 3]


@pytest.mark.asyncio
async def test_embed_batch_async_success():
    """_embed_batch_async should embed a single batch successfully."""
    # Mock httpx.AsyncClient
    mock_client = AsyncMock()

    # Mock response from Ollama
    mock_response = {
        "embeddings": [
            [0.1, 0.2, 0.3],
            [0.4, 0.5, 0.6],
        ]
    }

    with patch("nyxgpt.rag.embeddings._post_json_async", return_value=mock_response):
        result = await _embed_batch_async(
            client=mock_client,
            url="http://localhost:11434/api/embed",
            model="nomic-embed-text",
            batch=["text1", "text2"],
            timeout=120,
            expected_dim=3,
        )

    assert len(result) == 2
    assert result[0] == [0.1, 0.2, 0.3]
    assert result[1] == [0.4, 0.5, 0.6]


@pytest.mark.asyncio
async def test_embed_batch_async_wrong_dimension():
    """_embed_batch_async should raise error if dimension mismatch."""
    mock_client = AsyncMock()
    mock_response = {"embeddings": [[0.1, 0.2]]}  # Wrong dimension (2 instead of 3)

    with patch("nyxgpt.rag.embeddings._post_json_async", return_value=mock_response):
        with pytest.raises(Exception, match="Embedding has dim 2 but expected 3"):
            await _embed_batch_async(
                client=mock_client,
                url="http://localhost:11434/api/embed",
                model="nomic-embed-text",
                batch=["text1"],
                timeout=120,
                expected_dim=3,
            )


@pytest.mark.asyncio
async def test_embed_texts_async_concurrent_batches():
    """_embed_texts_async should process batches concurrently."""
    cfg = EmbeddingConfig(
        base_url="http://localhost:11434",
        model="nomic-embed-text",
        dimension=3,
        timeout=120,
        batch_size=2,
        max_concurrent_batches=4,
        use_async=True,
    )

    texts = ["text1", "text2", "text3", "text4", "text5"]

    # Mock _embed_batch_async to return different results per batch
    async def mock_embed_batch(client, url, model, batch, timeout, expected_dim):
        # Return unique vectors per batch based on batch content
        return [[float(i), float(i), float(i)] for i, _ in enumerate(batch)]

    with patch("nyxgpt.rag.embeddings._embed_batch_async", side_effect=mock_embed_batch):
        with patch("nyxgpt.rag.embeddings.httpx.AsyncClient"):
            result = await _embed_texts_async(texts, cfg)

    # Should have 5 embeddings (3 batches: 2, 2, 1)
    assert len(result) == 5
    # All embeddings should be lists of floats
    for emb in result:
        assert isinstance(emb, list)
        assert all(isinstance(x, float) for x in emb)


def test_embed_texts_sync_fallback(monkeypatch: pytest.MonkeyPatch):
    """_embed_texts_sync should work as synchronous fallback."""
    cfg = EmbeddingConfig(
        base_url="http://localhost:11434",
        model="nomic-embed-text",
        dimension=3,
        timeout=120,
        batch_size=2,
        max_concurrent_batches=1,
        use_async=False,
    )

    texts = ["text1", "text2"]

    # Mock _post_json
    def mock_post_json(url, payload, timeout):
        return {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    result = _embed_texts_sync(texts, cfg)

    assert len(result) == 2
    assert result[0] == [0.1, 0.2, 0.3]
    assert result[1] == [0.4, 0.5, 0.6]


def test_embed_texts_uses_async_when_available(monkeypatch: pytest.MonkeyPatch):
    """embed_texts should use async implementation when configured."""
    cfg = ConfigParser()
    cfg.add_section("ollama")
    cfg.set("ollama", "base_url", "http://localhost:11434")
    cfg.add_section("rag")
    cfg.set("rag", "embedding_model", "nomic-embed-text")
    cfg.set("rag", "embedding_dim", "3")
    cfg.set("rag", "embedding_batch_size", "2")
    cfg.set("rag", "embedding_timeout_seconds", "120")
    cfg.set("rag", "embedding_use_async", "true")
    cfg.set("rag", "embedding_max_concurrent_batches", "4")

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.HTTPX_AVAILABLE", True)

    # Mock asyncio.run to verify it's called
    mock_async_run_called = False

    def mock_async_run(coro):
        nonlocal mock_async_run_called
        mock_async_run_called = True
        # Return mock embeddings
        return [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    monkeypatch.setattr("nyxgpt.rag.embeddings.asyncio.run", mock_async_run)

    result = embed_texts(["text1", "text2"])

    assert mock_async_run_called
    assert len(result) == 2


def test_embed_texts_uses_sync_when_async_disabled(monkeypatch: pytest.MonkeyPatch):
    """embed_texts should use sync implementation when async disabled."""
    cfg = ConfigParser()
    cfg.add_section("ollama")
    cfg.set("ollama", "base_url", "http://localhost:11434")
    cfg.add_section("rag")
    cfg.set("rag", "embedding_model", "nomic-embed-text")
    cfg.set("rag", "embedding_dim", "3")
    cfg.set("rag", "embedding_batch_size", "2")
    cfg.set("rag", "embedding_timeout_seconds", "120")
    cfg.set("rag", "embedding_use_async", "false")  # Disabled
    cfg.set("rag", "embedding_max_concurrent_batches", "4")

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.HTTPX_AVAILABLE", True)

    # Mock _post_json
    def mock_post_json(url, payload, timeout):
        return {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    result = embed_texts(["text1", "text2"])

    assert len(result) == 2


def test_embed_texts_uses_sync_when_httpx_unavailable(monkeypatch: pytest.MonkeyPatch):
    """embed_texts should fallback to sync when httpx not available."""
    cfg = ConfigParser()
    cfg.add_section("ollama")
    cfg.set("ollama", "base_url", "http://localhost:11434")
    cfg.add_section("rag")
    cfg.set("rag", "embedding_model", "nomic-embed-text")
    cfg.set("rag", "embedding_dim", "3")
    cfg.set("rag", "embedding_batch_size", "2")
    cfg.set("rag", "embedding_timeout_seconds", "120")
    cfg.set("rag", "embedding_use_async", "true")
    cfg.set("rag", "embedding_max_concurrent_batches", "4")

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.HTTPX_AVAILABLE", False)  # Not available

    # Mock _post_json
    def mock_post_json(url, payload, timeout):
        return {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    result = embed_texts(["text1", "text2"])

    # Should use sync implementation
    assert len(result) == 2


def test_embed_texts_with_metrics_async(monkeypatch: pytest.MonkeyPatch):
    """embed_texts should collect metrics with async implementation."""
    cfg = ConfigParser()
    cfg.add_section("ollama")
    cfg.set("ollama", "base_url", "http://localhost:11434")
    cfg.add_section("rag")
    cfg.set("rag", "embedding_model", "nomic-embed-text")
    cfg.set("rag", "embedding_dim", "3")
    cfg.set("rag", "embedding_batch_size", "2")
    cfg.set("rag", "embedding_timeout_seconds", "120")
    cfg.set("rag", "embedding_use_async", "true")
    cfg.set("rag", "embedding_max_concurrent_batches", "4")

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.HTTPX_AVAILABLE", True)

    # Mock asyncio.run
    def mock_async_run(coro):
        return [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    monkeypatch.setattr("nyxgpt.rag.embeddings.asyncio.run", mock_async_run)

    result, metrics = embed_texts(["text1", "text2"], collect_metrics=True)

    assert len(result) == 2
    assert metrics.embedding_model == "nomic-embed-text"
    assert metrics.embedding_dim == 3
    assert metrics.num_texts_embedded == 2
    assert metrics.batch_size == 2
    assert metrics.embedding_time_ms >= 0.0


def test_configuration_defaults(monkeypatch: pytest.MonkeyPatch):
    """Test that default configuration values are correct."""
    from nyxgpt.rag.embeddings import _embedding_cfg

    cfg = ConfigParser()
    cfg.add_section("ollama")
    cfg.set("ollama", "base_url", "http://localhost:11434")
    cfg.add_section("rag")
    # Empty rag section, should use defaults

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.get_default_model", lambda *_a, **_k: "llama3.1:8b")
    monkeypatch.setattr("nyxgpt.rag.embeddings.HTTPX_AVAILABLE", True)

    ecfg = _embedding_cfg()

    assert ecfg.base_url == "http://localhost:11434"
    assert ecfg.model == "llama3.1:8b"
    assert ecfg.dimension == 768  # Default
    assert ecfg.timeout == 120  # Default
    assert ecfg.batch_size == 16  # Default
    assert ecfg.max_concurrent_batches == 4  # Default
    assert ecfg.use_async is True  # Default when httpx available
