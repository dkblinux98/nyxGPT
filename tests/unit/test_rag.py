

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