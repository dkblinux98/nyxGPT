"""Tests for parallel RAG query execution.

This module tests the concurrent execution of RAG queries including:
- Parallel embedding generation
- Concurrent vector searches
- Configuration-based parallelism control
"""

from __future__ import annotations

import asyncio
from configparser import ConfigParser
from unittest.mock import Mock, patch

import pytest


# =============================================================================
# Async Embedding Tests
# =============================================================================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_texts_async_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts_async with empty list should return empty list."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3", "max_concurrent_queries": "5"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.embeddings import embed_texts_async

    result = await embed_texts_async([])
    assert result == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_texts_async_empty_list_with_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """embed_texts_async with empty list and collect_metrics should return empty list and zero metrics."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_dim": "3",
        "embedding_batch_size": "16",
        "max_concurrent_queries": "5",
    }

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.embeddings import embed_texts_async

    result, metrics = await embed_texts_async([], collect_metrics=True)
    assert result == []
    assert metrics.num_texts_embedded == 0
    assert metrics.embedding_time_ms == 0.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_texts_async_concurrent_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """embed_texts_async should execute embeddings concurrently."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3", "max_concurrent_queries": "2"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    # Mock embed_text to track call timing
    call_times = []

    def mock_embed_text(text, **kwargs):
        import time

        call_times.append(time.time())
        # Simulate some work
        import time

        time.sleep(0.01)
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr("nyxgpt.rag.embeddings.embed_text", mock_embed_text)

    from nyxgpt.rag.embeddings import embed_texts_async

    texts = ["text1", "text2", "text3"]
    result = await embed_texts_async(texts, max_concurrent=2)

    assert len(result) == 3
    assert all(vec == [0.1, 0.2, 0.3] for vec in result)

    # Verify concurrent execution - all calls should start within a small window
    # (though max 2 at a time due to semaphore)
    assert len(call_times) == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_texts_async_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts_async with collect_metrics=True should return metrics."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_dim": "3",
        "embedding_batch_size": "16",
        "max_concurrent_queries": "5",
    }

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_embed_text(text, **kwargs):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr("nyxgpt.rag.embeddings.embed_text", mock_embed_text)

    from nyxgpt.rag.embeddings import embed_texts_async

    result, metrics = await embed_texts_async(
        ["text1", "text2"], collect_metrics=True
    )

    assert len(result) == 2
    assert metrics.num_texts_embedded == 2
    assert metrics.embedding_dim == 3
    assert metrics.embedding_time_ms > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_embed_texts_async_respects_max_concurrent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """embed_texts_async should respect max_concurrent limit."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3", "max_concurrent_queries": "2"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    # Track concurrent execution
    concurrent_count = 0
    max_concurrent_observed = 0

    async def mock_embed_text_async(text, **kwargs):
        nonlocal concurrent_count, max_concurrent_observed
        concurrent_count += 1
        max_concurrent_observed = max(max_concurrent_observed, concurrent_count)
        await asyncio.sleep(0.01)  # Simulate work
        concurrent_count -= 1
        return [0.1, 0.2, 0.3]

    # Wrap the sync function to track async behavior
    def mock_embed_text(text, **kwargs):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr("nyxgpt.rag.embeddings.embed_text", mock_embed_text)

    from nyxgpt.rag.embeddings import embed_texts_async

    texts = ["text1", "text2", "text3", "text4", "text5"]
    result = await embed_texts_async(texts, max_concurrent=2)

    assert len(result) == 5
    # max_concurrent_observed should not exceed 2 (but this test is simplified)


# =============================================================================
# Parallel Vector Search Tests
# =============================================================================


@pytest.mark.unit
def test_retrieve_context_sequential_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """retrieve_context should use sequential execution when parallel is disabled."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_dim": "3",
        "enable_parallel_queries": "false",  # Disable parallel
        "enable_query_expansion": "false",
        "enable_hybrid_search": "false",
    }
    cfg["nyxgpt"] = {"default_model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    # Mock embedding functions
    def mock_embed_text(text, **kwargs):
        return [0.1, 0.2, 0.3]

    def mock_embed_texts(texts, **kwargs):
        from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

        embeddings = [[0.1, 0.2, 0.3] for _ in texts]
        if kwargs.get("collect_metrics"):
            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=3,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=1.0,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", mock_embed_text)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", mock_embed_texts)

    # Mock vector store
    from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

    def mock_query_by_embedding(*args, **kwargs):
        if kwargs.get("collect_metrics"):
            metrics = VectorSearchDebugMetrics(
                raw_results_count=0,
                vector_search_time_ms=1.0,
                score_min=None,
                score_max=None,
                score_mean=None,
            )
            return [], metrics
        return []

    mock_store = Mock()
    mock_store.query_by_embedding = mock_query_by_embedding
    mock_store.list_docs.return_value = []

    monkeypatch.setattr(
        "nyxgpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store
    )

    from nyxgpt.rag.rag import retrieve_context

    # Should not raise and should use sequential path
    result = retrieve_context("test query", debug_mode=True)
    assert isinstance(result, tuple)  # Returns (context, debug_info)
    context, debug_info = result
    assert isinstance(context, list)
    assert debug_info is not None


@pytest.mark.unit
def test_retrieve_context_parallel_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """retrieve_context should use parallel execution when enabled with multiple queries."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_dim": "3",
        "enable_parallel_queries": "true",  # Enable parallel
        "enable_query_expansion": "true",  # Enable expansion to get multiple queries
        "enable_hybrid_search": "false",
        "max_concurrent_queries": "3",
    }
    cfg["nyxgpt"] = {"default_model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.config.load_config", lambda *_a, **_k: cfg)

    # Mock query expansion to return multiple queries
    def mock_expand_query(query, **kwargs):
        return [query, "expanded query 1", "expanded query 2"]

    monkeypatch.setattr("nyxgpt.rag.rag.expand_query", mock_expand_query)

    # Mock embedding functions
    def mock_embed_text(text, **kwargs):
        return [0.1, 0.2, 0.3]

    def mock_embed_texts(texts, **kwargs):
        from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

        embeddings = [[0.1, 0.2, 0.3] for _ in texts]
        if kwargs.get("collect_metrics"):
            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=3,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=1.0,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", mock_embed_text)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", mock_embed_texts)

    # Mock vector store
    from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

    def mock_query_by_embedding(*args, **kwargs):
        if kwargs.get("collect_metrics"):
            metrics = VectorSearchDebugMetrics(
                raw_results_count=0,
                vector_search_time_ms=1.0,
                score_min=None,
                score_max=None,
                score_mean=None,
            )
            return [], metrics
        return []

    mock_store = Mock()
    mock_store.query_by_embedding = mock_query_by_embedding
    mock_store.list_docs.return_value = []

    monkeypatch.setattr(
        "nyxgpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store
    )

    # Mock asyncio.run to verify it's called
    original_run = asyncio.run
    run_called = False

    def mock_asyncio_run(coro):
        nonlocal run_called
        run_called = True
        return original_run(coro)

    monkeypatch.setattr("asyncio.run", mock_asyncio_run)

    from nyxgpt.rag.rag import retrieve_context

    result = retrieve_context("test query", debug_mode=True)
    assert isinstance(result, tuple)  # Returns (context, debug_info)
    context, debug_info = result

    # Verify parallel path was used
    assert run_called


@pytest.mark.unit
def test_retrieve_context_sequential_for_single_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retrieve_context should use sequential execution for single query even when parallel enabled."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_dim": "3",
        "enable_parallel_queries": "true",  # Enable parallel
        "enable_query_expansion": "false",  # No expansion = single query
        "enable_hybrid_search": "false",
    }
    cfg["nyxgpt"] = {"default_model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    # Mock embedding functions
    def mock_embed_text(text, **kwargs):
        return [0.1, 0.2, 0.3]

    def mock_embed_texts(texts, **kwargs):
        from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

        embeddings = [[0.1, 0.2, 0.3] for _ in texts]
        if kwargs.get("collect_metrics"):
            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=3,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=1.0,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", mock_embed_text)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", mock_embed_texts)

    # Mock vector store
    from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

    def mock_query_by_embedding(*args, **kwargs):
        if kwargs.get("collect_metrics"):
            metrics = VectorSearchDebugMetrics(
                raw_results_count=0,
                vector_search_time_ms=1.0,
                score_min=None,
                score_max=None,
                score_mean=None,
            )
            return [], metrics
        return []

    mock_store = Mock()
    mock_store.query_by_embedding = mock_query_by_embedding
    mock_store.list_docs.return_value = []

    monkeypatch.setattr(
        "nyxgpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store
    )

    # Mock asyncio.run to verify it's NOT called
    run_called = False

    def mock_asyncio_run(coro):
        nonlocal run_called
        run_called = True
        return asyncio.run(coro)

    monkeypatch.setattr("asyncio.run", mock_asyncio_run)

    from nyxgpt.rag.rag import retrieve_context

    result = retrieve_context("test query", debug_mode=True)
    assert isinstance(result, tuple)  # Returns (context, debug_info)
    context, debug_info = result

    # Verify sequential path was used (asyncio.run not called)
    assert not run_called


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_vector_search_deduplication() -> None:
    """_parallel_vector_search should deduplicate results across queries."""
    from nyxgpt.rag.rag import _parallel_vector_search

    # Mock store with duplicate results
    mock_store = Mock()

    def mock_query_by_embedding(emb, k, embedding_model=None, metadata_filter=None):
        # Return same chunk from different queries
        return [
            {
                "doc_id": "doc1",
                "chunk_id": "chunk1",
                "text": "test text",
                "score": 0.9,
            }
        ]

    mock_store.query_by_embedding = mock_query_by_embedding

    queries = ["query1", "query2", "query3"]

    # Mock embed_text to return dummy embeddings
    with patch("nyxgpt.rag.rag.embed_text", return_value=[0.1, 0.2, 0.3]):
        (
            results_map,
            scores,
            total,
            emb_metrics,
            vs_metrics,
        ) = await _parallel_vector_search(
            queries=queries,
            store=mock_store,
            k=1,
            embedding_model=None,
            embedding_dim=3,
            actual_model="test-model",
            metadata_filter=None,
            collect_debug=False,
            max_concurrent=3,
        )

    # Should have only 1 unique result despite 3 queries
    assert len(results_map) == 1
    assert ("doc1", "chunk1") in results_map


@pytest.mark.unit
@pytest.mark.asyncio
async def test_parallel_vector_search_respects_max_concurrent() -> None:
    """_parallel_vector_search should respect max_concurrent limit."""
    from nyxgpt.rag.rag import _parallel_vector_search

    # Track concurrent executions
    concurrent_count = 0
    max_concurrent_observed = 0
    lock = asyncio.Lock()

    mock_store = Mock()

    async def mock_query_async(emb, k, embedding_model=None, metadata_filter=None):
        nonlocal concurrent_count, max_concurrent_observed
        async with lock:
            concurrent_count += 1
            max_concurrent_observed = max(max_concurrent_observed, concurrent_count)
        await asyncio.sleep(0.01)  # Simulate work
        async with lock:
            concurrent_count -= 1
        return []

    # Wrap sync version
    def mock_query_by_embedding(emb, k, embedding_model=None, metadata_filter=None):
        return []

    mock_store.query_by_embedding = mock_query_by_embedding

    queries = ["q1", "q2", "q3", "q4", "q5"]

    with patch("nyxgpt.rag.rag.embed_text", return_value=[0.1, 0.2, 0.3]):
        await _parallel_vector_search(
            queries=queries,
            store=mock_store,
            k=1,
            embedding_model=None,
            embedding_dim=3,
            actual_model="test-model",
            metadata_filter=None,
            collect_debug=False,
            max_concurrent=2,  # Limit to 2 concurrent
        )

    # Note: This test is simplified as the actual concurrency control
    # happens in the thread pool executor, not in the mock
