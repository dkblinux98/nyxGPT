"""Coverage for the session message-format endpoints in src/nyxgpt/app.py:
edit_message, get_message_rag_chunks, _escape_markdown, export_session_citations,
regenerate_response, and sessions_export.

Includes the _escape_markdown regression coverage for #3233: it previously
raised AttributeError when a citation's doc_id/text was None (a chunk
persisted without that field) or a non-string value, since `.replace()`
was called directly on whatever `citation.get(...)` returned.
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


def _make_session(
    tmp_path: Path, name: str, messages: list[dict], meta: dict | None = None
) -> Path:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_file = sessions.session_file_for(name, sessions_dir)
    meta_file = sessions.meta_file_for(session_file)
    sessions.save_session_messages(session_file, messages)
    sessions.save_session_meta(meta_file, meta or {})
    return sessions_dir


# ============================================================================
# _escape_markdown
# ============================================================================


def test_escape_markdown_handles_none() -> None:
    assert _escape_markdown(None) == ""


def test_escape_markdown_handles_dict_without_crashing() -> None:
    result = _escape_markdown({"doc_id": "d1"})
    assert isinstance(result, str)
    assert "doc" in result
    assert "d1" in result


def test_escape_markdown_escapes_special_characters() -> None:
    result = _escape_markdown("*bold* [link](url) # header")
    assert result == r"\*bold\* \[link\]\(url\) \# header"


# ============================================================================
# edit_message
# ============================================================================


def test_edit_message_forks_and_updates_content(tmp_path: Path) -> None:
    sd = _make_session(
        tmp_path,
        "edit-test",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "leftover"},
        ],
    )
    client = TestClient(app)
    response = client.patch(
        "/api/v1/sessions/edit-test/messages/0",
        params={"sessions_dir": str(sd)},
        json={"content": "edited hi", "fork": True},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    msgs = sessions.load_session_messages(sessions.session_file_for("edit-test", sd))
    assert len(msgs) == 1
    assert msgs[0]["content"] == "edited hi"
    assert msgs[0]["original_content"] == "hi"


def test_edit_message_invalid_index_returns_400(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "edit-bad-index", [{"role": "user", "content": "hi"}])
    client = TestClient(app)
    response = client.patch(
        "/api/v1/sessions/edit-bad-index/messages/5",
        params={"sessions_dir": str(sd)},
        json={"content": "x"},
    )
    assert response.status_code == 400


# ============================================================================
# get_message_rag_chunks
# ============================================================================


def test_get_message_rag_chunks_returns_chunks(tmp_path: Path) -> None:
    rag_chunks = [{"doc_id": "d1", "text": "source text", "score": 0.8}]
    sd = _make_session(
        tmp_path,
        "rag-chunks-test",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello", "rag_chunks": rag_chunks},
        ],
    )
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/rag-chunks-test/messages/1/rag",
        params={"sessions_dir": str(sd)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["has_rag"] is True
    assert body["chunks"] == rag_chunks


def test_get_message_rag_chunks_no_rag_present(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "rag-chunks-empty-test", [{"role": "user", "content": "hi"}])
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/rag-chunks-empty-test/messages/0/rag",
        params={"sessions_dir": str(sd)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["has_rag"] is False
    assert body["chunks"] == []


def test_get_message_rag_chunks_session_not_found(tmp_path: Path) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/does-not-exist/messages/0/rag",
        params={"sessions_dir": str(sd)},
    )
    assert response.status_code == 404


def test_get_message_rag_chunks_invalid_index(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "rag-chunks-bad-index", [{"role": "user", "content": "hi"}])
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/rag-chunks-bad-index/messages/9/rag",
        params={"sessions_dir": str(sd)},
    )
    assert response.status_code == 400


# ============================================================================
# export_session_citations
# ============================================================================


def test_export_session_citations_rejects_path_traversal_name() -> None:
    client = TestClient(app)
    # A literal ".." path segment gets normalized away by URL routing before
    # it reaches the handler, so use a name that merely *contains* ".." --
    # this still fails the handler's own `".." in name` check.
    response = client.get("/api/v1/sessions/foo..bar/citations/export")
    assert response.status_code == 400


def test_export_session_citations_rejects_invalid_format(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "citations-bad-format", [{"role": "user", "content": "hi"}])
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/citations-bad-format/citations/export",
        params={"sessions_dir": str(sd), "format": "yaml"},
    )
    assert response.status_code == 400


def test_export_session_citations_session_not_found(tmp_path: Path) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/does-not-exist/citations/export",
        params={"sessions_dir": str(sd)},
    )
    assert response.status_code == 404


def test_export_session_citations_json_includes_all_assistant_chunks(tmp_path: Path) -> None:
    sd = _make_session(
        tmp_path,
        "citations-json-test",
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "hello",
                "rag_chunks": [
                    {"doc_id": "d1", "chunk_id": 1, "text": "t1", "score": 0.5},
                    {"doc_id": "d2", "text": "t2", "similarity_score": 0.9},
                ],
            },
        ],
    )
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/citations-json-test/citations/export",
        params={"sessions_dir": str(sd), "format": "json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["session"] == "citations-json-test"
    assert body["total_citations"] == 2
    assert body["citations"][0]["doc_id"] == "d1"
    assert body["citations"][1]["similarity_score"] == 0.9


def test_export_session_citations_markdown_handles_none_fields_without_crashing(
    tmp_path: Path,
) -> None:
    """Regression test: a citation with doc_id/text explicitly None must not
    crash _escape_markdown with AttributeError (#3233)."""
    sd = _make_session(
        tmp_path,
        "citations-md-none-test",
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "hello",
                "rag_chunks": [
                    {"doc_id": None, "chunk_id": None, "text": None, "score": 0.25},
                ],
            },
        ],
    )
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/citations-md-none-test/citations/export",
        params={"sessions_dir": str(sd), "format": "markdown"},
    )

    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert "source" in response.text  # falls back to "source" when chunk_id is None
    assert "0.250" in response.text


def test_export_session_citations_markdown_includes_chunk_ref_and_text(tmp_path: Path) -> None:
    sd = _make_session(
        tmp_path,
        "citations-md-test",
        [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "hello",
                "rag_chunks": [
                    {
                        "doc_id": "report.pdf",
                        "chunk_id": 3,
                        "text": "*emphasized* source text",
                        "similarity_score": 0.75,
                    },
                ],
            },
        ],
    )
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/citations-md-test/citations/export",
        params={"sessions_dir": str(sd), "format": "markdown"},
    )

    assert response.status_code == 200
    # chunk_id=3 is the zero-based internal key; citations display the
    # human-readable 1-based position ("chunk 4"), not the raw chunk_id.
    assert "chunk 4" in response.text
    assert r"report\.pdf" in response.text  # "." is markdown-escaped
    assert r"\*emphasized\*" in response.text
    assert 'filename="citations-md-test-citations.md"' in response.headers["content-disposition"]


# ============================================================================
# regenerate_response
# ============================================================================


def test_regenerate_response_session_not_found(tmp_path: Path) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/does-not-exist/messages/0/regenerate",
        params={"sessions_dir": str(sd)},
        json={},
    )
    assert response.status_code == 404


def test_regenerate_response_invalid_index(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "regen-bad-index", [{"role": "user", "content": "hi"}])
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/regen-bad-index/messages/9/regenerate",
        params={"sessions_dir": str(sd)},
        json={},
    )
    assert response.status_code == 400


def test_regenerate_response_rejects_non_user_message(tmp_path: Path) -> None:
    sd = _make_session(
        tmp_path,
        "regen-non-user",
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    )
    client = TestClient(app)
    response = client.post(
        "/api/v1/sessions/regen-non-user/messages/1/regenerate",
        params={"sessions_dir": str(sd)},
        json={},
    )
    assert response.status_code == 400
    assert "user messages" in response.json()["error"]["message"]


def test_regenerate_response_without_new_prompt_truncates_and_regenerates(
    tmp_path: Path,
) -> None:
    sd = _make_session(
        tmp_path,
        "regen-truncate",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "second question"},
        ],
    )
    client = TestClient(app)
    fake_result = ChatResult(
        session="regen-truncate",
        model="m",
        reply="regenerated reply",
        rag_used=False,
        rag_chunks=0,
        rag_context=None,
    )

    with patch("nyxgpt.app.chat_module.chat", return_value=fake_result) as mock_chat:
        response = client.post(
            "/api/v1/sessions/regen-truncate/messages/0/regenerate",
            params={"sessions_dir": str(sd)},
            json={},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["reply"] == "regenerated reply"
    mock_chat.assert_called_once()
    assert mock_chat.call_args.kwargs["prompt"] == "hi"

    msgs = sessions.load_session_messages(sessions.session_file_for("regen-truncate", sd))
    assert len(msgs) == 1


def test_regenerate_response_with_new_prompt_edits_before_regenerating(
    tmp_path: Path,
) -> None:
    sd = _make_session(
        tmp_path,
        "regen-edit",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
    )
    client = TestClient(app)
    fake_result = ChatResult(
        session="regen-edit",
        model="m",
        reply="new reply",
        rag_used=False,
        rag_chunks=0,
        rag_context=None,
    )

    with patch("nyxgpt.app.chat_module.chat", return_value=fake_result) as mock_chat:
        response = client.post(
            "/api/v1/sessions/regen-edit/messages/0/regenerate",
            params={"sessions_dir": str(sd)},
            json={"prompt": "revised question", "model": "override-model", "rag_enabled": True},
        )

    assert response.status_code == 200
    assert mock_chat.call_args.kwargs["prompt"] == "revised question"
    assert mock_chat.call_args.kwargs["model"] == "override-model"
    assert mock_chat.call_args.kwargs["rag_enabled"] is True

    msgs = sessions.load_session_messages(sessions.session_file_for("regen-edit", sd))
    assert msgs[0]["content"] == "revised question"


def test_regenerate_response_edit_failure_returns_400(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "regen-edit-fail", [{"role": "user", "content": "hi"}])
    client = TestClient(app)

    with patch("nyxgpt.app.sessions.edit_message", return_value=(False, "edit boom")):
        response = client.post(
            "/api/v1/sessions/regen-edit-fail/messages/0/regenerate",
            params={"sessions_dir": str(sd)},
            json={"prompt": "new prompt"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "edit boom"


def test_regenerate_response_truncate_failure_returns_400(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "regen-truncate-fail", [{"role": "user", "content": "hi"}])
    client = TestClient(app)

    with patch("nyxgpt.app.sessions.truncate_after_message", return_value=(False, "truncate boom")):
        response = client.post(
            "/api/v1/sessions/regen-truncate-fail/messages/0/regenerate",
            params={"sessions_dir": str(sd)},
            json={},
        )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "truncate boom"


def test_regenerate_response_chat_failure_returns_500(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "regen-chat-fail", [{"role": "user", "content": "hi"}])
    client = TestClient(app)

    with patch("nyxgpt.app.chat_module.chat", side_effect=RuntimeError("model boom")):
        response = client.post(
            "/api/v1/sessions/regen-chat-fail/messages/0/regenerate",
            params={"sessions_dir": str(sd)},
            json={},
        )

    assert response.status_code == 500
    assert "model boom" in response.json()["error"]["message"]


# ============================================================================
# sessions_export
# ============================================================================


def test_sessions_export_rejects_invalid_format(tmp_path: Path) -> None:
    sd = _make_session(tmp_path, "export-bad-format", [{"role": "user", "content": "hi"}])
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/export-bad-format/export",
        params={"sessions_dir": str(sd), "format": "xml"},
    )
    assert response.status_code == 400


def test_sessions_export_not_found_returns_404(tmp_path: Path) -> None:
    sd = tmp_path / "sessions"
    sd.mkdir(parents=True, exist_ok=True)
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/does-not-exist/export",
        params={"sessions_dir": str(sd), "format": "markdown"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("fmt", "content_type", "extension"),
    [
        ("markdown", "text/markdown", "md"),
        ("json", "application/json", "json"),
        ("html", "text/html", "html"),
    ],
)
def test_sessions_export_success(
    tmp_path: Path, fmt: str, content_type: str, extension: str
) -> None:
    sd = _make_session(
        tmp_path,
        "export-success-test",
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        meta={"title": "Export Test"},
    )
    client = TestClient(app)
    response = client.get(
        "/api/v1/sessions/export-success-test/export",
        params={"sessions_dir": str(sd), "format": fmt},
    )

    assert response.status_code == 200
    assert content_type in response.headers["content-type"]
    assert f'filename="export-success-test.{extension}"' in response.headers["content-disposition"]
