from __future__ import annotations

import urllib.error
from configparser import ConfigParser
from typing import Any
from unittest.mock import Mock

import pytest

# =============================================================================
# Embeddings Tests
# =============================================================================


@pytest.mark.unit
def test_embed_text_single_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_text should call embed_texts and return first vector."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        return {"embedding": [0.1, 0.2, 0.3]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import embed_text

    result = embed_text("test text")
    assert result == [0.1, 0.2, 0.3]


@pytest.mark.unit
def test_embed_texts_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts with empty list should return empty list."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.embeddings import embed_texts

    result = embed_texts([])
    assert result == []


@pytest.mark.unit
def test_embed_texts_empty_list_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts with empty list and collect_metrics should return empty list and zero metrics."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3", "embedding_batch_size": "16"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.embeddings import embed_texts

    result, metrics = embed_texts([], collect_metrics=True)
    assert result == []
    assert metrics.num_texts_embedded == 0
    assert metrics.embedding_time_ms == 0.0


@pytest.mark.unit
def test_embed_texts_batch_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts should batch requests according to batch_size."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "2", "embedding_batch_size": "2"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    call_count = 0

    def mock_post_json(url, payload, timeout):
        nonlocal call_count
        call_count += 1
        # Return embeddings for the batch
        batch_size = len(payload["input"])
        return {"embeddings": [[0.1, 0.2] for _ in range(batch_size)]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import embed_texts

    # 5 texts with batch_size=2 should make 3 calls (2+2+1)
    result = embed_texts(["a", "b", "c", "d", "e"])
    assert len(result) == 5
    assert call_count == 3


@pytest.mark.unit
def test_embed_texts_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts with collect_metrics=True should return metrics."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {
        "embedding_dim": "3",
        "embedding_batch_size": "16",
        "embedding_model": "test-model",
    }
    cfg["default"] = {"model": "default-model"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        return {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import embed_texts

    result, metrics = embed_texts(["text1", "text2"], collect_metrics=True)
    assert len(result) == 2
    assert metrics.num_texts_embedded == 2
    assert metrics.embedding_model == "test-model"
    assert metrics.embedding_dim == 3
    assert metrics.batch_size == 16
    assert metrics.embedding_time_ms > 0


@pytest.mark.unit
def test_embed_texts_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts should raise EmbeddingError on HTTP error."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        from nyxgpt.rag.embeddings import EmbeddingError

        raise EmbeddingError("HTTP error calling url: 500 Internal Server Error")

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import EmbeddingError, embed_texts

    with pytest.raises(EmbeddingError, match="HTTP error"):
        embed_texts(["test"])


@pytest.mark.unit
def test_embed_texts_dimension_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts should raise EmbeddingError if dimension doesn't match config."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "5"}  # Expect 5 dimensions

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        # Return 3 dimensions instead of expected 5
        return {"embeddings": [[0.1, 0.2, 0.3]]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import EmbeddingError, embed_texts

    with pytest.raises(EmbeddingError, match="Embedding has dim 3 but expected 5"):
        embed_texts(["test"])


@pytest.mark.unit
def test_embed_texts_unexpected_response_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """embed_texts should raise EmbeddingError for unexpected response format."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        # Missing both 'embeddings' and 'embedding' keys
        return {"unexpected_key": "value"}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import EmbeddingError, embed_texts

    with pytest.raises(EmbeddingError, match="Unexpected Ollama embed response keys"):
        embed_texts(["test"])


@pytest.mark.unit
def test_embed_texts_non_list_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts should raise EmbeddingError if embedding is not a list."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3"}

    monkeypatch.setattr("nyxgpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        # Return non-list embedding
        return {"embeddings": ["not a list"]}

    monkeypatch.setattr("nyxgpt.rag.embeddings._post_json", mock_post_json)

    from nyxgpt.rag.embeddings import EmbeddingError, embed_texts

    with pytest.raises(EmbeddingError, match="Embedding is not a list"):
        embed_texts(["test"])


@pytest.mark.unit
def test_post_json_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """_post_json should raise EmbeddingError on URLError."""
    from nyxgpt.rag.embeddings import EmbeddingError, _post_json

    def mock_urlopen(req, timeout):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    with pytest.raises(EmbeddingError, match="Failed to reach Ollama"):
        _post_json("http://localhost:11434/api/embed", {}, 30)


# =============================================================================
# Vectorstore Tests
# =============================================================================


@pytest.mark.unit
def test_cassandra_vectorstore_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    """CassandraVectorStore should initialize with config values."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "cassandra_hosts": "localhost,127.0.0.1",
        "cassandra_port": "9042",
        "cassandra_keyspace": "test_keyspace",
        "cassandra_table": "test_table",
    }

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_cluster = Mock()
    mock_session = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    assert store.cfg.keyspace == "test_keyspace"
    assert store.cfg.table == "test_table"
    assert store.cfg.hosts == ["localhost", "127.0.0.1"]
    assert store.cfg.port == 9042


@pytest.mark.unit
def test_cassandra_vectorstore_upsert_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """upsert_chunks should insert all chunks with proper parameters."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    from cassandra.query import BatchStatement, BoundStatement, PreparedStatement

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    # BatchStatement.add() type-checks its statement argument against the
    # real driver classes, so the prepared statement mock must carry a spec.
    mock_prepared = Mock(spec=PreparedStatement)

    def _bind(params):
        bound = Mock(spec=BoundStatement)
        bound.values = params
        bound.custom_payload = None
        bound.keyspace = None
        bound.routing_key = None
        return bound

    mock_prepared.bind.side_effect = _bind
    mock_session.prepare.return_value = mock_prepared

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    texts = ["chunk1", "chunk2"]
    embeddings = [[0.1, 0.2], [0.3, 0.4]]
    metadatas = [{"key": "value1"}, {"key": "value2"}]

    store.upsert_chunks("doc1", texts, embeddings, metadatas)

    # Verify session.prepare was called (once, statement is cached on the instance)
    assert mock_session.prepare.call_count == 1
    # Both chunks fit under the default batch size, so they're sent as a
    # single BatchStatement instead of one execute() per chunk.
    assert mock_session.execute.call_count == 1
    executed = mock_session.execute.call_args[0][0]
    assert isinstance(executed, BatchStatement)


@pytest.mark.unit
def test_cassandra_vectorstore_upsert_length_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upsert_chunks should raise error on length mismatch."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore, VectorStoreError

    store = CassandraVectorStore()
    store._keyspace_ready = True

    with pytest.raises(VectorStoreError, match="length mismatch"):
        store.upsert_chunks("doc1", ["text1"], [[0.1]], [{"m1": 1}, {"m2": 2}])


@pytest.mark.unit
def test_cassandra_vectorstore_query_by_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query_by_embedding should return formatted results."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    # Mock database rows
    mock_row1 = Mock()
    mock_row1.doc_id = "doc1"
    mock_row1.chunk_id = 0
    mock_row1.text = "chunk text 1"
    mock_row1.metadata = '{"key": "value1"}'
    mock_row1.score = 0.95

    mock_row2 = Mock()
    mock_row2.doc_id = "doc2"
    mock_row2.chunk_id = 1
    mock_row2.text = "chunk text 2"
    mock_row2.metadata = '{"key": "value2"}'
    mock_row2.score = 0.85

    mock_session = Mock()
    mock_session.execute.return_value = [mock_row1, mock_row2]

    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    results = store.query_by_embedding([0.1, 0.2], k=2)

    assert len(results) == 2
    assert results[0]["doc_id"] == "doc1"
    assert results[0]["text"] == "chunk text 1"
    assert results[0]["score"] == 0.95
    assert results[0]["metadata"] == {"key": "value1"}
    assert results[1]["doc_id"] == "doc2"


@pytest.mark.unit
def test_cassandra_vectorstore_query_with_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query_by_embedding with collect_metrics should return metrics."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_row = Mock()
    mock_row.doc_id = "doc1"
    mock_row.chunk_id = 0
    mock_row.text = "text"
    mock_row.metadata = "{}"
    mock_row.score = 0.90

    mock_session = Mock()
    mock_session.execute.return_value = [mock_row]

    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    results, metrics = store.query_by_embedding([0.1, 0.2], k=5, collect_metrics=True)

    assert len(results) == 1
    assert metrics.raw_results_count == 1
    assert metrics.score_min == 0.90
    assert metrics.score_max == 0.90
    assert metrics.score_mean == 0.90
    assert metrics.vector_search_time_ms > 0


@pytest.mark.unit
def test_cassandra_vectorstore_list_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_docs should return sorted list of documents."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    # After GROUP BY fix, list_docs fetches individual rows and aggregates in Python
    # So we mock individual chunk rows instead of pre-aggregated results
    def make_row(doc_id: str, embedding_model: str = "test-model") -> Mock:
        row = Mock()
        row.doc_id = doc_id
        row.embedding_model = embedding_model
        return row

    # doc_b has 5 chunks, doc_a has 3 chunks
    mock_rows = [make_row("doc_b") for _ in range(5)] + [make_row("doc_a") for _ in range(3)]

    mock_session = Mock()
    mock_session.execute.return_value = mock_rows

    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    docs = store.list_docs()

    assert len(docs) == 2
    # Should be sorted by doc_id
    assert docs[0]["doc_id"] == "doc_a"
    assert docs[0]["chunks"] == 3
    assert docs[1]["doc_id"] == "doc_b"
    assert docs[1]["chunks"] == 5


# =============================================================================
# Fresh-install graceful degrade: missing keyspace/table (#3182)
# =============================================================================


def _store_with_cassandra_error(
    monkeypatch: pytest.MonkeyPatch, message: str, *, on: str = "execute"
):
    """Build a CassandraVectorStore whose session raises InvalidRequest(message).

    `on` selects whether `session.execute` or `session.prepare` raises, so
    callers can simulate a missing keyspace (fails on the `USE` execute) or a
    missing table (fails on preparing/executing the table-scoped query).
    """
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    from cassandra import InvalidRequest

    mock_session = Mock()
    setattr(mock_session, on, Mock(side_effect=InvalidRequest(message)))

    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    return CassandraVectorStore()


@pytest.mark.unit
def test_query_by_embedding_missing_keyspace_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query_by_embedding should return [] (not raise) when the keyspace
    doesn't exist yet -- the state of a fresh Docker Compose install before
    any RAG document has been ingested."""
    store = _store_with_cassandra_error(monkeypatch, "Keyspace 'test_ks' does not exist")

    assert store.query_by_embedding([0.1, 0.2], k=2) == []


@pytest.mark.unit
def test_query_by_embedding_missing_table_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query_by_embedding should return [] when the keyspace exists but this
    collection's table hasn't been created yet."""
    store = _store_with_cassandra_error(monkeypatch, "unconfigured table test_tbl", on="prepare")
    store._keyspace_ready = True  # keyspace exists; only the table is missing

    assert store.query_by_embedding([0.1, 0.2], k=2) == []


@pytest.mark.unit
def test_query_by_embedding_reraises_unrelated_invalid_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine CQL error unrelated to missing schema must still raise."""
    from cassandra import InvalidRequest

    store = _store_with_cassandra_error(monkeypatch, "malformed query string")
    store._keyspace_ready = True

    with pytest.raises(InvalidRequest, match="malformed query string"):
        store.query_by_embedding([0.1, 0.2], k=2)


@pytest.mark.unit
def test_list_docs_missing_keyspace_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_docs should return [] (not raise) on a fresh install."""
    store = _store_with_cassandra_error(monkeypatch, "Keyspace 'test_ks' does not exist")

    assert store.list_docs() == []


@pytest.mark.unit
def test_query_by_embeddings_batch_missing_keyspace_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """query_by_embeddings_batch should return one empty list per query
    instead of raising when the schema hasn't been created yet."""
    store = _store_with_cassandra_error(monkeypatch, "Keyspace 'test_ks' does not exist")

    results = store.query_by_embeddings_batch([[0.1, 0.2], [0.3, 0.4]], k=2)

    assert results == [[], []]


@pytest.mark.unit
def test_schema_exists_false_before_table_created(monkeypatch: pytest.MonkeyPatch) -> None:
    """schema_exists should report False without ever selecting the
    keyspace, so it's safe to call before the keyspace exists."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}
    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_result = Mock()
    mock_result.one.return_value = None
    mock_session = Mock()
    mock_session.execute.return_value = mock_result

    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session
    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()

    assert store.schema_exists() is False
    assert store._keyspace_ready is False  # never issued `USE <keyspace>`

    mock_result.one.return_value = Mock()
    assert store.schema_exists() is True


@pytest.mark.unit
def test_cassandra_vectorstore_delete_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_doc should execute DELETE statement."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    store.delete_doc("doc_to_delete")

    # Verify DELETE was executed
    assert mock_session.execute.called
    # Check the tuple at index 0 contains the doc_id
    call_args = mock_session.execute.call_args[0]
    assert len(call_args) == 2  # Statement and parameters
    assert call_args[1] == ("doc_to_delete",)


@pytest.mark.unit
def test_cassandra_vectorstore_truncate(monkeypatch: pytest.MonkeyPatch) -> None:
    """truncate should execute TRUNCATE statement."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    store.truncate()

    assert mock_session.execute.called


@pytest.mark.unit
def test_cassandra_vectorstore_ensure_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_schema should create keyspace, table, and index."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr("nyxgpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg)

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.Cluster", lambda hosts, **kwargs: mock_cluster
    )

    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store.ensure_schema(embedding_dim=768)

    # Should execute 6 statements: CREATE KEYSPACE, USE, CREATE TABLE, CREATE INDEX (vector), CREATE INDEX (embedding_model), CREATE TABLE (collection_settings)
    assert mock_session.execute.call_count == 6
    assert store._keyspace_ready


# =============================================================================
# RAG Chunking Tests
# =============================================================================


@pytest.mark.unit
def test_chunk_text_paragraph_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should split on paragraph boundaries."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "50", "chunk_overlap": "10"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    chunks = chunk_text(text)

    # Should create multiple chunks based on paragraphs
    assert len(chunks) > 0
    assert all(len(c) <= 60 for c in chunks)  # Allow some buffer for overlap


@pytest.mark.unit
def test_chunk_text_long_paragraph_wrapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should word-wrap very long paragraphs."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "30", "chunk_overlap": "5"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    # Single long paragraph without blank lines
    text = "word " * 50  # 250 characters
    chunks = chunk_text(text)

    assert len(chunks) > 1
    # Each chunk should be roughly chunk_size or less (with overlap consideration)
    for chunk in chunks:
        assert len(chunk) <= 40  # Allow some buffer


@pytest.mark.unit
def test_chunk_text_overlap_application(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should apply overlap between consecutive chunks."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "50", "chunk_overlap": "10"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = "A" * 60 + "\n\n" + "B" * 60
    chunks = chunk_text(text)

    # With overlap, later chunks should contain some text from previous chunks
    assert len(chunks) >= 2


@pytest.mark.unit
def test_chunk_text_whitespace_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should normalize different newline formats."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "100", "chunk_overlap": "10"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    # Test with different newline formats
    text_crlf = "Para1\r\n\r\nPara2"
    text_cr = "Para1\r\rPara2"

    chunks_crlf = chunk_text(text_crlf)
    chunks_cr = chunk_text(text_cr)

    assert len(chunks_crlf) > 0
    assert len(chunks_cr) > 0


# =============================================================================
# RAG Ingestion Tests
# =============================================================================


@pytest.mark.unit
def test_ingest_document_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """ingest_document with empty text should return 0 chunks."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "100", "chunk_overlap": "10"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import ingest_document

    result = ingest_document("doc1", "")
    assert result == {
        "status": "skipped",
        "chunks_ingested": 0,
        "doc_hash": None,
        "previous_hash": None,
    }


@pytest.mark.unit
def test_ingest_document_with_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """ingest_document should pass metadata to all chunks."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "50", "chunk_overlap": "10"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.embed_texts", lambda texts, **kwargs: [[0.1] for _ in texts]
    )

    mock_store = Mock()
    # Configure mock for update detection methods (document doesn't exist yet)
    mock_store.document_needs_update.return_value = True
    mock_store.get_document_hash.return_value = None  # New document
    mock_store.get_document_info.return_value = None  # Not an update

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store)

    from nyxgpt.rag.rag import ingest_document

    metadata = {"source": "test", "version": "1.0"}
    _ = ingest_document("doc1", "Some text to chunk", metadata=metadata)

    # Verify upsert_chunks was called with metadata
    assert mock_store.upsert_chunks.called
    call_args = mock_store.upsert_chunks.call_args
    metadatas = call_args[1]["metadatas"]
    assert all(m == metadata for m in metadatas)


@pytest.mark.unit
def test_ingest_document_auto_creates_schema_on_fresh_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ingest_document should create the schema automatically on a fresh
    collection even when the caller didn't pass ensure_schema=True.

    Without this, a fresh Docker Compose install's first RAG ingest (which
    defaults ensure_schema=False from the API/CLI) would fail with
    "keyspace does not exist" instead of bootstrapping the schema (#3182).
    """
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "50", "chunk_overlap": "10"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.embed_texts", lambda texts, **kwargs: [[0.1, 0.2, 0.3] for _ in texts]
    )

    mock_store = Mock()
    mock_store.schema_exists.return_value = False
    mock_store.get_document_hash.return_value = None
    mock_store.get_document_info.return_value = None

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store)

    from nyxgpt.rag.rag import ingest_document

    ingest_document("doc1", "Some text to chunk", ensure_schema=False)

    assert mock_store.ensure_schema.called
    # The document_needs_update path assumes the table already exists --
    # skipping it here confirms auto-creation took the ensure_schema branch.
    assert not mock_store.document_needs_update.called
    assert mock_store.upsert_chunks.called


@pytest.mark.unit
def test_ingest_document_skips_auto_schema_when_already_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ingest_document should not re-create the schema (or take the
    dimension-inference path) once the collection's table already exists."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "50", "chunk_overlap": "10"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.embed_texts", lambda texts, **kwargs: [[0.1, 0.2, 0.3] for _ in texts]
    )

    mock_store = Mock()
    mock_store.schema_exists.return_value = True
    mock_store.document_needs_update.return_value = True
    mock_store.get_document_hash.return_value = None
    mock_store.get_document_info.return_value = None

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store)

    from nyxgpt.rag.rag import ingest_document

    ingest_document("doc1", "Some text to chunk", ensure_schema=False)

    assert not mock_store.ensure_schema.called
    assert mock_store.document_needs_update.called
    assert mock_store.upsert_chunks.called


# =============================================================================
# RAG Query Expansion Tests
# =============================================================================


@pytest.mark.unit
def test_expand_query_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """expand_query with disabled config should return original query only."""
    cfg = ConfigParser()
    cfg["rag"] = {"enable_query_expansion": "false"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import expand_query

    result = expand_query("test query")
    assert result == ["test query"]


@pytest.mark.unit
def test_expand_query_enabled_with_valid_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """expand_query should parse LLM response and return variants."""
    cfg = ConfigParser()
    cfg["rag"] = {"enable_query_expansion": "true"}
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["default"] = {"model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    def mock_ollama_chat(base_url, model, messages, timeout_s):
        return '["variant 1", "variant 2"]'

    # ollama_chat is imported inside expand_query, so mock the module
    monkeypatch.setattr("nyxgpt.ollama_client.ollama_chat", mock_ollama_chat)

    from nyxgpt.rag.rag import expand_query

    result = expand_query("original query")
    assert len(result) == 3
    assert result[0] == "original query"
    assert "variant 1" in result
    assert "variant 2" in result


@pytest.mark.unit
def test_expand_query_handles_markdown_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """expand_query should strip markdown code blocks from JSON response."""
    cfg = ConfigParser()
    cfg["rag"] = {"enable_query_expansion": "true"}
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["default"] = {"model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    def mock_ollama_chat(base_url, model, messages, timeout_s):
        return '```json\n["variant 1", "variant 2"]\n```'

    monkeypatch.setattr("nyxgpt.ollama_client.ollama_chat", mock_ollama_chat)

    from nyxgpt.rag.rag import expand_query

    result = expand_query("original query")
    assert len(result) == 3
    assert result[0] == "original query"


@pytest.mark.unit
def test_expand_query_error_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """expand_query should fall back to original query on error."""
    cfg = ConfigParser()
    cfg["rag"] = {"enable_query_expansion": "true"}
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["default"] = {"model": "test-model"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    def mock_ollama_chat(base_url, model, messages, timeout_s):
        raise Exception("LLM error")

    monkeypatch.setattr("nyxgpt.ollama_client.ollama_chat", mock_ollama_chat)

    from nyxgpt.rag.rag import expand_query

    result = expand_query("original query")
    assert result == ["original query"]


# =============================================================================
# RAG Retrieval Tests (existing tests follow below)
# =============================================================================


@pytest.mark.unit
def test_retrieve_context_applies_min_score_and_max_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retrieve_context should filter by min_score, dedupe, and cap to max_chunks."""
    # Build a config that enables pruning
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "10",
        "min_score": "0.50",
        "max_chunks": "2",
        "dedupe": "true",
    }

    # Force rag.py to use our config
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    # Avoid real embedding / Cassandra
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)

    class FakeStore:
        def __init__(self, **kwargs):
            pass

        def query_by_embedding(self, _emb: Any, k: int, **kwargs):
            assert k == 10
            # Includes: below-threshold, duplicates, and valid unique
            # Results are sorted by score descending after filtering
            return [
                {
                    "text": "weak",
                    "score": 0.10,
                    "doc_id": "doc1",
                    "chunk_id": 0,
                },  # filtered: below min_score
                {"text": "keep one", "score": 0.90, "doc_id": "doc2", "chunk_id": 0},
                {
                    "text": "keep one",
                    "score": 0.91,
                    "doc_id": "doc2",
                    "chunk_id": 0,
                },  # filtered: duplicate
                {
                    "text": "keep two",
                    "score": 0.70,
                    "doc_id": "doc3",
                    "chunk_id": 0,
                },  # dropped: max_chunks=2
                {"text": "keep three", "score": 0.80, "doc_id": "doc4", "chunk_id": 0},
            ]

        def list_docs(self):
            return []

        def close(self):
            return None

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    rows = retrieve_context("hello")
    assert len(rows) == 2
    # Sorted by score descending: 0.90 > 0.80 > 0.70
    assert [r["text"] for r in rows] == ["keep one", "keep three"]


@pytest.mark.unit
def test_compose_context_respects_budget_and_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """compose_context should honor max chars and header/score flags."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_context_max_chars": "30",
        "include_headers": "true",
        "include_scores": "true",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import compose_context

    text = compose_context(
        [
            {"text": "abcdefghijklmnopqrstuvwxyz", "score": 0.9},
            {"text": "SECOND", "score": 0.8},
        ]
    )

    # Should include a header and be truncated to the configured budget
    assert "[Context 1]" in text
    assert "score=" in text
    assert len(text) <= 30


@pytest.mark.unit
def test_chunking_config_error_when_overlap_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_chunking_cfg should raise RAGError if overlap >= chunk_size."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "100",
        "chunk_overlap": "100",  # Invalid: overlap equals chunk_size
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import RAGError, _chunking_cfg

    with pytest.raises(RAGError, match="chunk_overlap must be smaller than chunk_size"):
        _chunking_cfg()


@pytest.mark.unit
def test_chunking_config_error_when_overlap_greater_than_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_chunking_cfg should raise RAGError if overlap > chunk_size."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "100",
        "chunk_overlap": "150",  # Invalid: overlap > chunk_size
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import RAGError, _chunking_cfg

    with pytest.raises(RAGError, match="chunk_overlap must be smaller than chunk_size"):
        _chunking_cfg()


@pytest.mark.unit
def test_chunk_text_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should handle empty string gracefully."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "100",
        "chunk_overlap": "20",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    chunks = chunk_text("")
    assert chunks == []


@pytest.mark.unit
def test_chunk_text_very_small_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should handle text smaller than chunk_size."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "1000",
        "chunk_overlap": "100",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    small_text = "This is a small text."
    chunks = chunk_text(small_text)
    assert len(chunks) == 1
    assert chunks[0] == small_text


@pytest.mark.unit
def test_compose_context_empty_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """compose_context should handle empty chunks list."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_context_max_chars": "1000",
        "include_headers": "false",
        "include_scores": "false",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import compose_context

    text = compose_context([])
    assert text == ""


@pytest.mark.unit
def test_annotate_chunk_numbering_adds_1_based_number_and_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chunk_number must be 1-based (chunk_id is the internal zero-based
    Cassandra clustering key), and every row is stamped with the collection
    it was retrieved from -- the fix for the "(chunk 0)" citation bug."""
    from nyxgpt.rag.rag import annotate_chunk_numbering

    mock_store = Mock()
    mock_store.get_document_info.return_value = {"chunks": 5}
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **_k: mock_store)

    rows = [
        {"doc_id": "doc-1", "chunk_id": 0, "text": "a"},
        {"doc_id": "doc-1", "chunk_id": 4, "text": "b"},
    ]
    result = annotate_chunk_numbering(rows, collection="research-notes")

    assert result[0]["chunk_number"] == 1
    assert result[0]["total_chunks"] == 5
    assert result[0]["collection"] == "research-notes"
    assert result[1]["chunk_number"] == 5
    # get_document_info looked up once per unique doc_id, not once per row
    mock_store.get_document_info.assert_called_once_with("doc-1")
    mock_store.close.assert_called_once()


@pytest.mark.unit
def test_annotate_chunk_numbering_degrades_gracefully_when_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed total-chunks lookup (e.g. Cassandra unreachable) must not
    fail the whole chat/query response -- collection and chunk_number (which
    need no DB access) still get set."""
    from nyxgpt.rag.rag import annotate_chunk_numbering

    def boom(**_k: Any) -> Any:
        raise RuntimeError("vector store unreachable")

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", boom)

    rows = [{"doc_id": "doc-1", "chunk_id": 0, "text": "a"}]
    result = annotate_chunk_numbering(rows, collection="default")

    assert result[0]["chunk_number"] == 1
    assert result[0]["collection"] == "default"
    assert result[0].get("total_chunks") is None


@pytest.mark.unit
def test_annotate_chunk_numbering_empty_rows_is_a_noop() -> None:
    from nyxgpt.rag.rag import annotate_chunk_numbering

    assert annotate_chunk_numbering([], collection="default") == []


@pytest.mark.unit
def test_retrieve_context_empty_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """retrieve_context should handle empty query string."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "dedupe": "false",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)

    class FakeStore:
        def __init__(self, **kwargs) -> None:
            self.last_k: int | None = None

        def query_by_embedding(self, _emb: list[float], k: int, **kwargs) -> list:
            self.last_k = k
            return []

        def list_docs(self):
            return []

        def close(self) -> None:
            pass

    fake_store = FakeStore()
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda *a, **kw: fake_store)

    from nyxgpt.rag.rag import retrieve_context

    rows = retrieve_context("")
    assert rows == []
    assert fake_store.last_k == 5  # Verify chat_top_k was passed correctly


@pytest.mark.unit
def test_retrieve_context_debug_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """retrieve_context with debug_mode=True should return debug info."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.50",
        "max_chunks": "3",
        "dedupe": "true",
        "debug_mode": "false",  # Test explicit override
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    # Mock embed_texts to return embeddings + metrics
    from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

    def mock_embed_texts(texts, *, collect_metrics=False, **kwargs):
        embeddings = [[0.1, 0.2, 0.3] for _ in texts]
        if collect_metrics:
            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=3,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=10.5,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", mock_embed_texts)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.1, 0.2, 0.3])

    # Mock vector store
    from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

    class FakeStore:
        def __init__(self, **kwargs):
            pass

        def query_by_embedding(self, _emb, k: int, *, collect_metrics=False, **kwargs):
            results = [
                {"text": "result one", "score": 0.90, "doc_id": "doc1", "chunk_id": 0},
                {"text": "result two", "score": 0.75, "doc_id": "doc2", "chunk_id": 0},
                {
                    "text": "result three",
                    "score": 0.60,
                    "doc_id": "doc3",
                    "chunk_id": 0,
                },
            ]
            if collect_metrics:
                metrics = VectorSearchDebugMetrics(
                    raw_results_count=3,
                    score_min=0.60,
                    score_max=0.90,
                    score_mean=0.75,
                    vector_search_time_ms=25.3,
                )
                return results, metrics
            return results

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    # Test with explicit debug_mode=True (overrides config)
    result = retrieve_context("test query", debug_mode=True)
    assert isinstance(result, tuple)
    results, debug_info = result

    # Verify results
    assert len(results) == 3
    assert results[0]["text"] == "result one"

    # Verify debug info structure
    assert debug_info.total_time_ms > 0
    assert debug_info.embedding_time_ms == 10.5
    assert debug_info.vector_search_time_ms == 25.3
    assert debug_info.original_query == "test query"
    assert debug_info.query_variants == ["test query"]
    assert debug_info.num_queries == 1
    assert debug_info.embedding_model == "test-model"
    assert debug_info.embedding_dim == 3
    assert debug_info.num_texts_embedded == 1
    assert debug_info.raw_results_count == 3
    assert debug_info.score_min == 0.60
    assert debug_info.score_max == 0.90
    assert debug_info.score_mean == 0.75
    assert debug_info.after_dedupe_filter == 3
    assert debug_info.after_min_score_filter == 3
    assert debug_info.chunks_included == 3


@pytest.mark.unit
def test_retrieve_context_debug_mode_reports_effective_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for #3464: RAGDebugInfo.collection must reflect the
    collection that was *actually* queried, so the Playground Debug tab's
    collection display is trustworthy rather than just echoing the request
    payload. Uses a non-default collection so a hardcoded fallback (e.g.
    always "default") would be caught. Exercises the real retrieve_context
    code path (not mocked out), unlike the API-level test that only checks
    app.py forwards debug_info.collection into the response."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "dedupe": "true",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

    def mock_embed_texts(texts, *, collect_metrics=False, **kwargs):
        embeddings = [[0.1, 0.2, 0.3] for _ in texts]
        if collect_metrics:
            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=3,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=10.5,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", mock_embed_texts)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.1, 0.2, 0.3])

    from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

    class FakeStore:
        def __init__(self, **kwargs):
            pass

        def query_by_embedding(self, _emb, k: int, *, collect_metrics=False, **kwargs):
            results = [{"text": "result one", "score": 0.90, "doc_id": "doc1", "chunk_id": 0}]
            if collect_metrics:
                metrics = VectorSearchDebugMetrics(
                    raw_results_count=1,
                    score_min=0.90,
                    score_max=0.90,
                    score_mean=0.90,
                    vector_search_time_ms=25.3,
                )
                return results, metrics
            return results

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    result = retrieve_context("test query", debug_mode=True, collection="research-notes")
    assert isinstance(result, tuple)
    _, debug_info = result

    assert debug_info.collection == "research-notes"


# =============================================================================
# Evaluation Metrics Tests
# =============================================================================


@pytest.mark.unit
def test_compute_evaluation_metrics_with_results() -> None:
    """compute_evaluation_metrics should return complete metrics for successful query."""
    from nyxgpt.rag.rag import (
        RAGDebugInfo,
        compute_evaluation_metrics,
    )

    # Mock retrieval results
    results = [
        {"doc_id": "doc1", "chunk_id": 0, "text": "chunk 1", "score": 0.95},
        {"doc_id": "doc1", "chunk_id": 1, "text": "chunk 2", "score": 0.85},
        {"doc_id": "doc2", "chunk_id": 0, "text": "chunk 3", "score": 0.75},
    ]

    # Mock debug info
    debug_info = RAGDebugInfo(
        total_time_ms=100.0,
        query_expansion_time_ms=10.0,
        embedding_time_ms=20.0,
        vector_search_time_ms=50.0,
        keyword_search_time_ms=None,
        fusion_time_ms=None,
        reranking_time_ms=None,
        filtering_time_ms=5.0,
        composition_time_ms=15.0,
        original_query="test query",
        query_variants=["test query"],
        num_queries=1,
        embedding_model="nomic-embed-text",
        embedding_dim=768,
        num_texts_embedded=1,
        batch_size=1,
        raw_results_count=3,
        score_min=0.75,
        score_max=0.95,
        score_mean=0.85,
        hybrid_enabled=False,
        keyword_results_count=None,  # None because hybrid search is disabled
        # vector_results_count matches raw_results_count (3) for vector-only search
        vector_results_count=3,
        fusion_method=None,
        reranking_enabled=False,
        reranker_model=None,
        num_candidates_reranked=None,
        num_results_after_rerank=None,
        after_min_score_filter=3,
        after_dedupe_filter=3,
        after_max_chunks_filter=3,
        total_chars_before_truncation=100,
        total_chars_after_truncation=100,
        chunks_included=3,
    )

    min_score = 0.3

    # Compute evaluation metrics
    eval_metrics = compute_evaluation_metrics(results, debug_info, min_score)

    # Verify retrieval accuracy metrics
    assert eval_metrics.retrieval_accuracy.results_returned == 3
    assert eval_metrics.retrieval_accuracy.query_success is True
    assert eval_metrics.retrieval_accuracy.unique_docs_retrieved == 2
    assert eval_metrics.retrieval_accuracy.total_chunks_retrieved == 3
    assert "p50" in eval_metrics.retrieval_accuracy.score_distribution
    assert "p75" in eval_metrics.retrieval_accuracy.score_distribution
    assert "p95" in eval_metrics.retrieval_accuracy.score_distribution
    assert "p99" in eval_metrics.retrieval_accuracy.score_distribution

    # Verify latency metrics
    assert eval_metrics.latency.total_time_ms == 100.0
    assert eval_metrics.latency.stage_timings["embedding"] == 20.0
    assert eval_metrics.latency.stage_timings["vector_search"] == 50.0
    assert eval_metrics.latency.stage_timings["filtering"] == 5.0

    # Verify hit rate metrics
    assert eval_metrics.hit_rate.query_success_rate == 1.0
    assert eval_metrics.hit_rate.total_queries == 1
    assert eval_metrics.hit_rate.successful_queries == 1
    assert eval_metrics.hit_rate.failed_queries == 0
    assert eval_metrics.hit_rate.avg_top_score == 0.95
    assert eval_metrics.hit_rate.score_above_threshold_rate == 1.0

    # Verify metadata
    assert len(eval_metrics.query_id) == 36  # UUID length
    assert eval_metrics.timestamp > 0


@pytest.mark.unit
def test_compute_evaluation_metrics_empty_results() -> None:
    """compute_evaluation_metrics should handle empty results correctly."""
    from nyxgpt.rag.rag import (
        RAGDebugInfo,
        compute_evaluation_metrics,
    )

    # Empty results
    results: list[dict] = []

    # Mock debug info
    debug_info = RAGDebugInfo(
        total_time_ms=50.0,
        query_expansion_time_ms=None,
        embedding_time_ms=15.0,
        vector_search_time_ms=30.0,
        keyword_search_time_ms=None,
        fusion_time_ms=None,
        reranking_time_ms=None,
        filtering_time_ms=5.0,
        composition_time_ms=0.0,
        original_query="test query",
        query_variants=["test query"],
        num_queries=1,
        embedding_model="nomic-embed-text",
        embedding_dim=768,
        num_texts_embedded=1,
        batch_size=1,
        raw_results_count=0,
        score_min=None,
        score_max=None,
        score_mean=None,
        hybrid_enabled=False,
        keyword_results_count=None,  # None because hybrid search is disabled
        # vector_results_count is 0 because no results were returned
        vector_results_count=0,
        fusion_method=None,
        reranking_enabled=False,
        reranker_model=None,
        num_candidates_reranked=None,
        num_results_after_rerank=None,
        after_min_score_filter=0,
        after_dedupe_filter=0,
        after_max_chunks_filter=0,
        total_chars_before_truncation=0,
        total_chars_after_truncation=0,
        chunks_included=0,
    )

    min_score = 0.3

    # Compute evaluation metrics
    eval_metrics = compute_evaluation_metrics(results, debug_info, min_score)

    # Verify retrieval accuracy metrics for empty results
    assert eval_metrics.retrieval_accuracy.results_returned == 0
    assert eval_metrics.retrieval_accuracy.query_success is False
    assert eval_metrics.retrieval_accuracy.unique_docs_retrieved == 0
    assert eval_metrics.retrieval_accuracy.total_chunks_retrieved == 0
    assert eval_metrics.retrieval_accuracy.score_distribution == {}

    # Verify hit rate metrics for failed query
    assert eval_metrics.hit_rate.query_success_rate == 0.0
    assert eval_metrics.hit_rate.total_queries == 1
    assert eval_metrics.hit_rate.successful_queries == 0
    assert eval_metrics.hit_rate.failed_queries == 1
    assert eval_metrics.hit_rate.avg_top_score is None
    assert eval_metrics.hit_rate.score_above_threshold_rate == 0.0


# =============================================================================
# Chunk Boundary Optimization Tests
# =============================================================================


@pytest.mark.unit
def test_chunk_text_sentence_aware_splitting(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text with sentence_aware=True should split on sentence boundaries."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "100",
        "chunk_overlap": "20",
        "sentence_aware": "true",
        "preserve_headings": "false",
        "overlap_strategy": "trailing",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = "First sentence here. Second sentence here. Third sentence is longer and should split properly. Fourth sentence."
    chunks = chunk_text(text)

    # Should create chunks respecting sentence boundaries
    assert len(chunks) > 1
    # Verify no mid-sentence breaks (each chunk should end with punctuation or be at a boundary)
    for chunk in chunks:
        # Chunks should be reasonably sized
        assert len(chunk) <= 130  # Allow for overlap


@pytest.mark.unit
def test_chunk_text_heading_aware_splitting(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text with preserve_headings=True should keep headings with content."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "200",
        "chunk_overlap": "20",
        "sentence_aware": "true",
        "preserve_headings": "true",
        "overlap_strategy": "trailing",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = """# Main Heading

This is content under main heading.

## Subheading One

Content for subheading one goes here.

## Subheading Two

Content for subheading two goes here."""

    chunks = chunk_text(text)

    # Should create chunks with headings preserved
    assert len(chunks) > 0
    # First chunk should contain the main heading
    assert "# Main Heading" in chunks[0]
    # Verify headings are kept with their content
    heading_chunks = [c for c in chunks if "#" in c]
    assert len(heading_chunks) > 0


@pytest.mark.unit
def test_chunk_text_overlap_strategy_sentence(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text with overlap_strategy='sentence' should overlap with complete sentences."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "80",
        "chunk_overlap": "40",
        "sentence_aware": "true",
        "preserve_headings": "false",
        "overlap_strategy": "sentence",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = "First sentence. Second sentence. Third sentence. Fourth sentence. Fifth sentence."
    chunks = chunk_text(text)

    # Should have overlapping content
    assert len(chunks) >= 2
    # Overlap should be complete sentences, not partial
    if len(chunks) >= 2:
        # Check that overlap content appears in both chunks
        # This is a heuristic check - complete sentences should appear in adjacent chunks
        for i in range(1, len(chunks)):
            # Current chunk should start with recognizable content from previous chunk
            # (though exact match is hard due to formatting)
            assert len(chunks[i]) > 0


@pytest.mark.unit
def test_chunk_text_overlap_strategy_semantic(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text with overlap_strategy='semantic' should overlap with complete paragraphs."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "150",
        "chunk_overlap": "60",
        "sentence_aware": "true",
        "preserve_headings": "false",
        "overlap_strategy": "semantic",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = """First paragraph with some content here.

Second paragraph with more content.

Third paragraph with additional content.

Fourth paragraph with final content."""

    chunks = chunk_text(text)

    # Should create multiple chunks with paragraph-level overlap
    assert len(chunks) >= 2
    # Verify chunks contain paragraph boundaries (\n\n)
    paragraph_chunks = [c for c in chunks if "\n\n" in c or len(c.split("\n\n")) == 1]
    assert len(paragraph_chunks) > 0


@pytest.mark.unit
def test_split_sentences_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    """_split_sentences should split text into sentences correctly."""
    from nyxgpt.rag.rag import _split_sentences

    text = "First sentence. Second sentence! Third sentence? Fourth."
    sentences = _split_sentences(text)

    assert len(sentences) == 4
    assert "First sentence" in sentences[0]
    assert "Second sentence" in sentences[1]
    assert "Third sentence" in sentences[2]
    assert "Fourth" in sentences[3]


@pytest.mark.unit
def test_split_sentences_abbreviations(monkeypatch: pytest.MonkeyPatch) -> None:
    """_split_sentences should not split on abbreviations like Dr., Mr., etc."""
    from nyxgpt.rag.rag import _split_sentences

    text = "Dr. Smith works at Mt. Everest. He is a professor."
    sentences = _split_sentences(text)

    # Should not split on "Dr." or "Mt."
    assert len(sentences) == 2
    assert "Dr. Smith works at Mt. Everest" in sentences[0]
    assert "He is a professor" in sentences[1]


@pytest.mark.unit
def test_is_heading_atx_style(monkeypatch: pytest.MonkeyPatch) -> None:
    """_is_heading should detect ATX-style Markdown headings."""
    from nyxgpt.rag.rag import _is_heading

    assert _is_heading("# Heading 1") is True
    assert _is_heading("## Heading 2") is True
    assert _is_heading("### Heading 3") is True
    assert _is_heading("#### Heading 4") is True
    assert _is_heading("##### Heading 5") is True
    assert _is_heading("###### Heading 6") is True
    assert _is_heading("Not a heading") is False
    assert _is_heading("#No space") is False
    assert _is_heading("") is False


@pytest.mark.unit
def test_extract_heading_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """_extract_heading_level should return correct heading level."""
    from nyxgpt.rag.rag import _extract_heading_level

    assert _extract_heading_level("# Heading") == 1
    assert _extract_heading_level("## Heading") == 2
    assert _extract_heading_level("### Heading") == 3
    assert _extract_heading_level("#### Heading") == 4
    assert _extract_heading_level("##### Heading") == 5
    assert _extract_heading_level("###### Heading") == 6
    assert _extract_heading_level("Not a heading") == 0
    assert _extract_heading_level("#No space") == 0


@pytest.mark.unit
def test_chunk_text_mixed_features(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text with all features enabled should produce high-quality chunks."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "300",
        "chunk_overlap": "80",
        "sentence_aware": "true",
        "preserve_headings": "true",
        "overlap_strategy": "sentence",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = """# Introduction

This is the introduction section. It contains multiple sentences. Each sentence provides important context.

## Background

The background section provides historical context. It explains the motivation. It sets up the problem.

## Methodology

Our methodology involves several steps. First, we analyze the data. Then, we process it. Finally, we draw conclusions.

## Results

The results show significant findings. They confirm our hypothesis. The data supports our claims."""

    chunks = chunk_text(text)

    # Should create well-structured chunks
    assert len(chunks) > 0
    # Verify headings are preserved
    heading_chunks = [c for c in chunks if "#" in c]
    assert len(heading_chunks) > 0
    # Verify all chunks are within reasonable size
    for chunk in chunks:
        assert len(chunk) <= 400  # Allow some overage for semantic boundaries


@pytest.mark.unit
def test_chunk_text_backward_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text with legacy config should still work (backward compatibility)."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "100",
        "chunk_overlap": "20",
        # Legacy config without new features
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = "This is a simple test. It should work with legacy configuration."
    chunks = chunk_text(text)

    # Should still create chunks
    assert len(chunks) > 0
    assert all(len(c) <= 120 for c in chunks)


@pytest.mark.unit
def test_batched_query_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test batched execution of multiple query variants via query_by_embeddings_batch."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.50",
        "max_chunks": "5",
        "dedupe": "true",
        "enable_query_expansion": "true",
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    # Mock query expansion to return multiple queries
    def mock_expand_query(query, max_expansions=3):
        return [query, f"{query} variant 1", f"{query} variant 2"]

    monkeypatch.setattr("nyxgpt.rag.rag.expand_query", mock_expand_query)

    # Track which queries were embedded (should be batched)
    embedded_texts = []

    def mock_embed_texts(texts, *, collect_metrics=False, **kwargs):
        nonlocal embedded_texts
        embedded_texts.extend(texts)
        embeddings = [[0.1, 0.2, 0.3] for _ in texts]
        if collect_metrics:
            from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=3,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=10.0,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", mock_embed_texts)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.1, 0.2, 0.3])

    # Mock vector store that tracks the batched search call
    class FakeStore:
        def __init__(self, **kwargs):
            self.batch_calls: list[list[list[float]]] = []

        def query_by_embeddings_batch(self, embeddings, k: int, **kwargs):
            self.batch_calls.append(embeddings)
            # Simulate varying results from different query variants
            return [
                [
                    {
                        "text": f"result {idx}",
                        "score": 0.90 - (idx * 0.05),
                        "doc_id": f"doc{idx}",
                        "chunk_id": 0,
                    },
                ]
                for idx in range(1, len(embeddings) + 1)
            ]

        def list_docs(self):
            return []

        def close(self):
            pass

    fake_store = FakeStore()
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: fake_store)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context("test query")

    # Verify all three query embeddings were sent in a single batched call
    assert len(fake_store.batch_calls) == 1
    assert len(fake_store.batch_calls[0]) == 3

    # Verify all queries were embedded in batch
    assert len(embedded_texts) == 3
    assert embedded_texts[0] == "test query"
    assert embedded_texts[1] == "test query variant 1"
    assert embedded_texts[2] == "test query variant 2"

    # Verify results were deduplicated and returned
    assert len(results) > 0


@pytest.mark.unit
def test_parallel_query_execution_single_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that single query execution still works correctly (no parallelism needed)."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.50",
        "max_chunks": "5",
        "dedupe": "true",
        "enable_query_expansion": "false",  # Single query only
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.1, 0.2, 0.3])

    # Mock vector store
    class FakeStore:
        def __init__(self, **kwargs):
            self.query_count = 0

        def query_by_embedding(self, _emb, k: int, **kwargs):
            self.query_count += 1
            return [
                {"text": "single result", "score": 0.90, "doc_id": "doc1", "chunk_id": 0},
            ]

        def list_docs(self):
            return []

        def close(self):
            pass

    fake_store = FakeStore()
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: fake_store)

    from nyxgpt.rag.rag import retrieve_context

    # Execute retrieval with single query
    results = retrieve_context("test query")

    # Verify single query was executed (no parallelism overhead)
    assert fake_store.query_count == 1

    # Verify results were returned
    assert len(results) == 1
    assert results[0]["text"] == "single result"


@pytest.mark.unit
def test_compute_evaluation_metrics_score_percentiles() -> None:
    """compute_evaluation_metrics should calculate score percentiles correctly."""
    from nyxgpt.rag.rag import (
        RAGDebugInfo,
        compute_evaluation_metrics,
    )

    # Results with varied scores
    results = [
        {"doc_id": "doc1", "chunk_id": i, "text": f"chunk {i}", "score": 0.5 + i * 0.1}
        for i in range(5)
    ]

    debug_info = RAGDebugInfo(
        total_time_ms=100.0,
        query_expansion_time_ms=None,
        embedding_time_ms=20.0,
        vector_search_time_ms=50.0,
        keyword_search_time_ms=None,
        fusion_time_ms=None,
        reranking_time_ms=None,
        filtering_time_ms=5.0,
        composition_time_ms=25.0,
        original_query="test",
        query_variants=["test"],
        num_queries=1,
        embedding_model="nomic-embed-text",
        embedding_dim=768,
        num_texts_embedded=1,
        batch_size=1,
        raw_results_count=5,
        score_min=0.5,
        score_max=0.9,
        score_mean=0.7,
        hybrid_enabled=False,
        keyword_results_count=None,  # None because hybrid search is disabled
        # vector_results_count matches raw_results_count (5) for vector-only search
        vector_results_count=5,
        fusion_method=None,
        reranking_enabled=False,
        reranker_model=None,
        num_candidates_reranked=None,
        num_results_after_rerank=None,
        after_min_score_filter=5,
        after_dedupe_filter=5,
        after_max_chunks_filter=5,
        total_chars_before_truncation=200,
        total_chars_after_truncation=200,
        chunks_included=5,
    )

    min_score = 0.3

    eval_metrics = compute_evaluation_metrics(results, debug_info, min_score)

    # Verify percentiles are calculated
    score_dist = eval_metrics.retrieval_accuracy.score_distribution
    assert "p50" in score_dist
    assert "p75" in score_dist
    assert "p95" in score_dist
    assert "p99" in score_dist

    # Verify percentile values are reasonable (median should be middle value)
    assert 0.6 <= score_dist["p50"] <= 0.8
    assert score_dist["p75"] >= score_dist["p50"]
    assert score_dist["p95"] >= score_dist["p75"]
    assert score_dist["p99"] >= score_dist["p95"]
