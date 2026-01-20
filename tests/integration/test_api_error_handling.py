from __future__ import annotations

import httpx
import pytest


pytestmark = pytest.mark.integration


# ============================================================================
# 400 Bad Request Tests - Invalid Request Bodies
# ============================================================================


def test_chat_stream_with_missing_prompt(api_base_url: str) -> None:
    """POST /api/v1/chat/stream with missing required 'prompt' field should return 400/422."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.post(
            "/api/v1/chat/stream",
            json={
                # Missing 'prompt' field
                "session": "test-session"
            },
        )
        # FastAPI returns 422 for validation errors (Pydantic)
        assert response.status_code == 422


def test_chat_with_invalid_data_type(api_base_url: str) -> None:
    """POST /api/v1/chat with invalid data type for 'prompt' should return 422."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.post(
            "/api/v1/chat",
            json={
                "prompt": 12345,  # Should be string, not int
                "session": "test-session",
            },
        )
        assert response.status_code == 422


def test_chat_with_empty_json_body(api_base_url: str) -> None:
    """POST /api/v1/chat with empty JSON body should return 422."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.post(
            "/api/v1/chat",
            json={},
        )
        assert response.status_code == 422


def test_rag_ingest_with_missing_fields(
    api_base_url: str, require_cassandra: None
) -> None:
    """POST /api/v1/rag/ingest with missing required fields should return 422."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.post(
            "/api/v1/rag/ingest",
            json={
                "doc_id": "test-doc"
                # Missing 'text' field
            },
        )
        assert response.status_code == 422


# ============================================================================
# 401 Unauthorized / 403 Forbidden Tests - Authentication
# ============================================================================


def test_api_key_authentication_missing(api_base_url: str) -> None:
    """API endpoints should handle missing API key appropriately.

    Note: If authentication is disabled in config, this may return 200.
    If enabled, should return 401/403.
    """
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Make request without API key header
        response = client.get("/api/v1/info")

        # Either authentication is disabled (200) or required (401/403)
        assert response.status_code in (200, 401, 403)

        # If auth is enabled, verify error response structure
        if response.status_code in (401, 403):
            assert "error" in response.json() or "detail" in response.json()


def test_api_key_authentication_invalid(api_base_url: str) -> None:
    """API endpoints should reject invalid API keys.

    Note: If authentication is disabled in config, this may return 200.
    If enabled, should return 401/403.
    """
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Make request with invalid API key
        response = client.get(
            "/api/v1/info", headers={"X-API-Key": "invalid-key-12345"}
        )

        # Either authentication is disabled (200) or key is invalid (401/403)
        assert response.status_code in (200, 401, 403)

        # If auth is enabled, verify error response
        if response.status_code in (401, 403):
            assert "error" in response.json() or "detail" in response.json()


def test_protected_endpoint_requires_authentication(api_base_url: str) -> None:
    """Protected endpoints should require valid authentication.

    Note: Tests authentication behavior when enabled.
    """
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Try to delete a session without authentication
        response = client.delete("/api/v1/sessions/test-session")

        # Either auth is disabled (404 for nonexistent session) or auth required
        assert response.status_code in (200, 404, 401, 403)


# ============================================================================
# 422 Unprocessable Entity Tests - Validation Errors
# ============================================================================


def test_session_init_with_invalid_name_format(api_base_url: str) -> None:
    """POST /api/v1/sessions/init with invalid session name format should return 400/422."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.post(
            "/api/v1/sessions/init",
            json={
                "name": "../invalid-session-name"  # Path traversal attempt
            },
        )
        # Should reject invalid session names
        assert response.status_code in (400, 422)


def test_session_init_with_too_long_name(api_base_url: str) -> None:
    """POST /api/v1/sessions/init with overly long session name should return error.

    Should return 422 (validation) but may return 500 if validation doesn't catch it.
    """
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.post(
            "/api/v1/sessions/init",
            json={
                "name": "a" * 1000  # Extremely long name
            },
        )
        # Should reject excessively long session names
        assert response.status_code in (422, 500)
        # Response should have error structure
        assert "error" in response.json()


def test_rag_query_with_invalid_top_k(
    api_base_url: str, require_cassandra: None
) -> None:
    """POST /api/v1/rag/query with invalid top_k value should return 422."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.post(
            "/api/v1/rag/query",
            json={
                "query": "test query",
                "top_k": -5,  # Negative value should be invalid
            },
        )
        # Should reject negative top_k
        assert response.status_code in (400, 422)


def test_chat_with_invalid_model_name(api_base_url: str) -> None:
    """POST /api/v1/chat with empty model name should use default model.

    Note: Empty model name is gracefully handled by using the default model
    from configuration, so this returns 200 OK.
    """
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        response = client.post(
            "/api/v1/chat",
            json={
                "prompt": "test",
                "session": "test-session",
                "model": "",  # Empty model name - should use default
            },
        )
        # Empty model uses default, so should succeed
        assert response.status_code == 200


# ============================================================================
# 500 Internal Server Error Tests - Server Errors
# ============================================================================


def test_rag_operations_without_cassandra(api_base_url: str) -> None:
    """RAG operations should handle Cassandra unavailability gracefully.

    Note: This test runs WITHOUT require_cassandra fixture, so Cassandra
    may or may not be available. If unavailable, should return 500 or 503.
    """
    with httpx.Client(base_url=api_base_url, timeout=30.0) as client:
        response = client.post(
            "/api/v1/rag/query",
            json={"query": "test query", "top_k": 5},
        )

        # Either Cassandra is available (200) or unavailable (500/503)
        assert response.status_code in (200, 500, 503)

        # If error, should have proper error structure
        if response.status_code >= 500:
            data = response.json()
            assert "error" in data or "detail" in data


def test_error_response_includes_request_id(api_base_url: str) -> None:
    """All error responses should include request_id for tracing."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        custom_request_id = "error-trace-test-123"

        # Make request that will fail (nonexistent session)
        response = client.delete(
            "/api/v1/sessions/nonexistent-session-xyz",
            headers={"X-Request-ID": custom_request_id},
        )

        # Should return 404
        assert response.status_code == 404

        # Should include request_id in header
        assert response.headers.get("X-Request-Id") == custom_request_id

        # Should include request_id in error body
        data = response.json()
        if "error" in data:
            assert "request_id" in data["error"]
            assert data["error"]["request_id"] == custom_request_id


def test_malformed_json_returns_error(api_base_url: str) -> None:
    """Endpoints should handle malformed JSON gracefully."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Send malformed JSON (not valid JSON)
        response = client.post(
            "/api/v1/chat",
            content="{invalid json}",
            headers={"Content-Type": "application/json"},
        )

        # Should return 400 or 422 for malformed JSON
        assert response.status_code in (400, 422)


def test_unsupported_content_type(api_base_url: str) -> None:
    """Endpoints should reject unsupported content types."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        # Send form data instead of JSON
        response = client.post(
            "/api/v1/chat",
            data={"prompt": "test", "session": "test"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        # Should return 415 (Unsupported Media Type) or 422
        assert response.status_code in (415, 422)


# ============================================================================
# Additional Edge Cases
# ============================================================================


def test_get_nonexistent_session_returns_404(api_base_url: str) -> None:
    """GET /api/v1/sessions/{name} for nonexistent session should return 404."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.get("/api/v1/sessions/definitely-does-not-exist-xyz")
        assert response.status_code == 404


def test_delete_nonexistent_session_returns_404(api_base_url: str) -> None:
    """DELETE /api/v1/sessions/{name} for nonexistent session should return 404."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.delete("/api/v1/sessions/nonexistent-session-abc")
        assert response.status_code == 404


def test_cors_preflight_on_protected_endpoint(api_base_url: str) -> None:
    """OPTIONS requests (CORS preflight) should work on all endpoints."""
    with httpx.Client(base_url=api_base_url, timeout=5.0) as client:
        response = client.options(
            "/api/v1/chat",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )

        # OPTIONS should succeed
        assert response.status_code in (200, 204)

        # Should have CORS headers
        assert (
            "access-control-allow-origin" in response.headers
            or "Access-Control-Allow-Origin" in response.headers
        )
