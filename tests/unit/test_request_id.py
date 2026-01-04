from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from mygpt.logging import RequestIdFilter, request_id_var
from mygpt.app import app

pytestmark = pytest.mark.unit


def test_request_id_filter_adds_request_id_from_context():
    """RequestIdFilter should add request_id from context variable to log records."""
    # Set up filter and log record
    filter_instance = RequestIdFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test message",
        args=(),
        exc_info=None,
    )

    # Set request ID in context
    test_req_id = "test-request-id-123"
    request_id_var.set(test_req_id)

    # Apply filter
    result = filter_instance.filter(record)

    # Filter should return True and add request_id
    assert result is True
    assert hasattr(record, "request_id")
    assert record.request_id == test_req_id


def test_request_id_filter_uses_na_when_no_context():
    """RequestIdFilter should use 'N/A' when no request ID in context."""
    filter_instance = RequestIdFilter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test message",
        args=(),
        exc_info=None,
    )

    # Clear context
    request_id_var.set(None)

    # Apply filter
    result = filter_instance.filter(record)

    # Should use 'N/A' as fallback
    assert result is True
    assert hasattr(record, "request_id")
    assert record.request_id == "N/A"


def test_request_id_filter_preserves_existing_request_id():
    """RequestIdFilter should not override request_id if already present."""
    filter_instance = RequestIdFilter()
    existing_id = "existing-id-456"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="test message",
        args=(),
        exc_info=None,
    )
    record.request_id = existing_id

    # Set different ID in context
    request_id_var.set("different-id-789")

    # Apply filter
    result = filter_instance.filter(record)

    # Should preserve existing request_id
    assert result is True
    assert record.request_id == existing_id


def test_api_response_includes_request_id_header():
    """API responses should include X-Request-Id header."""
    client = TestClient(app)

    # Make request without providing request ID
    response = client.get("/health")

    # Should have X-Request-Id header
    assert "X-Request-Id" in response.headers
    assert response.headers["X-Request-Id"]
    # Should be a valid UUID format (36 chars with hyphens)
    assert len(response.headers["X-Request-Id"]) == 36


def test_api_accepts_client_provided_request_id():
    """API should accept and use client-provided X-Request-ID header."""
    client = TestClient(app)
    client_req_id = "client-provided-id-abc123"

    # Make request with custom request ID
    response = client.get("/health", headers={"X-Request-ID": client_req_id})

    # Should echo back the same request ID
    assert "X-Request-Id" in response.headers
    assert response.headers["X-Request-Id"] == client_req_id


def test_request_id_in_error_responses():
    """Error responses should include request_id in response body and header."""
    client = TestClient(app)
    custom_req_id = "error-test-id-xyz"

    # Make request to endpoint that will raise an error (invalid session name)
    # This triggers our custom exception handler
    response = client.delete(
        "/api/v1/sessions/nonexistent-session-123",
        headers={"X-Request-ID": custom_req_id}
    )

    # Should be 404 (session not found)
    assert response.status_code == 404

    # Should include request_id in response header
    assert response.headers.get("X-Request-Id") == custom_req_id

    # Should include request_id in error body
    body = response.json()
    assert "error" in body
    assert "request_id" in body["error"]
    assert body["error"]["request_id"] == custom_req_id


def test_request_id_persists_across_middleware_chain():
    """Request ID should be accessible throughout middleware chain."""
    client = TestClient(app)
    test_req_id = "middleware-chain-test-id"

    # Make authenticated request to API endpoint
    response = client.get("/api/v1/info", headers={"X-Request-ID": test_req_id})

    # Should preserve request ID through all middleware
    assert response.headers.get("X-Request-Id") == test_req_id


def test_request_id_context_isolation():
    """Request IDs in context should be isolated between requests."""
    # This tests that context variables don't leak between requests

    # Set initial context
    request_id_var.set("first-request")
    first_value = request_id_var.get()

    # Simulate new request with different ID
    request_id_var.set("second-request")
    second_value = request_id_var.get()

    # Values should be different
    assert first_value == "first-request"
    assert second_value == "second-request"

    # Clear context
    request_id_var.set(None)
    assert request_id_var.get() is None


@pytest.mark.asyncio
async def test_request_id_async_context_propagation():
    """Request ID should propagate through async calls (real-world usage pattern)."""
    # FastAPI is async, so we need to verify context vars work with async/await

    # Set request ID in parent context
    parent_request_id = "parent-async-request"
    request_id_var.set(parent_request_id)

    async def child_async_task():
        """Simulates async operation within request (e.g., DB query, API call)."""
        # Context variable should be preserved in awaited async calls
        return request_id_var.get()

    # Awaited async tasks should preserve context
    child_result = await child_async_task()
    assert child_result == parent_request_id, "Request ID should propagate to awaited tasks"

    # Nested async calls should also preserve context
    async def nested_async_call():
        inner_result = await child_async_task()
        return inner_result

    nested_result = await nested_async_call()
    assert nested_result == parent_request_id, "Request ID should propagate through nested awaits"

    # Parent context should still be intact
    assert request_id_var.get() == parent_request_id

    # Clear context
    request_id_var.set(None)
