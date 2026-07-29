"""Unit tests for the RAG query cache API surface in app.py.

Covers the two dedicated cache endpoints (`GET /rag/cache/stats`,
`POST /rag/cache/clear`) plus the invalidation call site wired into
`DELETE /rag/collections/{name}`.
"""

from __future__ import annotations

from configparser import ConfigParser
from typing import Any
from unittest.mock import Mock

import pytest


class FakeStore:
    """Minimal CassandraVectorStore stand-in that counts query calls."""

    call_count = 0

    def __init__(self, **kwargs: Any) -> None:
        pass

    def query_by_embedding(self, _emb: Any, k: int, **kwargs: Any) -> Any:
        FakeStore.call_count += 1
        return [{"text": "result one", "score": 0.9, "doc_id": "doc1", "chunk_id": 0}]

    def list_docs(self) -> list[dict]:
        return []

    def close(self) -> None:
        return None


def _cache_cfg(**cache_overrides: str) -> ConfigParser:
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "dedupe": "true",
    }
    cfg["cache"] = {
        "query_cache_enabled": "true",
        "query_cache_backend": "memory",
        "query_cache_ttl_seconds": "3600",
        **cache_overrides,
    }
    return cfg


@pytest.mark.unit
def test_cache_stats_endpoint_disabled_returns_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    """With caching disabled, the stats endpoint returns a zeroed payload."""
    from fastapi.testclient import TestClient

    from nyxgpt.app import app

    cfg = ConfigParser()
    cfg["cache"] = {"query_cache_enabled": "false"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    with TestClient(app) as client:
        resp = client.get("/api/v1/rag/cache/stats")

    assert resp.status_code == 200
    assert resp.json() == {
        "hits": 0,
        "misses": 0,
        "hit_rate": 0.0,
        "size": 0,
        "enabled": False,
        "backend": "none",
        "max_size": None,
        "ttl_seconds": None,
        "rag_enabled": False,
    }


@pytest.mark.unit
def test_cache_stats_endpoint_reports_hits_and_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stats endpoint reflects real hit/miss/size counters from retrieve_context."""
    from fastapi.testclient import TestClient

    from nyxgpt.app import app
    from nyxgpt.rag.rag import retrieve_context

    cfg = _cache_cfg()
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    retrieve_context("hello")  # miss
    retrieve_context("hello")  # hit

    with TestClient(app) as client:
        resp = client.get("/api/v1/rag/cache/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["hits"] == 1
    assert data["misses"] == 1
    assert data["hit_rate"] == pytest.approx(0.5)
    assert data["size"] == 1
    assert data["enabled"] is True
    assert data["backend"] == "memory"
    assert data["ttl_seconds"] == 3600
    assert data["rag_enabled"] is False


@pytest.mark.unit
def test_cache_stats_endpoint_reports_rag_enabled_when_rag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache enabled + global RAG enabled: the endpoint reports rag_enabled=True."""
    from fastapi.testclient import TestClient

    from nyxgpt.app import app

    cfg = _cache_cfg()
    cfg["rag"]["enable_chat_context"] = "true"
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    with TestClient(app) as client:
        resp = client.get("/api/v1/rag/cache/stats")

    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["rag_enabled"] is True


@pytest.mark.unit
def test_cache_clear_endpoint_empties_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /rag/cache/clear actually empties a populated cache."""
    from fastapi.testclient import TestClient

    from nyxgpt.app import app
    from nyxgpt.rag.rag import get_query_cache_stats, retrieve_context

    cfg = _cache_cfg()
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    retrieve_context("hello")
    assert get_query_cache_stats()["size"] == 1

    with TestClient(app) as client:
        resp = client.post("/api/v1/rag/cache/clear")

    assert resp.status_code == 200
    assert resp.json() == {"status": "Query result cache cleared"}
    assert get_query_cache_stats()["size"] == 0

    # A subsequent identical query must recompute rather than hit the cache.
    retrieve_context("hello")
    assert FakeStore.call_count == 2


@pytest.mark.unit
def test_collection_delete_invalidates_query_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """DELETE /rag/collections/{name} clears previously cached query results."""
    from fastapi.testclient import TestClient

    from nyxgpt.app import app
    from nyxgpt.rag.rag import get_query_cache_stats, retrieve_context

    cfg = _cache_cfg()
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    retrieve_context("hello", collection="mycoll")
    assert get_query_cache_stats()["size"] == 1

    mock_store = Mock()
    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.CassandraVectorStore", lambda **kwargs: mock_store
    )

    with TestClient(app) as client:
        resp = client.delete("/api/v1/rag/collections/mycoll")

    assert resp.status_code == 200
    mock_store.truncate.assert_called_once()
    assert get_query_cache_stats()["size"] == 0


@pytest.mark.unit
def test_collection_reindex_invalidates_query_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST /rag/collections/{name}/reindex clears previously cached query results."""
    from fastapi.testclient import TestClient

    from nyxgpt.app import app
    from nyxgpt.rag.rag import get_query_cache_stats, retrieve_context

    cfg = _cache_cfg()
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    retrieve_context("hello", collection="mycoll")
    assert get_query_cache_stats()["size"] == 1

    mock_store = Mock()
    mock_store.list_collections.return_value = ["mycoll"]
    mock_store.get_all_chunks.return_value = [{"text": "chunk one"}]
    monkeypatch.setattr(
        "nyxgpt.rag.vectorstore_cassandra.CassandraVectorStore", lambda **kwargs: mock_store
    )
    monkeypatch.setattr("nyxgpt.rag.embeddings.embed_texts", lambda texts, **kwargs: [[0.1]])

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/rag/collections/mycoll/reindex",
            json={"target_embedding_model": "new-model", "embedding_dim": 768},
        )

    assert resp.status_code == 200
    assert get_query_cache_stats()["size"] == 0
