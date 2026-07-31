"""Coverage for the streaming chat endpoints (_create_streaming_response /
/api/v1/chat/stream / /api/chat/stream) in src/nyxgpt/app.py.

Targets branches not already exercised by test_streaming_request_id.py and
test_chat_error_logging.py:
- Invalid session name -> 422 before the generator starts
- Client-capability negotiation: legacy (plain text), simple SSE (no
  structured events), and full structured SSE (heartbeat/metadata/text/done)
- RAG-context and retry-status SSE events, and marker stripping for
  non-structured clients
- The optional rag_filters/attachments/output_format kwargs forwarded to
  chat_stream() when it supports them
- request_id_var.set() failure being swallowed (logged, not fatal)
- The error SSE event emitted on a mid-stream failure
- Usage-analytics-recording failure being swallowed
- The generic setup-failure -> 500 handler
"""

from __future__ import annotations

import base64
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def _sse_events(text: str) -> list[dict]:
    """Parse `event: X\\ndata: Y\\nid: Z\\n\\n` blocks into simple dicts."""
    events = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event_type = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_type = line[len("event: ") :]
            elif line.startswith("data: "):
                data = line[len("data: ") :]
        if event_type is not None:
            events.append({"type": event_type, "data": data})
    return events


def test_stream_invalid_session_name_returns_422() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/chat/stream",
        json={"prompt": "hi", "session": "../escape"},
    )
    assert response.status_code == 422


def test_stream_legacy_client_gets_plain_text_and_strips_markers() -> None:
    """No Accept:text/event-stream and no capability headers -> plain text chunks.

    Also exercises the __RAG_START__/__RETRY_START__ marker-stripping logic
    shared by all client types.
    """
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield "hello "
        yield "__RAG_START__{}__RAG_END__world"
        yield "__RETRY_START__{}__RETRY_END__!"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "legacy-test"},
            headers={"Accept": "*/*"},
        ) as response,
    ):
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = "".join(response.iter_text())

    assert body == "hello world!"


def test_stream_simple_sse_client_gets_data_events_only() -> None:
    """Accept:text/event-stream but no structured-events header -> simple `data:` framing."""
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield "chunk1"
        yield "chunk2"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "simple-sse-test"},
            headers={"Accept": "text/event-stream"},
        ) as response,
    ):
        assert response.status_code == 200
        body = response.read().decode()

    assert "data: chunk1\n\n" in body
    assert "data: chunk2\n\n" in body
    assert "event: metadata" not in body
    assert "event: heartbeat" not in body


def test_stream_structured_client_gets_heartbeat_metadata_text_done_events() -> None:
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield "Hello"
        yield " world"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "structured-test", "model": "structured-model"},
            headers={
                "Accept": "text/event-stream",
                "X-Client-Supports-Structured-Events": "true",
            },
        ) as response,
    ):
        assert response.status_code == 200
        events = _sse_events(response.read().decode())

    types = [e["type"] for e in events]
    assert types[0] == "heartbeat"
    assert types[1] == "metadata"
    metadata = json.loads(events[1]["data"])
    assert metadata["session"] == "structured-test"
    assert metadata["model"] == "structured-model"
    assert "text" in types
    text_events = [e for e in events if e["type"] == "text"]
    assert "".join(json.loads(e["data"])["content"] for e in text_events) == "Hello world"
    assert types[-1] == "done"
    done_data = json.loads(events[-1]["data"])
    assert done_data["total_tokens"] > 0


def test_stream_structured_client_gets_rag_context_event() -> None:
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield '__RAG_START__{"chunks": [{"doc_id": "d1"}]}__RAG_END__answer text'

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "rag-event-test"},
            headers={
                "Accept": "text/event-stream",
                "X-Client-Supports-Structured-Events": "true",
            },
        ) as response,
    ):
        events = _sse_events(response.read().decode())

    rag_events = [e for e in events if e["type"] == "rag_context"]
    assert len(rag_events) == 1
    assert json.loads(rag_events[0]["data"]) == {"chunks": [{"doc_id": "d1"}]}
    text_events = [e for e in events if e["type"] == "text"]
    assert any("answer text" in json.loads(e["data"])["content"] for e in text_events)


def test_stream_token_counting_failure_is_swallowed_in_both_text_branches() -> None:
    """count_tokens() failures must not break the stream, for both the
    RAG-remaining-text branch and the plain regular-text branch."""
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield '__RAG_START__{"chunks": []}__RAG_END__rag-adjacent text'
        yield "plain text chunk"

    with (
        patch("nyxgpt.token_counter.count_tokens", side_effect=RuntimeError("tiktoken boom")),
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "token-count-fail-test"},
            headers={
                "Accept": "text/event-stream",
                "X-Client-Supports-Structured-Events": "true",
            },
        ) as response,
    ):
        events = _sse_events(response.read().decode())

    text_events = [e for e in events if e["type"] == "text"]
    contents = [json.loads(e["data"])["content"] for e in text_events]
    assert "rag-adjacent text" in contents
    assert "plain text chunk" in contents
    # Token counts stayed at 0 since count_tokens always raised.
    assert all(json.loads(e["data"])["tokens"] == 0 for e in text_events)


def test_stream_structured_client_gets_retry_event() -> None:
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield '__RETRY_START__{"attempt": 1}__RETRY_END__'
        yield "final answer"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "retry-event-test"},
            headers={
                "Accept": "text/event-stream",
                "X-Client-Supports-Structured-Events": "true",
            },
        ) as response,
    ):
        events = _sse_events(response.read().decode())

    retry_events = [e for e in events if e["type"] == "retry"]
    assert len(retry_events) == 1
    assert json.loads(retry_events[0]["data"]) == {"attempt": 1}


def test_stream_error_event_emitted_on_mid_stream_failure() -> None:
    client = TestClient(app, raise_server_exceptions=False)

    def mock_chat_stream(*args, **kwargs):
        yield "partial"
        raise RuntimeError("upstream crashed")

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "error-event-test"},
            headers={
                "Accept": "text/event-stream",
                "X-Client-Supports-Structured-Events": "true",
            },
        ) as response,
    ):
        events = _sse_events(response.read().decode())

    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) == 1
    assert "upstream crashed" in json.loads(error_events[0]["data"])["error"]


def test_stream_usage_analytics_failure_does_not_break_stream() -> None:
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield "ok"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        patch("nyxgpt.app.usage_analytics_module.record", side_effect=RuntimeError("db down")),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "analytics-fail-stream-test"},
        ) as response,
    ):
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert body == "ok"


def test_stream_request_id_var_set_failure_is_logged_and_swallowed(caplog) -> None:
    """Only the generator's request_id_var.set() call must be swallowed.

    The request-ID middleware (add_request_id_and_limits) also calls
    request_id_var.set() once per request, before the route handler even
    runs -- that call must keep succeeding so we isolate the failure to the
    second (in-generator) call.
    """

    class _RaisingOnSecondCall:
        def __init__(self):
            self.calls = 0

        def set(self, *_a, **_k):
            self.calls += 1
            if self.calls >= 2:
                raise RuntimeError("context boom")

    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield "still works"

    with (
        patch("nyxgpt.app.request_id_var", _RaisingOnSecondCall()),
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "ctxvar-fail-test"},
        ) as response,
    ):
        body = "".join(response.iter_text())

    assert body == "still works"
    assert "Failed to set request ID in streaming context" in caplog.text


def test_stream_forwards_rag_filters_attachments_output_format() -> None:
    """When chat_stream's real signature supports these kwargs, the endpoint must pass them."""
    client = TestClient(app)
    captured_kwargs: dict = {}

    def fake_chat_stream(prompt, **kwargs):
        captured_kwargs.update(kwargs)
        yield "ok"

    img_b64 = base64.b64encode(b"fake-bytes").decode()

    with (
        patch("nyxgpt.app.chat_stream", autospec=True, side_effect=fake_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "hi",
                "session": "stream-kwargs-test",
                "rag_filters": {"filename": "a.pdf"},
                "attachments": [
                    {
                        "type": "image",
                        "media_type": "image/png",
                        "data": img_b64,
                        "filename": "a.png",
                    }
                ],
                "output_format": {"type": "object"},
            },
        ) as response,
    ):
        list(response.iter_text())

    assert captured_kwargs.get("rag_filters") == {
        "doc_ids": None,
        "filename": "a.pdf",
        "tags": None,
        "date_from": None,
        "date_to": None,
        "collection": None,
    }
    assert captured_kwargs.get("attachments") == [
        {"type": "image", "media_type": "image/png", "data": img_b64, "filename": "a.png"}
    ]
    assert captured_kwargs.get("output_format") == {"type": "object"}


def test_stream_setup_failure_returns_500() -> None:
    client = TestClient(app)
    with patch("nyxgpt.app._parse_client_capabilities", side_effect=RuntimeError("setup boom")):
        response = client.post(
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "setup-fail-test"},
        )

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Internal server error"


def test_legacy_alias_endpoint_delegates_to_shared_helper() -> None:
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield "legacy alias works"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/chat/stream",
            json={"prompt": "hi", "session": "legacy-alias-test"},
        ) as response,
    ):
        assert response.status_code == 200
        body = "".join(response.iter_text())

    assert body == "legacy alias works"
