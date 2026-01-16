

from __future__ import annotations

from configparser import ConfigParser
from typing import Any

import pytest


@pytest.mark.unit
def test_retrieve_context_applies_min_score_and_max_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """retrieve_context should filter by min_score, dedupe, and cap to max_chunks."""
    # Build a config that enables pruning
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "10",
        "min_score": "0.50",
        "max_chunks": "2",
        "dedupe": "true",
    }

    # Force rag.py to use our config
    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    # Avoid real embedding / Cassandra
    monkeypatch.setattr("mygpt.rag.rag.embed_text", lambda _q: [0.0] * 3)

    class FakeStore:
        def query_by_embedding(self, _emb: Any, k: int):
            assert k == 10
            # Includes: below-threshold, duplicates, and valid unique
            # Results are sorted by score descending after filtering
            return [
                {"text": "weak", "score": 0.10},       # filtered: below min_score
                {"text": "keep one", "score": 0.90},
                {"text": "keep one", "score": 0.91},   # filtered: duplicate
                {"text": "keep two", "score": 0.70},   # dropped: max_chunks=2
                {"text": "keep three", "score": 0.80},
            ]

        def close(self):
            return None

    monkeypatch.setattr("mygpt.rag.rag.CassandraVectorStore", FakeStore)

    from mygpt.rag.rag import retrieve_context

    rows = retrieve_context("hello")
    assert len(rows) == 2
    # Sorted by score descending: 0.90 > 0.80 > 0.70
    assert [r["text"] for r in rows] == ["keep one", "keep three"]


@pytest.mark.unit
def test_compose_context_respects_budget_and_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """compose_context should honor max chars and header/score flags."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_context_max_chars": "30",
        "include_headers": "true",
        "include_scores": "true",
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import compose_context

    text = compose_context(
        [
            {"text": "abcdefghijklmnopqrstuvwxyz", "score": 0.9},
            {"text": "SECOND", "score": 0.8},
        ]
    )

    # Should include a header and be truncated to the configured budget
    assert "[Context 1]" in text
    assert "score=" in text
    assert len(text) <= 30


@pytest.mark.unit
def test_chunking_config_error_when_overlap_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    """_chunking_cfg should raise RAGError if overlap >= chunk_size."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "100",
        "chunk_overlap": "100",  # Invalid: overlap equals chunk_size
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import _chunking_cfg, RAGError

    with pytest.raises(RAGError, match="chunk_overlap must be smaller than chunk_size"):
        _chunking_cfg()


@pytest.mark.unit
def test_chunking_config_error_when_overlap_greater_than_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """_chunking_cfg should raise RAGError if overlap > chunk_size."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "100",
        "chunk_overlap": "150",  # Invalid: overlap > chunk_size
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import _chunking_cfg, RAGError

    with pytest.raises(RAGError, match="chunk_overlap must be smaller than chunk_size"):
        _chunking_cfg()


@pytest.mark.unit
def test_chunk_text_empty_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should handle empty string gracefully."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "100",
        "chunk_overlap": "20",
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import chunk_text

    chunks = chunk_text("")
    assert chunks == []


@pytest.mark.unit
def test_chunk_text_very_small_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_text should handle text smaller than chunk_size."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "1000",
        "chunk_overlap": "100",
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import chunk_text

    small_text = "This is a small text."
    chunks = chunk_text(small_text)
    assert len(chunks) == 1
    assert chunks[0] == small_text


@pytest.mark.unit
def test_compose_context_empty_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """compose_context should handle empty chunks list."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_context_max_chars": "1000",
        "include_headers": "false",
        "include_scores": "false",
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from mygpt.rag.rag import compose_context

    text = compose_context([])
    assert text == ""


@pytest.mark.unit
def test_retrieve_context_empty_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """retrieve_context should handle empty query string."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "dedupe": "false",
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("mygpt.rag.rag.embed_text", lambda _q: [0.0] * 3)

    class FakeStore:
        def __init__(self) -> None:
            self.last_k: int | None = None

        def query_by_embedding(self, _emb: list[float], k: int) -> list:
            self.last_k = k
            return []

        def close(self) -> None:
            pass

    fake_store = FakeStore()
    monkeypatch.setattr("mygpt.rag.rag.CassandraVectorStore", lambda *a, **kw: fake_store)

    from mygpt.rag.rag import retrieve_context

    rows = retrieve_context("")
    assert rows == []
    assert fake_store.last_k == 5  # Verify chat_top_k was passed correctly


@pytest.mark.unit
def test_retrieve_context_debug_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """retrieve_context with debug_mode=True should return debug info."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.50",
        "max_chunks": "3",
        "dedupe": "true",
        "debug_mode": "false",  # Test explicit override
    }

    monkeypatch.setattr("mygpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    # Mock embed_texts to return embeddings + metrics
    from mygpt.rag.embeddings import EmbeddingDebugMetrics
    def mock_embed_texts(texts, *, collect_metrics=False):
        embeddings = [[0.1, 0.2, 0.3] for _ in texts]
        if collect_metrics:
            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=3,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=10.5,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("mygpt.rag.rag.embed_texts", mock_embed_texts)
    monkeypatch.setattr("mygpt.rag.rag.embed_text", lambda _q: [0.1, 0.2, 0.3])

    # Mock vector store
    from mygpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics
    class FakeStore:
        def query_by_embedding(self, _emb, k: int, *, collect_metrics=False):
            results = [
                {"text": "result one", "score": 0.90, "doc_id": "doc1", "chunk_id": 0},
                {"text": "result two", "score": 0.75, "doc_id": "doc2", "chunk_id": 0},
                {"text": "result three", "score": 0.60, "doc_id": "doc3", "chunk_id": 0},
            ]
            if collect_metrics:
                metrics = VectorSearchDebugMetrics(
                    raw_results_count=3,
                    score_min=0.60,
                    score_max=0.90,
                    score_mean=0.75,
                    vector_search_time_ms=25.3,
                )
                return results, metrics
            return results

        def close(self):
            pass

    monkeypatch.setattr("mygpt.rag.rag.CassandraVectorStore", FakeStore)

    from mygpt.rag.rag import retrieve_context

    # Test with explicit debug_mode=True (overrides config)
    result = retrieve_context("test query", debug_mode=True)
    assert isinstance(result, tuple)
    results, debug_info = result

    # Verify results
    assert len(results) == 3
    assert results[0]["text"] == "result one"

    # Verify debug info structure
    assert debug_info.total_time_ms > 0
    assert debug_info.embedding_time_ms == 10.5
    assert debug_info.vector_search_time_ms == 25.3
    assert debug_info.original_query == "test query"
    assert debug_info.query_variants == ["test query"]
    assert debug_info.num_queries == 1
    assert debug_info.embedding_model == "test-model"
    assert debug_info.embedding_dim == 3
    assert debug_info.num_texts_embedded == 1
    assert debug_info.raw_results_count == 3
    assert debug_info.score_min == 0.60
    assert debug_info.score_max == 0.90
    assert debug_info.score_mean == 0.75
    assert debug_info.after_dedupe_filter == 3
    assert debug_info.after_min_score_filter == 3
    assert debug_info.chunks_included == 3