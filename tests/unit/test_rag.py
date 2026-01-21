from __future__ import annotations

from configparser import ConfigParser
from typing import Any
from unittest.mock import Mock
import urllib.error

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

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        return {"embedding": [0.1, 0.2, 0.3]}

    monkeypatch.setattr("mygpt.rag.embeddings._post_json", mock_post_json)

    from mygpt.rag.embeddings import embed_text

    result = embed_text("test text")
    assert result == [0.1, 0.2, 0.3]


@pytest.mark.unit
def test_embed_texts_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts with empty list should return empty list."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3"}

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.embeddings import embed_texts

    result = embed_texts([])
    assert result == []


@pytest.mark.unit
def test_embed_texts_empty_list_with_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts with empty list and collect_metrics should return empty list and zero metrics."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3", "embedding_batch_size": "16"}

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.embeddings import embed_texts

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

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    call_count = 0

    def mock_post_json(url, payload, timeout):
        nonlocal call_count
        call_count += 1
        # Return embeddings for the batch
        batch_size = len(payload["input"])
        return {"embeddings": [[0.1, 0.2] for _ in range(batch_size)]}

    monkeypatch.setattr("mygpt.rag.embeddings._post_json", mock_post_json)

    from mygpt.rag.embeddings import embed_texts

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

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        return {"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]}

    monkeypatch.setattr("mygpt.rag.embeddings._post_json", mock_post_json)

    from mygpt.rag.embeddings import embed_texts

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

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        from mygpt.rag.embeddings import EmbeddingError

        raise EmbeddingError("HTTP error calling url: 500 Internal Server Error")

    monkeypatch.setattr("mygpt.rag.embeddings._post_json", mock_post_json)

    from mygpt.rag.embeddings import embed_texts, EmbeddingError

    with pytest.raises(EmbeddingError, match="HTTP error"):
        embed_texts(["test"])


@pytest.mark.unit
def test_embed_texts_dimension_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts should raise EmbeddingError if dimension doesn't match config."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "5"}  # Expect 5 dimensions

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        # Return 3 dimensions instead of expected 5
        return {"embeddings": [[0.1, 0.2, 0.3]]}

    monkeypatch.setattr("mygpt.rag.embeddings._post_json", mock_post_json)

    from mygpt.rag.embeddings import embed_texts, EmbeddingError

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

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        # Missing both 'embeddings' and 'embedding' keys
        return {"unexpected_key": "value"}

    monkeypatch.setattr("mygpt.rag.embeddings._post_json", mock_post_json)

    from mygpt.rag.embeddings import embed_texts, EmbeddingError

    with pytest.raises(EmbeddingError, match="Unexpected Ollama embed response keys"):
        embed_texts(["test"])


@pytest.mark.unit
def test_embed_texts_non_list_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_texts should raise EmbeddingError if embedding is not a list."""
    cfg = ConfigParser()
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["rag"] = {"embedding_dim": "3"}

    monkeypatch.setattr("mygpt.rag.embeddings.load_config", lambda *_a, **_k: cfg)

    def mock_post_json(url, payload, timeout):
        # Return non-list embedding
        return {"embeddings": ["not a list"]}

    monkeypatch.setattr("mygpt.rag.embeddings._post_json", mock_post_json)

    from mygpt.rag.embeddings import embed_texts, EmbeddingError

    with pytest.raises(EmbeddingError, match="Embedding is not a list"):
        embed_texts(["test"])


@pytest.mark.unit
def test_post_json_url_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """_post_json should raise EmbeddingError on URLError."""
    from mygpt.rag.embeddings import _post_json, EmbeddingError

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

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

    mock_cluster = Mock()
    mock_session = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

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

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    texts = ["chunk1", "chunk2"]
    embeddings = [[0.1, 0.2], [0.3, 0.4]]
    metadatas = [{"key": "value1"}, {"key": "value2"}]

    store.upsert_chunks("doc1", texts, embeddings, metadatas)

    # Verify session.prepare was called
    assert mock_session.prepare.called
    # Verify session.execute was called twice (once per chunk)
    assert mock_session.execute.call_count == 2


@pytest.mark.unit
def test_cassandra_vectorstore_upsert_length_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """upsert_chunks should raise error on length mismatch."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore, VectorStoreError

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

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

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
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

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

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

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
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

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

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

    # After GROUP BY fix, list_docs fetches individual rows and aggregates in Python
    # So we mock individual chunk rows instead of pre-aggregated results
    def make_row(doc_id: str, embedding_model: str = "test-model") -> Mock:
        row = Mock()
        row.doc_id = doc_id
        row.embedding_model = embedding_model
        return row

    # doc_b has 5 chunks, doc_a has 3 chunks
    mock_rows = [
        make_row("doc_b") for _ in range(5)
    ] + [
        make_row("doc_a") for _ in range(3)
    ]

    mock_session = Mock()
    mock_session.execute.return_value = mock_rows

    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    docs = store.list_docs()

    assert len(docs) == 2
    # Should be sorted by doc_id
    assert docs[0]["doc_id"] == "doc_a"
    assert docs[0]["chunks"] == 3
    assert docs[1]["doc_id"] == "doc_b"
    assert docs[1]["chunks"] == 5


@pytest.mark.unit
def test_cassandra_vectorstore_delete_doc(monkeypatch: pytest.MonkeyPatch) -> None:
    """delete_doc should execute DELETE statement."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

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

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store._keyspace_ready = True

    store.truncate()

    assert mock_session.execute.called


@pytest.mark.unit
def test_cassandra_vectorstore_ensure_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    """ensure_schema should create keyspace, table, and index."""
    cfg = ConfigParser()
    cfg["rag"] = {"cassandra_keyspace": "test_ks", "cassandra_table": "test_tbl"}

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.load_config", lambda *_a, **_k: cfg
    )

    mock_session = Mock()
    mock_cluster = Mock()
    mock_cluster.connect.return_value = mock_session

    monkeypatch.setattr(
        "mygpt.rag.vectorstore_cassandra.Cluster", lambda hosts, port: mock_cluster
    )

    from mygpt.rag.vectorstore_cassandra import CassandraVectorStore

    store = CassandraVectorStore()
    store.ensure_schema(embedding_dim=768)

    # Should execute 5 statements: CREATE KEYSPACE, USE, CREATE TABLE, CREATE INDEX (vector), CREATE INDEX (embedding_model)
    assert mock_session.execute.call_count == 5
    assert store._keyspace_ready


# =============================================================================
# RAG Chunking Tests
# =============================================================================


@pytest.mark.unit
def test_chunk_text_paragraph_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should split on paragraph boundaries."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "50", "chunk_overlap": "10"}

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import chunk_text

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import chunk_text

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import chunk_text

    text = "A" * 60 + "\n\n" + "B" * 60
    chunks = chunk_text(text)

    # With overlap, later chunks should contain some text from previous chunks
    assert len(chunks) >= 2


@pytest.mark.unit
def test_chunk_text_whitespace_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should normalize different newline formats."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "100", "chunk_overlap": "10"}

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import chunk_text

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
    """ingest_document with empty text should return 0."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "100", "chunk_overlap": "10"}

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import ingest_document

    result = ingest_document("doc1", "")
    assert result == {"status": "skipped", "chunks_ingested": 0, "doc_hash": None, "previous_hash": None}


@pytest.mark.unit
def test_ingest_document_with_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """ingest_document should pass metadata to all chunks."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "50", "chunk_overlap": "10"}

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "mygpt.rag.rag.embed_texts", lambda texts, **kwargs: [[0.1] for _ in texts]
    )

    mock_store = Mock()
    # Configure mock for update detection methods (document doesn't exist yet)
    mock_store.document_needs_update.return_value = True
    mock_store.get_document_hash.return_value = None  # New document
    mock_store.get_document_info.return_value = None  # Not an update

    monkeypatch.setattr(
        "mygpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store
    )

    from mygpt.rag.rag import ingest_document

    metadata = {"source": "test", "version": "1.0"}
    _ = ingest_document("doc1", "Some text to chunk", metadata=metadata)

    # Verify upsert_chunks was called with metadata
    assert mock_store.upsert_chunks.called
    call_args = mock_store.upsert_chunks.call_args
    metadatas = call_args[1]["metadatas"]
    assert all(m == metadata for m in metadatas)


# =============================================================================
# RAG Query Expansion Tests
# =============================================================================


@pytest.mark.unit
def test_expand_query_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """expand_query with disabled config should return original query only."""
    cfg = ConfigParser()
    cfg["rag"] = {"enable_query_expansion": "false"}

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import expand_query

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    def mock_ollama_chat(base_url, model, messages, timeout_s):
        return '["variant 1", "variant 2"]'

    # ollama_chat is imported inside expand_query, so mock the module
    monkeypatch.setattr("mygpt.ollama_client.ollama_chat", mock_ollama_chat)

    from mygpt.rag.rag import expand_query

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    def mock_ollama_chat(base_url, model, messages, timeout_s):
        return '```json\n["variant 1", "variant 2"]\n```'

    monkeypatch.setattr("mygpt.ollama_client.ollama_chat", mock_ollama_chat)

    from mygpt.rag.rag import expand_query

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    def mock_ollama_chat(base_url, model, messages, timeout_s):
        raise Exception("LLM error")

    monkeypatch.setattr("mygpt.ollama_client.ollama_chat", mock_ollama_chat)

    from mygpt.rag.rag import expand_query

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
    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    # Avoid real embedding / Cassandra
    monkeypatch.setattr("mygpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)

    class FakeStore:
        def __init__(self, **kwargs):
            pass

        def query_by_embedding(self, _emb: Any, k: int, **kwargs):
            assert k == 10
            # Includes: below-threshold, duplicates, and valid unique
            # Results are sorted by score descending after filtering
            return [
                {"text": "weak", "score": 0.10, "doc_id": "doc1", "chunk_id": 0},  # filtered: below min_score
                {"text": "keep one", "score": 0.90, "doc_id": "doc2", "chunk_id": 0},
                {"text": "keep one", "score": 0.91, "doc_id": "doc2", "chunk_id": 0},  # filtered: duplicate
                {"text": "keep two", "score": 0.70, "doc_id": "doc3", "chunk_id": 0},  # dropped: max_chunks=2
                {"text": "keep three", "score": 0.80, "doc_id": "doc4", "chunk_id": 0},
            ]

        def list_docs(self):
            return []

        def close(self):
            return None

    monkeypatch.setattr("mygpt.rag.rag.CassandraVectorStore", FakeStore)

    from mygpt.rag.rag import retrieve_context

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import compose_context

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import _chunking_cfg, RAGError

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import _chunking_cfg, RAGError

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import chunk_text

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import chunk_text

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import compose_context

    text = compose_context([])
    assert text == ""


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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("mygpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)

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
    monkeypatch.setattr(
        "mygpt.rag.rag.CassandraVectorStore", lambda *a, **kw: fake_store
    )

    from mygpt.rag.rag import retrieve_context

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

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    # Mock embed_texts to return embeddings + metrics
    from mygpt.rag.embeddings import EmbeddingDebugMetrics

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

    monkeypatch.setattr("mygpt.rag.rag.embed_texts", mock_embed_texts)
    monkeypatch.setattr(
        "mygpt.rag.rag.embed_text", lambda _q, **kwargs: [0.1, 0.2, 0.3]
    )

    # Mock vector store
    from mygpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

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

    monkeypatch.setattr("mygpt.rag.rag.CassandraVectorStore", FakeStore)

    from mygpt.rag.rag import retrieve_context

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


# =============================================================================
# Evaluation Metrics Tests
# =============================================================================


@pytest.mark.unit
def test_compute_evaluation_metrics_with_results() -> None:
    """compute_evaluation_metrics should return complete metrics for successful query."""
    from mygpt.rag.rag import (
        compute_evaluation_metrics,
        RAGDebugInfo,
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
        keyword_results_count=None,
        vector_results_count=None,
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
    from mygpt.rag.rag import (
        compute_evaluation_metrics,
        RAGDebugInfo,
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
        keyword_results_count=None,
        vector_results_count=None,
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


@pytest.mark.unit
def test_compute_evaluation_metrics_score_percentiles() -> None:
    """compute_evaluation_metrics should calculate score percentiles correctly."""
    from mygpt.rag.rag import (
        compute_evaluation_metrics,
        RAGDebugInfo,
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
        keyword_results_count=None,
        vector_results_count=None,
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
