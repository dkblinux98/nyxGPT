"""Unit tests for vector search performance optimizations (issue #2686)."""

from __future__ import annotations

from configparser import ConfigParser
from unittest.mock import Mock, MagicMock, patch
import pytest


# =============================================================================
# Embedding Cache Tests
# =============================================================================


@pytest.mark.unit
def test_embed_text_cache_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_text with use_cache=True should cache repeated queries."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3", "embedding_model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    call_count = 0

    def mock_post_json(url, payload, timeout):
        nonlocal call_count
        call_count += 1
        return {"embedding": [0.1, 0.2, 0.3]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import embed_text, clear_embedding_cache

    # Clear cache to ensure clean state
    clear_embedding_cache()

    # First call should hit the API
    result1 = embed_text("test text", use_cache=True)
    assert result1 == [0.1, 0.2, 0.3]
    assert call_count == 1

    # Second call with same text should use cache
    result2 = embed_text("test text", use_cache=True)
    assert result2 == [0.1, 0.2, 0.3]
    assert call_count == 1  # No additional API call

    # Different text should hit API again
    result3 = embed_text("different text", use_cache=True)
    assert result3 == [0.1, 0.2, 0.3]
    assert call_count == 2


@pytest.mark.unit
def test_embed_text_cache_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_text with use_cache=False should not use cache."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3", "embedding_model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    call_count = 0

    def mock_post_json(url, payload, timeout):
        nonlocal call_count
        call_count += 1
        return {"embedding": [0.1, 0.2, 0.3]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import embed_text

    # Both calls should hit API when cache is disabled
    result1 = embed_text("test text", use_cache=False)
    assert result1 == [0.1, 0.2, 0.3]
    assert call_count == 1

    result2 = embed_text("test text", use_cache=False)
    assert result2 == [0.1, 0.2, 0.3]
    assert call_count == 2  # Second API call made


@pytest.mark.unit
def test_clear_embedding_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """clear_embedding_cache should invalidate cached embeddings."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3", "embedding_model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    call_count = 0

    def mock_post_json(url, payload, timeout):
        nonlocal call_count
        call_count += 1
        return {"embedding": [0.1, 0.2, 0.3]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import embed_text, clear_embedding_cache

    # Prime the cache
    clear_embedding_cache()
    embed_text("test text", use_cache=True)
    assert call_count == 1

    # Use cache
    embed_text("test text", use_cache=True)
    assert call_count == 1

    # Clear cache
    clear_embedding_cache()

    # Should hit API again after cache clear
    embed_text("test text", use_cache=True)
    assert call_count == 2


# =============================================================================
# Batch Query Tests
# =============================================================================


@pytest.mark.unit
def test_batch_query_by_embeddings_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch_query_by_embeddings with empty list should return empty list."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Mock cluster and session
    mock_cluster = Mock()
    mock_session = Mock()
    mock_cluster.connect.return_value = mock_session

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store._keyspace_ready = True

        results = store.batch_query_by_embeddings([])
        assert results == []


@pytest.mark.unit
def test_batch_query_by_embeddings_multiple_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch_query_by_embeddings should execute queries concurrently."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Mock config
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "localhost",
        "cassandra_port": "9042",
        "cassandra_keyspace": "test",
        "cassandra_table": "test_table",
        "vector_fetch_size_multiplier": "2.0",
    }
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    # Mock cluster and session
    mock_cluster = Mock()
    mock_session = Mock()
    mock_cluster.connect.return_value = mock_session

    # Mock async execution
    mock_future1 = Mock()
    mock_future2 = Mock()

    # Mock result rows
    mock_row1 = Mock()
    mock_row1.doc_id = "doc1"
    mock_row1.chunk_id = 0
    mock_row1.text = "chunk 1"
    mock_row1.metadata = "{}"
    mock_row1.score = 0.9
    mock_row1.embedding_model = "test-model"
    mock_row1.embedding_dim = 3
    mock_row1.ingested_at = None

    mock_row2 = Mock()
    mock_row2.doc_id = "doc2"
    mock_row2.chunk_id = 0
    mock_row2.text = "chunk 2"
    mock_row2.metadata = "{}"
    mock_row2.score = 0.8
    mock_row2.embedding_model = "test-model"
    mock_row2.embedding_dim = 3
    mock_row2.ingested_at = None

    mock_future1.result.return_value = [mock_row1]
    mock_future2.result.return_value = [mock_row2]

    mock_session.execute_async.side_effect = [mock_future1, mock_future2]
    mock_session.prepare.return_value = Mock()

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store._keyspace_ready = True

        embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        results = store.batch_query_by_embeddings(embeddings, k=5)

        # Should return results for both queries
        assert len(results) == 2
        assert len(results[0]) == 1
        assert len(results[1]) == 1
        assert results[0][0]["doc_id"] == "doc1"
        assert results[1][0]["doc_id"] == "doc2"

        # Verify execute_async was called twice (once per embedding)
        assert mock_session.execute_async.call_count == 2


@pytest.mark.unit
def test_batch_query_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """batch_query_by_embeddings with collect_metrics should return metrics."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Mock config
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "localhost",
        "cassandra_port": "9042",
        "cassandra_keyspace": "test",
        "cassandra_table": "test_table",
        "vector_fetch_size_multiplier": "2.0",
    }
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    # Mock cluster and session
    mock_cluster = Mock()
    mock_session = Mock()
    mock_cluster.connect.return_value = mock_session

    # Mock async execution
    mock_future = Mock()
    mock_row = Mock()
    mock_row.doc_id = "doc1"
    mock_row.chunk_id = 0
    mock_row.text = "chunk 1"
    mock_row.metadata = "{}"
    mock_row.score = 0.9
    mock_row.embedding_model = "test-model"
    mock_row.embedding_dim = 3
    mock_row.ingested_at = None

    mock_future.result.return_value = [mock_row]
    mock_session.execute_async.return_value = mock_future
    mock_session.prepare.return_value = Mock()

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store._keyspace_ready = True

        embeddings = [[0.1, 0.2, 0.3]]
        results, metrics = store.batch_query_by_embeddings(embeddings, k=5, collect_metrics=True)

        # Should return results and metrics
        assert len(results) == 1
        assert len(metrics) == 1
        assert metrics[0].raw_results_count == 1
        assert metrics[0].score_mean == 0.9


# =============================================================================
# Connection Pool Configuration Tests
# =============================================================================


@pytest.mark.unit
def test_connection_pool_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """CassandraVectorStore should configure connection pooling from config."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Mock config with custom pool settings
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "localhost",
        "cassandra_port": "9042",
        "cassandra_keyspace": "test",
        "cassandra_table": "test_table",
        "cassandra_pool_size": "20",
        "cassandra_max_requests_per_connection": "256",
    }
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    # Mock cluster
    mock_cluster = Mock()
    mock_session = Mock()
    mock_cluster.connect.return_value = mock_session

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster) as cluster_cls:
        store = CassandraVectorStore()

        # Verify pool settings were applied
        assert store.cluster.connection_class.pool_size == 20
        assert store.cluster.connection_class.max_requests_per_connection == 256


@pytest.mark.unit
def test_connection_pool_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """CassandraVectorStore should use default pool settings if not configured."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Mock config without pool settings
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "localhost",
        "cassandra_port": "9042",
        "cassandra_keyspace": "test",
        "cassandra_table": "test_table",
    }
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    # Mock cluster
    mock_cluster = Mock()
    mock_session = Mock()
    mock_cluster.connect.return_value = mock_session

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()

        # Should use defaults (10, 128)
        assert store.cluster.connection_class.pool_size == 10
        assert store.cluster.connection_class.max_requests_per_connection == 128


# =============================================================================
# Prepared Statement Caching Tests
# =============================================================================


@pytest.mark.unit
def test_prepared_statement_caching(monkeypatch: pytest.MonkeyPatch) -> None:
    """query_by_embedding should cache and reuse prepared statements."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Mock config
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "localhost",
        "cassandra_port": "9042",
        "cassandra_keyspace": "test",
        "cassandra_table": "test_table",
        "vector_fetch_size_multiplier": "2.0",
    }
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    # Mock cluster and session
    mock_cluster = Mock()
    mock_session = Mock()
    mock_cluster.connect.return_value = mock_session

    mock_prepared_stmt = Mock()
    mock_session.prepare.return_value = mock_prepared_stmt
    mock_session.execute.return_value = []

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store._keyspace_ready = True

        # First query should prepare statement
        store.query_by_embedding([0.1, 0.2, 0.3], k=5)
        assert mock_session.prepare.call_count == 1

        # Second query should reuse prepared statement
        store.query_by_embedding([0.4, 0.5, 0.6], k=5)
        assert mock_session.prepare.call_count == 1  # Still 1, not 2

        # Verify prepared statement is cached
        stmt_key = f"query_by_embedding_{store.table_name}"
        assert stmt_key in store._prepared_statements
