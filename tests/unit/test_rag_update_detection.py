"""Tests for RAG document update detection."""

import uuid

import pytest

from nyxgpt.rag.rag import compute_document_hash, ingest_document
from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore


def _unique_doc_id(prefix: str) -> str:
    """Generate a unique doc_id for testing to avoid test pollution."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def test_compute_document_hash():
    """Test document hash computation."""
    text1 = "Hello, world!"
    text2 = "Hello, world!"
    text3 = "Goodbye, world!"

    hash1 = compute_document_hash(text1)
    hash2 = compute_document_hash(text2)
    hash3 = compute_document_hash(text3)

    # Same content should produce same hash
    assert hash1 == hash2
    # Different content should produce different hash
    assert hash1 != hash3
    # Hash should be hex string (SHA-256 is 64 hex chars)
    assert len(hash1) == 64
    assert all(c in "0123456789abcdef" for c in hash1)


@pytest.mark.integration
def test_document_ingestion_with_hash(cassandra_test_setup):
    """Test document ingestion includes hash tracking."""
    doc_id = _unique_doc_id("test-doc-hash")
    text = "This is a test document for hash tracking."

    result = ingest_document(
        doc_id=doc_id,
        text=text,
        ensure_schema=True,
    )

    assert result["status"] == "ingested"
    assert result["chunks_ingested"] > 0
    assert result["doc_hash"] is not None
    assert len(result["doc_hash"]) == 64
    assert result["previous_hash"] is None


@pytest.mark.integration
def test_document_update_detection_unchanged(cassandra_test_setup):
    """Test that unchanged documents are skipped on re-ingestion."""
    doc_id = _unique_doc_id("test-doc-unchanged")
    text = "This document will not change."

    # First ingestion
    result1 = ingest_document(
        doc_id=doc_id,
        text=text,
        ensure_schema=True,
    )

    assert result1["status"] == "ingested"
    hash1 = result1["doc_hash"]

    # Second ingestion with same content
    result2 = ingest_document(
        doc_id=doc_id,
        text=text,
    )

    assert result2["status"] == "skipped"
    assert result2["chunks_ingested"] == 0
    assert result2["doc_hash"] == hash1
    assert result2["previous_hash"] == hash1


@pytest.mark.integration
def test_document_update_detection_changed(cassandra_test_setup):
    """Test that changed documents are re-ingested."""
    doc_id = _unique_doc_id("test-doc-changed")
    text1 = "This is the original text."
    text2 = "This is the updated text with different content."

    # First ingestion
    result1 = ingest_document(
        doc_id=doc_id,
        text=text1,
        ensure_schema=True,
    )

    assert result1["status"] == "ingested"
    hash1 = result1["doc_hash"]

    # Second ingestion with changed content
    result2 = ingest_document(
        doc_id=doc_id,
        text=text2,
    )

    assert result2["status"] == "updated"
    assert result2["chunks_ingested"] > 0
    hash2 = result2["doc_hash"]
    assert hash2 != hash1
    assert result2["previous_hash"] == hash1


@pytest.mark.integration
def test_document_update_force_reindex(cassandra_test_setup):
    """Test force_update flag bypasses hash check."""
    doc_id = _unique_doc_id("test-doc-force")
    text = "This document will be force re-indexed."

    # First ingestion
    result1 = ingest_document(
        doc_id=doc_id,
        text=text,
        ensure_schema=True,
    )

    assert result1["status"] == "ingested"
    hash1 = result1["doc_hash"]

    # Force re-ingestion with same content
    result2 = ingest_document(
        doc_id=doc_id,
        text=text,
        force_update=True,
    )

    assert result2["status"] == "updated"
    assert result2["chunks_ingested"] > 0
    assert result2["doc_hash"] == hash1  # Same hash
    assert result2["previous_hash"] == hash1


@pytest.mark.integration
def test_document_info_retrieval(cassandra_test_setup):
    """Test retrieving document version information."""
    doc_id = _unique_doc_id("test-doc-info")
    text = "This document has version info."

    # Ingest document
    result = ingest_document(
        doc_id=doc_id,
        text=text,
        ensure_schema=True,
    )

    # Get document info
    store = CassandraVectorStore()
    try:
        info = store.get_document_info(doc_id)

        assert info is not None
        assert info["doc_id"] == doc_id
        assert info["doc_hash"] == result["doc_hash"]
        assert info["chunks"] == result["chunks_ingested"]
        assert info["embedding_model"] is not None
        assert info["ingested_at"] is not None
        assert info["updated_at"] is not None
    finally:
        store.close()


@pytest.mark.integration
def test_document_info_not_found(cassandra_test_setup):
    """Test retrieving info for non-existent document."""
    store = CassandraVectorStore()
    try:
        info = store.get_document_info("non-existent-doc")
        assert info is None
    finally:
        store.close()


@pytest.mark.integration
def test_stale_chunks_deletion(cassandra_test_setup):
    """Test that old chunks are deleted when document is updated."""
    doc_id = _unique_doc_id("test-doc-stale-chunks")
    text1 = "Short text."
    text2 = "This is a much longer text that will create more chunks than the first version."

    # First ingestion - short text
    ingest_document(
        doc_id=doc_id,
        text=text1,
        ensure_schema=True,
    )

    # Second ingestion - longer text
    result2 = ingest_document(
        doc_id=doc_id,
        text=text2,
    )

    chunks2 = result2["chunks_ingested"]

    # Verify document was updated
    assert result2["status"] == "updated"

    # Get document info to verify final chunk count
    store = CassandraVectorStore()
    try:
        info = store.get_document_info(doc_id)
        # Should have chunks from second ingestion only
        assert info["chunks"] == chunks2
    finally:
        store.close()
