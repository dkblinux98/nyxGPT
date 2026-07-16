"""Unit tests for POST /api/v1/rag/upload in src/nyxgpt/app.py.

This module covers the "validation + plain-text/.txt + .json + .md +
doc_id/ingest wiring" slice of the ``rag_upload_file`` endpoint:

- file-type and file-size validation (shared by every branch)
- the ``.md`` branch (frontmatter parsing, markdown-to-text conversion,
  the ImportError fallback, and the generic parsing-exception path)
- the ``.json`` branch (valid JSON re-serialization and JSONDecodeError)
- the plain-text/``else`` branch
- doc_id derivation (explicit doc_id vs. filename vs. generated uuid)
- the final ``ingest_document`` call and its success/failure handling

``ingest_document`` is mocked throughout so no real Cassandra/vector-store
connection is required. The .pdf/.docx/.pptx/.epub/.html branches are
covered by sibling test modules and are intentionally not exercised here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit

UPLOAD_URL = "/api/v1/rag/upload"


def _ingest_result(
    status: str = "ingested",
    chunks_ingested: int = 3,
    doc_hash: str = "deadbeef",
    previous_hash: str | None = None,
) -> dict:
    return {
        "chunks_ingested": chunks_ingested,
        "status": status,
        "doc_hash": doc_hash,
        "previous_hash": previous_hash,
    }


def _mock_ingest(**kwargs):
    return patch("nyxgpt.app.ingest_document", return_value=_ingest_result(**kwargs))


# ============================================================================
# File-type and file-size validation (shared by all branches)
# ============================================================================


def test_upload_unsupported_extension_returns_400():
    client = TestClient(app)
    response = client.post(
        UPLOAD_URL,
        files={"file": ("evil.exe", b"binary-ish content", "application/octet-stream")},
    )

    assert response.status_code == 400
    body = response.json()
    assert ".exe" in body["detail"]
    assert "not supported" in body["detail"]


def test_upload_no_filename_extension_returns_400():
    """A filename with no suffix has an empty extension, which is not allowed."""
    client = TestClient(app)
    response = client.post(
        UPLOAD_URL,
        files={"file": ("noextension", b"some content", "application/octet-stream")},
    )

    assert response.status_code == 400


def test_upload_oversized_file_returns_413():
    oversized = b"x" * (10 * 1024 * 1024 + 1)
    client = TestClient(app)
    response = client.post(
        UPLOAD_URL,
        files={"file": ("big.txt", oversized, "text/plain")},
    )

    assert response.status_code == 413
    assert "too large" in response.json()["detail"].lower()


def test_upload_size_exactly_at_limit_is_allowed():
    """A file exactly at MAX_UPLOAD_SIZE should pass validation (only '>' rejects)."""
    exactly_max = b"x" * (10 * 1024 * 1024)
    client = TestClient(app)
    with _mock_ingest():
        response = client.post(
            UPLOAD_URL,
            files={"file": ("exact.txt", exactly_max, "text/plain")},
        )

    assert response.status_code == 200


# ============================================================================
# Plain-text / .txt branch (the "else" fallback)
# ============================================================================


def test_upload_txt_happy_path():
    client = TestClient(app)
    with _mock_ingest(status="ingested", chunks_ingested=2, doc_hash="hash123") as mock:
        response = client.post(
            UPLOAD_URL,
            files={"file": ("notes.txt", b"hello world, this is plain text", "text/plain")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["doc_id"] == "notes.txt"
    assert body["chunks_ingested"] == 2
    assert body["status"] == "ingested"
    assert body["doc_hash"] == "hash123"
    mock.assert_called_once()
    _, kwargs = mock.call_args
    assert kwargs["text"] == "hello world, this is plain text"


def test_upload_txt_invalid_utf8_returns_400():
    client = TestClient(app)
    invalid_bytes = b"\xff\xfe\x00invalid-utf8"
    response = client.post(
        UPLOAD_URL,
        files={"file": ("bad.bin_placeholder.txt", invalid_bytes, "text/plain")},
    )

    assert response.status_code == 400
    assert "encoding error" in response.json()["detail"].lower()


# ============================================================================
# .json branch
# ============================================================================


def test_upload_json_valid_reformats_and_ingests():
    payload = b'{"b": 2, "a": 1}'
    client = TestClient(app)
    with _mock_ingest() as mock:
        response = client.post(
            UPLOAD_URL,
            files={"file": ("data.json", payload, "application/json")},
        )

    assert response.status_code == 200
    _, kwargs = mock.call_args
    # Re-serialized with indent=2, preserving key order from the source dict.
    assert kwargs["text"] == '{\n  "b": 2,\n  "a": 1\n}'


def test_upload_json_invalid_returns_400():
    client = TestClient(app)
    response = client.post(
        UPLOAD_URL,
        files={"file": ("bad.json", b"{not valid json", "application/json")},
    )

    assert response.status_code == 400
    assert "invalid json" in response.json()["detail"].lower()


# ============================================================================
# .md branch
# ============================================================================


def test_upload_md_without_frontmatter():
    md_content = b"# Title\n\nSome **bold** paragraph text."
    client = TestClient(app)
    with _mock_ingest() as mock:
        response = client.post(
            UPLOAD_URL,
            files={"file": ("doc.md", md_content, "text/markdown")},
        )

    assert response.status_code == 200
    _, kwargs = mock.call_args
    text = kwargs["text"]
    assert "Title" in text
    assert "bold" in text
    # No frontmatter present, so no metadata section should be prepended.
    assert "[Metadata]" not in text


def test_upload_md_with_frontmatter():
    md_content = (
        b"---\n"
        b"title: My Doc\n"
        b"author: nyx\n"
        b"---\n"
        b"# Heading\n\nBody paragraph.\n"
    )
    client = TestClient(app)
    with _mock_ingest() as mock:
        response = client.post(
            UPLOAD_URL,
            files={"file": ("frontmatter.md", md_content, "text/markdown")},
        )

    assert response.status_code == 200
    _, kwargs = mock.call_args
    text = kwargs["text"]
    assert text.startswith("[Metadata]")
    assert "title: My Doc" in text
    assert "author: nyx" in text
    assert "Heading" in text
    assert "Body paragraph" in text


def test_upload_md_import_error_falls_back_to_plain_text():
    """If the markdown/frontmatter/bs4 stack is unavailable, raw decoded text is used."""
    md_content = b"# Raw Heading\n\nRaw body."
    client = TestClient(app)
    with (
        patch("frontmatter.loads", side_effect=ImportError("no frontmatter")),
        _mock_ingest() as mock,
    ):
        response = client.post(
            UPLOAD_URL,
            files={"file": ("fallback.md", md_content, "text/markdown")},
        )

    assert response.status_code == 200
    _, kwargs = mock.call_args
    # Fallback path decodes the raw bytes verbatim, unlike the parsed path.
    assert kwargs["text"] == md_content.decode("utf-8")


def test_upload_md_parsing_exception_returns_400():
    """A non-ImportError raised during markdown parsing surfaces as HTTP 400."""
    md_content = b"# Heading\n\nBody."
    client = TestClient(app)
    with patch("nyxgpt.app.frontmatter.loads", side_effect=ValueError("boom")):
        response = client.post(
            UPLOAD_URL,
            files={"file": ("broken.md", md_content, "text/markdown")},
        )

    assert response.status_code == 400
    assert "markdown parsing failed" in response.json()["detail"].lower()
    assert "boom" in response.json()["detail"]


# ============================================================================
# doc_id derivation: explicit vs. filename vs. generated uuid
# ============================================================================


def test_upload_doc_id_explicit_overrides_filename():
    client = TestClient(app)
    with _mock_ingest() as mock:
        response = client.post(
            UPLOAD_URL,
            files={"file": ("original.txt", b"content", "text/plain")},
            data={"doc_id": "custom-doc-id"},
        )

    assert response.status_code == 200
    assert response.json()["doc_id"] == "custom-doc-id"
    _, kwargs = mock.call_args
    assert kwargs["doc_id"] == "custom-doc-id"


def test_upload_doc_id_derived_from_filename_when_absent():
    client = TestClient(app)
    with _mock_ingest():
        response = client.post(
            UPLOAD_URL,
            files={"file": ("my-notes.txt", b"content", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["doc_id"] == "my-notes.txt"


def test_upload_doc_id_sanitizes_path_traversal_in_filename():
    """The filename is basename()'d to prevent path traversal in the derived doc_id."""
    client = TestClient(app)
    with _mock_ingest():
        response = client.post(
            UPLOAD_URL,
            files={"file": ("../../etc/passwd.txt", b"content", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["doc_id"] == "passwd.txt"


def test_upload_doc_id_generated_when_no_filename_and_no_doc_id():
    """An empty filename with no doc_id falls back to a generated upload_<uuid> id."""
    client = TestClient(app)
    with _mock_ingest():
        response = client.post(
            UPLOAD_URL,
            files={"file": ("", b"content", "text/plain")},
        )

    # An empty filename has no extension, so this should fail type validation
    # before doc_id derivation is ever reached.
    assert response.status_code == 400


def test_upload_doc_id_generated_when_filename_blank_after_strip():
    """A filename that is only whitespace still passes extension validation via
    Path(...).suffix, but strips to an empty safe_filename, so a uuid is generated.
    """
    client = TestClient(app)
    with _mock_ingest():
        response = client.post(
            UPLOAD_URL,
            files={"file": ("   .txt", b"content", "text/plain")},
        )

    assert response.status_code == 200
    doc_id = response.json()["doc_id"]
    assert doc_id.startswith("upload_")
    assert len(doc_id) == len("upload_") + 8


# ============================================================================
# Ingestion wiring: success and failure
# ============================================================================


def test_upload_ingest_success_returns_full_response_fields():
    client = TestClient(app)
    with _mock_ingest(
        status="updated", chunks_ingested=5, doc_hash="newhash", previous_hash="oldhash"
    ):
        response = client.post(
            UPLOAD_URL,
            files={"file": ("versioned.txt", b"v2 content", "text/plain")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "doc_id": "versioned.txt",
        "chunks_ingested": 5,
        "status": "updated",
        "doc_hash": "newhash",
        "previous_hash": "oldhash",
    }


def test_upload_ingest_failure_returns_500():
    client = TestClient(app)
    with patch("nyxgpt.app.ingest_document", side_effect=RuntimeError("cassandra is down")):
        response = client.post(
            UPLOAD_URL,
            files={"file": ("fails.txt", b"content", "text/plain")},
        )

    assert response.status_code == 500
    assert "ingestion failed" in response.json()["detail"].lower()
    assert "cassandra is down" in response.json()["detail"]
