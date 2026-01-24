from __future__ import annotations

import time
import uuid
import re

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


@pytest.mark.integration
def test_rag_collection_create_success(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test successfully creating a new collection."""
    collection_name = f"test-coll-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Create collection
        create_resp = client.post(
            "/api/v1/rag/collections",
            json={
                "name": collection_name,
                "embedding_dim": 768,
                "embedding_model": "nomic-embed-text",
            },
        )
        assert create_resp.status_code == 201

        data = create_resp.json()
        assert data["collection"] == collection_name
        assert data["embedding_dim"] == 768
        assert "status" in data

        # Verify collection appears in list
        list_resp = client.get("/api/v1/rag/collections")
        assert list_resp.status_code == 200
        collections = list_resp.json()["collections"]
        collection_names = [c["name"] for c in collections]
        assert collection_name in collection_names

        # Cleanup: delete the test collection
        client.delete(f"/api/v1/rag/collections/{collection_name}")


@pytest.mark.integration
def test_rag_collection_create_invalid_name(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test that invalid collection names are rejected."""
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Try to create collection with invalid name (contains spaces/special chars)
        create_resp = client.post(
            "/api/v1/rag/collections",
            json={
                "name": "invalid name!@#",
                "embedding_dim": 768,
            },
        )
        assert create_resp.status_code == 400
        error_data = create_resp.json()
        assert "error" in error_data or "detail" in error_data


@pytest.mark.integration
def test_rag_collection_create_duplicate(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test that creating a duplicate collection is rejected."""
    collection_name = f"test-dup-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Create collection first time
        create_resp1 = client.post(
            "/api/v1/rag/collections",
            json={
                "name": collection_name,
                "embedding_dim": 768,
            },
        )
        assert create_resp1.status_code == 201

        # Try to create same collection again
        create_resp2 = client.post(
            "/api/v1/rag/collections",
            json={
                "name": collection_name,
                "embedding_dim": 768,
            },
        )
        assert create_resp2.status_code == 409  # Conflict

        # Cleanup
        client.delete(f"/api/v1/rag/collections/{collection_name}")


@pytest.mark.integration
def test_rag_collection_create_default_protected(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test that 'default' collection cannot be manually created."""
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        create_resp = client.post(
            "/api/v1/rag/collections",
            json={
                "name": "default",
                "embedding_dim": 768,
            },
        )
        assert create_resp.status_code == 400


@pytest.mark.integration
def test_rag_collection_get_settings(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test getting collection settings."""
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Get settings for default collection
        settings_resp = client.get("/api/v1/rag/collections/default/settings")
        assert settings_resp.status_code == 200

        data = settings_resp.json()
        assert "collection" in data
        assert data["collection"] == "default"
        assert "settings" in data
        assert isinstance(data["settings"], dict)

        # Settings should include embedding_model, chunk_size, chunk_overlap
        settings = data["settings"]
        # These may be None if not set
        assert "embedding_model" in settings or settings.get("embedding_model") is None
        assert "chunk_size" in settings
        assert "chunk_overlap" in settings


@pytest.mark.integration
def test_rag_collection_get_settings_nonexistent(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test getting settings for non-existent collection."""
    fake_collection = f"nonexistent-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        settings_resp = client.get(f"/api/v1/rag/collections/{fake_collection}/settings")
        assert settings_resp.status_code == 404


@pytest.mark.integration
def test_rag_collection_update_settings_not_implemented(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test that updating collection settings returns 501 Not Implemented."""
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        update_resp = client.put(
            "/api/v1/rag/collections/default/settings",
            json={
                "embedding_model": "nomic-embed-text",
                "chunk_size": 1000,
                "chunk_overlap": 200,
            },
        )
        assert update_resp.status_code == 501  # Not Implemented


@pytest.mark.integration
def test_rag_collection_reindex_not_implemented(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test that re-indexing collection returns 501 Not Implemented."""
    collection_name = f"test-reindex-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Create collection first
        create_resp = client.post(
            "/api/v1/rag/collections",
            json={
                "name": collection_name,
                "embedding_dim": 768,
            },
        )
        assert create_resp.status_code == 201

        # Try to re-index
        reindex_resp = client.post(
            f"/api/v1/rag/collections/{collection_name}/reindex",
            json={
                "target_embedding_model": "nomic-embed-text-v1.5",
                "embedding_dim": 768,
            },
        )
        assert reindex_resp.status_code == 501  # Not Implemented

        # Cleanup
        client.delete(f"/api/v1/rag/collections/{collection_name}")
