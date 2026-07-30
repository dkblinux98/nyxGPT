"""Unit tests for the RAG query/metrics endpoints, per-session RAG
enable/disable, and a couple of small helper functions in
src/nyxgpt/app.py (issue #3235, scoped split of #3218):

- POST /rag/query (rag_query) -- metadata-filter + debug_mode branches,
  the malformed-date validation path, and the generic exception -> 400
  translation.
- POST /rag/metrics/query (rag_metrics_query) -- the collect_metrics
  evaluation path (using the real compute_evaluation_metrics, not a
  mock) and the generic exception -> 400 translation.
- GET /sessions/{name}/metadata, POST /sessions/{name}/rag/enable,
  POST /sessions/{name}/rag/disable (get_session_metadata,
  enable_session_rag, disable_session_rag) -- real nyxgpt.sessions
  read/write against a tmp_path sessions dir.
- POST /rag/index-repo (rag_index_repo) -- success, ValueError -> 400,
  and generic Exception -> 500 translation.
- Two branches inside rag_upload_file (rag_upload_file) that real
  inputs can't reach through the normal multipart-upload path (see the
  docstrings on the two tests below for why), covered with a minimal
  fake ebooklib Book and a bytes subclass that always fails to decode.

Other rag_upload_file coverage (txt/md/json/pdf/pptx/docx/epub/html
parsing, size limits, format validation) already lives in
tests/unit/test_rag_upload_document_coverage.py; this file intentionally
does not duplicate that and follows the same client.post(..., files=...)
conventions established there.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUploadFile

from nyxgpt import sessions as sessions_module
from nyxgpt.app import app
from nyxgpt.rag.rag import RAGDebugInfo

pytestmark = pytest.mark.unit

client = TestClient(app)

UPLOAD_URL = "/api/v1/rag/upload"


def _fake_debug_info(**overrides: Any) -> RAGDebugInfo:
    defaults: dict[str, Any] = {
        "total_time_ms": 42.0,
        "query_expansion_time_ms": 5.0,
        "embedding_time_ms": 10.0,
        "vector_search_time_ms": 20.0,
        "keyword_search_time_ms": None,
        "fusion_time_ms": None,
        "reranking_time_ms": None,
        "filtering_time_ms": 3.0,
        "composition_time_ms": 4.0,
        "original_query": "test query",
        "query_variants": ["test query"],
        "num_queries": 1,
        "embedding_model": "nomic-embed-text",
        "embedding_dim": 768,
        "num_texts_embedded": 1,
        "batch_size": 1,
        "raw_results_count": 2,
        "score_min": 0.7,
        "score_max": 0.9,
        "score_mean": 0.8,
        "hybrid_enabled": False,
        "keyword_results_count": None,
        "vector_results_count": 2,
        "fusion_method": None,
        "reranking_enabled": False,
        "reranker_model": None,
        "num_candidates_reranked": None,
        "num_results_after_rerank": None,
        "after_min_score_filter": 2,
        "after_dedupe_filter": 2,
        "after_max_chunks_filter": 2,
        "total_chars_before_truncation": 200,
        "total_chars_after_truncation": 200,
        "chunks_included": 2,
    }
    defaults.update(overrides)
    return RAGDebugInfo(**defaults)


def _fake_rows() -> list[dict[str, Any]]:
    return [
        {"doc_id": "doc-1", "chunk_id": 0, "text": "chunk one", "score": 0.9},
        {"doc_id": "doc-2", "chunk_id": 1, "text": "chunk two", "score": 0.7},
    ]


# ============================================================================
# POST /rag/query
# ============================================================================


def test_rag_query_debug_mode_with_date_filter_returns_debug_info() -> None:
    debug_info = _fake_debug_info()
    with patch(
        "nyxgpt.app.retrieve_context", return_value=(_fake_rows(), debug_info)
    ) as mock_retrieve:
        response = client.post(
            "/api/v1/rag/query",
            json={"query": "test query", "debug_mode": True, "date_from": "2024-01-01"},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    assert body["debug_info"]["original_query"] == "test query"
    assert body["debug_info"]["embedding_model"] == "nomic-embed-text"

    # The date_from filter must have been parsed into a real datetime and
    # passed through as a MetadataFilter.
    _, kwargs = mock_retrieve.call_args
    metadata_filter = kwargs["metadata_filter"]
    assert metadata_filter is not None
    assert metadata_filter.date_from == datetime(2024, 1, 1)
    assert metadata_filter.date_to is None


def test_rag_query_no_filters_no_debug_returns_plain_results() -> None:
    with patch("nyxgpt.app.retrieve_context", return_value=_fake_rows()) as mock_retrieve:
        response = client.post("/api/v1/rag/query", json={"query": "plain query"})

    assert response.status_code == 200
    body = response.json()
    assert body["debug_info"] is None
    assert len(body["results"]) == 2
    _, kwargs = mock_retrieve.call_args
    assert kwargs["metadata_filter"] is None


def test_rag_query_invalid_date_format_returns_400() -> None:
    """A malformed date_from can't be parsed by datetime.fromisoformat();
    the ValueError it raises is caught by the endpoint's generic
    `except Exception` and translated into a 400."""
    response = client.post(
        "/api/v1/rag/query",
        json={"query": "test query", "date_from": "not-a-real-date"},
    )

    assert response.status_code == 400
    body = response.json()
    assert "error" in body
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"] != ""


def test_rag_query_retrieval_failure_returns_400() -> None:
    with patch("nyxgpt.app.retrieve_context", side_effect=RuntimeError("vector store unreachable")):
        response = client.post("/api/v1/rag/query", json={"query": "boom"})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "vector store unreachable"


# ============================================================================
# POST /rag/metrics/query
# ============================================================================


def test_rag_metrics_query_success_computes_evaluation_metrics() -> None:
    """Uses the real compute_evaluation_metrics() (not mocked) so the full
    evaluation_metrics construction in the handler is exercised end to
    end, not just stubbed."""
    debug_info = _fake_debug_info()
    with patch("nyxgpt.app.retrieve_context", return_value=(_fake_rows(), debug_info)):
        response = client.post(
            "/api/v1/rag/metrics/query",
            json={"query": "test query", "top_k": 5, "collect_metrics": True},
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    assert body["debug_info"]["original_query"] == "test query"

    eval_metrics = body["evaluation_metrics"]
    assert eval_metrics is not None
    assert eval_metrics["retrieval_accuracy"]["results_returned"] == 2
    assert eval_metrics["retrieval_accuracy"]["query_success"] is True
    assert eval_metrics["retrieval_accuracy"]["unique_docs_retrieved"] == 2
    assert eval_metrics["latency"]["total_time_ms"] == debug_info.total_time_ms
    assert eval_metrics["hit_rate"]["total_queries"] == 1
    assert "query_id" in eval_metrics
    assert "timestamp" in eval_metrics


def test_rag_metrics_query_increments_rag_queries_total_metric() -> None:
    """Regression test for #3424: the RAG Playground's "Collect Metrics"
    checkbox defaults on, routing every playground query through this
    endpoint instead of /rag/query -- which meant nyxgpt_rag_queries_total
    never moved for playground traffic and the RAG Performance dashboard
    stayed empty even under active use. This endpoint must increment the
    same counter/label /rag/query does."""
    debug_info = _fake_debug_info()
    with patch("nyxgpt.app.retrieve_context", return_value=(_fake_rows(), debug_info)):
        response = client.post(
            "/api/v1/rag/metrics/query",
            json={"query": "test query"},
        )

    assert response.status_code == 200
    metrics_text = client.get("/metrics").text
    assert 'nyxgpt_rag_queries_total{source="rag_query"}' in metrics_text.replace(" ", "")


def test_rag_metrics_query_retrieval_failure_returns_400() -> None:
    with patch("nyxgpt.app.retrieve_context", side_effect=RuntimeError("embedding backend down")):
        response = client.post(
            "/api/v1/rag/metrics/query",
            json={"query": "boom"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "embedding backend down"


# ============================================================================
# GET /sessions/{name}/metadata
# ============================================================================


def test_get_session_metadata_creates_session_and_returns_defaults(tmp_path: Path) -> None:
    with patch("nyxgpt.app.get_sessions_dir", return_value=tmp_path):
        response = client.get("/api/v1/sessions/brand-new-session/metadata")

    assert response.status_code == 200
    body = response.json()
    assert "rag_enabled" in body
    assert isinstance(body["rag_enabled"], bool)

    sf = sessions_module.session_file_for("brand-new-session", tmp_path)
    assert sf.exists()


# ============================================================================
# POST /sessions/{name}/rag/enable and /rag/disable
# ============================================================================


def test_enable_session_rag_sets_flag_true(tmp_path: Path) -> None:
    with patch("nyxgpt.app.get_sessions_dir", return_value=tmp_path):
        response = client.post("/api/v1/sessions/toggle-session/rag/enable")

    assert response.status_code == 200
    assert response.json() == {"session": "toggle-session", "rag_enabled": True}

    sf = sessions_module.session_file_for("toggle-session", tmp_path)
    mf = sessions_module.meta_file_for(sf)
    meta = sessions_module.load_session_meta(mf)
    assert meta["rag_enabled"] is True


def test_disable_session_rag_sets_flag_false(tmp_path: Path) -> None:
    with patch("nyxgpt.app.get_sessions_dir", return_value=tmp_path):
        # Called directly on a session with no existing file, exercising
        # disable_session_rag's own "create if missing" branch (distinct
        # from enable_session_rag's, which the enable test above covers).
        response = client.post("/api/v1/sessions/toggle-session-2/rag/disable")

    assert response.status_code == 200
    assert response.json() == {"session": "toggle-session-2", "rag_enabled": False}

    sf = sessions_module.session_file_for("toggle-session-2", tmp_path)
    mf = sessions_module.meta_file_for(sf)
    meta = sessions_module.load_session_meta(mf)
    assert meta["rag_enabled"] is False


# ============================================================================
# POST /rag/index-repo
# ============================================================================


def test_rag_index_repo_success_returns_indexed_files() -> None:
    fake_result = {
        "total_files": 3,
        "total_chunks": 10,
        "files": ["a.py", "b.py", "c.py"],
        "doc_ids": ["code:a.py", "code:b.py", "code:c.py"],
    }
    with patch("nyxgpt.rag.rag.ingest_repository", return_value=fake_result) as mock_ingest:
        response = client.post(
            "/api/v1/rag/index-repo",
            json={"repo_path": "/tmp/some-repo", "extensions": [".py", ".md"]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["total_files"] == 3
    assert body["total_chunks"] == 10
    assert body["doc_ids"] == fake_result["doc_ids"]

    _, kwargs = mock_ingest.call_args
    assert kwargs["extensions"] == {".py", ".md"}
    assert kwargs["repo_path"] == "/tmp/some-repo"

    metrics_text = client.get("/metrics").text
    assert 'nyxgpt_rag_ingests_total{result="success",source="repo"}' in metrics_text.replace(
        " ", ""
    )


def test_rag_index_repo_value_error_returns_400() -> None:
    with patch("nyxgpt.rag.rag.ingest_repository", side_effect=ValueError("bad repo path")):
        response = client.post(
            "/api/v1/rag/index-repo",
            json={"repo_path": "/does/not/exist"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "bad repo path"


def test_rag_index_repo_generic_exception_returns_500() -> None:
    with patch("nyxgpt.rag.rag.ingest_repository", side_effect=RuntimeError("disk exploded")):
        response = client.post(
            "/api/v1/rag/index-repo",
            json={"repo_path": "/tmp/some-repo"},
        )

    assert response.status_code == 500
    assert "Repository indexing failed" in response.json()["error"]["message"]


# ============================================================================
# POST /rag/upload -- two branches real inputs can't reach
# ============================================================================


class _FakeEpubItem:
    """Stand-in for an ebooklib document item with attacker-controlled body
    content -- see test below for why this is needed instead of a real
    ebooklib round-trip."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def get_type(self) -> Any:
        import ebooklib

        return ebooklib.ITEM_DOCUMENT

    def get_body_content(self) -> bytes:
        return self._body


class _FakeEpubBook:
    """Minimal fake of ebooklib's EpubBook exposing only the 3 methods
    rag_upload_file's .epub branch actually calls: get_metadata,
    get_items, get_items_of_type."""

    def __init__(self, items: list[_FakeEpubItem], title_metadata: list[Any]) -> None:
        self._items = items
        self._title_metadata = title_metadata

    def get_metadata(self, _namespace: str, name: str) -> list[Any]:
        if name == "title":
            return self._title_metadata
        return []

    def get_items(self) -> Any:
        return iter(self._items)

    def get_items_of_type(self, _item_type: int) -> Any:
        return iter(self._items)


def test_rag_upload_epub_strips_script_style_and_string_metadata_fallback() -> None:
    """Exercises two branches that a real ebooklib write_epub/read_epub
    round-trip never reaches:

    - extract_metadata_values()'s non-tuple "String format (fallback)"
      branch (app.py:4542). Real ebooklib get_metadata() always returns
      (value, attrs_dict) tuples (confirmed by the sibling upload test's
      NOTE on this same quirk), never plain strings, so the `else` branch
      of `isinstance(item, tuple)` is otherwise dead in practice.
    - the <script>/<style> removal loop (app.py:4590-4592). Round-tripping
      a chapter containing a <script> tag through epub.write_epub() /
      epub.read_epub() strips the tag before app.py's BeautifulSoup pass
      ever runs (verified directly), so real uploaded epub content can
      never contain a <script>/<style> tag at that point either.

    A minimal fake Book (patched in for ebooklib.epub.read_epub()'s return
    value) supplies both a non-tuple metadata item and a body with a
    surviving <script>/<style> tag.
    """
    body = (
        b"<script>alert(1)</script><style>.x{color:red}</style>"
        b"<h1>Heading</h1><p>" + (b"word " * 30) + b"</p>"
    )
    fake_book = _FakeEpubBook(
        items=[_FakeEpubItem(body)],
        title_metadata=["Plain String Title"],  # not a tuple -> app.py:4542
    )
    captured: dict[str, Any] = {}

    def fake_ingest(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "chunks_ingested": 1,
            "status": "ingested",
            "doc_hash": "hash123",
            "previous_hash": None,
        }

    with (
        patch("ebooklib.epub.read_epub", return_value=fake_book),
        patch("nyxgpt.app.ingest_document", side_effect=fake_ingest),
    ):
        response = client.post(
            UPLOAD_URL,
            files={"file": ("book.epub", b"placeholder epub bytes", "application/epub+zip")},
        )

    assert response.status_code == 200
    text = captured["text"]
    assert "Title: Plain String Title" in text
    assert "alert(1)" not in text
    assert "color:red" not in text
    assert "Heading" in text


def test_rag_upload_html_all_fallback_encodings_fail_returns_400() -> None:
    """Forces every decode attempt (utf-8 plus all three fallback
    encodings) to fail, driving the loop's `except UnicodeDecodeError:
    continue` body (app.py:4667-4668) on each fallback and the for/else
    `raise` once none succeed (app.py:4670).

    Real byte content can't trigger this: iso-8859-1 (tried first in the
    fallback list) is a total mapping over all 256 byte values, so
    `bytes.decode("iso-8859-1")` never raises -- the loop would always
    `break` on the first fallback and the `else` clause is unreachable
    with genuine bytes. Reaching it requires forcing every candidate
    decode to fail, done here by swapping in a bytes subclass whose
    .decode() always raises UnicodeDecodeError regardless of encoding,
    returned from an overridden UploadFile.read(). Starlette's own
    multipart parser instantiates `starlette.datastructures.UploadFile`
    directly (not the `fastapi.UploadFile` subclass), so that's the class
    that must be patched for the override to actually take effect on a
    real multipart upload.
    """

    class _AlwaysFailsDecode(bytes):
        def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
            raise UnicodeDecodeError(encoding, b"\x80", 0, 1, "forced failure")

    async def fake_read(self: Any, size: int = -1) -> bytes:
        return _AlwaysFailsDecode(b"<html><body><p>content</p></body></html>")

    with patch.object(StarletteUploadFile, "read", fake_read):
        response = client.post(
            UPLOAD_URL,
            files={"file": ("page.html", b"placeholder", "text/html")},
        )

    assert response.status_code == 400
    assert "Unable to decode HTML file" in response.json()["error"]["message"]
