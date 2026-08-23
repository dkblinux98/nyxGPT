"""Coverage completeness tests for ollama_client.py.

Targets the branches not already exercised by test_model_runtime_errors.py
(error classification) and test_ollama_reconnect.py (retry/backoff): the
success paths of post_json/post_json_lines/ollama_chat/ollama_chat_stream,
delete_json, blank-line skipping in the streaming reader, non-5xx HTTP
errors while streaming, and the retry-loop's final fallback branch.
"""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from nyxgpt.ollama_client import (
    _retry_with_backoff,
    delete_json,
    ollama_chat,
    ollama_chat_stream,
    ollama_chat_stream_tokens,
    post_json,
    post_json_lines,
)

pytestmark = pytest.mark.unit


def _mock_response(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=None)
    resp.read.return_value = body
    return resp


def _http_error(code: int, body: bytes = b'{"error": "boom"}') -> urllib.error.HTTPError:
    fp = MagicMock()
    fp.read.return_value = body
    return urllib.error.HTTPError(url="http://x/api/chat", code=code, msg="err", hdrs={}, fp=fp)


def _streaming_response(lines: list[bytes]) -> MagicMock:
    resp = MagicMock()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=None)
    resp.close = MagicMock()
    resp.__iter__ = MagicMock(return_value=iter(lines))
    return resp


# ============================================================================
# post_json: success path
# ============================================================================


def test_post_json_success_returns_parsed_body() -> None:
    with patch("urllib.request.urlopen", return_value=_mock_response(b'{"message": "ok"}')):
        result = post_json("http://x/api/chat", {"model": "m"})
    assert result == {"message": "ok"}


def test_post_json_success_empty_body_returns_empty_dict() -> None:
    with patch("urllib.request.urlopen", return_value=_mock_response(b"")):
        result = post_json("http://x/api/chat", {"model": "m"})
    assert result == {}


# ============================================================================
# post_json_lines: non-5xx HTTP error and non-timeout URLError after retries
# ============================================================================


def test_post_json_lines_4xx_raises_plain_runtime_error() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(400, b'{"error": "bad"}')),
        pytest.raises(RuntimeError) as excinfo,
    ):
        list(post_json_lines("http://x/api/chat", {"model": "m"}, max_retries=0))
    assert "Ollama HTTP 400" in str(excinfo.value)


def test_post_json_lines_connection_refused_after_retries_raises_plain_runtime_error() -> None:
    conn_err = urllib.error.URLError(ConnectionRefusedError("refused"))
    with (
        patch("urllib.request.urlopen", side_effect=conn_err),
        patch("time.sleep"),
        pytest.raises(RuntimeError) as excinfo,
    ):
        list(post_json_lines("http://x/api/chat", {"model": "m"}, max_retries=1))
    assert "Failed to reach Ollama" in str(excinfo.value)


def test_post_json_lines_skips_blank_lines_in_stream() -> None:
    resp = _streaming_response(
        [b"\n", b"   \n", b'{"message": {"content": "hi"}, "done": false}\n']
    )
    with patch("urllib.request.urlopen", return_value=resp):
        results = list(post_json_lines("http://x/api/chat", {"model": "m"}, max_retries=0))
    assert results == [{"message": {"content": "hi"}, "done": False}]


# ============================================================================
# ollama_chat / ollama_chat_stream: success paths and unexpected response
# ============================================================================


def test_ollama_chat_returns_content() -> None:
    resp = _mock_response(b'{"message": {"content": "hello there"}}')
    with patch("urllib.request.urlopen", return_value=resp):
        reply = ollama_chat("http://x", "m", [{"role": "user", "content": "hi"}])
    assert reply == "hello there"


def test_ollama_chat_includes_output_format_in_payload() -> None:
    resp = _mock_response(b'{"message": {"content": "ok"}}')
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}

    with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
        ollama_chat("http://x", "m", [{"role": "user", "content": "hi"}], output_format=schema)

    sent_request = mock_urlopen.call_args[0][0]
    sent_payload = json.loads(sent_request.data.decode("utf-8"))
    assert sent_payload["format"] == schema


def test_ollama_chat_sends_think_when_set_and_omits_it_when_not() -> None:
    """`think` reaches the wire, and absent means absent -- not `false`.

    Sending `think: false` unconditionally would override a model's own
    default for callers that never asked; `None` has to stay off the payload.
    """
    for value in (False, True):
        resp = _mock_response(b'{"message": {"content": "ok"}}')
        with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
            ollama_chat("http://x", "m", [{"role": "user", "content": "hi"}], think=value)
        sent = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
        assert sent["think"] is value

    resp = _mock_response(b'{"message": {"content": "ok"}}')
    with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
        ollama_chat("http://x", "m", [{"role": "user", "content": "hi"}])
    sent = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert "think" not in sent


def test_ollama_chat_stream_sends_think() -> None:
    resp = _streaming_response([b'{"message": {"content": "a"}, "done": true}'])
    with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
        list(
            ollama_chat_stream_tokens(
                "http://x", "m", [{"role": "user", "content": "hi"}], think=False
            )
        )
    sent = json.loads(mock_urlopen.call_args[0][0].data.decode("utf-8"))
    assert sent["think"] is False


def test_reasoning_with_no_answer_is_an_error_not_an_empty_reply() -> None:
    """The defect that reddened three smoke families (#4028).

    A reasoning model that spends its whole budget thinking answers HTTP 200
    with empty `content` and a full `thinking`. Returning "" made that
    indistinguishable from a model with nothing to say: a blank reply, no
    error, and every smoke forced to discover it by asserting on emptiness.
    """
    resp = _mock_response(b'{"message": {"content": "", "thinking": "step 1... step 2..."}}')
    with (
        patch("urllib.request.urlopen", return_value=resp),
        pytest.raises(RuntimeError, match="reasoning but no answer"),
    ):
        ollama_chat("http://x", "m", [{"role": "user", "content": "hi"}])


def test_streaming_reasoning_with_no_answer_is_also_an_error() -> None:
    """The web UI streams, so the silent blank had to be closed there too.

    Raising on `done` is safe precisely because nothing was yielded: there is
    no half-emitted answer for the error to contradict.
    """
    resp = _streaming_response(
        [
            b'{"message": {"thinking": "weighing it up..."}}',
            b'{"message": {"thinking": "still weighing..."}, "done": true}',
        ]
    )
    with (
        patch("urllib.request.urlopen", return_value=resp),
        pytest.raises(RuntimeError, match="reasoning but no answer"),
    ):
        list(ollama_chat_stream_tokens("http://x", "m", [{"role": "user", "content": "hi"}]))


def test_streaming_thinking_alongside_a_real_answer_is_not_an_error() -> None:
    """Reasoning is only a problem when it replaces the answer, not when it precedes it."""
    resp = _streaming_response(
        [
            b'{"message": {"thinking": "hmm"}}',
            b'{"message": {"content": "OK"}, "done": true}',
        ]
    )
    with patch("urllib.request.urlopen", return_value=resp):
        chunks = list(
            ollama_chat_stream_tokens("http://x", "m", [{"role": "user", "content": "hi"}])
        )
    assert "".join(chunks) == "OK"


def test_an_ordinary_empty_reply_is_still_returned_not_raised() -> None:
    """Only reasoning-with-no-answer is an error; an empty answer is an answer."""
    resp = _mock_response(b'{"message": {"content": ""}}')
    with patch("urllib.request.urlopen", return_value=resp):
        assert ollama_chat("http://x", "m", [{"role": "user", "content": "hi"}]) == ""


def test_ollama_chat_unexpected_response_raises() -> None:
    resp = _mock_response(b'{"message": {"content": 123}}')
    with (
        patch("urllib.request.urlopen", return_value=resp),
        pytest.raises(RuntimeError, match="Unexpected Ollama response"),
    ):
        ollama_chat("http://x", "m", [{"role": "user", "content": "hi"}])


def test_ollama_chat_stream_joins_token_chunks() -> None:
    resp = _streaming_response(
        [
            b'{"message": {"content": "hel"}, "done": false}\n',
            b'{"message": {"content": "lo"}, "done": true}\n',
        ]
    )
    with patch("urllib.request.urlopen", return_value=resp):
        reply = ollama_chat_stream("http://x", "m", [{"role": "user", "content": "hi"}])
    assert reply == "hello"


# ============================================================================
# delete_json
# ============================================================================


def test_ollama_chat_stream_tokens_includes_output_format_in_payload() -> None:
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    resp = _streaming_response([b'{"message": {"content": "hi"}, "done": true}\n'])

    with patch("urllib.request.urlopen", return_value=resp) as mock_urlopen:
        list(
            ollama_chat_stream_tokens(
                "http://x", "m", [{"role": "user", "content": "hi"}], output_format=schema
            )
        )

    sent_request = mock_urlopen.call_args[0][0]
    sent_payload = json.loads(sent_request.data.decode("utf-8"))
    assert sent_payload["format"] == schema


def test_delete_json_success_returns_parsed_body() -> None:
    with patch("urllib.request.urlopen", return_value=_mock_response(b'{"deleted": true}')):
        result = delete_json("http://x/api/delete", {"model": "m"})
    assert result == {"deleted": True}


def test_delete_json_success_empty_body_returns_empty_dict() -> None:
    with patch("urllib.request.urlopen", return_value=_mock_response(b"")):
        result = delete_json("http://x/api/delete", {"model": "m"})
    assert result == {}


def test_delete_json_http_error_raises_runtime_error() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=_http_error(404, b'{"error": "missing"}')),
        pytest.raises(RuntimeError) as excinfo,
    ):
        delete_json("http://x/api/delete", {"model": "m"})
    assert "Ollama HTTP 404" in str(excinfo.value)


def test_delete_json_connection_error_raises_runtime_error() -> None:
    conn_err = urllib.error.URLError(ConnectionRefusedError("refused"))
    with (
        patch("urllib.request.urlopen", side_effect=conn_err),
        pytest.raises(RuntimeError) as excinfo,
    ):
        delete_json("http://x/api/delete", {"model": "m"})
    assert "Failed to reach Ollama" in str(excinfo.value)


# ============================================================================
# _retry_with_backoff: unreachable-loop fallback (negative max_retries)
# ============================================================================


def test_retry_with_backoff_negative_max_retries_raises_fallback_error() -> None:
    """max_retries < 0 means range(max_retries + 1) never executes, so the
    loop falls through to the final defensive RuntimeError."""
    mock_func = MagicMock(return_value="unused")

    with pytest.raises(RuntimeError, match="Retry loop ended unexpectedly"):
        _retry_with_backoff(mock_func, max_retries=-1)

    mock_func.assert_not_called()


# ============================================================================
# Duration/status logging (#3415 gap 2)
# ============================================================================


def test_post_json_logs_duration_on_success(caplog: pytest.LogCaptureFixture) -> None:
    resp = _mock_response(b'{"message": "ok"}')
    resp.status = 200
    with (
        patch("urllib.request.urlopen", return_value=resp),
        caplog.at_level("DEBUG", logger="nyxgpt.ollama_client"),
    ):
        post_json("http://x/api/chat", {"model": "m"})

    records = [r for r in caplog.records if r.getMessage() == "Ollama request completed"]
    assert records
    assert records[0].url == "http://x/api/chat"
    assert records[0].status == 200
    assert isinstance(records[0].duration_ms, float)


def test_post_json_logs_duration_on_http_error(caplog: pytest.LogCaptureFixture) -> None:
    from nyxgpt.ollama_client import ModelRuntimeError

    with (
        patch("urllib.request.urlopen", side_effect=_http_error(500)),
        caplog.at_level("WARNING", logger="nyxgpt.ollama_client"),
        pytest.raises(ModelRuntimeError),
    ):
        post_json("http://x/api/chat", {"model": "m"})

    records = [r for r in caplog.records if r.getMessage() == "Ollama request failed"]
    assert records
    assert records[0].status == 500


def test_ollama_chat_stream_tokens_logs_duration_on_done(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lines = [
        json.dumps({"message": {"content": "hi"}, "done": False}).encode() + b"\n",
        json.dumps({"message": {"content": ""}, "done": True}).encode() + b"\n",
    ]
    with (
        patch("urllib.request.urlopen", return_value=_streaming_response(lines)),
        caplog.at_level("DEBUG", logger="nyxgpt.ollama_client"),
    ):
        list(ollama_chat_stream_tokens("http://x", "m", [{"role": "user", "content": "hi"}]))

    records = [r for r in caplog.records if r.getMessage() == "Ollama streaming request completed"]
    assert records
    assert records[0].model == "m"
    assert isinstance(records[0].duration_ms, float)
