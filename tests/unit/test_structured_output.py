"""Unit tests for structured output (output_format) support."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------
# ollama_client: ollama_chat with output_format
# ------------------------------------------------------------------


@patch("nyxgpt.ollama_client.post_json")
def test_ollama_chat_without_format(mock_post_json):
    """ollama_chat sends no 'format' key when output_format is None."""
    from nyxgpt.ollama_client import ollama_chat

    mock_post_json.return_value = {"message": {"content": "hello"}}
    ollama_chat("http://localhost:11434", "llama3.1:8b", [{"role": "user", "content": "hi"}])

    _, call_args, _ = mock_post_json.mock_calls[0]
    payload = call_args[1]
    assert "format" not in payload


@patch("nyxgpt.ollama_client.post_json")
def test_ollama_chat_with_output_format(mock_post_json):
    """ollama_chat includes 'format' in payload when output_format is provided."""
    from nyxgpt.ollama_client import ollama_chat

    mock_post_json.return_value = {"message": {"content": '{"age": 30}'}}
    schema = {"type": "object", "properties": {"age": {"type": "integer"}}, "required": ["age"]}
    result = ollama_chat(
        "http://localhost:11434",
        "llama3.1:8b",
        [{"role": "user", "content": "How old?"}],
        output_format=schema,
    )

    assert result == '{"age": 30}'
    _, call_args, _ = mock_post_json.mock_calls[0]
    payload = call_args[1]
    assert payload["format"] == schema


@patch("nyxgpt.ollama_client.post_json_lines")
def test_ollama_chat_stream_tokens_without_format(mock_post_json_lines):
    """Streaming: no 'format' key when output_format is None."""
    from nyxgpt.ollama_client import ollama_chat_stream_tokens

    mock_post_json_lines.return_value = [
        {"message": {"content": "hi"}},
        {"done": True},
    ]
    list(
        ollama_chat_stream_tokens(
            "http://localhost:11434",
            "llama3.1:8b",
            [{"role": "user", "content": "hi"}],
        )
    )

    _, call_args, _ = mock_post_json_lines.mock_calls[0]
    payload = call_args[1]
    assert "format" not in payload


@patch("nyxgpt.ollama_client.post_json_lines")
def test_ollama_chat_stream_tokens_with_output_format(mock_post_json_lines):
    """Streaming: 'format' is included in payload when output_format is provided."""
    from nyxgpt.ollama_client import ollama_chat_stream_tokens

    mock_post_json_lines.return_value = [
        {"message": {"content": '{"ok":'}},
        {"message": {"content": "true}"}},
        {"done": True},
    ]
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
    tokens = list(
        ollama_chat_stream_tokens(
            "http://localhost:11434",
            "llama3.1:8b",
            [{"role": "user", "content": "test"}],
            output_format=schema,
        )
    )

    _, call_args, _ = mock_post_json_lines.mock_calls[0]
    payload = call_args[1]
    assert payload["format"] == schema
    assert "".join(tokens) == '{"ok":true}'


# ------------------------------------------------------------------
# ChatRequest: output_format field
# ------------------------------------------------------------------


def test_chat_request_output_format_default_none():
    from nyxgpt.api_models import ChatRequest

    req = ChatRequest(prompt="hello")
    assert req.output_format is None


def test_chat_request_output_format_schema():
    from nyxgpt.api_models import ChatRequest

    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    req = ChatRequest(prompt="hello", output_format=schema)
    assert req.output_format == schema


# ------------------------------------------------------------------
# chat(): output_format threads through to ollama_chat
# ------------------------------------------------------------------


@patch("nyxgpt.chat.ollama_chat")
@patch("nyxgpt.chat.load_session")
@patch("nyxgpt.chat.save_session")
@patch("nyxgpt.chat._get_response_cache")
@patch("nyxgpt.chat.load_config")
def test_chat_passes_output_format(
    mock_load_config,
    mock_get_cache,
    mock_save_session,
    mock_load_session,
    mock_ollama_chat,
):
    """chat() passes output_format to ollama_chat."""
    from configparser import ConfigParser

    from nyxgpt.chat import chat

    cfg = ConfigParser()
    cfg.read_dict(
        {
            "ollama": {"base_url": "http://localhost:11434"},
            "nyxgpt": {"default_model": "llama3.1:8b", "chat_timeout_seconds": "30"},
            "rag": {"enable_chat_context": "false"},
            "cache": {"response_cache_enabled": "false"},
        }
    )
    mock_load_config.return_value = cfg

    state = MagicMock()
    state.messages = []
    state.meta = {}
    state.name = "default"
    mock_load_session.return_value = state

    cache = MagicMock()
    cache.get.return_value = None
    mock_get_cache.return_value = cache

    mock_ollama_chat.return_value = '{"result": "ok"}'

    schema = {"type": "object", "properties": {"result": {"type": "string"}}}
    result = chat("test prompt", output_format=schema)

    assert result.reply == '{"result": "ok"}'
    mock_ollama_chat.assert_called_once()
    call_kwargs = mock_ollama_chat.call_args[1]
    assert call_kwargs["output_format"] == schema


@patch("nyxgpt.chat.ollama_chat")
@patch("nyxgpt.chat.load_session")
@patch("nyxgpt.chat.save_session")
@patch("nyxgpt.chat._get_response_cache")
@patch("nyxgpt.chat.load_config")
def test_chat_without_output_format(
    mock_load_config,
    mock_get_cache,
    mock_save_session,
    mock_load_session,
    mock_ollama_chat,
):
    """chat() passes output_format=None to ollama_chat when not specified."""
    from configparser import ConfigParser

    from nyxgpt.chat import chat

    cfg = ConfigParser()
    cfg.read_dict(
        {
            "ollama": {"base_url": "http://localhost:11434"},
            "nyxgpt": {"default_model": "llama3.1:8b", "chat_timeout_seconds": "30"},
            "rag": {"enable_chat_context": "false"},
            "cache": {"response_cache_enabled": "false"},
        }
    )
    mock_load_config.return_value = cfg

    state = MagicMock()
    state.messages = []
    state.meta = {}
    state.name = "default"
    mock_load_session.return_value = state

    cache = MagicMock()
    cache.get.return_value = None
    mock_get_cache.return_value = cache

    mock_ollama_chat.return_value = "plain response"

    chat("test prompt")

    call_kwargs = mock_ollama_chat.call_args[1]
    assert call_kwargs.get("output_format") is None


# ------------------------------------------------------------------
# models/pull SSE progress: unit-level smoke test
# ------------------------------------------------------------------


@patch("nyxgpt.models.post_json_lines")
def test_pull_model_progress_callback(mock_post_json_lines):
    """pull_model calls progress_callback with status and percentage."""
    from nyxgpt.models import pull_model

    mock_post_json_lines.return_value = [
        {"status": "pulling manifest", "completed": 0, "total": 0},
        {"status": "downloading", "completed": 512, "total": 1024},
        {"status": "success", "completed": 1024, "total": 1024},
    ]

    calls: list[tuple[str, float]] = []

    def _cb(status: str, pct: float) -> None:
        calls.append((status, pct))

    pull_model("llama3.1:8b", base_url="http://localhost:11434", progress_callback=_cb)

    assert len(calls) == 3
    assert calls[0] == ("pulling manifest", 0.0)
    assert calls[1] == ("downloading", 50.0)
    assert calls[2] == ("success", 100.0)
