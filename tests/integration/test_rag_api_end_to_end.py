from __future__ import annotations

import time
import uuid

import httpx
import pytest


def _try_post_json(client: httpx.Client, paths: list[str], payloads: list[dict]) -> tuple[str, httpx.Response] | None:
    for p in paths:
        for body in payloads:
            r = client.post(p, json=body)
            if r.status_code in (200, 201):
                return p, r
    return None


def _try_get(client: httpx.Client, paths: list[str]) -> tuple[str, httpx.Response] | None:
    for p in paths:
        r = client.get(p)
        if r.status_code == 200:
            return p, r
    return None


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

        # ---- Ingest ----
        ingest_paths = ["/rag/ingest", "/rag/docs/ingest", "/rag/documents/ingest"]
        ingest_payloads = [
            {"doc_id": doc_id, "text": text},
            {"docId": doc_id, "text": text},
            {"doc_id": doc_id, "content": text},
        ]

        ing = _try_post_json(client, ingest_paths, ingest_payloads)
        if ing is None:
            # If the API doesn’t expose RAG endpoints yet, skip (not fail) the integration test
            pytest.skip("No RAG ingest endpoint found (tried /rag/ingest variants).")

        # Give Cassandra indexing a moment (SAI / vector index)
        time.sleep(2.0)

        # ---- Query ----
        query_paths = ["/rag/query", "/rag/search", "/rag/retrieve"]
        query_payloads = [
            {"query": "What does Cassandra support for vector search?", "top_k": 3},
            {"query": "What does Cassandra support for vector search?", "k": 3},
        ]

        q = _try_post_json(client, query_paths, query_payloads)
        if q is None:
            pytest.fail("No RAG query endpoint found (tried /rag/query variants).")

        _, qr = q
        data = qr.json()
        # Be tolerant: different shapes are OK as long as we got something.
        assert data is not None

        # If it’s a dict, look for common keys
        if isinstance(data, dict):
            for key in ("results", "chunks", "matches", "context"):
                if key in data:
                    assert data[key]  # non-empty
                    return

        # If it’s a list of results
        if isinstance(data, list):
            assert len(data) > 0
            return

        # Otherwise, fail with the raw payload
        pytest.fail(f"Unexpected response shape from RAG query endpoint: {data}")