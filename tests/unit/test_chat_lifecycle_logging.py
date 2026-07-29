"""Unit tests for chat lifecycle logging (#3415 gap 2).

Regression coverage for the instrumentation audit finding that `chat.py`
logged cache-init/warnings only -- no INFO record of session, model,
streaming, token counts, Ollama call duration, or outcome for a chat
request, making RCA of a slow/failed chat turn dependent on reproducing it.
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from typing import Any

import pytest

from nyxgpt.chat import chat, chat_stream

pytestmark = pytest.mark.unit


def _cfg(tmp_path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
    }
    cfg["ollama"] = {"base_url": "http://example"}
    cfg["rag"] = {"enable_chat_context": "false"}
    cfg["cache"] = {
        "response_cache_enabled": "false",
        "embedding_cache_enabled": "false",
    }
    return cfg


def test_chat_logs_start_and_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.chat.ollama_chat", lambda *a, **k: "hello")

    with caplog.at_level(logging.INFO, logger="nyxgpt.chat"):
        result = chat("hi", session="lifecycle-test", config_path=None)

    assert result.reply == "hello"
    records = {r.getMessage(): r for r in caplog.records if r.name == "nyxgpt.chat"}

    assert "Chat request started" in records
    started = records["Chat request started"]
    assert started.session == "lifecycle-test"
    assert started.model == "llama3.1:8b"
    assert started.streaming is False

    assert "Chat request completed" in records
    completed = records["Chat request completed"]
    assert completed.session == "lifecycle-test"
    assert completed.model == "llama3.1:8b"
    assert completed.streaming is False
    assert completed.outcome == "success"
    assert isinstance(completed.duration_ms, float)
    assert isinstance(completed.ollama_duration_ms, float)


class _StubCache:
    """Always returns a cached reply, so `chat()` takes the cache-hit branch."""

    def get(self, key: str) -> str | None:
        return "cached reply"

    def set(self, key: str, value: str) -> None:
        pass


def test_chat_logs_cache_hit_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.chat._get_response_cache", lambda: _StubCache())

    def fake_ollama_chat(*a: Any, **k: Any) -> str:
        raise AssertionError("ollama_chat should not be called on a cache hit")

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    with caplog.at_level(logging.INFO, logger="nyxgpt.chat"):
        result = chat("hi", session="cache-test", config_path=None)

    assert result.reply == "cached reply"
    records = [r for r in caplog.records if r.getMessage() == "Chat request completed"]
    assert records
    assert records[-1].outcome == "cache_hit"
    assert records[-1].session == "cache-test"


def test_chat_logs_failure_outcome(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    def fake_ollama_chat(*a: Any, **k: Any) -> str:
        raise RuntimeError("model runtime crashed")

    monkeypatch.setattr("nyxgpt.chat.ollama_chat", fake_ollama_chat)

    with (
        caplog.at_level(logging.INFO, logger="nyxgpt.chat"),
        pytest.raises(RuntimeError, match="model runtime crashed"),
    ):
        chat("hi", session="fail-test", config_path=None)

    records = [r for r in caplog.records if r.getMessage() == "Chat request failed"]
    assert records
    assert records[0].session == "fail-test"
    assert records[0].outcome == "error"
    assert records[0].error_type == "RuntimeError"


def test_chat_stream_logs_start_and_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    def fake_stream_tokens(*, messages: list[dict], **_: Any):
        yield "hel"
        yield "lo"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat_stream_tokens", fake_stream_tokens)

    with caplog.at_level(logging.INFO, logger="nyxgpt.chat"):
        chunks = list(chat_stream("hi", session="stream-test", config_path=None))

    assert "".join(chunks) == "hello"
    records = {r.getMessage(): r for r in caplog.records if r.name == "nyxgpt.chat"}

    assert "Chat request started" in records
    assert records["Chat request started"].streaming is True

    assert "Chat request completed" in records
    completed = records["Chat request completed"]
    assert completed.streaming is True
    assert completed.outcome == "success"
    assert isinstance(completed.duration_ms, float)
    assert isinstance(completed.ollama_duration_ms, float)
