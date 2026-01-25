from __future__ import annotations

from pathlib import Path
from typing import Any
import configparser
import pytest
from nyxgpt.chat import chat, chat_stream, ContextBudgetExceededError

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


def test_chat_with_rag_injects_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_chat_stream_emits_rag_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    monkeypatch.setattr(
        "nyxgpt.chat.ollama_chat", lambda **_: "answer without RAG context"
    )

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
    assert last_msg["rag_chunks"] == [], (
        "rag_chunks should be empty array when no chunks found"
    )

    # This distinguishes "RAG was enabled but found nothing" from "RAG was disabled"
    # - Message WITHOUT rag_chunks field = RAG was disabled
    # - Message WITH rag_chunks: [] = RAG was enabled but found nothing
    # - Message WITH rag_chunks: [...] = RAG was enabled and found chunks


def test_chat_with_custom_rag_templates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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


def test_chat_raises_on_extreme_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test that chat raises ContextBudgetExceededError when minimal context exceeds budget.

    This tests the edge case where even the current prompt alone (without any
    conversation history) exceeds the context window budget. Related to #2614, #3011.
    """
    cfg = _cfg(tmp_path, rag_enabled=False)

    # Set a very small context window budget
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.chat.get_context_window_size", lambda *_a, **_k: 10
    )  # Extremely small budget

    # Mock count_message_tokens to simulate a very long prompt
    def fake_count_tokens(messages: list[dict[str, str]]) -> int:
        # System message + current prompt will exceed the budget of 10 tokens
        return 100  # Always return a count that exceeds budget

    monkeypatch.setattr("nyxgpt.chat.count_message_tokens", fake_count_tokens)

    # Mock ollama_chat (should never be called because we should raise before this)
    def fake_ollama_chat(*args: Any, **kwargs: Any) -> str:
        pytest.fail("ollama_chat should not be called when context budget is exceeded")

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    # Attempt to chat with a prompt that will exceed budget
    with pytest.raises(ContextBudgetExceededError) as exc_info:
        chat("This is a test prompt", config_path=None)

    # Verify the error message contains helpful information
    error_msg = str(exc_info.value)
    assert "exceeds context window budget" in error_msg
    assert "100/10 tokens" in error_msg
    assert "shorter prompt" in error_msg
    assert "increase context window" in error_msg


def test_chat_extreme_truncation_with_system_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Test extreme truncation when system prompt + current prompt exceeds budget.

    This tests the specific scenario mentioned in #3011 where a very long
    system prompt combined with the current prompt exceeds the budget.
    """
    cfg = _cfg(tmp_path, rag_enabled=False)

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr(
        "nyxgpt.chat.get_context_window_size", lambda *_a, **_k: 50
    )  # Small budget

    # Track how many times count_message_tokens is called
    call_count = {"count": 0}

    def fake_count_tokens(messages: list[dict[str, str]]) -> int:
        call_count["count"] += 1
        # Simulate: system + current prompt always exceeds budget
        # but conversation history would be even larger
        if call_count["count"] == 1:
            # First call: full conversation (too large)
            return 200
        else:
            # Subsequent calls during truncation: still too large even for minimal
            return 100

    monkeypatch.setattr("nyxgpt.chat.count_message_tokens", fake_count_tokens)

    # Mock ollama_chat (should not be called)
    monkeypatch.setattr(
        "nyxgpt.chat.ollama_chat",
        lambda *a, **k: pytest.fail("Should not call ollama_chat"),
    )

    # Provide a custom system prompt to ensure we hit the edge case
    with pytest.raises(ContextBudgetExceededError) as exc_info:
        chat(
            "Test prompt",
            system="This is a very long system prompt that exceeds the budget",
            config_path=None,
        )

    # Verify error message
    error_msg = str(exc_info.value)
    assert "100/50 tokens" in error_msg
    assert "exceeds context window budget" in error_msg
