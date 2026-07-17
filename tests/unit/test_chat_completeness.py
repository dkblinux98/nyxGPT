"""Coverage completeness tests for chat.py.

Targets branches not already exercised by test_chat_prompting.py,
test_chat_attachments.py, test_chat_error_logging.py, test_session_rag.py,
test_system_prompt_minimize.py, and test_cache_integration.py:
- The except-fallback branches of _get_bool/_get_int/_get_str
- The disk and unknown-backend branches of _get_response_cache
- The ImportError/warning-threshold/empty-history branches of
  _truncate_messages_to_budget
- The rag_filters date-range parsing branch of _prepare_chat_context
- The retry-status marker plumbing in chat_stream
"""

from __future__ import annotations

import configparser
import logging
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from nyxgpt import chat as chat_module
from nyxgpt.chat import (
    _get_bool,
    _get_int,
    _get_response_cache,
    _get_str,
    _prepare_chat_context,
    _truncate_messages_to_budget,
    chat_stream,
    clear_response_cache,
)
from nyxgpt.sessions import SessionState

pytestmark = pytest.mark.unit


# ============================================================================
# _get_bool / _get_int / _get_str: fallback branches
# ============================================================================


class _RaisingConfig:
    """Fake config object whose typed getters always raise."""

    def __init__(self, get_value: str | None = None, get_raises: bool = False):
        self._get_value = get_value
        self._get_raises = get_raises

    def getboolean(self, *_a, **_k):
        raise TypeError("not a bool")

    def getint(self, *_a, **_k):
        raise TypeError("not an int")

    def get(self, *_a, **_k):
        if self._get_raises:
            raise TypeError("not a string")
        return self._get_value


def test_get_bool_falls_back_to_string_parsing() -> None:
    cfg = _RaisingConfig(get_value="yes")
    assert _get_bool(cfg, "section", "key", False) is True


def test_get_bool_falls_back_to_default_when_get_also_raises() -> None:
    cfg = _RaisingConfig(get_raises=True)
    assert _get_bool(cfg, "section", "key", True) is True


def test_get_int_falls_back_to_string_parsing() -> None:
    cfg = _RaisingConfig(get_value="42")
    assert _get_int(cfg, "section", "key", 0) == 42


def test_get_int_falls_back_to_default_when_get_also_raises() -> None:
    cfg = _RaisingConfig(get_raises=True)
    assert _get_int(cfg, "section", "key", 7) == 7


def test_get_str_falls_back_to_default_when_get_raises() -> None:
    cfg = _RaisingConfig(get_raises=True)
    assert _get_str(cfg, "section", "key", "fallback") == "fallback"


# ============================================================================
# _get_response_cache: disk and unknown-backend branches
# ============================================================================


class TestResponseCacheBackendSelection:
    def setup_method(self):
        clear_response_cache()
        chat_module._response_cache = None

    def teardown_method(self):
        chat_module._response_cache = None

    def test_disk_backend_initializes_disk_cache(self, tmp_path: Path) -> None:
        from nyxgpt.cache import DiskCache

        cfg = configparser.ConfigParser()
        cfg["cache"] = {
            "response_cache_enabled": "true",
            "response_cache_backend": "disk",
            "response_cache_dir": str(tmp_path / "resp-cache"),
            "response_cache_ttl_seconds": "3600",
        }

        with patch("nyxgpt.chat.load_config", return_value=cfg):
            cache = _get_response_cache()

        assert isinstance(cache, DiskCache)

    def test_unknown_backend_falls_back_to_noop_cache(self, caplog) -> None:
        from nyxgpt.cache import NoOpCache

        cfg = configparser.ConfigParser()
        cfg["cache"] = {
            "response_cache_enabled": "true",
            "response_cache_backend": "not-a-real-backend",
        }

        with (
            patch("nyxgpt.chat.load_config", return_value=cfg),
            caplog.at_level(logging.WARNING),
        ):
            cache = _get_response_cache()

        assert isinstance(cache, NoOpCache)
        assert "Unknown cache backend" in caplog.text


# ============================================================================
# _truncate_messages_to_budget
# ============================================================================


def _messages(n_conversation: int) -> list[dict]:
    msgs = [{"role": "system", "content": "sys prompt"}]
    for i in range(n_conversation):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append({"role": role, "content": f"turn {i}"})
    return msgs


def test_truncate_returns_unmodified_when_tiktoken_unavailable(caplog) -> None:
    cfg = configparser.ConfigParser()
    messages = _messages(2)

    with (
        patch("nyxgpt.chat.count_message_tokens", side_effect=ImportError("no tiktoken")),
        caplog.at_level(logging.WARNING),
    ):
        result = _truncate_messages_to_budget(messages, 1000, cfg, "m")

    assert result == messages
    assert "tiktoken not installed" in caplog.text


def test_truncate_logs_warning_when_approaching_threshold(caplog) -> None:
    cfg = configparser.ConfigParser()
    messages = _messages(2)

    # Within budget (900 <= 1000) but over the 80% warning threshold (800).
    with (
        patch("nyxgpt.chat.count_message_tokens", return_value=900),
        caplog.at_level(logging.WARNING),
    ):
        result = _truncate_messages_to_budget(messages, 1000, cfg, "m")

    assert result == messages
    assert "Context approaching limit" in caplog.text


def test_truncate_returns_unchanged_when_only_system_messages_present() -> None:
    cfg = configparser.ConfigParser()
    messages = [{"role": "system", "content": "sys prompt"}]

    # Over budget, but there's no conversation history to trim.
    with patch("nyxgpt.chat.count_message_tokens", return_value=99999):
        result = _truncate_messages_to_budget(messages, 10, cfg, "m")

    assert result == messages


def test_truncate_succeeds_partway_through_history_removal(caplog) -> None:
    cfg = configparser.ConfigParser()
    messages = _messages(4)  # system + 4 conversation turns

    # First call (initial count): over budget. Second call (first candidate,
    # after dropping oldest once): fits.
    with (
        patch("nyxgpt.chat.count_message_tokens", side_effect=[5000, 500]),
        caplog.at_level(logging.INFO),
    ):
        result = _truncate_messages_to_budget(messages, 1000, cfg, "m")

    assert "Truncated to" in caplog.text
    assert result[0]["role"] == "system"
    assert result[-1]["content"] == "turn 3"


def test_truncate_stops_when_tiktoken_fails_mid_loop_and_at_minimal(caplog) -> None:
    cfg = configparser.ConfigParser()
    messages = _messages(4)

    # Initial count over budget, then ImportError on the first candidate
    # check (breaks the while loop), then ImportError again on the final
    # "minimal" count (caught and ignored).
    with patch(
        "nyxgpt.chat.count_message_tokens",
        side_effect=[5000, ImportError("gone"), ImportError("gone")],
    ):
        result = _truncate_messages_to_budget(messages, 1000, cfg, "m")

    # Falls through to "minimal" = system + last conversation message.
    assert result[0]["role"] == "system"
    assert result[-1]["content"] == "turn 3"
    assert len(result) == 2


# ============================================================================
# _prepare_chat_context: rag_filters date-range parsing
# ============================================================================


def _rag_cfg(tmp_path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
    }
    cfg["ollama"] = {"base_url": "http://example"}
    return cfg


def _rag_state(tmp_path: Path) -> SessionState:
    return SessionState(
        name="test-session",
        session_file=tmp_path / "test-session.json",
        meta_file=tmp_path / "test-session.meta.json",
        messages=[],
        meta={},
    )


def test_rag_filters_parses_valid_date_range(monkeypatch, tmp_path: Path) -> None:
    cfg = _rag_cfg(tmp_path)
    state = _rag_state(tmp_path)

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *_a, **_k: state)
    monkeypatch.setattr("nyxgpt.chat.compose_context", lambda rows: "")

    captured_filters: list = []

    def fake_retrieve(query, **kwargs):
        captured_filters.append(kwargs.get("metadata_filter"))
        return []

    monkeypatch.setattr("nyxgpt.chat.retrieve_context", fake_retrieve)

    rag_filters = {
        "doc_ids": ["doc-1"],
        "filename": "report.pdf",
        "tags": ["finance"],
        "date_from": "2024-01-01",
        "date_to": "2024-01-31",
    }

    _prepare_chat_context("hello", rag_enabled=True, rag_filters=rag_filters)

    metadata_filter = captured_filters[0]
    assert metadata_filter is not None
    assert metadata_filter.doc_ids == ["doc-1"]
    assert metadata_filter.filename == "report.pdf"
    assert metadata_filter.tags == ["finance"]
    assert metadata_filter.date_from == datetime.fromisoformat("2024-01-01")
    assert metadata_filter.date_to == datetime.fromisoformat("2024-01-31")


def test_rag_filters_logs_warning_on_invalid_dates(monkeypatch, tmp_path: Path, caplog) -> None:
    cfg = _rag_cfg(tmp_path)
    state = _rag_state(tmp_path)

    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)
    monkeypatch.setattr("nyxgpt.chat.load_session", lambda *_a, **_k: state)
    monkeypatch.setattr("nyxgpt.chat.compose_context", lambda rows: "")
    monkeypatch.setattr("nyxgpt.chat.retrieve_context", lambda *_a, **_k: [])

    rag_filters = {"date_from": "not-a-date", "date_to": "also-not-a-date"}

    with caplog.at_level(logging.WARNING):
        _prepare_chat_context("hello", rag_enabled=True, rag_filters=rag_filters)

    assert "Invalid date_from format" in caplog.text
    assert "Invalid date_to format" in caplog.text


# ============================================================================
# chat_stream: retry-status marker plumbing
# ============================================================================


def _stream_cfg(tmp_path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {
        "default_model": "llama3.1:8b",
        "sessions_dir": str(tmp_path / "sessions"),
        "chat_timeout_seconds": "5",
    }
    cfg["ollama"] = {"base_url": "http://example"}
    cfg["rag"] = {"enable_chat_context": "false"}
    cfg["cache"] = {"response_cache_enabled": "false"}
    return cfg


def test_chat_stream_yields_retry_status_before_next_chunk(monkeypatch, tmp_path: Path) -> None:
    cfg = _stream_cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    outer_calls: list = []

    def fake_stream_tokens(*, on_retry, **_kwargs):
        on_retry(1, 0.5, RuntimeError("connection blip"))
        yield "hello"

    monkeypatch.setattr("nyxgpt.chat.ollama_chat_stream_tokens", fake_stream_tokens)

    def fake_on_retry(attempt, delay, error):
        outer_calls.append((attempt, delay, str(error)))

    chunks = list(chat_stream("hi", session="retry-test", config_path=None, on_retry=fake_on_retry))

    assert any(c.startswith("__RETRY_START__") and "retry_status" in c for c in chunks)
    assert "hello" in chunks
    # The user-supplied on_retry callback must still be invoked.
    assert outer_calls == [(1, 0.5, "connection blip")]


def test_chat_stream_flushes_retry_status_before_reraising_on_failure(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = _stream_cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    def fake_stream_tokens(*, on_retry, **_kwargs):
        on_retry(1, 0.5, RuntimeError("connection blip"))
        raise RuntimeError("Ollama HTTP 500: model runtime crashed")
        yield  # pragma: no cover - unreachable, makes this a generator

    monkeypatch.setattr("nyxgpt.chat.ollama_chat_stream_tokens", fake_stream_tokens)

    collected: list[str] = []
    with pytest.raises(RuntimeError, match="model runtime crashed"):
        for chunk in chat_stream("hi", session="retry-fail-test", config_path=None):
            collected.append(chunk)

    assert any("__RETRY_START__" in c for c in collected)
