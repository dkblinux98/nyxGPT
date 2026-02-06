"""
Integration tests for RAG playground functionality.

Tests the RAG query and metrics endpoints that power the playground UI.
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest


@pytest.mark.integration
def test_rag_playground_query_with_debug(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test RAG query with debug mode enabled for playground."""
    doc_id = f"playground-test-{uuid.uuid4().hex[:10]}"
    text = (
        "The RAG playground provides an interactive interface for testing queries, "
        "adjusting parameters, comparing results, and performing A/B testing. "
        "It includes real-time debugging and performance metrics visualization."
    )

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Verify API is up
        r = client.get("/health")
        assert r.status_code == 200

        # Ingest test document
        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id,
                "text": text,
            },
        )
        assert ingest_resp.status_code in (200, 201)

        # Give Cassandra indexing time
        time.sleep(2.0)

        # Query with debug mode enabled
        query_resp = client.post(
            "/api/v1/rag/query",
            json={
                "query": "What features does the RAG playground provide?",
                "top_k": 5,
                "debug_mode": True,
            },
        )
        assert query_resp.status_code == 200

        data = query_resp.json()
        assert isinstance(data, dict)
        assert "results" in data
        assert isinstance(data["results"], list)
        assert len(data["results"]) > 0

        # Verify debug info is present
        assert "debug_info" in data
        debug_info = data["debug_info"]
        assert debug_info is not None
        assert isinstance(debug_info, dict)

        # Verify debug info contains expected fields
        assert "total_time_ms" in debug_info
        assert isinstance(debug_info["total_time_ms"], (int, float))
        assert debug_info["total_time_ms"] > 0

        assert "original_query" in debug_info
        assert debug_info["original_query"] == "What features does the RAG playground provide?"

        assert "raw_results_count" in debug_info
        assert isinstance(debug_info["raw_results_count"], int)

        assert "after_min_score_filter" in debug_info
        assert "after_dedupe_filter" in debug_info
        assert "after_max_chunks_filter" in debug_info


@pytest.mark.integration
def test_rag_playground_metrics_query(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test RAG query with full evaluation metrics for playground."""
    doc_id = f"playground-metrics-{uuid.uuid4().hex[:10]}"
    text = (
        "Query performance metrics include retrieval accuracy, latency breakdowns, "
        "and hit rate analysis. The playground displays these metrics in real-time "
        "to help optimize RAG system configuration and query strategies."
    )

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Verify API is up
        r = client.get("/health")
        assert r.status_code == 200

        # Ingest test document
        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id,
                "text": text,
            },
        )
        assert ingest_resp.status_code in (200, 201)

        # Give Cassandra indexing time
        time.sleep(2.0)

        # Query with metrics collection enabled
        query_resp = client.post(
            "/api/v1/rag/metrics/query",
            json={
                "query": "What metrics are available in the playground?",
                "top_k": 5,
                "collect_metrics": True,
            },
        )
        assert query_resp.status_code == 200

        data = query_resp.json()
        assert isinstance(data, dict)
        assert "results" in data
        assert isinstance(data["results"], list)

        # Verify evaluation metrics are present
        assert "evaluation_metrics" in data
        metrics = data["evaluation_metrics"]
        assert metrics is not None
        assert isinstance(metrics, dict)

        # Verify retrieval accuracy metrics
        assert "retrieval_accuracy" in metrics
        accuracy = metrics["retrieval_accuracy"]
        assert isinstance(accuracy, dict)
        assert "results_returned" in accuracy
        assert "unique_docs" in accuracy
        assert "score_distribution" in accuracy

        score_dist = accuracy["score_distribution"]
        assert "min" in score_dist
        assert "max" in score_dist
        assert "mean" in score_dist
        assert "median" in score_dist

        # Verify latency metrics
        assert "latency" in metrics
        latency = metrics["latency"]
        assert isinstance(latency, dict)
        assert "total_time_ms" in latency
        assert isinstance(latency["total_time_ms"], (int, float))
        assert latency["total_time_ms"] > 0

        # Verify hit rate metrics
        assert "hit_rate" in metrics
        hit_rate = metrics["hit_rate"]
        assert isinstance(hit_rate, dict)
        assert "success_rate" in hit_rate
        assert isinstance(hit_rate["success_rate"], (int, float))
        assert 0.0 <= hit_rate["success_rate"] <= 1.0


@pytest.mark.integration
def test_rag_playground_parameter_variations(
    api_base_url: str, require_ollama: None, require_cassandra: None
) -> None:
    """Test A/B comparison with different parameters."""
    doc_id = f"playground-params-{uuid.uuid4().hex[:10]}"
    text = (
        "The playground allows testing different parameter combinations "
        "to find optimal settings for your RAG system. "
        "Parameters include top_k, min_score, and collection selection."
    )

    with httpx.Client(base_url=api_base_url, timeout=60.0) as client:
        # Ingest test document
        ingest_resp = client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": doc_id,
                "text": text,
            },
        )
        assert ingest_resp.status_code in (200, 201)

        time.sleep(2.0)

        # Test with different top_k values
        query_text = "What parameters can be tested?"

        # Query A: top_k = 3
        resp_a = client.post(
            "/api/v1/rag/query",
            json={
                "query": query_text,
                "top_k": 3,
                "debug_mode": True,
            },
        )
        assert resp_a.status_code == 200
        data_a = resp_a.json()

        # Query B: top_k = 10
        resp_b = client.post(
            "/api/v1/rag/query",
            json={
                "query": query_text,
                "top_k": 10,
                "debug_mode": True,
            },
        )
        assert resp_b.status_code == 200
        data_b = resp_b.json()

        # Verify both queries returned results
        assert len(data_a["results"]) > 0
        assert len(data_b["results"]) > 0

        # Verify top_k affects result count (up to available chunks)
        # top_k=10 should return same or more results than top_k=3
        assert len(data_b["results"]) >= len(data_a["results"])

        # Verify both have debug info for comparison
        assert "debug_info" in data_a
        assert "debug_info" in data_b
        assert data_a["debug_info"]["original_query"] == query_text
        assert data_b["debug_info"]["original_query"] == query_text


@pytest.mark.integration
def test_rag_playground_collections_list(api_base_url: str, require_cassandra: None) -> None:
    """Test listing RAG collections for playground collection selector."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.get("/api/v1/rag/collections")
        assert resp.status_code == 200

        data = resp.json()
        assert isinstance(data, dict)
        assert "collections" in data
        assert isinstance(data["collections"], list)

        # Verify default collection exists
        collection_names = [c["name"] for c in data["collections"]]
        assert "default" in collection_names

        # Verify collection structure matches playground requirements
        for coll in data["collections"]:
            assert "name" in coll
            assert "doc_count" in coll
            assert "chunk_count" in coll
            assert "embedding_models" in coll
            assert isinstance(coll["embedding_models"], list)


@pytest.mark.integration
def test_rag_playground_config(api_base_url: str) -> None:
    """Test RAG config endpoint for playground threshold indicators."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.get("/api/v1/rag/config")
        assert resp.status_code == 200

        data = resp.json()
        assert isinstance(data, dict)

        # Verify config contains threshold values used by playground
        assert "min_score" in data
        assert isinstance(data["min_score"], (int, float))

        # Optional threshold fields
        if "good_score_threshold" in data:
            assert isinstance(data["good_score_threshold"], (int, float))
        if "medium_score_threshold" in data:
            assert isinstance(data["medium_score_threshold"], (int, float))
