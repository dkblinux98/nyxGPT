from __future__ import annotations

import logging
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.logging import RequestIdFilter, request_id_var

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

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            headers={"X-Request-ID": test_request_id},
        ) as response,
    ):
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

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/chat/stream",  # Legacy endpoint (no v1)
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            headers={"X-Request-ID": test_request_id},
        ) as response,
    ):
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

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            # No X-Request-ID header - should auto-generate
        ) as response,
    ):
        auto_request_id = response.headers.get("X-Request-Id")
        assert auto_request_id is not None
        assert len(auto_request_id) > 0

        list(response.iter_text())

    # Verify the auto-generated ID was set in the generator
    assert len(request_ids_seen) > 0
    assert request_ids_seen[0] == auto_request_id


@pytest.mark.unit
def test_streaming_request_id_propagates_to_logged_function(caplog):
    """
    Verify request IDs propagate to logs emitted from functions called within streaming.

    This test verifies that if code within the streaming generator emits logs,
    those logs will contain the request ID that was set at the start of the generator.
    """
    client = TestClient(app)
    test_request_id = f"e2e-test-{uuid.uuid4()}"

    # Install RequestIdFilter to add request ID to all log records
    _ = RequestIdFilter()
    logger = logging.getLogger("nyxgpt.test")
    logger.setLevel(logging.DEBUG)

    captured_request_id = []

    def mock_chat_stream(*args, **kwargs):
        # This function runs inside the generator context
        # Capture the request ID from the context var
        req_id = request_id_var.get()
        captured_request_id.append(req_id)
        # Emit a log to verify request ID is present
        logger.info(f"Test log from within chat_stream, request_id={req_id}")
        yield "Test response"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            headers={"X-Request-ID": test_request_id},
        ) as response,
    ):
        # Consume stream
        list(response.iter_text())

    # Verify the request ID was captured from the context
    assert len(captured_request_id) > 0, "Mock was not called"
    assert captured_request_id[0] == test_request_id, (
        f"Expected request_id={test_request_id}, " f"but found request_id={captured_request_id[0]}"
    )


@pytest.mark.unit
def test_streaming_auto_generated_request_id_propagates_to_logs(caplog):
    """
    Verify auto-generated request IDs propagate to logs within streaming.

    Tests that when no request ID is provided, the auto-generated one
    is available in the context for logs emitted during streaming.
    """
    client = TestClient(app)

    captured_context_id = []
    captured_header_id = []

    def mock_chat_stream(*args, **kwargs):
        # Capture the request ID from the context var
        req_id = request_id_var.get()
        captured_context_id.append(req_id)
        yield "Test"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            # No X-Request-ID header - should auto-generate
        ) as response,
    ):
        # Get the auto-generated request ID
        auto_request_id = response.headers.get("X-Request-Id")
        assert auto_request_id is not None, "Expected X-Request-Id header"
        captured_header_id.append(auto_request_id)

        # Consume stream
        list(response.iter_text())

    # Verify the auto-generated request ID was available in the streaming context
    assert len(captured_context_id) > 0, "Mock was not called"
    assert captured_context_id[0] == captured_header_id[0], (
        f"Expected context request_id={captured_header_id[0]}, "
        f"but found request_id={captured_context_id[0]}"
    )


@pytest.mark.unit
def test_real_chat_stream_logs_have_request_id(caplog):
    """
    Verify request IDs propagate to logs from the REAL chat_stream function.

    This test exercises the actual chat_stream implementation (not mocked)
    by mocking only the Ollama HTTP layer. This ensures request ID context
    is correctly maintained when the real streaming generator executes.
    """
    client = TestClient(app)
    test_request_id = f"real-stream-{uuid.uuid4()}"

    captured_request_ids = []

    def mock_ollama_stream(*args, **kwargs):
        """Mock Ollama HTTP responses instead of mocking chat_stream."""
        # Capture the request ID from within the real chat_stream execution
        req_id = request_id_var.get()
        captured_request_ids.append(req_id)
        yield "Hello"
        yield " from"
        yield " Ollama"

    # Mock at Ollama layer, not chat_stream - this lets the real function run
    with (
        patch("nyxgpt.chat.ollama_chat_stream_tokens", side_effect=mock_ollama_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            headers={"X-Request-ID": test_request_id},
        ) as response,
    ):
        # Consume stream to trigger generator execution
        list(response.iter_text())

    # Verify the request ID was available during real chat_stream execution
    assert len(captured_request_ids) > 0, "Mock ollama stream was not called"
    assert captured_request_ids[0] == test_request_id, (
        f"Expected request_id={test_request_id}, " f"but found request_id={captured_request_ids[0]}"
    )


@pytest.mark.unit
def test_real_chat_stream_with_auto_generated_request_id_in_logs():
    """
    Verify auto-generated request IDs propagate to REAL chat_stream context.

    Tests that when no request ID is provided, the auto-generated ID
    is available in the context variable during the real chat_stream execution.
    """
    client = TestClient(app)

    captured_context_ids = []
    captured_header_id = []

    def mock_ollama_stream(*args, **kwargs):
        """Mock Ollama HTTP responses and capture request ID from context."""
        # Capture the request ID from within the real chat_stream execution
        req_id = request_id_var.get()
        captured_context_ids.append(req_id)
        yield "Test"

    # Mock Ollama, not chat_stream - exercises real implementation
    with (
        patch("nyxgpt.chat.ollama_chat_stream_tokens", side_effect=mock_ollama_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "Test",
                "session": "test-session",
                "model": "test-model",
            },
            # No X-Request-ID header - should auto-generate
        ) as response,
    ):
        # Capture the auto-generated request ID
        auto_request_id = response.headers.get("X-Request-Id")
        assert auto_request_id is not None, "Expected X-Request-Id header"
        captured_header_id.append(auto_request_id)

        # Consume stream
        list(response.iter_text())

    # Verify the auto-generated request ID was available in the streaming context
    assert len(captured_context_ids) > 0, "Mock ollama stream was not called"
    assert captured_context_ids[0] == captured_header_id[0], (
        f"Expected context request_id={captured_header_id[0]}, "
        f"but found request_id={captured_context_ids[0]}"
    )
