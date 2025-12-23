from __future__ import annotations

from pathlib import Path
from typing import Any
import configparser
import pytest

pytestmark = pytest.mark.unit

from mygpt.chat import chat




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
        lambda *a, **k: "RAG CONTEXT HERE",
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
