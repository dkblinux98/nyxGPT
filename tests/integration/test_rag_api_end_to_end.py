from __future__ import annotations

import time
import uuid

import httpx
import pytest


@pytest.mark.integration
def test_rag_api_ingest_and_query(api_base_url: str, require_ollama: None, require_cassandra: None) -> None:
    doc_id = f"itest-{uuid.uuid4().hex[:10]}"
    text = (
        "Cassandra 5.0 supports vector search with SAI indexes. "
        "This sentence is used for myGPT integration testing."
    )

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Optional: verify the API is up before we do slower work
        r = client.get("/health")
        assert r.status_code == 200

        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id,
                "text": text,
            },
        )
        assert ingest_resp.status_code in (200, 201)

        # Give Cassandra indexing a moment (SAI / vector index)
        time.sleep(2.0)

        query_resp = client.post(
            "/api/v1/rag/query",
            json={
                "query": "What does Cassandra support for vector search?",
                "top_k": 3,
            },
        )
        assert query_resp.status_code == 200

        data = query_resp.json()
        assert isinstance(data, dict)
        assert "results" in data
        assert isinstance(data["results"], list)
        assert len(data["results"]) > 0

        top = data["results"][0]
        assert isinstance(top, dict)
        assert "text" in top
        assert isinstance(top["text"], str)
        assert len(top["text"]) > 0
        assert "doc_id" in top
        assert "chunk_id" in top
        assert "score" in top


@pytest.mark.integration
def test_session_rag_enable_disable(api_base_url: str) -> None:
    """Test per-session RAG enable/disable endpoints."""
    session_name = f"rag-test-{uuid.uuid4().hex[:8]}"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # 1. Get initial metadata (creates session)
        meta_resp = client.get(f"/api/v1/sessions/{session_name}/metadata")
        assert meta_resp.status_code == 200
        meta = meta_resp.json()
        assert "rag_enabled" in meta
        # Default should be False (or inherited from config)
        assert isinstance(meta["rag_enabled"], bool)

        # 2. Enable RAG
        enable_resp = client.post(f"/api/v1/sessions/{session_name}/rag/enable")
        assert enable_resp.status_code == 200
        enable_data = enable_resp.json()
        assert enable_data["session"] == session_name
        assert enable_data["rag_enabled"] is True

        # 3. Verify RAG is enabled in metadata
        meta_resp2 = client.get(f"/api/v1/sessions/{session_name}/metadata")
        assert meta_resp2.status_code == 200
        meta2 = meta_resp2.json()
        assert meta2["rag_enabled"] is True

        # 4. Disable RAG
        disable_resp = client.post(f"/api/v1/sessions/{session_name}/rag/disable")
        assert disable_resp.status_code == 200
        disable_data = disable_resp.json()
        assert disable_data["session"] == session_name
        assert disable_data["rag_enabled"] is False

        # 5. Verify RAG is disabled in metadata
        meta_resp3 = client.get(f"/api/v1/sessions/{session_name}/metadata")
        assert meta_resp3.status_code == 200
        meta3 = meta_resp3.json()
        assert meta3["rag_enabled"] is False


@pytest.mark.integration
def test_rag_upload_text_file(api_base_url: str, require_ollama: None, require_cassandra: None, tmp_path) -> None:
    """Test RAG file upload endpoint with .txt file."""
    # Create a test text file
    test_file = tmp_path / "test_upload.txt"
    test_content = "This is a test document for RAG upload testing. It contains important information."
    test_file.write_text(test_content)

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Upload the file
        with open(test_file, "rb") as f:
            files = {"file": ("test_upload.txt", f, "text/plain")}
            upload_resp = client.post("/api/v1/rag/upload", files=files)

        assert upload_resp.status_code == 200
        upload_data = upload_resp.json()
        assert "doc_id" in upload_data
        assert "chunks_ingested" in upload_data
        assert upload_data["chunks_ingested"] > 0

        # Give Cassandra indexing a moment
        time.sleep(2.0)

        # Verify we can query the uploaded content
        query_resp = client.post(
            "/api/v1/rag/query",
            json={"query": "test document", "top_k": 5}
        )
        assert query_resp.status_code == 200
        results = query_resp.json()["results"]
        assert len(results) > 0


@pytest.mark.integration
def test_rag_upload_markdown_file(api_base_url: str, require_ollama: None, require_cassandra: None, tmp_path) -> None:
    """Test RAG file upload endpoint with .md file with frontmatter."""
    # Create a test markdown file with frontmatter
    test_file = tmp_path / "test.md"
    markdown_content = """---
title: Test Document
author: Integration Test
tags: [test, markdown]
---

# Main Heading

This is a test markdown document with **bold** and *italic* text.

## Section 1

Some content here with `code` inline.

```python
def hello():
    print("Hello, world!")
```

## Section 2

More content for testing RAG ingestion.
"""
    test_file.write_text(markdown_content)

    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        # Upload the markdown file
        with open(test_file, "rb") as f:
            files = {"file": ("test.md", f, "text/markdown")}
            upload_resp = client.post("/api/v1/rag/upload", files=files)

        assert upload_resp.status_code == 200
        upload_data = upload_resp.json()
        assert "doc_id" in upload_data
        assert "chunks_ingested" in upload_data
        assert upload_data["chunks_ingested"] > 0

        # Give Cassandra indexing a moment
        time.sleep(2.0)

        # Verify we can query the uploaded content
        query_resp = client.post(
            "/api/v1/rag/query",
            json={"query": "markdown document", "top_k": 5}
        )
        assert query_resp.status_code == 200
        results = query_resp.json()["results"]
        assert len(results) > 0


@pytest.mark.integration
def test_rag_upload_invalid_file_type(api_base_url: str, tmp_path) -> None:
    """Test that uploading unsupported file types is rejected."""
    # Create a test file with unsupported extension
    test_file = tmp_path / "test.exe"
    test_file.write_bytes(b"fake executable content")

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        # Try to upload unsupported file type
        with open(test_file, "rb") as f:
            files = {"file": ("test.exe", f, "application/octet-stream")}
            upload_resp = client.post("/api/v1/rag/upload", files=files)

        # Should reject with 400
        assert upload_resp.status_code == 400
        error_data = upload_resp.json()
        assert "error" in error_data
        assert "not supported" in error_data["error"]["message"].lower()