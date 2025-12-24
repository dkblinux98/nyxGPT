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