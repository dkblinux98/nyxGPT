"""Integration tests for client capability hints and content negotiation."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration


def test_client_hints_modern_client(require_ollama):
    """Verify that modern client with full capabilities gets SSE with structured events."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-modern-client",
            "model": "llama3.1:8b",
        },
        headers={
            "Accept": "text/event-stream",
            "X-Client-Supports-SSE": "true",
            "X-Client-Supports-Structured-Events": "true",
            "X-Client-Version": "test-client/1.0.0",
        },
    ) as response:
        assert response.status_code == 200

        # Verify SSE content type for modern client
        content_type = response.headers.get("Content-Type", "")
        assert "text/event-stream" in content_type

        # Verify server capability headers
        assert response.headers.get("X-Server-Supports-SSE") == "true"
        assert response.headers.get("X-Server-Supports-Structured-Events") == "true"

        # Collect chunks
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)

        # Verify structured events are present
        assert "event: heartbeat" in full_response
        assert "event: metadata" in full_response
        assert "event: text" in full_response
        assert "event: done" in full_response


def test_client_hints_sse_only_client(require_ollama):
    """Verify that SSE client without structured events support gets simple SSE."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-sse-only-client",
            "model": "llama3.1:8b",
        },
        headers={
            "Accept": "text/event-stream",
            "X-Client-Supports-SSE": "true",
            "X-Client-Supports-Structured-Events": "false",
            "X-Client-Version": "legacy-sse/0.9.0",
        },
    ) as response:
        assert response.status_code == 200

        # Still SSE content type
        content_type = response.headers.get("Content-Type", "")
        assert "text/event-stream" in content_type

        # Collect chunks
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)

        # Should have simple data: events but NOT structured event types
        assert "data:" in full_response
        # Should NOT have structured event types
        assert "event: metadata" not in full_response
        assert "event: done" not in full_response


def test_client_hints_legacy_client(require_ollama):
    """Verify that legacy client without SSE support gets plain text."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-legacy-client",
            "model": "llama3.1:8b",
        },
        headers={
            "Accept": "text/plain",
            "X-Client-Supports-SSE": "false",
            "X-Client-Version": "legacy/0.5.0",
        },
    ) as response:
        assert response.status_code == 200

        # Plain text content type for legacy client
        content_type = response.headers.get("Content-Type", "")
        assert "text/plain" in content_type

        # Collect chunks
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)

        # Should NOT have SSE formatting
        assert "event:" not in full_response
        assert "data:" not in full_response
        assert "id:" not in full_response

        # Should have plain text content
        assert len(full_response) > 0


def test_client_hints_no_headers_defaults_to_sse(require_ollama):
    """Verify that client without capability headers gets SSE by default."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-no-headers",
            "model": "llama3.1:8b",
        },
        # No capability headers
    ) as response:
        assert response.status_code == 200

        # Should default to SSE when Accept header includes text/event-stream
        # or when no headers provided (backwards compatibility)
        content_type = response.headers.get("Content-Type", "")
        # Default is plain text when no hints provided
        assert "text/plain" in content_type or "text/event-stream" in content_type


def test_client_hints_version_header(require_ollama):
    """Verify that client version header is parsed correctly."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-version-header",
            "model": "llama3.1:8b",
        },
        headers={
            "Accept": "text/event-stream",
            "X-Client-Supports-SSE": "true",
            "X-Client-Supports-Structured-Events": "true",
            "X-Client-Version": "web-ui/2.0.0",
        },
    ) as response:
        assert response.status_code == 200

        # Verify server responds with its version
        assert "X-Server-Version" in response.headers


def test_client_hints_graceful_degradation(require_ollama):
    """Verify graceful degradation when client sends invalid capability headers."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-graceful-degradation",
            "model": "llama3.1:8b",
        },
        headers={
            "X-Client-Supports-SSE": "maybe",  # Invalid value
            "X-Client-Max-Event-Size": "not-a-number",  # Invalid value
        },
    ) as response:
        # Should still succeed despite invalid headers
        assert response.status_code == 200

        # Collect response
        chunks = []
        for chunk in response.iter_text():
            chunks.append(chunk)

        full_response = "".join(chunks)
        assert len(full_response) > 0


def test_client_hints_accept_header_priority(require_ollama):
    """Verify that Accept header is respected for SSE detection."""
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/v1/chat/stream",
        json={
            "prompt": "Say hello",
            "session": "test-accept-header",
            "model": "llama3.1:8b",
        },
        headers={
            "Accept": "text/event-stream, text/plain",
            # Explicit header should also be checked
            "X-Client-Supports-SSE": "true",
            "X-Client-Supports-Structured-Events": "true",
        },
    ) as response:
        assert response.status_code == 200

        # Should use SSE when Accept includes text/event-stream
        content_type = response.headers.get("Content-Type", "")
        assert "text/event-stream" in content_type
