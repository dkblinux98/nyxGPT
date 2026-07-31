"""Regression coverage for #3459: every web chat message created a new
session instead of appending to the active one.

Root cause: `persist_after_exchange()` in sessions.py auto-renamed the
session's file to match its LLM-generated title (via
`sync_filename_with_title`) as a side effect of auto-summarization, whenever
`auto_summarize_enabled` + `auto_sync_filename` were both on (both default
`true`). Neither the web client nor the CLI REPL learn about that rename --
they keep addressing the session by its original name (e.g. "default") -- so
the next turn silently created a brand-new, empty session under the old
name.

These tests exercise the real chat -> sessions persistence chain (not a
mocked `run_chat`) through the actual `/api/v1/chat` endpoint, with RAG
enabled, to prove a multi-turn conversation in a single named session
(including "default") actually appends instead of fragmenting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from nyxgpt import sessions
from nyxgpt.app import app

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_rag_and_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid any real network calls to Ollama/RAG for these API-level tests."""
    monkeypatch.setattr("nyxgpt.chat.retrieve_context", lambda *a, **k: [])


def test_chat_endpoint_appends_to_named_session_with_rag_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two POSTs to /api/v1/chat with the same session name and RAG enabled
    must append to that one session -- no new session should appear on
    disk, and the second turn's message history must contain the first."""
    sent_messages: list[list[dict[str, str]]] = []

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent_messages.append(messages)
        return f"reply {len(sent_messages)}"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    client = TestClient(app)
    sessions_dir = str(tmp_path / "sessions")

    payload_common = {
        "model": "llama3.1:8b",
        "session": "default",
        "rag_enabled": True,
        "sessions_dir": sessions_dir,
    }

    first_resp = client.post("/api/v1/chat", json={**payload_common, "prompt": "first message"})
    assert first_resp.status_code == 200
    first_body = first_resp.json()
    assert first_body["session"] == "default"

    second_resp = client.post("/api/v1/chat", json={**payload_common, "prompt": "second message"})
    assert second_resp.status_code == 200
    second_body = second_resp.json()
    assert second_body["session"] == "default"

    # The second turn must have seen the first exchange -- proving context
    # actually carries over, not just that the session file grew.
    second_call_text = "\n".join(m.get("content", "") for m in sent_messages[1])
    assert "first message" in second_call_text
    assert "reply 1" in second_call_text

    # Exactly one session exists on disk; its file has both turns persisted.
    session_files = [
        p for p in Path(sessions_dir).glob("*.json") if not p.name.endswith(".meta.json")
    ]
    assert [p.stem for p in session_files] == ["default"]

    persisted = sessions.load_session_messages(session_files[0])
    persisted_text = json.dumps(persisted)
    assert "first message" in persisted_text
    assert "second message" in persisted_text
    assert len(persisted) == 4  # 2 user + 2 assistant messages


def test_chat_endpoint_auto_summarize_does_not_fragment_default_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With auto-summarize + auto-sync-filename both enabled (the documented
    default), consecutive turns against "default" must still append -- the
    title/tags may be generated, but the session must not be renamed out
    from under the client mid-conversation."""
    sessions_dir_path = tmp_path / "sessions"
    config_path = tmp_path / "config.ini"
    config_path.write_text(
        "[nyxgpt]\n"
        f"sessions_dir = {sessions_dir_path}\n"
        "auto_summarize_enabled = true\n"
        "auto_summarize_after_messages = 2\n"
        "auto_sync_filename = true\n"
    )

    from nyxgpt.config import load_config

    cfg = load_config(config_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.app.load_config", lambda *_a, **_k: cfg)

    call_count = {"n": 0}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "first reply"
        # Second call is the auto-summarize request issued by
        # summarize_session() once the 2-message threshold is hit.
        return json.dumps({"title": "Auto Title", "summary": "s", "tags": ["t"]})

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)
    monkeypatch.setattr("nyxgpt.sessions.ollama_chat", fake_ollama_chat)

    client = TestClient(app)

    resp = client.post(
        "/api/v1/chat",
        json={
            "prompt": "hello",
            "session": "default",
            "model": "llama3.1:8b",
            "sessions_dir": str(sessions_dir_path),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["session"] == "default"

    # "default" must still be the only session on disk, and the title must
    # have been generated onto it (not a renamed copy).
    session_files = [
        p for p in sessions_dir_path.glob("*.json") if not p.name.endswith(".meta.json")
    ]
    assert [p.stem for p in session_files] == ["default"]

    meta = sessions.load_session_meta(sessions.meta_file_for(session_files[0]))
    assert meta.get("title") == "Auto Title"
