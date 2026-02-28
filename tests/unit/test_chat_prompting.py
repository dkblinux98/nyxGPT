from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

import pytest

from nyxgpt.chat import chat, chat_stream
from nyxgpt.sessions import SessionState

pytestmark = pytest.mark.unit


def _cfg(tmp_path: Path, *, rag_enabled: bool) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
    }
    cfg["ollama"] = {"base_url": "http://example"}
    cfg["rag"] = {
        "enable_chat_context": "true" if rag_enabled else "false",
        "chat_top_k": "2",
        "chat_context_max_chars": "500",
    }
    cfg["cache"] = {
        "response_cache_enabled": "false",
        "embedding_cache_enabled": "false",
    }
    return cfg


def test_chat_without_rag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rag_enabled=False)

    # Ensure chat() uses our in-memory config
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    # Mock ollama_chat
    def fake_ollama_chat(*args: Any, **kwargs: Any) -> str:
        return "hello"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("hi", config_path=None)
    assert result.reply == "hello"


def test_chat_with_rag_injects_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rag_enabled=True)

    # Ensure chat() and sessions use our in-memory config
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Mock RAG retrieval
    monkeypatch.setattr(
        "nyxgpt.chat.retrieve_context",
        lambda *a, **k: [
            {"text": "RAG CONTEXT HERE", "score": 0.9},
        ],
    )

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "rag reply"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("question", config_path=None)
    assert result.reply == "rag reply"

    # The implementation may inject retrieved context as a system message or another role.
    all_text = "\n".join(m.get("content", "") for m in sent["messages"])

    assert (
        "RAG CONTEXT HERE" in all_text
        or "BEGIN RETRIEVED CONTEXT" in all_text
        or "retrieved context" in all_text.lower()
    )


def test_chat_rag_disabled_does_not_call_retrieve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Clear global cache to prevent pollution from other tests
    import nyxgpt.chat as chat_module

    chat_module._response_cache = None

    cfg = _cfg(tmp_path, rag_enabled=False)

    # Ensure chat() uses our in-memory config
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    called = {"count": 0}

    def fake_retrieve(*_: Any, **__: Any) -> str:
        called["count"] += 1
        return "SHOULD NOT BE USED"

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda **_: "ok")

    result = chat("hi", config_path=None)
    assert result.reply == "ok"
    assert called["count"] == 0


def test_chat_stream_yields_chunks_and_persists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """chat_stream should yield incremental chunks and persist the final reply."""
    cfg = _cfg(tmp_path, rag_enabled=False)

    # Ensure chat_stream() uses our in-memory config
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    # Fake streaming tokens from Ollama
    def fake_stream_tokens(*args: Any, **kwargs: Any):
        yield "hel"
        yield "lo"

    monkeypatch.setattr(
        "nyxgpt.chat.ollama_chat_stream_tokens",
        fake_stream_tokens,
    )

    # Track what gets saved
    saved = {}

    def fake_save_session(state, *_a, **_k):
        saved["messages"] = list(state.messages)

    monkeypatch.setattr("nyxgpt.chat.save_session", fake_save_session)

    # Run streaming chat
    chunks = list(chat_stream("hi", config_path=None))

    # Chunks should stream incrementally
    assert chunks == ["hel", "lo"]

    # Final assembled reply should be persisted
    last_msg = saved["messages"][-1]
    assert last_msg["role"] == "assistant"
    assert last_msg["content"] == "hello"


def test_chat_with_rag_returns_chunk_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """chat() should return RAG chunk metadata when RAG is enabled."""
    cfg = _cfg(tmp_path, rag_enabled=True)

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Mock RAG retrieval with full chunk structure
    fake_chunks = [
        {"text": "Chunk 1 text", "score": 0.95, "doc_id": "doc1", "chunk_id": 0},
        {"text": "Chunk 2 text", "score": 0.87, "doc_id": "doc1", "chunk_id": 1},
    ]
    monkeypatch.setattr("nyxgpt.chat.retrieve_context", lambda *a, **k: fake_chunks)
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda **_: "answer")

    result = chat("question", config_path=None)

    # Verify RAG context is captured
    assert result.rag_used is True
    assert result.rag_chunks == 2
    assert result.rag_context is not None
    assert len(result.rag_context) == 2
    assert result.rag_context[0]["text"] == "Chunk 1 text"
    assert result.rag_context[0]["score"] == 0.95
    assert result.rag_context[0]["doc_id"] == "doc1"
    assert result.rag_context[0]["chunk_id"] == 0


def test_chat_stream_emits_rag_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """chat_stream should emit RAG metadata as first chunk when RAG is enabled."""
    cfg = _cfg(tmp_path, rag_enabled=True)

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Mock RAG retrieval
    fake_chunks = [
        {"text": "Context text", "score": 0.92, "doc_id": "testdoc", "chunk_id": 5},
    ]
    monkeypatch.setattr("nyxgpt.chat.retrieve_context", lambda *a, **k: fake_chunks)

    # Fake streaming tokens
    def fake_stream_tokens(*args: Any, **kwargs: Any):
        yield "answer"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat_stream_tokens", fake_stream_tokens)
    monkeypatch.setattr("nyxgpt.chat.save_session", lambda *a, **k: None)

    # Run streaming chat
    chunks = list(chat_stream("question", config_path=None))

    # First chunk should be RAG metadata
    assert len(chunks) >= 2
    first_chunk = chunks[0]
    assert "__RAG_START__" in first_chunk
    assert "__RAG_END__" in first_chunk

    # Extract JSON between markers
    import json

    start_marker = "__RAG_START__"
    end_marker = "__RAG_END__"
    start_idx = first_chunk.index(start_marker) + len(start_marker)
    end_idx = first_chunk.index(end_marker)
    rag_json = first_chunk[start_idx:end_idx]
    rag_data = json.loads(rag_json)

    # Verify structure
    assert rag_data["type"] == "rag_metadata"
    assert len(rag_data["chunks"]) == 1
    assert rag_data["chunks"][0]["text"] == "Context text"
    assert rag_data["chunks"][0]["score"] == 0.92
    assert rag_data["chunks"][0]["doc_id"] == "testdoc"
    assert rag_data["chunks"][0]["chunk_id"] == 5

    # Subsequent chunks should be the actual response
    assert chunks[1] == "answer"


def test_chat_with_rag_enabled_but_no_chunks_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When RAG is enabled but no chunks are found, rag_chunks should be empty array (not absent)."""
    cfg = _cfg(tmp_path, rag_enabled=True)

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Mock RAG retrieval returning empty list
    monkeypatch.setattr("nyxgpt.chat.retrieve_context", lambda *a, **k: [])

    # Mock ollama response
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda **_: "answer without RAG context")

    # Track what gets saved
    saved = {}

    def fake_save_session(state, *_a, **_k):
        saved["messages"] = list(state.messages)

    monkeypatch.setattr("nyxgpt.chat.save_session", fake_save_session)

    # Run chat with RAG enabled but no results
    result = chat("question", config_path=None)

    # Verify response
    assert result.reply == "answer without RAG context"
    assert result.rag_used is True  # RAG was enabled and attempted
    assert result.rag_chunks == 0  # But found no chunks

    # Verify the saved message has rag_chunks field (even though empty)
    last_msg = saved["messages"][-1]
    assert last_msg["role"] == "assistant"
    assert "rag_chunks" in last_msg, "rag_chunks field should exist when RAG is enabled"
    assert last_msg["rag_chunks"] == [], "rag_chunks should be empty array when no chunks found"

    # This distinguishes "RAG was enabled but found nothing" from "RAG was disabled"
    # - Message WITHOUT rag_chunks field = RAG was disabled
    # - Message WITH rag_chunks: [] = RAG was enabled but found nothing
    # - Message WITH rag_chunks: [...] = RAG was enabled and found chunks


def test_chat_with_custom_rag_templates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that custom RAG templates are used when configured."""
    cfg = _cfg(tmp_path, rag_enabled=True)

    # Add custom templates to config
    cfg["rag"]["instruction_template"] = "CUSTOM INSTRUCTION: {context}"
    cfg["rag"]["context_format"] = "[[CUSTOM FORMAT: {context}]]"

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Mock RAG retrieval
    monkeypatch.setattr(
        "nyxgpt.chat.retrieve_context",
        lambda *a, **k: [{"text": "TEST CONTEXT", "score": 0.9}],
    )

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "answer"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("question", config_path=None)
    assert result.reply == "answer"

    # Extract the RAG system message
    all_text = "\n".join(m.get("content", "") for m in sent["messages"])

    # Verify custom templates were used
    assert "CUSTOM INSTRUCTION:" in all_text
    assert "[[CUSTOM FORMAT:" in all_text
    assert "TEST CONTEXT" in all_text

    # Verify default templates are NOT present
    assert "BEGIN RETRIEVED CONTEXT" not in all_text
    assert "Use the retrieved context below" not in all_text


def test_chat_rag_templates_default_backward_compatible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that default templates match the original hardcoded behavior."""
    cfg = _cfg(tmp_path, rag_enabled=True)

    # Don't set custom templates - should use defaults
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.sessions.load_config", lambda *_a, **_k: cfg)

    # Mock RAG retrieval
    monkeypatch.setattr(
        "nyxgpt.chat.retrieve_context",
        lambda *a, **k: [{"text": "CONTEXT TEXT", "score": 0.9}],
    )

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "answer"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("question", config_path=None)
    assert result.reply == "answer"

    # Extract the RAG system message
    all_text = "\n".join(m.get("content", "") for m in sent["messages"])

    # Verify default templates (matching original hardcoded behavior)
    assert "Use the retrieved context below when it is relevant and helpful" in all_text
    assert "--- BEGIN RETRIEVED CONTEXT ---" in all_text
    assert "--- END RETRIEVED CONTEXT ---" in all_text
    assert "CONTEXT TEXT" in all_text


# --- Force-include (attached_doc_ids) tests ---


def _make_fake_state(tmp_path: Path, meta: dict[str, Any]) -> SessionState:
    """Build a SessionState with the given meta for testing."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(exist_ok=True)
    return SessionState(
        name="test-session",
        session_file=sessions_dir / "test-session.json",
        meta_file=sessions_dir / "test-session.meta.json",
        messages=[],
        meta=meta,
    )


def test_force_include_makes_second_retrieve_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When attached_doc_ids is set, a second retrieve_context call is made with the correct filter."""
    cfg = _cfg(tmp_path, rag_enabled=True)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    fake_state = _make_fake_state(
        tmp_path, {"rag_enabled": True, "attached_doc_ids": ["doc-force-1"]}
    )
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *a, **k: fake_state)
    monkeypatch.setattr("nyxgpt.chat.save_session", lambda *a, **k: None)

    calls: list[Any] = []

    def fake_retrieve(
        prompt: str, *, debug_mode: bool = False, metadata_filter: Any = None
    ) -> list[dict]:
        calls.append(metadata_filter)
        return [{"text": "chunk", "score": 0.9, "doc_id": "doc-x", "chunk_id": 0}]

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda **_: "reply")

    result = chat("question", config_path=None)
    assert result.reply == "reply"

    # Expect two retrieve_context calls: one normal, one for force-include
    assert len(calls) == 2

    # First call: normal RAG (no filter since no rag_filters passed)
    assert calls[0] is None

    # Second call: force-include filter with the attached doc IDs
    force_filter = calls[1]
    assert force_filter is not None
    assert force_filter.doc_ids == ["doc-force-1"]


def test_force_include_rows_take_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Force-included rows appear before normal RAG rows in merged results."""
    cfg = _cfg(tmp_path, rag_enabled=True)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    fake_state = _make_fake_state(
        tmp_path, {"rag_enabled": True, "attached_doc_ids": ["doc-attached"]}
    )
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *a, **k: fake_state)
    monkeypatch.setattr("nyxgpt.chat.save_session", lambda *a, **k: None)

    call_count = {"n": 0}

    def fake_retrieve(
        prompt: str, *, debug_mode: bool = False, metadata_filter: Any = None
    ) -> list[dict]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Normal RAG results
            return [{"text": "normal chunk", "score": 0.7, "doc_id": "doc-normal", "chunk_id": 0}]
        else:
            # Force-include results
            return [{"text": "force chunk", "score": 0.5, "doc_id": "doc-attached", "chunk_id": 0}]

    captured: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        captured["messages"] = messages
        return "answer"

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    chat("question", config_path=None)

    # Force chunk should appear before normal chunk in the injected context
    all_text = "\n".join(m.get("content", "") for m in captured["messages"])
    force_pos = all_text.find("force chunk")
    normal_pos = all_text.find("normal chunk")
    assert force_pos != -1, "force chunk should appear in context"
    assert normal_pos != -1, "normal chunk should appear in context"
    assert force_pos < normal_pos, "force-included chunk should precede normal RAG chunk"


def test_force_include_deduplication(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When force-include and normal RAG return the same (doc_id, chunk_id), only one copy appears."""
    cfg = _cfg(tmp_path, rag_enabled=True)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    fake_state = _make_fake_state(
        tmp_path, {"rag_enabled": True, "attached_doc_ids": ["doc-overlap"]}
    )
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *a, **k: fake_state)
    monkeypatch.setattr("nyxgpt.chat.save_session", lambda *a, **k: None)

    duplicate_chunk = {"text": "shared chunk", "score": 0.9, "doc_id": "doc-overlap", "chunk_id": 0}
    call_count = {"n": 0}

    def fake_retrieve(
        prompt: str, *, debug_mode: bool = False, metadata_filter: Any = None
    ) -> list[dict]:
        call_count["n"] += 1
        return [duplicate_chunk]

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda **_: "answer")

    result = chat("question", config_path=None)

    # Both calls return the same chunk, but merged result should deduplicate
    assert result.rag_chunks == 1, "Deduplicated result should contain exactly one chunk"


def test_force_include_skipped_when_attached_doc_ids_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When attached_doc_ids is empty, no extra retrieve_context call is made."""
    cfg = _cfg(tmp_path, rag_enabled=True)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    fake_state = _make_fake_state(tmp_path, {"rag_enabled": True, "attached_doc_ids": []})
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *a, **k: fake_state)
    monkeypatch.setattr("nyxgpt.chat.save_session", lambda *a, **k: None)

    call_count = {"n": 0}

    def fake_retrieve(
        prompt: str, *, debug_mode: bool = False, metadata_filter: Any = None
    ) -> list[dict]:
        call_count["n"] += 1
        return []

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda **_: "answer")

    chat("question", config_path=None)

    # Only one retrieve call (normal RAG), no force-include call
    assert call_count["n"] == 1


def test_force_include_skipped_when_attached_doc_ids_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When attached_doc_ids is not in session metadata, no extra retrieve_context call is made."""
    cfg = _cfg(tmp_path, rag_enabled=True)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    # Meta has no attached_doc_ids key
    fake_state = _make_fake_state(tmp_path, {"rag_enabled": True})
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *a, **k: fake_state)
    monkeypatch.setattr("nyxgpt.chat.save_session", lambda *a, **k: None)

    call_count = {"n": 0}

    def fake_retrieve(
        prompt: str, *, debug_mode: bool = False, metadata_filter: Any = None
    ) -> list[dict]:
        call_count["n"] += 1
        return []

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda **_: "answer")

    chat("question", config_path=None)

    # Only one retrieve call (normal RAG), no force-include call
    assert call_count["n"] == 1
