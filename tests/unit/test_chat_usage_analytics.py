"""Unit tests verifying the chat endpoints record usage analytics.

Covers the usage_analytics_module.record() hooks added to the /api/v1/chat
and /api/v1/chat/stream handlers in src/nyxgpt/app.py.

Related: #2700
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt import usage_analytics
from nyxgpt.app import app
from nyxgpt.chat import ChatResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_events():
    """Ensure each test starts with an empty in-memory event buffer."""
    usage_analytics._events.clear()
    yield
    usage_analytics._events.clear()


def test_chat_endpoint_records_usage_event():
    client = TestClient(app)
    fake_result = ChatResult(
        session="usage-test",
        model="usage-test-model",
        reply="hello there",
        rag_used=False,
        rag_chunks=0,
        rag_context=None,
    )

    with patch("nyxgpt.app.run_chat", return_value=fake_result):
        response = client.post(
            "/api/v1/chat",
            json={"prompt": "hi", "session": "usage-test"},
        )

    assert response.status_code == 200

    events = usage_analytics.recent(limit=10)
    assert len(events) == 1
    assert events[0]["session"] == "usage-test"
    assert events[0]["model"] == "usage-test-model"
    assert events[0]["prompt_tokens"] > 0
    assert events[0]["completion_tokens"] > 0


def test_chat_stream_endpoint_records_usage_event():
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield "Hello world"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"prompt": "hi", "session": "usage-stream-test", "model": "usage-stream-model"},
        ) as response,
    ):
        assert response.status_code == 200
        list(response.iter_text())

    events = usage_analytics.recent(limit=10)
    assert len(events) == 1
    assert events[0]["session"] == "usage-stream-test"
    assert events[0]["model"] == "usage-stream-model"
    assert events[0]["prompt_tokens"] > 0
