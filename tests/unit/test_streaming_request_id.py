from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from mygpt.app import app
from mygpt.logging import request_id_var

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_streaming_endpoint_captures_request_id():
    """Verify streaming endpoints capture request ID before entering generator."""
    client = TestClient(app)
    test_request_id = f"unit-test-{uuid.uuid4()}"

    # Mock chat_stream to track if request ID is set when it's called
    request_ids_seen = []

    def mock_chat_stream(*args, **kwargs):
        # Capture the request ID that's active when chat_stream is called
        request_ids_seen.append(request_id_var.get())
        yield "Hello"
        yield " "
        yield "World"

    with patch("mygpt.app.chat_stream", side_effect=mock_chat_stream):
        # Make streaming request
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            headers={"X-Request-ID": test_request_id},
        ) as response:
            # Response should have the request ID
            assert response.headers.get("X-Request-Id") == test_request_id

            # Consume the stream
            list(response.iter_text())

    # Verify request ID was set when generator executed
    assert len(request_ids_seen) > 0, "chat_stream was not called"
    assert request_ids_seen[0] == test_request_id, (
        f"Request ID not set in generator. "
        f"Expected '{test_request_id}', got '{request_ids_seen[0]}'"
    )


@pytest.mark.unit
def test_legacy_streaming_endpoint_captures_request_id():
    """Verify legacy streaming endpoint also captures request ID."""
    client = TestClient(app)
    test_request_id = f"unit-legacy-{uuid.uuid4()}"

    request_ids_seen = []

    def mock_chat_stream(*args, **kwargs):
        request_ids_seen.append(request_id_var.get())
        yield "Test"

    with patch("mygpt.app.chat_stream", side_effect=mock_chat_stream):
        with client.stream(
            "POST",
            "/api/chat/stream",  # Legacy endpoint (no v1)
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            headers={"X-Request-ID": test_request_id},
        ) as response:
            assert response.headers.get("X-Request-Id") == test_request_id
            list(response.iter_text())

    assert len(request_ids_seen) > 0
    assert request_ids_seen[0] == test_request_id


@pytest.mark.unit
def test_streaming_endpoint_with_auto_generated_request_id():
    """Verify auto-generated request IDs work in streaming."""
    client = TestClient(app)

    request_ids_seen = []

    def mock_chat_stream(*args, **kwargs):
        request_ids_seen.append(request_id_var.get())
        yield "Test"

    with patch("mygpt.app.chat_stream", side_effect=mock_chat_stream):
        with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            # No X-Request-ID header - should auto-generate
        ) as response:
            auto_request_id = response.headers.get("X-Request-Id")
            assert auto_request_id is not None
            assert len(auto_request_id) > 0

            list(response.iter_text())

    # Verify the auto-generated ID was set in the generator
    assert len(request_ids_seen) > 0
    assert request_ids_seen[0] == auto_request_id
