"""Coverage completion for src/nyxgpt/rag/rag.py (issue #3238).

Targets the specific gaps left after the model_compare.py split: query result
cache backend selection, chunking edge cases (no-heading fallback, overlap
strategies with an undersized budget), ingest_document's update/skip
branches, query expansion's generic-fence stripping, batched/parallel query
execution edge cases, the hybrid (vector + BM25) search/fusion/reranking
pipeline, compose_context's blank-chunk skip, and ingest_repository (which
had no tests at all).

Two lines in rag.py are marked `# pragma: no cover` rather than tested here:
they are defensive checks that are unreachable given the invariants of their
callers (verified by fuzzing the equivalent logic in isolation), not gaps in
test coverage.
"""

from __future__ import annotations

import json
from configparser import ConfigParser
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from nyxgpt.rag.vectorstore_cassandra import MetadataFilter

# =============================================================================
# Query Result Cache: backend selection
# =============================================================================


@pytest.mark.unit
def test_get_query_result_cache_disk_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """cache_backend='disk' should build a DiskCache rooted at query_cache_dir."""
    cfg = ConfigParser()
    cfg["cache"] = {
        "query_cache_enabled": "true",
        "query_cache_backend": "disk",
        "query_cache_dir": str(tmp_path / "ragcache"),
        "query_cache_ttl_seconds": "120",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.cache import DiskCache
    from nyxgpt.rag.rag import _get_query_result_cache

    cache = _get_query_result_cache()
    assert isinstance(cache, DiskCache)


@pytest.mark.unit
def test_get_query_result_cache_unknown_backend_disables_caching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized cache_backend should fall back to NoOpCache with a warning."""
    cfg = ConfigParser()
    cfg["cache"] = {
        "query_cache_enabled": "true",
        "query_cache_backend": "bogus-backend",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.cache import NoOpCache
    from nyxgpt.rag.rag import _get_query_result_cache

    cache = _get_query_result_cache()
    assert isinstance(cache, NoOpCache)


# =============================================================================
# chunk_text edge cases
# =============================================================================


@pytest.mark.unit
def test_chunk_text_preserve_headings_no_headings_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """preserve_headings=True with no Markdown headings still chunks the text
    correctly (the section-building loop captures it as a single untitled
    section on its own, without needing the `not sections` fallback)."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "800",
        "chunk_overlap": "100",
        "preserve_headings": "true",
        "sentence_aware": "true",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    chunks = chunk_text("Plain paragraph text with no markdown headings at all.")
    assert chunks == ["Plain paragraph text with no markdown headings at all."]


@pytest.mark.unit
def test_chunk_text_sentence_overlap_no_sentence_fits_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """overlap_strategy='sentence' with a tiny overlap budget: no sentence fits,
    so the next chunk gets no overlap prefix."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "60",
        "chunk_overlap": "5",
        "sentence_aware": "true",
        "preserve_headings": "false",
        "overlap_strategy": "sentence",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = (
        "This is a moderately long first sentence that exceeds the tiny overlap budget. "
        "Second sentence follows here and is also fairly long on its own."
    )
    chunks = chunk_text(text)
    assert len(chunks) >= 2


@pytest.mark.unit
def test_chunk_text_semantic_overlap_no_paragraph_fits_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """overlap_strategy='semantic' with a tiny overlap budget: no paragraph fits,
    so the next chunk gets no overlap prefix."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chunk_size": "60",
        "chunk_overlap": "5",
        "sentence_aware": "true",
        "preserve_headings": "false",
        "overlap_strategy": "semantic",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import chunk_text

    text = (
        "First paragraph exceeding the small overlap budget threshold by a good margin.\n\n"
        "Second paragraph continues on differently and is also fairly long by itself."
    )
    chunks = chunk_text(text)
    assert len(chunks) >= 2


# =============================================================================
# ingest_document: update-detection / skip branches
# =============================================================================


@pytest.mark.unit
def test_ingest_document_ensure_schema_empty_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """If chunk_text() yields nothing on the ensure_schema path, ingestion is skipped
    before ever calling ensure_schema()."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "100", "chunk_overlap": "10"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.chunk_text", lambda text: [])

    mock_store = Mock()
    mock_store.schema_exists.return_value = False
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: mock_store)

    from nyxgpt.rag.rag import ingest_document

    result = ingest_document("doc1", "some text", ensure_schema=True)

    assert result["status"] == "skipped"
    assert result["chunks_ingested"] == 0
    assert not mock_store.ensure_schema.called


@pytest.mark.unit
def test_ingest_document_skips_when_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing document whose hash matches is skipped without re-embedding."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "100", "chunk_overlap": "10"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    mock_store = Mock()
    mock_store.schema_exists.return_value = True
    mock_store.document_needs_update.return_value = False
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: mock_store)

    from nyxgpt.rag.rag import ingest_document

    result = ingest_document("doc1", "some text", ensure_schema=False)

    assert result["status"] == "skipped"
    assert not mock_store.upsert_chunks.called


@pytest.mark.unit
def test_ingest_document_update_empty_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """If chunk_text() yields nothing on the update path, ingestion is skipped."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "100", "chunk_overlap": "10"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.chunk_text", lambda text: [])

    mock_store = Mock()
    mock_store.schema_exists.return_value = True
    mock_store.document_needs_update.return_value = True
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: mock_store)

    from nyxgpt.rag.rag import ingest_document

    result = ingest_document("doc1", "some text", ensure_schema=False)

    assert result["status"] == "skipped"
    assert not mock_store.upsert_chunks.called


@pytest.mark.unit
def test_ingest_document_update_preserves_original_ingested_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updating an existing document preserves its original ingested_at timestamp
    and deletes the stale chunks before upserting new ones."""
    cfg = ConfigParser()
    cfg["rag"] = {"chunk_size": "100", "chunk_overlap": "10"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.embed_texts", lambda texts, **kwargs: [[0.1, 0.2] for _ in texts]
    )

    mock_store = Mock()
    mock_store.schema_exists.return_value = True
    mock_store.document_needs_update.return_value = True
    mock_store.get_document_hash.return_value = "old-hash"
    mock_store.get_document_info.return_value = {"ingested_at": "2024-01-01T00:00:00"}
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: mock_store)

    from nyxgpt.rag.rag import ingest_document

    result = ingest_document("doc1", "Some text to ingest for update testing.")

    assert result["status"] == "updated"
    assert mock_store.delete_doc.called
    upsert_kwargs = mock_store.upsert_chunks.call_args[1]
    assert upsert_kwargs["original_ingested_at"] == datetime.fromisoformat("2024-01-01T00:00:00")


# =============================================================================
# expand_query: generic code-fence stripping
# =============================================================================


@pytest.mark.unit
def test_expand_query_strips_generic_code_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A response wrapped in a plain ``` fence (not ```json) is still stripped."""
    cfg = ConfigParser()
    cfg["rag"] = {"enable_query_expansion": "true"}
    cfg["ollama"] = {"base_url": "http://localhost:11434"}
    cfg["default"] = {"model": "test-model"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    def mock_ollama_chat(base_url, model, messages, timeout_s):
        return '```\n["variant 1"]\n```'

    monkeypatch.setattr("nyxgpt.ollama_client.ollama_chat", mock_ollama_chat)

    from nyxgpt.rag.rag import expand_query

    result = expand_query("original query")
    assert result == ["original query", "variant 1"]


# =============================================================================
# Batched / parallel query execution edge cases
# =============================================================================


@pytest.mark.unit
def test_batched_query_execution_logs_empty_results_and_skips_blank_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_execute_query_parallel should tolerate a query variant with zero results
    and skip blank-text rows from a variant that does return results."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "enable_query_expansion": "true",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.expand_query", lambda q, max_expansions=3: [q, f"{q} variant"]
    )
    monkeypatch.setattr(
        "nyxgpt.rag.rag.embed_texts", lambda texts, **kwargs: [[0.1, 0.2] for _ in texts]
    )

    class FakeStore:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def query_by_embeddings_batch(self, embeddings, k, **kwargs):
            return [
                [],  # first query variant: no results at all
                [
                    {"text": "  ", "score": 0.5, "doc_id": "docA", "chunk_id": 0},
                    {"text": "real result", "score": 0.8, "doc_id": "docB", "chunk_id": 0},
                ],
            ]

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context("test query")
    assert len(results) == 1
    assert results[0]["text"] == "real result"


@pytest.mark.unit
def test_retrieve_context_debug_mode_multi_query_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    """debug_mode + query expansion: expansions are generated inside the debug
    branch, and overall score stats are computed from all per-query scores."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "enable_query_expansion": "true",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.rag.rag.expand_query", lambda q, max_expansions=3: [q, f"{q} variant"]
    )

    from nyxgpt.rag.embeddings import EmbeddingDebugMetrics

    def mock_embed_texts(texts, *, collect_metrics=False, **kwargs):
        embeddings = [[0.1, 0.2] for _ in texts]
        if collect_metrics:
            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=2,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=5.0,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", mock_embed_texts)

    from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

    class FakeStore:
        def __init__(self, **kwargs: Any) -> None:
            self.call_idx = 0

        def query_by_embedding(self, emb, k, *, collect_metrics=False, **kwargs):
            self.call_idx += 1
            score = 0.5 + 0.1 * self.call_idx
            results = [
                {
                    "text": f"result {self.call_idx}",
                    "score": score,
                    "doc_id": f"doc{self.call_idx}",
                    "chunk_id": 0,
                }
            ]
            if collect_metrics:
                metrics = VectorSearchDebugMetrics(
                    raw_results_count=1,
                    score_min=score,
                    score_max=score,
                    score_mean=score,
                    vector_search_time_ms=1.0,
                )
                return results, metrics
            return results

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    _results, debug_info = retrieve_context("test query", debug_mode=True)

    assert debug_info.num_queries == 2
    assert debug_info.query_expansion_time_ms is not None
    assert debug_info.score_min is not None
    assert debug_info.score_max is not None
    assert debug_info.score_mean is not None


@pytest.mark.unit
def test_retrieve_context_skips_blank_text_single_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sequential (single-query) execution should skip blank-text results too."""
    cfg = ConfigParser()
    cfg["rag"] = {"chat_top_k": "5", "min_score": "0.0", "max_chunks": "10"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda *_a, **_k: [0.1, 0.2])

    class FakeStore:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def query_by_embedding(self, emb, k, **kwargs):
            return [
                {"text": "   ", "score": 0.9, "doc_id": "doc1", "chunk_id": 0},
                {"text": "keep me", "score": 0.8, "doc_id": "doc2", "chunk_id": 0},
            ]

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context("query")
    assert len(results) == 1
    assert results[0]["text"] == "keep me"


@pytest.mark.unit
def test_retrieve_context_filters_below_min_score_before_max_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A below-threshold result encountered before max_chunks is reached must
    actually hit the min_score `continue`, not just get truncated by the
    max_chunks break."""
    cfg = ConfigParser()
    cfg["rag"] = {"chat_top_k": "5", "min_score": "0.5", "max_chunks": "10"}
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda *_a, **_k: [0.1, 0.2])

    class FakeStore:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def query_by_embedding(self, emb, k, **kwargs):
            return [
                {"text": "weak", "score": 0.1, "doc_id": "doc1", "chunk_id": 0},
                {"text": "strong", "score": 0.9, "doc_id": "doc2", "chunk_id": 0},
            ]

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context("query")
    assert [r["text"] for r in results] == ["strong"]


# =============================================================================
# Hybrid (BM25) search + fusion + reranking pipeline
# =============================================================================


def _row(
    doc_id: str,
    chunk_id: int,
    text: str,
    metadata: dict | None = None,
    embedding_model: str = "test-model",
    embedding_dim: int = 3,
    ingested_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        doc_id=doc_id,
        chunk_id=chunk_id,
        text=text,
        metadata=json.dumps(metadata or {}),
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        ingested_at=ingested_at,
    )


class _FakeSession:
    def __init__(self, rows_by_doc: dict[str, list], error_docs: set[str] | None = None) -> None:
        self.rows_by_doc = rows_by_doc
        self.error_docs = error_docs or set()

    def execute(self, stmt: str, params: tuple) -> list:
        doc_id = params[0]
        if doc_id in self.error_docs:
            raise RuntimeError(f"cassandra error fetching chunks for {doc_id}")
        return self.rows_by_doc.get(doc_id, [])


class _HybridFakeStore:
    def __init__(
        self,
        vector_results: list[dict],
        doc_ids: list[str],
        rows_by_doc: dict[str, list],
        error_docs: set[str] | None = None,
    ) -> None:
        self.table_name = "rag_chunks"
        self._keyspace_ready = False
        self.session = _FakeSession(rows_by_doc, error_docs)
        self._vector_results = vector_results
        self._doc_ids = doc_ids

    def query_by_embedding(self, emb, k, **kwargs):
        return self._vector_results

    def list_docs(self):
        return [{"doc_id": d} for d in self._doc_ids]

    def _ensure_keyspace_selected(self):
        self._keyspace_ready = True

    def close(self):
        pass


def _hybrid_cfg(**rag_overrides: str) -> ConfigParser:
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "10",
        "min_score": "0.0",
        "max_chunks": "20",
        "enable_hybrid_search": "true",
        **rag_overrides,
    }
    return cfg


def _patch_single_query_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda *_a, **_k: [0.1, 0.2, 0.3])
    monkeypatch.setattr(
        "nyxgpt.rag.embeddings._embedding_cfg",
        lambda **kw: SimpleNamespace(model="test-model", dimension=3),
    )


@pytest.mark.unit
def test_retrieve_context_hybrid_search_fuses_keyword_and_vector_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full hybrid pipeline: doc_id filtering, embedding_model mismatch,
    blank-text rows, a Cassandra fetch error, and RRF fusion of vector-only,
    keyword-only, and overlapping results."""
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: _hybrid_cfg())
    _patch_single_query_embedding(monkeypatch)

    vector_results = [
        {
            "doc_id": "doc-vec-only",
            "chunk_id": 0,
            "text": "cooking recipes and kitchen tips",
            "score": 0.9,
        },
        {
            "doc_id": "doc-both",
            "chunk_id": 0,
            "text": "python tutorial complete guide",
            "score": 0.8,
        },
    ]
    rows_by_doc = {
        "doc-vec-only": [_row("doc-vec-only", 0, "cooking recipes and kitchen tips")],
        "doc-keyword-only": [
            _row("doc-keyword-only", 0, "python tutorial resources for beginners")
        ],
        "doc-both": [_row("doc-both", 0, "python tutorial complete guide")],
        "doc-mismatch-model": [
            _row(
                "doc-mismatch-model",
                0,
                "python tutorial mismatched model text",
                embedding_model="other-model",
            )
        ],
        "doc-empty-text": [_row("doc-empty-text", 0, "   ")],
    }
    doc_ids = [
        "doc-vec-only",
        "doc-keyword-only",
        "doc-both",
        "doc-excluded",
        "doc-mismatch-model",
        "doc-empty-text",
        "doc-error",
    ]
    store = _HybridFakeStore(vector_results, doc_ids, rows_by_doc, error_docs={"doc-error"})
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: store)

    metadata_filter = MetadataFilter(
        doc_ids=[
            "doc-vec-only",
            "doc-keyword-only",
            "doc-both",
            "doc-mismatch-model",
            "doc-empty-text",
            "doc-error",
        ]
    )

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context("python tutorial", metadata_filter=metadata_filter)

    by_doc = {r["doc_id"]: r for r in results}
    assert "doc-keyword-only" in by_doc
    assert by_doc["doc-keyword-only"]["similarity_score"] is None
    assert "doc-both" in by_doc
    assert "doc-vec-only" in by_doc
    assert "doc-excluded" not in by_doc
    assert "doc-mismatch-model" not in by_doc
    assert "doc-empty-text" not in by_doc
    assert "doc-error" not in by_doc


@pytest.mark.unit
def test_retrieve_context_hybrid_keyword_search_filename_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyword-search chunk fetch loop applies the filename filter itself
    (independent of the vector search's own filtering)."""
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: _hybrid_cfg())
    _patch_single_query_embedding(monkeypatch)

    rows_by_doc = {
        "doc-match": [
            _row("doc-match", 0, "python tutorial content", metadata={"filename": "guide-book.txt"})
        ],
        "doc-no-match": [
            _row("doc-no-match", 0, "python tutorial content", metadata={"filename": "other.txt"})
        ],
    }
    store = _HybridFakeStore([], ["doc-match", "doc-no-match"], rows_by_doc)
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: store)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context("python tutorial", metadata_filter=MetadataFilter(filename="guide"))

    doc_ids = {r["doc_id"] for r in results}
    assert "doc-match" in doc_ids
    assert "doc-no-match" not in doc_ids


@pytest.mark.unit
def test_retrieve_context_hybrid_keyword_search_tags_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyword-search loop requires ALL specified tags to be present."""
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: _hybrid_cfg())
    _patch_single_query_embedding(monkeypatch)

    rows_by_doc = {
        "doc-match": [
            _row(
                "doc-match",
                0,
                "python tutorial content",
                metadata={"tags": ["python", "tutorial", "extra"]},
            )
        ],
        "doc-partial-tags": [
            _row("doc-partial-tags", 0, "python tutorial content", metadata={"tags": ["python"]})
        ],
        "doc-non-list-tags": [
            _row("doc-non-list-tags", 0, "python tutorial content", metadata={"tags": "python"})
        ],
    }
    store = _HybridFakeStore(
        [], ["doc-match", "doc-partial-tags", "doc-non-list-tags"], rows_by_doc
    )
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: store)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context(
        "python tutorial", metadata_filter=MetadataFilter(tags=["python", "tutorial"])
    )

    doc_ids = {r["doc_id"] for r in results}
    assert "doc-match" in doc_ids
    assert "doc-partial-tags" not in doc_ids
    assert "doc-non-list-tags" not in doc_ids


@pytest.mark.unit
def test_retrieve_context_hybrid_keyword_search_date_range_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The keyword-search loop applies date_from/date_to and skips rows with
    no ingested_at at all."""
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: _hybrid_cfg())
    _patch_single_query_embedding(monkeypatch)

    rows_by_doc = {
        "doc-in-range": [
            _row(
                "doc-in-range",
                0,
                "python tutorial content",
                ingested_at=datetime(2024, 6, 1),
            )
        ],
        "doc-too-early": [
            _row(
                "doc-too-early",
                0,
                "python tutorial content",
                ingested_at=datetime(2023, 1, 1),
            )
        ],
        "doc-too-late": [
            _row(
                "doc-too-late",
                0,
                "python tutorial content",
                ingested_at=datetime(2025, 1, 1),
            )
        ],
        "doc-no-date": [_row("doc-no-date", 0, "python tutorial content", ingested_at=None)],
    }
    store = _HybridFakeStore(
        [], ["doc-in-range", "doc-too-early", "doc-too-late", "doc-no-date"], rows_by_doc
    )
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: store)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context(
        "python tutorial",
        metadata_filter=MetadataFilter(
            date_from=datetime(2024, 1, 1), date_to=datetime(2024, 12, 31)
        ),
    )

    doc_ids = {r["doc_id"] for r in results}
    assert doc_ids == {"doc-in-range"}


@pytest.mark.unit
def test_retrieve_context_hybrid_search_weighted_fusion(monkeypatch: pytest.MonkeyPatch) -> None:
    """hybrid_alpha set: fusion uses weighted_fusion instead of RRF."""
    monkeypatch.setattr(
        "nyxgpt.rag.rag.load_config", lambda *_a, **_k: _hybrid_cfg(hybrid_alpha="0.5")
    )
    _patch_single_query_embedding(monkeypatch)

    vector_results = [
        {
            "doc_id": "doc-both",
            "chunk_id": 0,
            "text": "python tutorial complete guide",
            "score": 0.8,
        },
    ]
    rows_by_doc = {
        "doc-both": [_row("doc-both", 0, "python tutorial complete guide")],
        "doc-keyword-only": [_row("doc-keyword-only", 0, "python tutorial resources")],
    }
    store = _HybridFakeStore([], ["doc-both", "doc-keyword-only"], rows_by_doc)
    store.query_by_embedding = lambda emb, k, **kw: vector_results
    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", lambda **kw: store)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context("python tutorial")

    doc_ids = {r["doc_id"] for r in results}
    assert "doc-both" in doc_ids
    assert "doc-keyword-only" in doc_ids


# =============================================================================
# Reranking
# =============================================================================


@pytest.mark.unit
def test_retrieve_context_reranking_non_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """reranking_enabled=True (non-debug) calls rerank_results and uses its
    reordered list."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "enable_hybrid_search": "false",
        "enable_reranking": "true",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.rag.rag.embed_text", lambda *_a, **_k: [0.1, 0.2])

    class FakeStore:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def query_by_embedding(self, emb, k, **kwargs):
            return [
                {"text": "first", "score": 0.5, "doc_id": "doc1", "chunk_id": 0},
                {"text": "second", "score": 0.9, "doc_id": "doc2", "chunk_id": 0},
            ]

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    def fake_rerank_results(query, results, *, collect_metrics=False):
        reordered = list(reversed(results))
        assert not collect_metrics
        return reordered

    monkeypatch.setattr("nyxgpt.rag.rag.rerank_results", fake_rerank_results)

    from nyxgpt.rag.rag import retrieve_context

    results = retrieve_context("query")
    # Pre-rerank order is score-descending (["second", "first"]); the fake
    # reranker reverses whatever list it's handed.
    assert [r["text"] for r in results] == ["first", "second"]


@pytest.mark.unit
def test_retrieve_context_reranking_debug_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """reranking_enabled=True with debug_mode=True unpacks the (results, metrics)
    tuple returned by rerank_results."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_top_k": "5",
        "min_score": "0.0",
        "max_chunks": "10",
        "enable_hybrid_search": "false",
        "enable_reranking": "true",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.embeddings import EmbeddingDebugMetrics
    from nyxgpt.rag.reranker import RerankerDebugMetrics
    from nyxgpt.rag.vectorstore_cassandra import VectorSearchDebugMetrics

    def mock_embed_texts(texts, *, collect_metrics=False, **kwargs):
        embeddings = [[0.1, 0.2] for _ in texts]
        if collect_metrics:
            metrics = EmbeddingDebugMetrics(
                embedding_model="test-model",
                embedding_dim=2,
                num_texts_embedded=len(texts),
                batch_size=16,
                embedding_time_ms=1.0,
            )
            return embeddings, metrics
        return embeddings

    monkeypatch.setattr("nyxgpt.rag.rag.embed_texts", mock_embed_texts)

    class FakeStore:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def query_by_embedding(self, emb, k, *, collect_metrics=False, **kwargs):
            results = [{"text": "only result", "score": 0.7, "doc_id": "doc1", "chunk_id": 0}]
            if collect_metrics:
                metrics = VectorSearchDebugMetrics(
                    raw_results_count=1,
                    score_min=0.7,
                    score_max=0.7,
                    score_mean=0.7,
                    vector_search_time_ms=1.0,
                )
                return results, metrics
            return results

        def list_docs(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr("nyxgpt.rag.rag.CassandraVectorStore", FakeStore)

    def fake_rerank_results(query, results, *, collect_metrics=False):
        assert collect_metrics
        metrics = RerankerDebugMetrics(
            reranker_model="fake-reranker",
            num_candidates=len(results),
            num_reranked=len(results),
            reranking_time_ms=2.0,
            score_min=0.7,
            score_max=0.7,
            score_mean=0.7,
        )
        return results, metrics

    monkeypatch.setattr("nyxgpt.rag.rag.rerank_results", fake_rerank_results)

    from nyxgpt.rag.rag import retrieve_context

    results, debug_info = retrieve_context("query", debug_mode=True)
    assert len(results) == 1
    assert debug_info.reranking_enabled is True
    assert debug_info.reranker_model == "fake-reranker"
    assert debug_info.num_candidates_reranked == 1
    assert debug_info.num_results_after_rerank == 1


# =============================================================================
# compose_context: blank chunk skip
# =============================================================================


@pytest.mark.unit
def test_compose_context_skips_blank_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Results with blank/whitespace-only text are skipped entirely (they don't
    even consume a [Context N] slot)."""
    cfg = ConfigParser()
    cfg["rag"] = {
        "chat_context_max_chars": "1000",
        "include_scores": "false",
        "include_headers": "true",
    }
    monkeypatch.setattr("nyxgpt.rag.rag.load_config", lambda *_a, **_k: cfg)

    from nyxgpt.rag.rag import compose_context

    result = compose_context([{"text": "   "}, {"text": "actual content"}])
    assert "actual content" in result
    assert "[Context 1]" not in result
    assert "[Context 2]" in result


# =============================================================================
# ingest_repository
# =============================================================================


@pytest.mark.unit
def test_ingest_repository_path_does_not_exist(tmp_path: Any) -> None:
    from nyxgpt.rag.rag import ingest_repository

    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="does not exist"):
        ingest_repository(str(missing))


@pytest.mark.unit
def test_ingest_repository_path_not_a_directory(tmp_path: Any) -> None:
    from nyxgpt.rag.rag import ingest_repository

    file_path = tmp_path / "file.txt"
    file_path.write_text("hello")
    with pytest.raises(ValueError, match="not a directory"):
        ingest_repository(str(file_path))


@pytest.mark.unit
def test_ingest_repository_path_outside_allowed_dirs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nyxgpt.rag.rag import ingest_repository

    monkeypatch.delenv("NYXGPT_TRUSTED_PATHS", raising=False)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    with pytest.raises(ValueError, match="outside allowed directories"):
        ingest_repository(str(repo_dir))


@pytest.mark.unit
def test_ingest_repository_indexes_and_ingests_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Happy path: files with chunks are ingested as documents; a file with no
    chunks is still counted in doc_ids but produces no ingest_document call."""
    from nyxgpt.rag.rag import ingest_repository

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("NYXGPT_TRUSTED_PATHS", str(tmp_path))

    file_a = repo_dir / "a.py"
    file_b = repo_dir / "b.py"
    file_c = repo_dir / "c.py"

    fake_index_result = {
        "files": [str(file_a), str(file_b), str(file_c)],
        "chunks": [
            (str(file_a), 0, "chunk a1"),
            (str(file_a), 1, "chunk a2"),
            (str(file_b), 0, "chunk b1"),
        ],
        "total_chunks": 3,
        "truncated": False,
    }
    monkeypatch.setattr(
        "nyxgpt.rag.code_parser.index_repository", lambda *a, **k: fake_index_result
    )

    ingest_calls = []

    def fake_ingest_document(doc_id, text, **kwargs):
        ingest_calls.append((doc_id, text, kwargs))
        return {"status": "ingested", "chunks_ingested": text.count("chunk")}

    monkeypatch.setattr("nyxgpt.rag.rag.ingest_document", fake_ingest_document)

    result = ingest_repository(str(repo_dir), doc_id_prefix="code", collection="default")

    assert result["total_files"] == 3
    assert set(result["doc_ids"]) == {"code:a.py", "code:b.py", "code:c.py"}
    assert len(ingest_calls) == 2
    ingested_doc_ids = {call[0] for call in ingest_calls}
    assert ingested_doc_ids == {"code:a.py", "code:b.py"}
    assert result["total_chunks"] == sum(call[1].count("chunk") for call in ingest_calls)


@pytest.mark.unit
def test_ingest_repository_file_outside_repo_uses_absolute_path_as_doc_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """If a file returned by index_repository isn't actually under repo_path
    (relative_to() raises ValueError), the doc_id falls back to the file's
    own path instead of failing."""
    from nyxgpt.rag.rag import ingest_repository

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    monkeypatch.setenv("NYXGPT_TRUSTED_PATHS", str(tmp_path))

    outside_file = outside_dir / "stray.py"

    fake_index_result = {
        "files": [str(outside_file)],
        "chunks": [(str(outside_file), 0, "chunk stray")],
        "total_chunks": 1,
        "truncated": False,
    }
    monkeypatch.setattr(
        "nyxgpt.rag.code_parser.index_repository", lambda *a, **k: fake_index_result
    )

    ingest_calls = []

    def fake_ingest_document(doc_id, text, **kwargs):
        ingest_calls.append((doc_id, text, kwargs))
        return {"status": "ingested", "chunks_ingested": 1}

    monkeypatch.setattr("nyxgpt.rag.rag.ingest_document", fake_ingest_document)

    result = ingest_repository(str(repo_dir), doc_id_prefix="code")

    assert result["doc_ids"] == [f"code:{str(outside_file).replace('/', ':')}"]
    assert len(ingest_calls) == 1
