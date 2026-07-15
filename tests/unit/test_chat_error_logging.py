"""Unit tests verifying chat/generate failures are logged with actionable detail.

Regression coverage for #3191: a chat request that 500'd on an upstream
Ollama/model-runtime crash left no trace in nyxgpt.log, so the operator
couldn't diagnose the failure from the View Logs UI. Both the streaming SSE
endpoint (src/nyxgpt/app.py::_stream_sse) and the chat_stream() generator
itself (src/nyxgpt/chat.py) previously caught the exception, forwarded it to
the client, and re-raised without ever calling log.error/logger.error.
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.chat import chat_stream

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


def test_chat_stream_logs_upstream_ollama_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """chat_stream() must log model-runtime failures before re-raising.

    This covers non-API callers (CLI, MCP tool) that invoke chat_stream()
    directly and would otherwise have no server-side record of the failure.
    """
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("nyxgpt.chat.load_config", lambda *_a, **_k: cfg)

    def fake_stream_tokens(*, messages, **_):
        yield "partial reply"
        raise RuntimeError('Ollama HTTP 500: {"error": "model runtime crashed"}')

    monkeypatch.setattr("nyxgpt.chat.ollama_chat_stream_tokens", fake_stream_tokens)

    with (
        caplog.at_level(logging.ERROR, logger="nyxgpt.chat"),
        pytest.raises(RuntimeError, match="model runtime crashed"),
    ):
        list(
            chat_stream(
                "hello",
                session="diag-test",
                config_path=None,
            )
        )

    error_records = [r for r in caplog.records if r.name == "nyxgpt.chat"]
    assert error_records, "Expected chat_stream to log the upstream failure"
    record = error_records[-1]
    assert record.session == "diag-test"
    assert record.model == "llama3.1:8b"
    assert "model runtime crashed" in record.error
    assert record.error_type == "RuntimeError"


def test_chat_stream_api_endpoint_logs_upstream_error(caplog: pytest.LogCaptureFixture) -> None:
    """The /chat/stream endpoint must log the failure before it's lost.

    Once the StreamingResponse has started, the client only ever sees a
    generic SSE `error` event -- nyxgpt.log (the file the View Logs UI
    shows) is the only place an operator can see what actually happened.
    """
    # Real clients never see in-process exceptions from the streaming
    # generator -- Starlette swallows them once the response has started.
    # Match that behavior so this test observes what an operator actually
    # gets: nothing from the client, only the server-side log.
    client = TestClient(app, raise_server_exceptions=False)

    def mock_chat_stream(*args, **kwargs):
        yield "partial"
        raise RuntimeError("Ollama HTTP 500: model runtime crashed")

    with (
        caplog.at_level(logging.ERROR, logger="nyxgpt.api"),
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "hi",
                "session": "diag-api-test",
                "model": "diag-model",
            },
        ) as response,
    ):
        list(response.iter_text())

    error_records = [
        r for r in caplog.records if r.name == "nyxgpt.api" and r.message == "Chat stream failed"
    ]
    assert error_records, "Expected the streaming endpoint to log the upstream failure"
    record = error_records[-1]
    assert record.session == "diag-api-test"
    assert record.model == "diag-model"
    assert "model runtime crashed" in record.error
    assert record.error_type == "RuntimeError"
