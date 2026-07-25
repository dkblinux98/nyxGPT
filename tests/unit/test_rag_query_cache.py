"""Unit tests for the RAG query result cache (retrieve_context caching)."""

from __future__ import annotations

import time
from configparser import ConfigParser
from pathlib import Path
from typing import Any

import pytest


class FakeStore:
    """Minimal CassandraVectorStore stand-in that counts query calls."""

    call_count = 0

    def __init__(self, **kwargs: Any) -> None:
        pass

    def query_by_embedding(self, _emb: Any, k: int, **kwargs: Any) -> Any:
        FakeStore.call_count += 1
        results = [
            {"text": "result one", "score": 0.9, "doc_id": "doc1", "chunk_id": 0},
            {"text": "result two", "score": 0.8, "doc_id": "doc2", "chunk_id": 0},
        ]
        if kwargs.get("collect_metrics"):
            from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

            metrics = VectorSearchDebugMetrics(
                raw_results_count=len(results),
                score_min=0.8,
                score_max=0.9,
                score_mean=0.85,
                vector_search_time_ms=0.0,
            )
            return results, metrics
        return results

    def list_docs(self) -> list[dict]:
        return []

    def close(self) -> None:
        return None


def _base_cfg(**cache_overrides: str) -> ConfigParser:
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
def test_query_cache_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no [cache] config, query caching is disabled and every call recomputes."""
    cfg = ConfigParser()
    cfg["rag"] = {"chat_top_k": "5", "min_score": "0.0", "max_chunks": "10"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    from nyxgpt.rag.rag import retrieve_context

    retrieve_context("hello")
    retrieve_context("hello")

    assert FakeStore.call_count == 2


@pytest.mark.unit
def test_query_cache_hit_skips_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """An identical repeated query should be served from cache, not recomputed."""
    cfg = _base_cfg()

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    from nyxgpt.rag.rag import retrieve_context

    first = retrieve_context("hello")
    second = retrieve_context("hello")

    assert FakeStore.call_count == 1  # Second call served from cache
    assert first == second


@pytest.mark.unit
def test_query_cache_different_queries_not_shared(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different query text must not share a cache entry."""
    cfg = _base_cfg()

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    from nyxgpt.rag.rag import retrieve_context

    retrieve_context("hello")
    retrieve_context("goodbye")

    assert FakeStore.call_count == 2


@pytest.mark.unit
def test_query_cache_ttl_expiration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cached results should expire after the configured TTL."""
    cfg = _base_cfg(query_cache_ttl_seconds="1")

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    from nyxgpt.rag.rag import retrieve_context

    retrieve_context("hello")
    assert FakeStore.call_count == 1

    time.sleep(1.1)

    retrieve_context("hello")
    assert FakeStore.call_count == 2  # Expired, recomputed


@pytest.mark.unit
def test_query_cache_debug_mode_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """debug_mode=True calls should never be served from or stored in the cache."""
    cfg = _base_cfg()

    from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

    fake_metrics = EmbeddingDebugMetrics(
        embedding_model="test-model",
        embedding_dim=3,
        num_texts_embedded=1,
        batch_size=1,
        embedding_time_ms=0.0,
    )

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.embed_texts",
        lambda queries, **kwargs: ([[0.0] * 3 for _ in queries], fake_metrics),
    )
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    from nyxgpt.rag.rag import retrieve_context

    retrieve_context("hello", debug_mode=True)
    retrieve_context("hello", debug_mode=True)

    assert FakeStore.call_count == 2

    from nyxgpt.rag.rag import get_query_cache_stats

    stats = get_query_cache_stats()
    assert stats["size"] == 0


@pytest.mark.unit
def test_query_cache_hit_rate_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_query_cache_stats should report hits/misses/hit_rate."""
    cfg = _base_cfg()

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    from nyxgpt.rag.rag import get_query_cache_stats, retrieve_context

    retrieve_context("hello")  # miss
    retrieve_context("hello")  # hit
    retrieve_context("hello")  # hit

    stats = get_query_cache_stats()
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["hit_rate"] == pytest.approx(2 / 3)
    assert stats["size"] == 1


@pytest.mark.unit
def test_query_cache_stats_disabled_returns_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    """When caching is disabled, stats should be a zeroed dict rather than error."""
    cfg = ConfigParser()
    cfg["cache"] = {"query_cache_enabled": "false"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import get_query_cache_stats

    stats = get_query_cache_stats()
    assert stats == {
        "hits": 0,
        "misses": 0,
        "hit_rate": 0.0,
        "size": 0,
        "enabled": False,
        "backend": "none",
        "max_size": None,
        "ttl_seconds": None,
    }


@pytest.mark.unit
def test_query_cache_stats_reports_memory_backend_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A memory-backed cache should report its backend, max_size, and ttl_seconds."""
    cfg = _base_cfg(query_cache_max_size="42", query_cache_ttl_seconds="120")

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import get_query_cache_stats

    stats = get_query_cache_stats()
    assert stats["enabled"] is True
    assert stats["backend"] == "memory"
    assert stats["max_size"] == 42
    assert stats["ttl_seconds"] == 120


@pytest.mark.unit
def test_query_cache_stats_reports_disk_backend_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A disk-backed cache should report backend='disk' with max_size=None."""
    cfg = _base_cfg(
        query_cache_backend="disk",
        query_cache_dir=str(tmp_path),
        query_cache_ttl_seconds="600",
    )

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import get_query_cache_stats

    stats = get_query_cache_stats()
    assert stats["enabled"] is True
    assert stats["backend"] == "disk"
    assert stats["max_size"] is None
    assert stats["ttl_seconds"] == 600


@pytest.mark.unit
def test_query_cache_stats_reports_memory_default_ttl_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an explicit query_cache_ttl_seconds, stats must report the same
    300s default that _get_query_result_cache actually initializes the memory
    cache with, not None."""
    cfg = ConfigParser()
    cfg["cache"] = {"query_cache_enabled": "true", "query_cache_backend": "memory"}

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import get_query_cache_stats

    stats = get_query_cache_stats()
    assert stats["enabled"] is True
    assert stats["backend"] == "memory"
    assert stats["ttl_seconds"] == 300


@pytest.mark.unit
def test_query_cache_stats_reports_disk_default_ttl_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without an explicit query_cache_ttl_seconds, stats must report the same
    600s default that _get_query_result_cache actually initializes the disk
    cache with, not None."""
    cfg = ConfigParser()
    cfg["cache"] = {
        "query_cache_enabled": "true",
        "query_cache_backend": "disk",
        "query_cache_dir": str(tmp_path),
    }

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import get_query_cache_stats

    stats = get_query_cache_stats()
    assert stats["enabled"] is True
    assert stats["backend"] == "disk"
    assert stats["ttl_seconds"] == 600


@pytest.mark.unit
def test_ingest_document_invalidates_query_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ingesting/updating a document must invalidate cached query results."""
    from unittest.mock import Mock

    cfg = _base_cfg()
    cfg["rag"]["chunk_size"] = "50"
    cfg["rag"]["chunk_overlap"] = "10"

    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.embed_texts", lambda texts, **kwargs: [[0.1] for _ in texts]
    )
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    FakeStore.call_count = 0

    from nyxgpt.rag.rag import get_query_cache_stats, ingest_document, retrieve_context

    retrieve_context("hello")
    retrieve_context("hello")
    assert FakeStore.call_count == 1
    assert get_query_cache_stats()["size"] == 1

    # Ingest a new document: an unrelated mock store, but the module-level
    # cache invalidation must fire regardless of which store received it.
    mock_store = Mock()
    mock_store.document_needs_update.return_value = True
    mock_store.get_document_hash.return_value = None
    mock_store.get_document_info.return_value = None
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store)

    ingest_document("doc1", "Some new content to ingest")

    assert get_query_cache_stats()["size"] == 0

    # Restore FakeStore and confirm the next identical query recomputes.
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)
    retrieve_context("hello")
    assert FakeStore.call_count == 2


@pytest.mark.unit
def test_query_cache_key_varies_with_metadata_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different metadata filters must produce different cache keys."""
    from nyxgpt.rag.rag import _query_cache_key
    from nyxgpt.rag.vectorstore_cassandra import MetadataFilter

    base_kwargs = {
        "query": "hello",
        "k": 5,
        "collection": "default",
        "embedding_model": "test-model",
        "embedding_dim": 768,
        "min_score": 0.0,
        "max_chunks": 10,
        "use_expansion": False,
        "use_hybrid": True,
        "hybrid_alpha": None,
        "reranking_enabled": False,
    }

    key_no_filter = _query_cache_key(metadata_filter=None, **base_kwargs)
    key_with_filter = _query_cache_key(
        metadata_filter=MetadataFilter(tags=["important"]), **base_kwargs
    )

    assert key_no_filter != key_with_filter
    # Same inputs -> same key (stable fingerprinting)
    assert key_no_filter == _query_cache_key(metadata_filter=None, **base_kwargs)
