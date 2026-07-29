"""Unit tests for RAG pipeline logging (#3415 gap 3).

Regression coverage for the instrumentation audit finding that rag.py,
vectorstore_cassandra.py, and bm25.py had zero log statements -- retrieval
failures, Cassandra query latency, and chunk counts/scores were invisible.
"""

from __future__ import annotations

import logging
from configparser import ConfigParser
from typing import Any
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit


class _FakeStore:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def query_by_embedding(self, _emb: Any, k: int, **kwargs: Any) -> list[dict]:
        return [
            {"text": "chunk one", "score": 0.9, "doc_id": "doc1", "chunk_id": 0},
            {"text": "chunk two", "score": 0.7, "doc_id": "doc2", "chunk_id": 0},
        ]

    def list_docs(self) -> list[dict]:
        return []

    def close(self) -> None:
        return None


def test_retrieve_context_logs_completion_with_result_count_and_scores(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = ConfigParser()
    cfg["rag"] = {"chat_top_k": "5", "min_score": "0.0", "max_chunks": "10"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", _FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    with caplog.at_level(logging.INFO, logger="nyxgpt.rag.rag"):
        results = retrieve_context("hello")

    assert len(results) == 2
    records = [r for r in caplog.records if r.getMessage() == "RAG retrieval completed"]
    assert records
    record = records[-1]
    assert record.result_count == 2
    assert record.cache_hit is False
    assert record.score_max == 0.9
    assert record.score_min == 0.7
    assert isinstance(record.duration_ms, float)


def test_retrieve_context_logs_completion_on_cache_hit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = ConfigParser()
    cfg["rag"] = {"chat_top_k": "5"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda _q, **kwargs: [0.0] * 3)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", _FakeStore)

    class _StubQueryCache:
        def get(self, key: str) -> list[dict] | None:
            return [{"text": "cached", "score": 0.5, "doc_id": "d", "chunk_id": 0}]

        def set(self, key: str, value: Any) -> None:
            pass

    monkeypatch.setattr("nyxgpt.rag.rag._get_query_result_cache", lambda: _StubQueryCache())

    from nyxgpt.rag.rag import retrieve_context

    with caplog.at_level(logging.INFO, logger="nyxgpt.rag.rag"):
        retrieve_context("hello")

    records = [r for r in caplog.records if r.getMessage() == "RAG retrieval completed"]
    assert records
    assert records[-1].cache_hit is True


def test_ingest_document_logs_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "50", "chunk_overlap": "10"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.embed_texts", lambda texts, **kwargs: [[0.1] for _ in texts]
    )

    mock_store = Mock()
    mock_store.document_needs_update.return_value = True
    mock_store.get_document_hash.return_value = None
    mock_store.get_document_info.return_value = None
    mock_store.upsert_chunks.side_effect = RuntimeError("cassandra unavailable")

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kwargs: mock_store)

    from nyxgpt.rag.rag import ingest_document

    with (
        caplog.at_level(logging.ERROR, logger="nyxgpt.rag.rag"),
        pytest.raises(RuntimeError, match="cassandra unavailable"),
    ):
        ingest_document("doc1", "Some text to chunk")

    records = [r for r in caplog.records if r.getMessage() == "Document ingestion failed"]
    assert records
    assert records[0].doc_id == "doc1"
    assert records[0].error_type == "RuntimeError"


def test_bm25_search_logs_result_count(caplog: pytest.LogCaptureFixture) -> None:
    from nyxgpt.rag.bm25 import BM25Index

    index = BM25Index()
    index.build_index(["the quick brown fox", "a slow red turtle", "the quick blue fox"])

    with caplog.at_level(logging.DEBUG, logger="nyxgpt.rag.bm25"):
        results = index.search("quick fox", k=5)

    assert len(results) == 2
    records = [r for r in caplog.records if r.getMessage() == "BM25 search completed"]
    assert records
    assert records[0].result_count == 2
    assert records[0].indexed_docs == 3
    assert isinstance(records[0].duration_ms, float)
