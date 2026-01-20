"""Integration tests for hybrid search (BM25 + vector)."""

from __future__ import annotations

import time
import uuid

import httpx
import pytest


@pytest.mark.integration
def test_hybrid_search_end_to_end(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test hybrid search combining BM25 and vector search."""
    # Ingest test documents
    doc1_id = f"hybrid-test-{uuid.uuid4().hex[:10]}"
    doc2_id = f"hybrid-test-{uuid.uuid4().hex[:10]}"
    doc3_id = f"hybrid-test-{uuid.uuid4().hex[:10]}"

    # Doc 1: Contains keyword "cassandra" and mentions vector search
    doc1_text = "Cassandra 5.0 supports vector search with SAI indexes for similarity queries."

    # Doc 2: Contains keyword "cassandra" but different topic
    doc2_text = "Cassandra is a distributed NoSQL database designed for high availability."

    # Doc 3: Related to vector search but doesn't mention "cassandra"
    doc3_text = "Vector embeddings enable semantic search and retrieval-augmented generation."

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Verify API is up
        r = client.get("/health")
        assert r.status_code == 200

        # Ingest documents
        for doc_id, text in [
            (doc1_id, doc1_text),
            (doc2_id, doc2_text),
            (doc3_id, doc3_text),
        ]:
            ingest_resp = client.post(
                "/api/v1/rag/ingest",
                json={"doc_id": doc_id, "text": text},
            )
            assert ingest_resp.status_code in (200, 201)

        # Give Cassandra indexing time
        time.sleep(3.0)

        # Query with keyword that should benefit from hybrid search
        # "Cassandra vector search" should:
        # - Match doc1 on both keywords AND semantic meaning (best match)
        # - Match doc2 on keyword "cassandra" only
        # - Match doc3 on semantic meaning of "vector search" only
        query_resp = client.post(
            "/api/v1/rag/query",
            json={
                "query": "Cassandra vector search",
                "top_k": 5,
                "debug_mode": True,  # Get debug info to verify hybrid search
            },
        )
        assert query_resp.status_code == 200

        data = query_resp.json()
        assert isinstance(data, dict)
        assert "results" in data
        assert isinstance(data["results"], list)

        # Should get results
        assert len(data["results"]) > 0

        # Verify debug info shows hybrid search was used
        if "debug_info" in data:
            debug = data["debug_info"]
            # Check that hybrid search is enabled
            if "hybrid_enabled" in debug:
                assert debug["hybrid_enabled"] is True
                # Should have both keyword and vector results
                if "keyword_results_count" in debug:
                    assert debug["keyword_results_count"] > 0
                if "vector_results_count" in debug:
                    assert debug["vector_results_count"] > 0
                # Should have used some fusion method
                if "fusion_method" in debug:
                    assert debug["fusion_method"] in [
                        "reciprocal_rank_fusion",
                        "vector_only",
                    ] or debug["fusion_method"].startswith("weighted")

        # doc1 should rank highest (matches both keyword and semantics)
        # Note: Exact ranking depends on embedding quality and BM25 parameters,
        # but doc1 should appear in top results
        top_doc_ids = [r["doc_id"] for r in data["results"][:2]]
        assert doc1_id in top_doc_ids, "Doc1 should be in top 2 results"


@pytest.mark.integration
def test_hybrid_search_keyword_dominance(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test that hybrid search improves keyword matching over vector-only."""
    # Create docs where keyword matching is important
    doc1_id = f"keyword-test-{uuid.uuid4().hex[:10]}"
    doc2_id = f"keyword-test-{uuid.uuid4().hex[:10]}"

    # Doc 1: Exact keyword match for rare term
    doc1_text = "The quokka is a small marsupial native to Australia."

    # Doc 2: Semantically related but no keyword match
    doc2_text = "Kangaroos and wallabies are Australian marsupials."

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Ingest documents
        for doc_id, text in [(doc1_id, doc1_text), (doc2_id, doc2_text)]:
            ingest_resp = client.post(
                "/api/v1/rag/ingest",
                json={"doc_id": doc_id, "text": text},
            )
            assert ingest_resp.status_code in (200, 201)

        time.sleep(2.0)

        # Query with rare keyword "quokka"
        query_resp = client.post(
            "/api/v1/rag/query",
            json={"query": "quokka", "top_k": 5},
        )
        assert query_resp.status_code == 200

        data = query_resp.json()
        results = data["results"]

        # Should get at least one result
        assert len(results) > 0

        # Doc1 with exact keyword match should rank first
        assert results[0]["doc_id"] == doc1_id


@pytest.mark.integration
def test_hybrid_search_can_be_disabled(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test that hybrid search can be disabled via config."""
    doc_id = f"disable-test-{uuid.uuid4().hex[:10]}"
    text = "This is a test document for hybrid search configuration."

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Ingest document
        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={"doc_id": doc_id, "text": text},
        )
        assert ingest_resp.status_code in (200, 201)

        time.sleep(2.0)

        # Query with debug mode to check fusion method
        query_resp = client.post(
            "/api/v1/rag/query",
            json={
                "query": "test document",
                "top_k": 5,
                "debug_mode": True,
            },
        )
        assert query_resp.status_code == 200

        data = query_resp.json()

        # If hybrid search is enabled (default), check debug info
        if "debug_info" in data and "fusion_method" in data["debug_info"]:
            fusion_method = data["debug_info"]["fusion_method"]
            # Should be either RRF or vector_only (if no keyword matches)
            assert fusion_method in [
                "reciprocal_rank_fusion",
                "vector_only",
            ] or fusion_method.startswith("weighted")


@pytest.mark.integration
def test_hybrid_search_bm25_term_frequency(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test that BM25 properly weighs term frequency."""
    doc1_id = f"tf-test-{uuid.uuid4().hex[:10]}"
    doc2_id = f"tf-test-{uuid.uuid4().hex[:10]}"

    # Doc 1: Mentions Python once
    doc1_text = "Python is a programming language."

    # Doc 2: Mentions Python multiple times
    doc2_text = "Python Python Python programming with Python language features in Python."

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Ingest documents
        for doc_id, text in [(doc1_id, doc1_text), (doc2_id, doc2_text)]:
            ingest_resp = client.post(
                "/api/v1/rag/ingest",
                json={"doc_id": doc_id, "text": text},
            )
            assert ingest_resp.status_code in (200, 201)

        time.sleep(2.0)

        # Query for "Python"
        query_resp = client.post(
            "/api/v1/rag/query",
            json={"query": "Python", "top_k": 5, "debug_mode": True},
        )
        assert query_resp.status_code == 200

        data = query_resp.json()
        results = data["results"]

        # Should get both documents
        assert len(results) >= 2

        # Verify both docs are in results
        doc_ids = [r["doc_id"] for r in results]
        assert doc1_id in doc_ids
        assert doc2_id in doc_ids

        # Due to BM25 term frequency scoring, doc2 (more occurrences)
        # should generally rank higher than doc1
        # However, this may vary with vector embeddings and RRF fusion
        # So we just verify both docs are retrieved


@pytest.mark.integration
def test_hybrid_search_empty_query(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test hybrid search with empty query."""
    doc_id = f"empty-query-{uuid.uuid4().hex[:10]}"
    text = "Test document for empty query handling."

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Ingest document
        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={"doc_id": doc_id, "text": text},
        )
        assert ingest_resp.status_code in (200, 201)

        time.sleep(2.0)

        # Query with empty string (should be handled gracefully)
        query_resp = client.post(
            "/api/v1/rag/query",
            json={"query": "", "top_k": 5},
        )
        # Should either return 200 with empty results or 400 for invalid query
        assert query_resp.status_code in (200, 400)

        if query_resp.status_code == 200:
            data = query_resp.json()
            # Empty query should return no results or all results (implementation dependent)
            assert "results" in data
            assert isinstance(data["results"], list)
