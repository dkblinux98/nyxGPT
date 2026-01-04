from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from mygpt.app import app
from mygpt.logging import request_id_var

pytestmark = pytest.mark.integration


@pytest.mark.integration
def test_request_id_propagates_in_streaming_response(caplog):
    """Verify request ID appears in logs during streaming chat responses."""
    client = TestClient(app)

    # Generate a unique request ID for this test
    test_request_id = f"test-stream-{uuid.uuid4()}"

    # Capture logs at INFO level
    caplog.set_level(logging.INFO)

    # Make streaming request with custom request ID
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-streaming-session",
            "model": "llama3.1:8b",
        },
        headers={"X-Request-ID": test_request_id},
    ) as response:
        # Response should have the request ID header
        assert response.headers.get("X-Request-Id") == test_request_id

        # Consume the stream
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

    # Verify we got a response
    assert len(chunks) > 0

    # Check that request ID appears in log records
    # The chat_stream() function should log with the request ID
    log_messages = [record.message for record in caplog.records]

    # At least some log entries should have been created during streaming
    assert len(log_messages) > 0, "No log messages captured during streaming"

    # Verify request ID appears in log record attributes
    request_ids_in_logs = [
        getattr(record, "request_id", None) for record in caplog.records
    ]

    # At least one log entry should have our test request ID
    assert test_request_id in request_ids_in_logs, (
        f"Request ID '{test_request_id}' not found in logs. "
        f"Found request IDs: {set(request_ids_in_logs)}"
    )


@pytest.mark.integration
def test_request_id_in_streaming_with_auto_generation(caplog):
    """Verify auto-generated request IDs work in streaming responses."""
    client = TestClient(app)

    # Capture logs
    caplog.set_level(logging.INFO)

    # Make streaming request WITHOUT providing request ID (auto-generate)
    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hi",
            "session": "test-streaming-auto",
            "model": "llama3.1:8b",
        },
    ) as response:
        # Response should have an auto-generated request ID
        auto_request_id = response.headers.get("X-Request-Id")
        assert auto_request_id is not None
        assert len(auto_request_id) > 0

        # Consume the stream
        list(response.iter_text())

    # Verify auto-generated request ID appears in logs
    request_ids_in_logs = [
        getattr(record, "request_id", None) for record in caplog.records
    ]

    assert auto_request_id in request_ids_in_logs, (
        f"Auto-generated request ID '{auto_request_id}' not found in logs"
    )
