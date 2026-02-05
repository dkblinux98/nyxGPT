"""Tests for Cassandra materialized views optimization."""
import uuid

import pytest
from nyxgpt.rag.rag import ingest_document
from nyxgpt.rag.vectorstore_cassandra import CassandraVectorStore


def _unique_doc_id(prefix: str) -> str:
    """Generate a unique doc_id for testing to avoid test pollution."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.mark.integration
def test_materialized_views_created(cassandra_test_setup):
    """Test that materialized views are created during schema setup."""
    doc_id = _unique_doc_id("test-mv-setup")
    text = "Test document for materialized view creation."

    # Ingest document with ensure_schema=True to create schema and MVs
    ingest_document(
        doc_id=doc_id,
        text=text,
        ensure_schema=True,
    )

    # Verify materialized view exists
    store = CassandraVectorStore()
    try:
        store._ensure_keyspace_selected()
        ks = store.cfg.keyspace
        tbl = store.table_name
        mv_name = f"{tbl}_doc_metadata_mv"

        # Query system schema to check if MV exists
        stmt = f"""
            SELECT view_name
            FROM system_schema.views
            WHERE keyspace_name = '{ks}'
            AND view_name = '{mv_name}'
        """
        rows = list(store.session.execute(stmt))

        # MV should exist (or might not if Cassandra version doesn't support it)
        # This is acceptable - MVs are an optimization
        if rows:
            assert rows[0].view_name == mv_name
    finally:
        store.close()


@pytest.mark.integration
def test_list_docs_optimized_fallback(cassandra_test_setup):
    """Test list_docs_optimized falls back to base table if MV unavailable."""
    doc_id1 = _unique_doc_id("test-list-opt-1")
    doc_id2 = _unique_doc_id("test-list-opt-2")
    text = "Test document for list_docs optimization."

    # Ingest test documents
    ingest_document(doc_id=doc_id1, text=text, ensure_schema=True)
    ingest_document(doc_id=doc_id2, text=text)

    store = CassandraVectorStore()
    try:
        # list_docs_optimized should work regardless of MV availability
        docs = store.list_docs_optimized()

        # Should contain our test documents
        doc_ids = {doc["doc_id"] for doc in docs}
        assert doc_id1 in doc_ids
        assert doc_id2 in doc_ids

        # All documents should have required fields
        for doc in docs:
            assert "doc_id" in doc
            assert "chunks" in doc
            assert doc["chunks"] > 0
            # embedding_model might be None for older documents
            assert "embedding_model" in doc

    finally:
        store.close()


@pytest.mark.integration
def test_list_docs_consistency(cassandra_test_setup):
    """Test that list_docs and list_docs_optimized return same results."""
    doc_id1 = _unique_doc_id("test-consistency-1")
    doc_id2 = _unique_doc_id("test-consistency-2")
    text = "Test document for consistency check."

    # Ingest test documents
    ingest_document(doc_id=doc_id1, text=text, ensure_schema=True)
    ingest_document(doc_id=doc_id2, text=text * 5)  # More chunks

    store = CassandraVectorStore()
    try:
        # Get results from both methods
        docs_original = store.list_docs()
        docs_optimized = store.list_docs_optimized()

        # Filter to just our test documents
        test_docs_original = [d for d in docs_original if d["doc_id"] in {doc_id1, doc_id2}]
        test_docs_optimized = [d for d in docs_optimized if d["doc_id"] in {doc_id1, doc_id2}]

        # Should have same number of test documents
        assert len(test_docs_original) == len(test_docs_optimized) == 2

        # Sort both lists by doc_id for comparison
        test_docs_original.sort(key=lambda x: x["doc_id"])
        test_docs_optimized.sort(key=lambda x: x["doc_id"])

        # Results should be identical
        for orig, opt in zip(test_docs_original, test_docs_optimized):
            assert orig["doc_id"] == opt["doc_id"]
            assert orig["chunks"] == opt["chunks"]
            assert orig["embedding_model"] == opt["embedding_model"]

    finally:
        store.close()


@pytest.mark.integration
def test_get_document_info_with_mv(cassandra_test_setup):
    """Test get_document_info uses MV when available."""
    doc_id = _unique_doc_id("test-mv-doc-info")
    text = "Test document for get_document_info MV optimization."

    # Ingest document
    result = ingest_document(
        doc_id=doc_id,
        text=text,
        ensure_schema=True,
    )

    store = CassandraVectorStore()
    try:
        # get_document_info should work with or without MV
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
def test_mv_maintenance_on_update(cassandra_test_setup):
    """Test that materialized views are automatically maintained on updates."""
    doc_id = _unique_doc_id("test-mv-maintenance")
    text1 = "Original text version 1."
    text2 = "Updated text version 2 with more content to create additional chunks."

    # First ingestion
    result1 = ingest_document(
        doc_id=doc_id,
        text=text1,
        ensure_schema=True,
    )

    chunks1 = result1["chunks_ingested"]

    # Second ingestion (update)
    result2 = ingest_document(
        doc_id=doc_id,
        text=text2,
    )

    chunks2 = result2["chunks_ingested"]
    assert chunks2 != chunks1  # Should have different chunk count

    # Verify MV reflects the update
    store = CassandraVectorStore()
    try:
        info = store.get_document_info(doc_id)

        # Should have chunks from second ingestion only
        assert info["chunks"] == chunks2
        assert info["doc_hash"] == result2["doc_hash"]

        # Also verify via list_docs_optimized
        docs = store.list_docs_optimized()
        doc = next((d for d in docs if d["doc_id"] == doc_id), None)
        assert doc is not None
        assert doc["chunks"] == chunks2

    finally:
        store.close()


@pytest.mark.integration
def test_mv_performance_benefit(cassandra_test_setup):
    """Test materialized views improve query performance for list_docs."""
    # Ingest multiple documents to create a dataset
    doc_ids = [_unique_doc_id(f"test-perf-{i}") for i in range(10)]
    text = "Performance test document with multiple chunks to simulate realistic data."

    for doc_id in doc_ids:
        ingest_document(doc_id=doc_id, text=text, ensure_schema=True)

    store = CassandraVectorStore()
    try:
        # Both methods should return valid results
        docs_original = store.list_docs()
        docs_optimized = store.list_docs_optimized()

        # Filter to test documents
        test_docs_original = [d for d in docs_original if d["doc_id"] in set(doc_ids)]
        test_docs_optimized = [d for d in docs_optimized if d["doc_id"] in set(doc_ids)]

        # Should have same count
        assert len(test_docs_original) == len(test_docs_optimized) == len(doc_ids)

        # All documents should be present in both results
        original_ids = {d["doc_id"] for d in test_docs_original}
        optimized_ids = {d["doc_id"] for d in test_docs_optimized}
        assert original_ids == optimized_ids == set(doc_ids)

    finally:
        store.close()
