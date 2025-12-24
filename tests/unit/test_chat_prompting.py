from __future__ import annotations

from pathlib import Path
from typing import Any
import configparser
import pytest

pytestmark = pytest.mark.unit

from mygpt.chat import chat, chat_stream




def _cfg(tmp_path: Path, *, rag_enabled: bool) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["mygpt"] = {
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
    monkeypatch.setattr("mygpt.chat.load_config", lambda *_a, **_k: cfg)

    # Mock ollama_chat
    def fake_ollama_chat(*args: Any, **kwargs: Any) -> str:
        return "hello"

    monkeypatch.setattr("mygpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("hi", config_path=None)
    assert result.reply == "hello"


def test_chat_with_rag_injects_context(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rag_enabled=True)

    # Ensure chat() uses our in-memory config
    monkeypatch.setattr("mygpt.chat.load_config", lambda *_a, **_k: cfg)

    # Mock RAG retrieval
    monkeypatch.setattr(
        "mygpt.chat.retrieve_context",
        lambda *a, **k: [
            {"text": "RAG CONTEXT HERE", "score": 0.9},
        ],
    )

    # Capture messages sent to ollama
    sent: dict[str, Any] = {}

    def fake_ollama_chat(*, messages: list[dict[str, str]], **_: Any) -> str:
        sent["messages"] = messages
        return "rag reply"

    monkeypatch.setattr("mygpt.chat.ollama_chat", fake_ollama_chat)

    result = chat("question", config_path=None)
    assert result.reply == "rag reply"

    # The implementation may inject retrieved context as a system message or another role.
    all_text = "\n".join(m.get("content", "") for m in sent["messages"])

    assert (
        "RAG CONTEXT HERE" in all_text
        or "BEGIN RETRIEVED CONTEXT" in all_text
        or "retrieved context" in all_text.lower()
    )


def test_chat_rag_disabled_does_not_call_retrieve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, rag_enabled=False)

    # Ensure chat() uses our in-memory config
    monkeypatch.setattr("mygpt.chat.load_config", lambda *_a, **_k: cfg)

    called = {"count": 0}

    def fake_retrieve(*_: Any, **__: Any) -> str:
        called["count"] += 1
        return "SHOULD NOT BE USED"

    monkeypatch.setattr("mygpt.chat.retrieve_context", fake_retrieve)
    monkeypatch.setattr("mygpt.chat.ollama_chat", lambda **_: "ok")

    result = chat("hi", config_path=None)
    assert result.reply == "ok"
    assert called["count"] == 0


def test_chat_stream_yields_chunks_and_persists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """chat_stream should yield incremental chunks and persist the final reply."""
    cfg = _cfg(tmp_path, rag_enabled=False)

    # Ensure chat_stream() uses our in-memory config
    monkeypatch.setattr("mygpt.chat.load_config", lambda *_a, **_k: cfg)

    # Fake streaming tokens from Ollama
    def fake_stream_tokens(*args: Any, **kwargs: Any):
        yield "hel"
        yield "lo"

    monkeypatch.setattr(
        "mygpt.chat.ollama_chat_stream_tokens",
        fake_stream_tokens,
    )

    # Track what gets saved
    saved = {}

    def fake_save_session(state, *_a, **_k):
        saved["messages"] = list(state.messages)

    monkeypatch.setattr("mygpt.chat.save_session", fake_save_session)

    # Run streaming chat
    chunks = list(chat_stream("hi", config_path=None))

    # Chunks should stream incrementally
    assert chunks == ["hel", "lo"]

    # Final assembled reply should be persisted
    assert saved["messages"][-1] == {"role": "assistant", "content": "hello"}
