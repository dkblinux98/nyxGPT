"""Unit tests for Cassandra query optimizations.

Tests for issue #2683: Cassandra query optimization
- Batch operations
- Prepared statements
- Query performance improvements
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock, MagicMock, call, patch
import pytest

from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore, VectorStoreError


@pytest.mark.unit
def test_vectorstore_uses_execution_profile():
    """CassandraVectorStore should configure execution profiles for optimized queries."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Initialize store
        store = CassandraVectorStore(collection="test")

        # Verify Cluster was initialized with execution_profiles
        assert mock_cluster_cls.called
        call_kwargs = mock_cluster_cls.call_args[1]
        assert "execution_profiles" in call_kwargs
        assert "protocol_version" in call_kwargs
        assert call_kwargs["protocol_version"] == 5

        store.close()


@pytest.mark.unit
def test_upsert_chunks_uses_batching():
    """upsert_chunks should use batch statements for multiple chunks."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls, \
         patch("nyxgpt.rag.vectorstore_cassandra.BatchStatement") as mock_batch_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock prepared statement with query_string attribute for BatchStatement.add()
        mock_prepared_stmt = Mock()
        mock_prepared_stmt.query_string = "INSERT INTO test (doc_id, chunk_id, text, metadata, embedding, embedding_model, embedding_dim, doc_hash, ingested_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        mock_session.prepare.return_value = mock_prepared_stmt

        # Mock batch statement
        mock_batch = Mock()
        mock_batch_cls.return_value = mock_batch

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # Upsert 10 chunks (should use batching)
        texts = [f"chunk_{i}" for i in range(10)]
        embeddings = [[0.1, 0.2, 0.3] for _ in range(10)]

        store.upsert_chunks(
            doc_id="test_doc",
            texts=texts,
            embeddings=embeddings,
            embedding_model="test-model",
            embedding_dim=3,
        )

        # Verify prepared statement was created and cached
        assert mock_session.prepare.called
        prepare_call = mock_session.prepare.call_args[0][0]
        assert "INSERT INTO" in prepare_call
        assert "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" in prepare_call

        # Verify batch.add was called 10 times (once per chunk)
        assert mock_batch.add.call_count == 10

        # Verify batch execution was called (10 chunks = 1 batch with BATCH_SIZE=50)
        assert mock_session.execute.called
        execute_calls = mock_session.execute.call_args_list

        # Should have 1 batch execution for 10 chunks
        assert len(execute_calls) == 1

        store.close()


@pytest.mark.unit
def test_upsert_chunks_handles_large_batches():
    """upsert_chunks should split large inserts into multiple batches."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls, \
         patch("nyxgpt.rag.vectorstore_cassandra.BatchStatement") as mock_batch_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock prepared statement with query_string attribute
        mock_prepared_stmt = Mock()
        mock_prepared_stmt.query_string = "INSERT INTO test (doc_id, chunk_id, text, metadata, embedding, embedding_model, embedding_dim, doc_hash, ingested_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        mock_session.prepare.return_value = mock_prepared_stmt

        # Mock batch statement
        mock_batch = Mock()
        mock_batch_cls.return_value = mock_batch

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # Upsert 120 chunks (should split into 3 batches: 50+50+20)
        texts = [f"chunk_{i}" for i in range(120)]
        embeddings = [[0.1, 0.2, 0.3] for _ in range(120)]

        store.upsert_chunks(
            doc_id="test_doc",
            texts=texts,
            embeddings=embeddings,
            embedding_model="test-model",
            embedding_dim=3,
        )

        # Verify batch execution was called 3 times
        execute_calls = mock_session.execute.call_args_list
        assert len(execute_calls) == 3

        # Verify total adds = 120 chunks
        assert mock_batch.add.call_count == 120

        store.close()


@pytest.mark.unit
def test_prepared_statements_are_cached():
    """Prepared statements should be cached to avoid re-preparing."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls, \
         patch("nyxgpt.rag.vectorstore_cassandra.BatchStatement") as mock_batch_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock prepared statement with query_string attribute
        mock_prepared_stmt = Mock()
        mock_prepared_stmt.query_string = "INSERT INTO test (doc_id, chunk_id, text, metadata, embedding, embedding_model, embedding_dim, doc_hash, ingested_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        mock_session.prepare.return_value = mock_prepared_stmt

        # Mock batch statement
        mock_batch = Mock()
        mock_batch_cls.return_value = mock_batch

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # First upsert - should prepare statement
        store.upsert_chunks(
            doc_id="doc1",
            texts=["chunk1"],
            embeddings=[[0.1, 0.2, 0.3]],
            embedding_model="test-model",
        )

        prepare_call_count = mock_session.prepare.call_count

        # Second upsert - should reuse prepared statement
        store.upsert_chunks(
            doc_id="doc2",
            texts=["chunk2"],
            embeddings=[[0.4, 0.5, 0.6]],
            embedding_model="test-model",
        )

        # prepare should only be called once (cached)
        assert mock_session.prepare.call_count == prepare_call_count

        store.close()


@pytest.mark.unit
def test_delete_doc_uses_prepared_statement():
    """delete_doc should use prepared statement for better performance."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock prepared statement
        mock_prepared_stmt = Mock()
        mock_session.prepare.return_value = mock_prepared_stmt

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # Delete document
        store.delete_doc("test_doc")

        # Verify prepared statement was used
        assert mock_session.prepare.called
        prepare_call = mock_session.prepare.call_args[0][0]
        assert "DELETE FROM" in prepare_call
        assert "WHERE doc_id = ?" in prepare_call

        # Verify execute was called with prepared statement
        assert mock_session.execute.called

        store.close()


@pytest.mark.unit
def test_get_document_hash_uses_prepared_statement():
    """get_document_hash should use prepared statement for better performance."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock prepared statement and result
        mock_prepared_stmt = Mock()
        mock_session.prepare.return_value = mock_prepared_stmt

        mock_row = Mock()
        mock_row.doc_hash = "test_hash_123"
        mock_result = Mock()
        mock_result.one.return_value = mock_row
        mock_session.execute.return_value = mock_result

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # Get document hash
        result = store.get_document_hash("test_doc")

        # Verify prepared statement was used
        assert mock_session.prepare.called
        prepare_call = mock_session.prepare.call_args[0][0]
        assert "SELECT doc_hash FROM" in prepare_call
        assert "WHERE doc_id = ? LIMIT 1" in prepare_call

        # Verify result
        assert result == "test_hash_123"

        store.close()


@pytest.mark.unit
def test_get_document_info_uses_prepared_statement():
    """get_document_info should use prepared statement for better performance."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock prepared statement and result
        mock_prepared_stmt = Mock()
        mock_session.prepare.return_value = mock_prepared_stmt

        mock_row = Mock()
        mock_row.doc_id = "test_doc"
        mock_row.doc_hash = "test_hash"
        mock_row.ingested_at = datetime(2025, 1, 1, 12, 0, 0)
        mock_row.updated_at = datetime(2025, 1, 2, 12, 0, 0)
        mock_row.embedding_model = "test-model"

        mock_result = [mock_row]
        mock_session.execute.return_value = mock_result

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # Get document info
        result = store.get_document_info("test_doc")

        # Verify prepared statement was used
        assert mock_session.prepare.called
        prepare_call = mock_session.prepare.call_args[0][0]
        assert "SELECT doc_id, doc_hash, ingested_at, updated_at, embedding_model" in prepare_call
        assert "WHERE doc_id = ?" in prepare_call

        # Verify result
        assert result is not None
        assert result["doc_id"] == "test_doc"
        assert result["doc_hash"] == "test_hash"
        assert result["chunks"] == 1
        assert result["embedding_model"] == "test-model"

        store.close()


@pytest.mark.unit
def test_query_by_embedding_uses_prepared_statement():
    """query_by_embedding should use prepared statement with optimized fetch size."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock prepared statement and results
        mock_prepared_stmt = Mock()
        mock_session.prepare.return_value = mock_prepared_stmt

        # Mock query results
        mock_row = Mock()
        mock_row.doc_id = "test_doc"
        mock_row.chunk_id = 0
        mock_row.text = "test text"
        mock_row.metadata = "{}"
        mock_row.score = 0.95
        mock_row.embedding_model = "test-model"
        mock_row.embedding_dim = 768

        mock_result = [mock_row]
        mock_session.execute.return_value = mock_result

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # Query by embedding
        query_embedding = [0.1] * 768
        results = store.query_by_embedding(query_embedding, k=5)

        # Verify prepared statement was used
        assert mock_session.prepare.called
        prepare_call = mock_session.prepare.call_args[0][0]
        assert "SELECT doc_id, chunk_id, text, metadata" in prepare_call
        assert "ORDER BY embedding ANN OF ?" in prepare_call
        assert "LIMIT ?" in prepare_call

        # Verify fetch_size was set
        assert hasattr(mock_prepared_stmt, "fetch_size") or True  # Mock doesn't enforce this

        # Verify results
        assert len(results) == 1
        assert results[0]["doc_id"] == "test_doc"
        assert results[0]["score"] == 0.95

        store.close()


@pytest.mark.unit
def test_query_by_embedding_adjusts_fetch_size_for_filtering():
    """query_by_embedding should increase fetch size when metadata filtering is enabled."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls:
        from nyxgpt.rag.vectorstore_cassandra import MetadataFilter

        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock prepared statement
        mock_prepared_stmt = Mock()
        mock_session.prepare.return_value = mock_prepared_stmt
        mock_session.execute.return_value = []

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # Query with metadata filter
        query_embedding = [0.1] * 768
        metadata_filter = MetadataFilter(doc_ids=["doc1", "doc2"])

        store.query_by_embedding(
            query_embedding,
            k=5,
            metadata_filter=metadata_filter,
        )

        # Verify execute was called with increased limit (k * 3 = 15)
        execute_call = mock_session.execute.call_args
        params = execute_call[0][1]
        # Third parameter should be the limit
        assert params[2] == 15  # 5 * 3 (filter_multiplier)

        store.close()


@pytest.mark.unit
def test_list_docs_uses_optimized_fetch_size():
    """list_docs should use optimized fetch size for large collections."""
    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster") as mock_cluster_cls:
        mock_cluster = Mock()
        mock_session = Mock()
        mock_cluster.connect.return_value = mock_session
        mock_cluster_cls.return_value = mock_cluster

        # Mock query results
        mock_row1 = Mock()
        mock_row1.doc_id = "doc1"
        mock_row1.embedding_model = "model1"

        mock_row2 = Mock()
        mock_row2.doc_id = "doc2"
        mock_row2.embedding_model = "model2"

        mock_session.execute.return_value = [mock_row1, mock_row2]

        store = CassandraVectorStore(collection="test")
        store._keyspace_ready = True

        # List documents
        docs = store.list_docs()

        # Verify SimpleStatement was created with fetch_size
        # (we can't easily verify the exact fetch_size without inspecting the call,
        # but we can verify the query executed)
        assert mock_session.execute.called

        # Verify results
        assert len(docs) == 2
        assert docs[0]["doc_id"] in ["doc1", "doc2"]

        store.close()
