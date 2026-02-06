from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


def test_request_id_propagates_in_streaming_response(require_ollama):
    """Verify request ID propagates correctly in streaming chat responses.

    This integration test verifies end-to-end request ID handling by:
    1. Sending a custom request ID in the header
    2. Verifying it appears in the response header
    3. Confirming the streaming response completes successfully

    Note: Request ID propagation to logs is verified by unit tests with mocks.
    Integration tests focus on end-to-end behavior with real Ollama integration.
    """
    client = TestClient(app)

    # Generate a unique request ID for this test
    test_request_id = f"test-stream-{uuid.uuid4()}"

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
        assert response.status_code == 200

        # Consume the stream
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

    # Verify we got a response
    assert len(chunks) > 0, "Expected streaming response chunks but got none"


def test_request_id_in_streaming_with_auto_generation(require_ollama):
    """Verify auto-generated request IDs work in streaming responses.

    This integration test verifies that when no request ID is provided,
    the system automatically generates one and includes it in the response header.
    """
    client = TestClient(app)

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
        assert auto_request_id is not None, "Expected auto-generated request ID in response header"
        assert len(auto_request_id) > 0, "Auto-generated request ID should not be empty"
        assert response.status_code == 200

        # Consume the stream
        chunks = list(response.iter_text())

    # Verify we got a response
    assert len(chunks) > 0, "Expected streaming response chunks but got none"
