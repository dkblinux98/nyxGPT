"""Unit tests for message-level session operations in src/nyxgpt/app.py.

Covers: edit_message, get_message_rag_chunks, _escape_markdown,
export_session_citations, regenerate_response, and sessions_export
(lines ~2000-2296 of src/nyxgpt/app.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt import sessions
from nyxgpt.app import _escape_markdown, app
from nyxgpt.chat import ChatResult

pytestmark = pytest.mark.unit


# ============================================================================
# Helpers
# ============================================================================


def _make_session(
    tmp_path: Path,
    name: str,
    messages: list[dict],
    meta: dict | None = None,
) -> Path:
    """Write a session file (and matching meta file) directly to tmp_path."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    sf = sessions.session_file_for(name, sessions_dir)
    sessions.save_session_messages(sf, messages)
    mf = sessions.meta_file_for(sf)
    sessions.save_session_meta(mf, meta or {"created_at": "2026-01-01T00:00:00"})
    return sessions_dir


# ============================================================================
# PATCH /api/v1/sessions/{name}/messages/{message_index}  (edit_message)
# ============================================================================


def test_edit_message_success_forks_by_default(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "edit-me",
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "another"},
        ],
    )

    client = TestClient(app)
    response = client.patch(
        "/api/v1/sessions/edit-me/messages/0",
        params={"sessions_dir": str(sessions_dir)},
        json={"content": "edited hello", "fork": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["message"] == "Message edited"

    sf = sessions.session_file_for("edit-me", sessions_dir)
    msgs = sessions.load_session_messages(sf)
    # Forked: only the edited message remains.
    assert len(msgs) == 1
    assert msgs[0]["content"] == "edited hello"
    assert msgs[0]["original_content"] == "hello"


def test_edit_message_success_without_fork_keeps_trailing_messages(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "edit-nofork",
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
    )

    client = TestClient(app)
    response = client.patch(
        "/api/v1/sessions/edit-nofork/messages/0",
        params={"sessions_dir": str(sessions_dir)},
        json={"content": "edited hello", "fork": False},
    )

    assert response.status_code == 200
    sf = sessions.session_file_for("edit-nofork", sessions_dir)
    msgs = sessions.load_session_messages(sf)
    assert len(msgs) == 2
    assert msgs[0]["content"] == "edited hello"
    assert msgs[1]["content"] == "hi there"


def test_edit_message_missing_session_returns_400(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    client = TestClient(app)
    response = client.patch(
        "/api/v1/sessions/nope/messages/0",
        params={"sessions_dir": str(sessions_dir)},
        json={"content": "x"},
    )

    assert response.status_code == 400
    assert "No such session" in response.json()["error"]["message"]


def test_edit_message_invalid_index_returns_400(tmp_path):
    sessions_dir = _make_session(
        tmp_path, "edit-badidx", [{"role": "user", "content": "hello"}]
    )

    client = TestClient(app)
    response = client.patch(
        "/api/v1/sessions/edit-badidx/messages/5",
        params={"sessions_dir": str(sessions_dir)},
        json={"content": "x"},
    )

    assert response.status_code == 400
    assert "Invalid message index" in response.json()["error"]["message"]


# ============================================================================
# GET /api/v1/sessions/{name}/messages/{message_index}/rag
# ============================================================================


def test_get_message_rag_chunks_session_not_found(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/missing/messages/0/rag",
        params={"sessions_dir": str(sessions_dir)},
    )

    assert response.status_code == 404
    assert "not found" in response.json()["error"]["message"]


def test_get_message_rag_chunks_invalid_index(tmp_path):
    sessions_dir = _make_session(
        tmp_path, "rag-badidx", [{"role": "user", "content": "hi"}]
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/rag-badidx/messages/9/rag",
        params={"sessions_dir": str(sessions_dir)},
    )

    assert response.status_code == 400
    assert "Invalid message_index" in response.json()["error"]["message"]


def test_get_message_rag_chunks_negative_index(tmp_path):
    sessions_dir = _make_session(
        tmp_path, "rag-negidx", [{"role": "user", "content": "hi"}]
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/rag-negidx/messages/-1/rag",
        params={"sessions_dir": str(sessions_dir)},
    )

    assert response.status_code == 400


def test_get_message_rag_chunks_no_rag_present(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "rag-none",
        [{"role": "assistant", "content": "answer without citations"}],
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/rag-none/messages/0/rag",
        params={"sessions_dir": str(sessions_dir)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {"message_index": 0, "has_rag": False, "chunks": []}


def test_get_message_rag_chunks_with_rag_present(tmp_path):
    chunks = [{"doc_id": "doc1", "chunk_id": "c1", "text": "excerpt", "score": 0.9}]
    sessions_dir = _make_session(
        tmp_path,
        "rag-yes",
        [{"role": "assistant", "content": "answer", "rag_chunks": chunks}],
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/rag-yes/messages/0/rag",
        params={"sessions_dir": str(sessions_dir)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["has_rag"] is True
    assert body["chunks"] == chunks


# ============================================================================
# _escape_markdown (helper)
# ============================================================================


def test_escape_markdown_escapes_special_characters():
    assert _escape_markdown("a*b_c") == "a\\*b\\_c"
    assert _escape_markdown("[link](url)") == "\\[link\\]\\(url\\)"
    assert _escape_markdown("# heading") == "\\# heading"
    assert _escape_markdown("a-b+c.d!e|f") == "a\\-b\\+c\\.d\\!e\\|f"
    assert _escape_markdown("`code` {brace}") == "\\`code\\` \\{brace\\}"


def test_escape_markdown_backslash_escaped_first():
    # Backslash must be escaped first, otherwise later replacements would
    # double-escape the backslashes they introduce.
    assert _escape_markdown("a\\b") == "a\\\\b"


def test_escape_markdown_plain_text_unchanged():
    assert _escape_markdown("hello world") == "hello world"


# ============================================================================
# GET /api/v1/sessions/{name}/citations/export
# ============================================================================


def test_export_citations_invalid_session_name():
    client = TestClient(app)
    response = client.get("/api/v1/sessions/../etc/citations/export")
    assert response.status_code in (400, 404)


def test_export_citations_invalid_name_with_slash(tmp_path):
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/foo%2Fbar/citations/export",
        params={"sessions_dir": str(tmp_path)},
    )
    assert response.status_code == 400
    assert "Invalid session name" in response.json()["error"]["message"]


def test_export_citations_invalid_format(tmp_path):
    sessions_dir = _make_session(tmp_path, "cite-fmt", [{"role": "user", "content": "hi"}])

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/cite-fmt/citations/export",
        params={"sessions_dir": str(sessions_dir), "format": "xml"},
    )
    assert response.status_code == 400
    assert "Invalid format" in response.json()["error"]["message"]


def test_export_citations_session_not_found(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/nope/citations/export",
        params={"sessions_dir": str(sessions_dir)},
    )
    assert response.status_code == 404


def test_export_citations_json_success(tmp_path):
    chunks = [
        {
            "doc_id": "doc-1",
            "chunk_id": "c-1",
            "text": "some excerpt",
            "score": 0.75,
            "similarity_score": 0.81,
        }
    ]
    sessions_dir = _make_session(
        tmp_path,
        "cite-json",
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer", "rag_chunks": chunks},
        ],
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/cite-json/citations/export",
        params={"sessions_dir": str(sessions_dir), "format": "json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session"] == "cite-json"
    assert body["total_citations"] == 1
    citation = body["citations"][0]
    assert citation["message_index"] == 1
    assert citation["citation_index"] == 0
    assert citation["doc_id"] == "doc-1"
    assert citation["chunk_id"] == "c-1"
    assert citation["similarity_score"] == 0.81


def test_export_citations_json_no_citations(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "cite-empty",
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer with no citations"},
        ],
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/cite-empty/citations/export",
        params={"sessions_dir": str(sessions_dir)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total_citations"] == 0
    assert body["citations"] == []


def test_export_citations_markdown_success(tmp_path):
    chunks = [
        {
            "doc_id": "doc*1",
            "chunk_id": "c-1",
            "text": "some [excerpt] text",
            "score": 0.5,
        },
        {
            # No chunk_id -> chunk_ref should be "source"; no similarity_score ->
            # falls back to "score"; no text -> no "Source text" line.
            "doc_id": "doc-2",
            "score": 0.42,
        },
    ]
    sessions_dir = _make_session(
        tmp_path,
        "cite-md",
        [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer", "rag_chunks": chunks},
        ],
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/cite-md/citations/export",
        params={"sessions_dir": str(sessions_dir), "format": "markdown"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert 'filename="cite-md-citations.md"' in response.headers["content-disposition"]

    text = response.text
    assert "# Citations for cite-md" in text
    assert "Total sources: 2" in text
    # First citation: doc_id escaped, chunk ref uses chunk id, source text present.
    assert "doc\\*1" in text
    assert "(chunk c-1)" in text
    assert "**Confidence:** 0.500" in text
    assert "some \\[excerpt\\] text" in text
    # Second citation: no chunk_id -> "(source)"; score fallback to 0.42; no text block.
    assert "(source)" in text
    assert "**Confidence:** 0.420" in text


# ============================================================================
# POST /api/v1/sessions/{name}/messages/{message_index}/regenerate
# ============================================================================


def test_regenerate_response_session_not_found(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/nope/messages/0/regenerate",
        params={"sessions_dir": str(sessions_dir)},
        json={},
    )
    assert response.status_code == 404
    assert "No such session" in response.json()["error"]["message"]


def test_regenerate_response_invalid_message_index(tmp_path):
    sessions_dir = _make_session(
        tmp_path, "regen-badidx", [{"role": "user", "content": "hi"}]
    )

    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/regen-badidx/messages/9/regenerate",
        params={"sessions_dir": str(sessions_dir)},
        json={},
    )
    assert response.status_code == 400
    assert "Invalid message index" in response.json()["error"]["message"]


def test_regenerate_response_non_user_message(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "regen-notuser",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    )

    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/regen-notuser/messages/1/regenerate",
        params={"sessions_dir": str(sessions_dir)},
        json={},
    )
    assert response.status_code == 400
    assert "Can only regenerate from user messages" in response.json()["error"]["message"]


def test_regenerate_response_success_without_new_prompt(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "regen-ok",
        [
            {"role": "user", "content": "original question"},
            {"role": "assistant", "content": "old answer"},
        ],
    )

    fake_result = ChatResult(
        session="regen-ok",
        model="llama3.1:8b",
        reply="new answer",
        rag_used=False,
        rag_chunks=0,
    )

    with patch("nyxgpt.app.chat_module.chat", return_value=fake_result) as mock_chat:
        client = TestClient(app)
        response = client.post(
            "/api/v1/sessions/regen-ok/messages/0/regenerate",
            params={"sessions_dir": str(sessions_dir)},
            json={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "ok": True,
        "session": "regen-ok",
        "model": "llama3.1:8b",
        "reply": "new answer",
        "rag_used": False,
    }
    mock_chat.assert_called_once()
    call_kwargs = mock_chat.call_args.kwargs
    assert call_kwargs["prompt"] == "original question"
    assert call_kwargs["session"] == "regen-ok"
    assert call_kwargs["new"] is False

    # Truncated after message 0: only the user message remains on disk.
    sf = sessions.session_file_for("regen-ok", sessions_dir)
    msgs = sessions.load_session_messages(sf)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "original question"


def test_regenerate_response_success_with_new_prompt(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "regen-newprompt",
        [
            {"role": "user", "content": "original question"},
            {"role": "assistant", "content": "old answer"},
        ],
    )

    fake_result = ChatResult(
        session="regen-newprompt",
        model="llama3.1:8b",
        reply="new answer",
        rag_used=True,
        rag_chunks=2,
    )

    with patch("nyxgpt.app.chat_module.chat", return_value=fake_result) as mock_chat:
        client = TestClient(app)
        response = client.post(
            "/api/v1/sessions/regen-newprompt/messages/0/regenerate",
            params={"sessions_dir": str(sessions_dir)},
            json={"prompt": "brand new question", "rag_enabled": True},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["rag_used"] is True
    call_kwargs = mock_chat.call_args.kwargs
    assert call_kwargs["prompt"] == "brand new question"
    assert call_kwargs["rag_enabled"] is True

    sf = sessions.session_file_for("regen-newprompt", sessions_dir)
    msgs = sessions.load_session_messages(sf)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "brand new question"
    assert msgs[0]["original_content"] == "original question"


def test_regenerate_response_edit_message_failure(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "regen-editfail",
        [{"role": "user", "content": "original question"}],
    )

    with patch("nyxgpt.app.sessions.edit_message", return_value=(False, "edit boom")):
        client = TestClient(app)
        response = client.post(
            "/api/v1/sessions/regen-editfail/messages/0/regenerate",
            params={"sessions_dir": str(sessions_dir)},
            json={"prompt": "new prompt"},
        )

    assert response.status_code == 400
    assert "edit boom" in response.json()["error"]["message"]


def test_regenerate_response_truncate_failure(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "regen-truncfail",
        [{"role": "user", "content": "original question"}],
    )

    with patch(
        "nyxgpt.app.sessions.truncate_after_message", return_value=(False, "truncate boom")
    ):
        client = TestClient(app)
        response = client.post(
            "/api/v1/sessions/regen-truncfail/messages/0/regenerate",
            params={"sessions_dir": str(sessions_dir)},
            json={},
        )

    assert response.status_code == 400
    assert "truncate boom" in response.json()["error"]["message"]


def test_regenerate_response_chat_failure_returns_500(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "regen-chatfail",
        [{"role": "user", "content": "original question"}],
    )

    with patch("nyxgpt.app.chat_module.chat", side_effect=RuntimeError("model crashed")):
        client = TestClient(app)
        response = client.post(
            "/api/v1/sessions/regen-chatfail/messages/0/regenerate",
            params={"sessions_dir": str(sessions_dir)},
            json={},
        )

    assert response.status_code == 500
    assert "Regeneration failed" in response.json()["error"]["message"]


# ============================================================================
# GET /api/v1/sessions/{name}/export  (sessions_export)
# ============================================================================


def test_sessions_export_invalid_format(tmp_path):
    sessions_dir = _make_session(tmp_path, "export-fmt", [{"role": "user", "content": "hi"}])

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/export-fmt/export",
        params={"sessions_dir": str(sessions_dir), "format": "xml"},
    )

    assert response.status_code == 400
    assert "Invalid format" in response.json()["error"]["message"]


def test_sessions_export_not_found(tmp_path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/nope/export",
        params={"sessions_dir": str(sessions_dir), "format": "markdown"},
    )

    assert response.status_code == 404


def test_sessions_export_markdown_default(tmp_path):
    sessions_dir = _make_session(
        tmp_path,
        "export-md",
        [{"role": "user", "content": "hello"}],
        meta={"created_at": "2026-01-01T00:00:00", "title": "My Session"},
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/export-md/export",
        params={"sessions_dir": str(sessions_dir)},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert 'filename="export-md.md"' in response.headers["content-disposition"]
    assert "My Session" in response.text


def test_sessions_export_json(tmp_path):
    sessions_dir = _make_session(
        tmp_path, "export-json", [{"role": "user", "content": "hello"}]
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/export-json/export",
        params={"sessions_dir": str(sessions_dir), "format": "json"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert 'filename="export-json.json"' in response.headers["content-disposition"]
    body = response.json()
    assert body["name"] == "export-json"
    assert body["messages"][0]["content"] == "hello"


def test_sessions_export_html(tmp_path):
    sessions_dir = _make_session(
        tmp_path, "export-html", [{"role": "user", "content": "hello"}]
    )

    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/export-html/export",
        params={"sessions_dir": str(sessions_dir), "format": "html"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'filename="export-html.html"' in response.headers["content-disposition"]
