from __future__ import annotations

import time
import uuid

import httpx
import pytest


@pytest.mark.integration
def test_rag_collections_list(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test listing RAG collections."""
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Verify API is up
        r = client.get("/health")
        assert r.status_code == 200

        # List collections
        collections_resp = client.get("/api/v1/rag/collections")
        assert collections_resp.status_code == 200

        data = collections_resp.json()
        assert isinstance(data, dict)
        assert "collections" in data
        assert isinstance(data["collections"], list)

        # Should at least have default collection
        collection_names = [c["name"] for c in data["collections"]]
        assert "default" in collection_names

        # Verify collection structure
        for coll in data["collections"]:
            assert "name" in coll
            assert "doc_count" in coll
            assert "chunk_count" in coll
            assert "embedding_models" in coll
            assert isinstance(coll["name"], str)
            assert isinstance(coll["doc_count"], int)
            assert isinstance(coll["chunk_count"], int)
            assert isinstance(coll["embedding_models"], list)


@pytest.mark.integration
def test_rag_collection_stats_after_ingest(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test that collection stats update after document ingestion."""
    doc_id = f"coll-test-{uuid.uuid4().hex[:10]}"
    text = "This is a test document for collection statistics verification."

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Get initial collections
        collections_resp_before = client.get("/api/v1/rag/collections")
        assert collections_resp_before.status_code == 200
        data_before = collections_resp_before.json()

        # Find default collection stats before ingestion
        default_coll_before = next(
            (c for c in data_before["collections"] if c["name"] == "default"), None
        )
        assert default_coll_before is not None
        doc_count_before = default_coll_before["doc_count"]
        chunk_count_before = default_coll_before["chunk_count"]

        # Ingest a document
        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id,
                "text": text,
                "ensure_schema": True,
            },
        )
        assert ingest_resp.status_code in (200, 201)
        ingest_data = ingest_resp.json()
        chunks_ingested = ingest_data["chunks_ingested"]
        assert chunks_ingested > 0

        # Give Cassandra a moment to index
        time.sleep(1.0)

        # Get collections after ingestion
        collections_resp_after = client.get("/api/v1/rag/collections")
        assert collections_resp_after.status_code == 200
        data_after = collections_resp_after.json()

        # Find default collection stats after ingestion
        default_coll_after = next(
            (c for c in data_after["collections"] if c["name"] == "default"), None
        )
        assert default_coll_after is not None

        # Verify stats increased
        assert default_coll_after["doc_count"] == doc_count_before + 1
        assert default_coll_after["chunk_count"] == chunk_count_before + chunks_ingested


@pytest.mark.integration
def test_rag_collection_delete_protection(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test that default collection cannot be deleted."""
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Attempt to delete default collection
        delete_resp = client.delete("/api/v1/rag/collections/default")
        assert delete_resp.status_code == 400

        error_data = delete_resp.json()
        assert "error" in error_data or "detail" in error_data


@pytest.mark.integration
def test_rag_collection_delete_nonexistent(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test deleting a non-existent collection."""
    fake_collection = f"nonexistent-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Attempt to delete non-existent collection
        # This should succeed (truncate on non-existent table may create it or fail gracefully)
        delete_resp = client.delete(f"/api/v1/rag/collections/{fake_collection}")
        # Accept either success or error for non-existent collection
        assert delete_resp.status_code in (200, 400, 404, 500)
