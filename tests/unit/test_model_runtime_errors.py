"""Unit tests for issue #3192: chat must surface a clear, actionable error
when the Ollama/llama-server model runtime fails (crash, OOM, timeout)
instead of an opaque bare 500.

Covers:
- ollama_client.py classifying upstream failures into ModelRuntimeError
  with an actionable message vs. leaving other errors as plain RuntimeError.
- app.py's /chat endpoint mapping ModelRuntimeError to 502 (both the direct
  and batch-processor code paths), and a batch-submit timeout to 504.
- A regression guard for a bug found while fixing this: /chat's catch-all
  `except Exception` was swallowing already-raised HTTPExceptions (e.g. the
  batch-error branch) into a generic 500, discarding the real status/detail.
- The batch-submit timeout being derived from chat_timeout_seconds instead
  of a hardcoded value unrelated to it.
"""

from __future__ import annotations

import configparser
import http.client
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.config import get_chat_timeout_seconds, load_config
from nyxgpt.ollama_client import ModelRuntimeError, post_json, post_json_lines

pytestmark = pytest.mark.unit


# ============================================================================
# ollama_client: error classification
# ============================================================================


def _http_error(code: int, body: bytes = b'{"error": "boom"}') -> urllib.error.HTTPError:
    resp = MagicMock()
    resp.read.return_value = body
    return urllib.error.HTTPError(url="http://x/api/chat", code=code, msg="err", hdrs={}, fp=resp)


def test_post_json_5xx_raises_model_runtime_error() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(500)),
        pytest.raises(ModelRuntimeError, match="model runtime returned an error"),
    ):
        post_json("http://x/api/chat", {"model": "m"})


def test_post_json_4xx_raises_plain_runtime_error() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(400, b'{"error": "bad request"}')),
        pytest.raises(RuntimeError) as excinfo,
    ):
        post_json("http://x/api/chat", {"model": "m"})
    assert not isinstance(excinfo.value, ModelRuntimeError)


def test_post_json_timeout_raises_model_runtime_error() -> None:
    timeout_err = urllib.error.URLError(TimeoutError("timed out"))
    with (
        patch("urllib.request.urlopen", side_effect=timeout_err),
        pytest.raises(ModelRuntimeError, match="may require more memory"),
    ):
        post_json("http://x/api/chat", {"model": "m"}, timeout_s=5)


def test_post_json_connection_refused_stays_plain_runtime_error() -> None:
    conn_err = urllib.error.URLError(ConnectionRefusedError("refused"))
    with (
        patch("urllib.request.urlopen", side_effect=conn_err),
        pytest.raises(RuntimeError) as excinfo,
    ):
        post_json("http://x/api/chat", {"model": "m"})
    assert not isinstance(excinfo.value, ModelRuntimeError)
    assert "Failed to reach Ollama" in str(excinfo.value)


def test_post_json_lines_5xx_raises_model_runtime_error() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(500)),
        pytest.raises(ModelRuntimeError, match="model runtime returned an error"),
    ):
        list(post_json_lines("http://x/api/chat", {"model": "m"}, max_retries=0))


def test_post_json_lines_timeout_after_retries_raises_model_runtime_error() -> None:
    timeout_err = urllib.error.URLError(TimeoutError("timed out"))
    with (
        patch("urllib.request.urlopen", side_effect=timeout_err),
        patch("time.sleep"),
        pytest.raises(ModelRuntimeError, match="may require more memory"),
    ):
        list(post_json_lines("http://x/api/chat", {"model": "m"}, max_retries=1))


def test_post_json_lines_connection_dropped_mid_stream_raises_model_runtime_error() -> None:
    """A crash mid-generation drops the socket instead of returning an HTTP error."""
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.close = MagicMock()

    def _iter_then_fail():
        yield b'{"message": {"content": "partial"}, "done": false}\n'
        raise ConnectionResetError("Connection reset by peer")

    mock_response.__iter__ = MagicMock(side_effect=_iter_then_fail)

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        pytest.raises(ModelRuntimeError, match="connection dropped"),
    ):
        list(post_json_lines("http://x/api/chat", {"model": "m"}, max_retries=0))


def test_post_json_lines_incomplete_read_raises_model_runtime_error() -> None:
    mock_response = MagicMock()
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=None)
    mock_response.close = MagicMock()

    def _iter_then_fail():
        yield b'{"message": {"content": "partial"}, "done": false}\n'
        raise http.client.IncompleteRead(b"")

    mock_response.__iter__ = MagicMock(side_effect=_iter_then_fail)

    with (
        patch("urllib.request.urlopen", return_value=mock_response),
        pytest.raises(ModelRuntimeError, match="connection dropped"),
    ):
        list(post_json_lines("http://x/api/chat", {"model": "m"}, max_retries=0))


# ============================================================================
# config: chat_timeout_seconds accessor
# ============================================================================


def test_get_chat_timeout_seconds_default() -> None:
    """300s, raised from 180 when the shipped chat model grew (#3987's CI escalation).

    `qwen3:0.6b` -> `qwen3.5:0.8b` pushed first-token latency on a CPU-only
    runner past 180s, so `terraform-local-smoke`'s chat returned 500 at exactly
    three minutes -- a timeout wearing an error's clothes, on three unrelated
    branches at once. The number is a property of the shipped model's size, so
    it lives next to the model default rather than being tuned per-caller.
    """
    cfg = configparser.ConfigParser()
    assert get_chat_timeout_seconds(cfg) == 300


def test_get_chat_timeout_seconds_reads_config() -> None:
    cfg = configparser.ConfigParser()
    cfg["nyxgpt"] = {"chat_timeout_seconds": "42"}
    assert get_chat_timeout_seconds(cfg) == 42


# ============================================================================
# /api/v1/chat: model runtime failures surface as actionable 502s
# ============================================================================


def _defaults(chat_timeout_seconds: int = 5) -> dict:
    return {
        "cfg": load_config(None),
        "default_model": "test-model",
        "rag_enabled": False,
        "chat_timeout_seconds": chat_timeout_seconds,
    }


def test_chat_endpoint_model_runtime_error_returns_502() -> None:
    client = TestClient(app)

    with (
        patch("nyxgpt.app._chat_runtime_defaults", return_value=_defaults()),
        patch("nyxgpt.app._batch_processor", None),
        patch(
            "nyxgpt.app.run_chat",
            side_effect=ModelRuntimeError(
                "Model failed to run — it may require more memory than is available "
                "on this host (Ollama HTTP 500: ...)"
            ),
        ),
    ):
        response = client.post(
            "/api/v1/chat",
            json={"prompt": "hi", "session": "runtime-error-test"},
        )

    assert response.status_code == 502
    assert "may require more memory" in response.json()["error"]["message"]


def test_chat_endpoint_generic_error_still_returns_500() -> None:
    """Non-runtime failures must keep the existing opaque-by-design 500 --
    only model-runtime failures get the more specific treatment."""
    client = TestClient(app)

    with (
        patch("nyxgpt.app._chat_runtime_defaults", return_value=_defaults()),
        patch("nyxgpt.app._batch_processor", None),
        patch("nyxgpt.app.run_chat", side_effect=RuntimeError("something else broke")),
    ):
        response = client.post(
            "/api/v1/chat",
            json={"prompt": "hi", "session": "generic-error-test"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Internal server error"


def test_chat_endpoint_batch_model_runtime_error_returns_502() -> None:
    """The batch-processor path must classify errors the same way as the
    direct path, and the HTTPException it raises must not be flattened into
    a generic 500 by the outer catch-all (regression: it previously was)."""
    client = TestClient(app)

    fake_batch_processor = MagicMock()
    fake_batch_processor.submit.return_value = {
        "error": "Model failed to run — it may require more memory than is available on this host (...)",
        "error_type": "ModelRuntimeError",
    }

    with (
        patch("nyxgpt.app._chat_runtime_defaults", return_value=_defaults()),
        patch("nyxgpt.app._batch_processor", fake_batch_processor),
    ):
        response = client.post(
            "/api/v1/chat",
            json={"prompt": "hi", "session": "batch-runtime-error-test"},
        )

    assert response.status_code == 502
    assert "may require more memory" in response.json()["error"]["message"]


def test_chat_endpoint_batch_generic_error_returns_500() -> None:
    client = TestClient(app)

    fake_batch_processor = MagicMock()
    fake_batch_processor.submit.return_value = {
        "error": "some other batch failure",
        "error_type": "ValueError",
    }

    with (
        patch("nyxgpt.app._chat_runtime_defaults", return_value=_defaults()),
        patch("nyxgpt.app._batch_processor", fake_batch_processor),
    ):
        response = client.post(
            "/api/v1/chat",
            json={"prompt": "hi", "session": "batch-generic-error-test"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "some other batch failure"


def test_chat_endpoint_batch_submit_uses_chat_timeout_seconds() -> None:
    """The batch-submit wait must track chat_timeout_seconds, not a fixed
    30s -- a mismatch here causes premature timeouts on slow cold loads."""
    client = TestClient(app)

    fake_batch_processor = MagicMock()
    fake_batch_processor.submit.return_value = {
        "session": "batch-timeout-align-test",
        "model": "test-model",
        "reply": "ok",
        "rag_used": False,
        "rag_context": None,
    }

    with (
        patch(
            "nyxgpt.app._chat_runtime_defaults", return_value=_defaults(chat_timeout_seconds=250)
        ),
        patch("nyxgpt.app._batch_processor", fake_batch_processor),
    ):
        response = client.post(
            "/api/v1/chat",
            json={"prompt": "hi", "session": "batch-timeout-align-test"},
        )

    assert response.status_code == 200
    _args, kwargs = fake_batch_processor.submit.call_args
    assert kwargs["timeout"] == 260  # chat_timeout_seconds + 10


def test_chat_endpoint_batch_submit_timeout_returns_504() -> None:
    client = TestClient(app)

    fake_batch_processor = MagicMock()
    fake_batch_processor.submit.side_effect = TimeoutError("Request timed out after 15s")

    with (
        patch("nyxgpt.app._chat_runtime_defaults", return_value=_defaults(chat_timeout_seconds=5)),
        patch("nyxgpt.app._batch_processor", fake_batch_processor),
    ):
        response = client.post(
            "/api/v1/chat",
            json={"prompt": "hi", "session": "batch-timeout-test"},
        )

    assert response.status_code == 504
    assert "timed out" in response.json()["error"]["message"]
