from __future__ import annotations

import time
import uuid

import httpx
import pytest


@pytest.mark.integration
def test_rag_documents_list_endpoint(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test that the /api/v1/rag/documents endpoint returns document list."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Test health first
        r = client.get("/health")
        assert r.status_code == 200

        # Ingest a test document first
        doc_id = f"filter-test-{uuid.uuid4().hex[:10]}"
        text = "This is a test document for RAG filtering functionality."

        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id,
                "text": text,
                "metadata": {"filename": "test_doc.txt", "tags": ["test", "filtering"]},
            },
        )
        assert ingest_resp.status_code in (200, 201)

        # Wait for indexing
        time.sleep(2.0)

        # Fetch document list
        list_resp = client.get("/api/v1/rag/documents")
        assert list_resp.status_code == 200

        data = list_resp.json()
        assert isinstance(data, dict)
        assert "documents" in data
        assert isinstance(data["documents"], list)
        assert len(data["documents"]) > 0

        # Verify document structure
        doc = next((d for d in data["documents"] if d["doc_id"] == doc_id), None)
        assert doc is not None
        assert "doc_id" in doc
        assert "chunks" in doc
        assert isinstance(doc["chunks"], int)


@pytest.mark.integration
def test_chat_stream_with_rag_filters(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test that chat stream endpoint accepts and forwards rag_filters."""
    session_name = f"filter-session-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Ingest two test documents
        doc_id_1 = f"doc-1-{uuid.uuid4().hex[:10]}"
        doc_id_2 = f"doc-2-{uuid.uuid4().hex[:10]}"

        client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id_1,
                "text": "Document 1 contains information about Python programming.",
                "metadata": {"filename": "python.txt"},
            },
        )

        client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id_2,
                "text": "Document 2 contains information about JavaScript programming.",
                "metadata": {"filename": "javascript.txt"},
            },
        )

        time.sleep(2.0)

        # Test chat stream with rag_filters
        chat_payload = {
            "session": session_name,
            "prompt": "Tell me about programming",
            "rag_enabled": True,
            "rag_filters": {
                "doc_ids": [doc_id_1],  # Only search doc_id_1
                "filename": "python",  # Filter by filename
            },
        }

        # Chat stream endpoint should accept the request without errors
        stream_resp = client.post(
            "/api/v1/chat/stream",
            json=chat_payload,
            timeout=30.0,
        )

        # Should return 200 and start streaming
        assert stream_resp.status_code == 200
        assert "text/plain" in stream_resp.headers.get("content-type", "")

        # Read a bit of the stream to verify it works
        content = b""
        for chunk in stream_resp.iter_bytes():
            content += chunk
            if len(content) > 100:  # Read enough to verify streaming works
                break

        # Should have received some content
        assert len(content) > 0


@pytest.mark.integration
def test_rag_query_with_metadata_filters(
    api_base_url: str, require_cassandra: None
) -> None:
    """Test RAG query endpoint with metadata filters."""
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Ingest documents with metadata
        doc_id_filtered = f"filtered-{uuid.uuid4().hex[:10]}"
        doc_id_other = f"other-{uuid.uuid4().hex[:10]}"

        client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id_filtered,
                "text": "This document should be found by the filter.",
                "metadata": {"filename": "important.txt", "tags": ["important"]},
            },
        )

        client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id_other,
                "text": "This document should be excluded by the filter.",
                "metadata": {"filename": "other.txt", "tags": ["other"]},
            },
        )

        time.sleep(2.0)

        # Query with doc_ids filter
        query_resp = client.post(
            "/api/v1/rag/query",
            json={
                "query": "document",
                "top_k": 10,
                "doc_ids": [doc_id_filtered],
            },
        )

        assert query_resp.status_code == 200
        data = query_resp.json()
        assert "results" in data
        results = data["results"]

        # All results should be from the filtered doc
        for result in results:
            assert result["doc_id"] == doc_id_filtered
