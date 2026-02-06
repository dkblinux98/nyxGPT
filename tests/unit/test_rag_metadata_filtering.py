"""Tests for RAG metadata filtering functionality."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from nyxgpt.rag.vectorstore_cassandra import MetadataFilter


@pytest.mark.unit
def test_metadata_filter_dataclass():
    """Test MetadataFilter dataclass creation."""
    # Test with all fields
    filter_all = MetadataFilter(
        doc_ids=["doc1", "doc2"],
        filename="test.txt",
        tags=["tag1", "tag2"],
        date_from=datetime(2024, 1, 1),
        date_to=datetime(2024, 12, 31),
    )
    assert filter_all.doc_ids == ["doc1", "doc2"]
    assert filter_all.filename == "test.txt"
    assert filter_all.tags == ["tag1", "tag2"]
    assert filter_all.date_from == datetime(2024, 1, 1)
    assert filter_all.date_to == datetime(2024, 12, 31)

    # Test with no fields (all None)
    filter_none = MetadataFilter()
    assert filter_none.doc_ids is None
    assert filter_none.filename is None
    assert filter_none.tags is None
    assert filter_none.date_from is None
    assert filter_none.date_to is None


@pytest.mark.unit
def test_query_by_embedding_filter_by_doc_ids():
    """Test filtering results by doc_ids."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    # Mock the Cassandra session
    mock_session = Mock()
    mock_cluster = Mock()

    # Create mock results
    mock_row1 = Mock()
    mock_row1.doc_id = "doc1"
    mock_row1.chunk_id = 0
    mock_row1.text = "Content from doc1"
    mock_row1.metadata = '{"filename": "file1.txt"}'
    mock_row1.score = 0.9
    mock_row1.embedding_model = "test-model"
    mock_row1.embedding_dim = 768
    mock_row1.ingested_at = datetime(2024, 1, 1)

    mock_row2 = Mock()
    mock_row2.doc_id = "doc2"
    mock_row2.chunk_id = 0
    mock_row2.text = "Content from doc2"
    mock_row2.metadata = '{"filename": "file2.txt"}'
    mock_row2.score = 0.8
    mock_row2.embedding_model = "test-model"
    mock_row2.embedding_dim = 768
    mock_row2.ingested_at = datetime(2024, 1, 2)

    mock_session.execute.return_value = [mock_row1, mock_row2]
    mock_session.prepare.return_value = Mock()

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store.session = mock_session
        store._keyspace_ready = True

        # Filter for only doc1
        metadata_filter = MetadataFilter(doc_ids=["doc1"])
        results = store.query_by_embedding(
            [0.1] * 768,
            k=5,
            metadata_filter=metadata_filter,
        )

        # Should only return doc1
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc1"


@pytest.mark.unit
def test_query_by_embedding_filter_by_filename():
    """Test filtering results by filename (partial match, case-insensitive)."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    mock_session = Mock()
    mock_cluster = Mock()

    mock_row1 = Mock()
    mock_row1.doc_id = "doc1"
    mock_row1.chunk_id = 0
    mock_row1.text = "Content"
    mock_row1.metadata = '{"filename": "MyGPT_Notes.txt"}'
    mock_row1.score = 0.9
    mock_row1.embedding_model = "test-model"
    mock_row1.embedding_dim = 768
    mock_row1.ingested_at = datetime(2024, 1, 1)

    mock_row2 = Mock()
    mock_row2.doc_id = "doc2"
    mock_row2.chunk_id = 0
    mock_row2.text = "Content"
    mock_row2.metadata = '{"filename": "other_file.txt"}'
    mock_row2.score = 0.8
    mock_row2.embedding_model = "test-model"
    mock_row2.embedding_dim = 768
    mock_row2.ingested_at = datetime(2024, 1, 2)

    mock_session.execute.return_value = [mock_row1, mock_row2]

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store.session = mock_session
        store._keyspace_ready = True

        # Filter for filename containing "mygpt" (case-insensitive)
        metadata_filter = MetadataFilter(filename="mygpt")
        results = store.query_by_embedding(
            [0.1] * 768,
            k=5,
            metadata_filter=metadata_filter,
        )

        # Should only return the file with "MyGPT" in name
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc1"
        assert "MyGPT" in results[0]["metadata"]["filename"]


@pytest.mark.unit
def test_query_by_embedding_filter_by_tags():
    """Test filtering results by tags (must have ALL tags)."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    mock_session = Mock()
    mock_cluster = Mock()

    mock_row1 = Mock()
    mock_row1.doc_id = "doc1"
    mock_row1.chunk_id = 0
    mock_row1.text = "Content"
    mock_row1.metadata = '{"tags": ["python", "tutorial", "beginner"]}'
    mock_row1.score = 0.9
    mock_row1.embedding_model = "test-model"
    mock_row1.embedding_dim = 768
    mock_row1.ingested_at = datetime(2024, 1, 1)

    mock_row2 = Mock()
    mock_row2.doc_id = "doc2"
    mock_row2.chunk_id = 0
    mock_row2.text = "Content"
    mock_row2.metadata = '{"tags": ["python", "advanced"]}'
    mock_row2.score = 0.8
    mock_row2.embedding_model = "test-model"
    mock_row2.embedding_dim = 768
    mock_row2.ingested_at = datetime(2024, 1, 2)

    mock_session.execute.return_value = [mock_row1, mock_row2]

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store.session = mock_session
        store._keyspace_ready = True

        # Filter for docs with both "python" AND "tutorial" tags
        metadata_filter = MetadataFilter(tags=["python", "tutorial"])
        results = store.query_by_embedding(
            [0.1] * 768,
            k=5,
            metadata_filter=metadata_filter,
        )

        # Should only return doc1 which has both tags
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc1"
        assert "python" in results[0]["metadata"]["tags"]
        assert "tutorial" in results[0]["metadata"]["tags"]


@pytest.mark.unit
def test_query_by_embedding_filter_by_date_range():
    """Test filtering results by date range."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    mock_session = Mock()
    mock_cluster = Mock()

    mock_row1 = Mock()
    mock_row1.doc_id = "doc1"
    mock_row1.chunk_id = 0
    mock_row1.text = "Old content"
    mock_row1.metadata = "{}"
    mock_row1.score = 0.9
    mock_row1.embedding_model = "test-model"
    mock_row1.embedding_dim = 768
    mock_row1.ingested_at = datetime(2023, 6, 1)

    mock_row2 = Mock()
    mock_row2.doc_id = "doc2"
    mock_row2.chunk_id = 0
    mock_row2.text = "Recent content"
    mock_row2.metadata = "{}"
    mock_row2.score = 0.8
    mock_row2.embedding_model = "test-model"
    mock_row2.embedding_dim = 768
    mock_row2.ingested_at = datetime(2024, 6, 1)

    mock_session.execute.return_value = [mock_row1, mock_row2]

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store.session = mock_session
        store._keyspace_ready = True

        # Filter for docs ingested in 2024
        metadata_filter = MetadataFilter(
            date_from=datetime(2024, 1, 1),
            date_to=datetime(2024, 12, 31),
        )
        results = store.query_by_embedding(
            [0.1] * 768,
            k=5,
            metadata_filter=metadata_filter,
        )

        # Should only return doc2 from 2024
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc2"


@pytest.mark.unit
def test_query_by_embedding_combined_filters():
    """Test combining multiple metadata filters."""
    from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore

    mock_session = Mock()
    mock_cluster = Mock()

    mock_row1 = Mock()
    mock_row1.doc_id = "doc1"
    mock_row1.chunk_id = 0
    mock_row1.text = "Content"
    mock_row1.metadata = '{"filename": "notes.txt", "tags": ["python", "tutorial"]}'
    mock_row1.score = 0.9
    mock_row1.embedding_model = "test-model"
    mock_row1.embedding_dim = 768
    mock_row1.ingested_at = datetime(2024, 6, 1)

    mock_row2 = Mock()
    mock_row2.doc_id = "doc2"
    mock_row2.chunk_id = 0
    mock_row2.text = "Content"
    mock_row2.metadata = '{"filename": "guide.txt", "tags": ["python"]}'
    mock_row2.score = 0.8
    mock_row2.embedding_model = "test-model"
    mock_row2.embedding_dim = 768
    mock_row2.ingested_at = datetime(2024, 6, 1)

    mock_session.execute.return_value = [mock_row1, mock_row2]

    with patch("nyxgpt.rag.vectorstore_cassandra.Cluster", return_value=mock_cluster):
        store = CassandraVectorStore()
        store.session = mock_session
        store._keyspace_ready = True

        # Combine filename AND tags filters
        metadata_filter = MetadataFilter(
            filename="notes",
            tags=["python", "tutorial"],
        )
        results = store.query_by_embedding(
            [0.1] * 768,
            k=5,
            metadata_filter=metadata_filter,
        )

        # Should only return doc1 which matches both filters
        assert len(results) == 1
        assert results[0]["doc_id"] == "doc1"


@pytest.mark.unit
def test_retrieve_context_with_metadata_filter():
    """Test that retrieve_context passes metadata_filter to vector store."""
    from nyxgpt.rag.rag import retrieve_context
    from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

    # This test verifies the integration - actual filtering is tested above
    # We just verify the parameter is passed through correctly
    with patch("nyxgpt.rag.rag.CassandraVectorStore") as mock_store_class:
        mock_store = Mock()

        # Mock to return tuple when collect_metrics=True, else empty list
        def mock_query_by_embedding(*args, **kwargs):
            if kwargs.get("collect_metrics"):
                metrics = VectorSearchDebugMetrics(
                    raw_results_count=0,
                    score_min=None,
                    score_max=None,
                    score_mean=None,
                    vector_search_time_ms=0.0,
                )
                return ([], metrics)
            return []

        mock_store.query_by_embedding.side_effect = mock_query_by_embedding
        mock_store.list_docs.return_value = []
        mock_store_class.return_value = mock_store

        with patch("nyxgpt.rag.rag.embed_text", return_value=[0.1] * 768):
            metadata_filter = MetadataFilter(filename="test.txt")
            _ = retrieve_context(
                "test query",
                top_k=5,
                metadata_filter=metadata_filter,
                debug_mode=False,  # Explicitly disable debug mode to avoid tuple unpacking
            )

            # Verify metadata_filter was passed to query_by_embedding
            mock_store.query_by_embedding.assert_called()
            call_kwargs = mock_store.query_by_embedding.call_args[1]
            assert "metadata_filter" in call_kwargs
            assert call_kwargs["metadata_filter"] == metadata_filter
