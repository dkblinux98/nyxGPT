from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from mygpt.app import app
from mygpt.logging import request_id_var, RequestIdFilter

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
    request_id_filter = RequestIdFilter()
    logger = logging.getLogger("mygpt.test")
    logger.setLevel(logging.DEBUG)

    def mock_chat_stream(*args, **kwargs):
        # This function runs inside the generator context
        # Emit a log to verify request ID is present
        logger.info("Test log from within chat_stream")
        yield "Test response"

    try:
        logger.addFilter(request_id_filter)
        with patch("mygpt.app.chat_stream", side_effect=mock_chat_stream):
            with caplog.at_level("DEBUG", logger="mygpt.test"):
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
                    # Consume stream
                    list(response.iter_text())
    finally:
        logger.removeFilter(request_id_filter)

    # Verify the log emitted from within chat_stream has the request ID
    test_logs = [r for r in caplog.records if r.name == "mygpt.test"]
    assert len(test_logs) > 0, "Expected to find test log from chat_stream"

    log_with_request_id = [
        r for r in test_logs
        if hasattr(r, "request_id") and r.request_id == test_request_id
    ]
    assert len(log_with_request_id) > 0, (
        f"Expected test log to have request_id={test_request_id}, "
        f"but found request_id={getattr(test_logs[0], 'request_id', 'MISSING')}"
    )


@pytest.mark.unit
def test_streaming_auto_generated_request_id_propagates_to_logs(caplog):
    """
    Verify auto-generated request IDs propagate to logs within streaming.

    Tests that when no request ID is provided, the auto-generated one
    is available in the context for logs emitted during streaming.
    """
    client = TestClient(app)

    # Install RequestIdFilter
    request_id_filter = RequestIdFilter()
    logger = logging.getLogger("mygpt.test")
    logger.setLevel(logging.DEBUG)

    def mock_chat_stream(*args, **kwargs):
        # Emit a log from within the generator
        logger.info("Test log with auto-generated ID")
        yield "Test"

    captured_request_id = None

    try:
        logger.addFilter(request_id_filter)
        with patch("mygpt.app.chat_stream", side_effect=mock_chat_stream):
            with caplog.at_level("DEBUG", logger="mygpt.test"):
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
                    # Get the auto-generated request ID
                    auto_request_id = response.headers.get("X-Request-Id")
                    assert auto_request_id is not None
                    captured_request_id = auto_request_id

                    # Consume stream
                    list(response.iter_text())
    finally:
        logger.removeFilter(request_id_filter)

    # Verify the auto-generated request ID appears in test logs
    test_logs = [r for r in caplog.records if r.name == "mygpt.test"]
    assert len(test_logs) > 0, "Expected to find test log from chat_stream"

    log_with_auto_id = [
        r for r in test_logs
        if hasattr(r, "request_id") and r.request_id == captured_request_id
    ]
    assert len(log_with_auto_id) > 0, (
        f"Expected log to have auto-generated request_id={captured_request_id}"
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

    # Install RequestIdFilter to add request ID to all log records
    request_id_filter = RequestIdFilter()
    logger = logging.getLogger("mygpt.chat")
    logger.setLevel(logging.DEBUG)

    def mock_ollama_stream(*args, **kwargs):
        """Mock Ollama HTTP responses instead of mocking chat_stream."""
        yield "Hello"
        yield " from"
        yield " Ollama"

    try:
        logger.addFilter(request_id_filter)
        # Mock at Ollama layer, not chat_stream - this lets the real function run
        with patch("mygpt.chat.ollama_chat_stream_tokens", side_effect=mock_ollama_stream):
            with caplog.at_level("DEBUG", logger="mygpt.chat"):
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
                    # Consume stream to trigger generator execution
                    list(response.iter_text())
    finally:
        logger.removeFilter(request_id_filter)

    # Verify logs from REAL chat_stream function have the request ID
    chat_logs = [r for r in caplog.records if r.name == "mygpt.chat"]
    assert len(chat_logs) > 0, "Expected to find logs from real chat_stream function"

    logs_with_request_id = [
        r for r in chat_logs
        if hasattr(r, "request_id") and r.request_id == test_request_id
    ]
    assert len(logs_with_request_id) > 0, (
        f"Expected chat_stream logs to have request_id={test_request_id}, "
        f"but found request_id={getattr(chat_logs[0], 'request_id', 'MISSING')}"
    )


@pytest.mark.unit
def test_real_chat_stream_with_auto_generated_request_id_in_logs(caplog):
    """
    Verify auto-generated request IDs propagate to REAL chat_stream logs.

    Tests that when no request ID is provided, the auto-generated ID
    is available in logs emitted from the actual chat_stream implementation.
    """
    client = TestClient(app)

    # Install RequestIdFilter
    request_id_filter = RequestIdFilter()
    logger = logging.getLogger("mygpt.chat")
    logger.setLevel(logging.DEBUG)

    def mock_ollama_stream(*args, **kwargs):
        """Mock Ollama HTTP responses."""
        yield "Test"

    captured_request_id = None

    try:
        logger.addFilter(request_id_filter)
        # Mock Ollama, not chat_stream - exercises real implementation
        with patch("mygpt.chat.ollama_chat_stream_tokens", side_effect=mock_ollama_stream):
            with caplog.at_level("DEBUG", logger="mygpt.chat"):
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
                    # Capture the auto-generated request ID
                    auto_request_id = response.headers.get("X-Request-Id")
                    assert auto_request_id is not None
                    captured_request_id = auto_request_id

                    # Consume stream
                    list(response.iter_text())
    finally:
        logger.removeFilter(request_id_filter)

    # Verify the auto-generated request ID appears in real chat_stream logs
    chat_logs = [r for r in caplog.records if r.name == "mygpt.chat"]
    assert len(chat_logs) > 0, "Expected to find logs from real chat_stream"

    logs_with_auto_id = [
        r for r in chat_logs
        if hasattr(r, "request_id") and r.request_id == captured_request_id
    ]
    assert len(logs_with_auto_id) > 0, (
        f"Expected real chat_stream logs to have auto-generated request_id={captured_request_id}"
    )
