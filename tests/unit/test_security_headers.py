from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


def test_security_headers_present_on_health_endpoint():
    """All security headers should be present on API responses."""
    client = TestClient(app)
    response = client.get("/health")

    # All security headers should be present
    assert "Content-Security-Policy" in response.headers
    assert "X-Content-Type-Options" in response.headers
    assert "X-Frame-Options" in response.headers


def test_content_security_policy_header():
    """Content-Security-Policy header should have correct directives."""
    client = TestClient(app)
    response = client.get("/health")

    csp = response.headers["Content-Security-Policy"]

    # Check for key CSP directives
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert "base-uri 'self'" in csp


def test_x_content_type_options_header():
    """X-Content-Type-Options should be set to nosniff."""
    client = TestClient(app)
    response = client.get("/health")

    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_x_frame_options_header():
    """X-Frame-Options should be set to DENY."""
    client = TestClient(app)
    response = client.get("/health")

    assert response.headers["X-Frame-Options"] == "DENY"


def test_hsts_header_not_present_for_http():
    """Strict-Transport-Security should NOT be present for HTTP requests."""
    client = TestClient(app)
    response = client.get("/health")

    # TestClient uses http:// by default, so HSTS should not be present
    assert "Strict-Transport-Security" not in response.headers


def test_security_headers_on_api_endpoints():
    """Security headers should be present on API endpoints."""
    client = TestClient(app)
    response = client.get("/api/v1/info")

    assert "Content-Security-Policy" in response.headers
    assert "X-Content-Type-Options" in response.headers
    assert "X-Frame-Options" in response.headers


def test_security_headers_on_error_responses():
    """Security headers should be present even on error responses."""
    client = TestClient(app)
    # Request to non-existent session should return 404
    response = client.delete("/api/v1/sessions/nonexistent-session-xyz")

    assert response.status_code == 404
    assert "Content-Security-Policy" in response.headers
    assert "X-Content-Type-Options" in response.headers
    assert "X-Frame-Options" in response.headers


def test_cors_headers_still_present():
    """CORS headers should still work alongside security headers."""
    client = TestClient(app)
    # Make OPTIONS request (preflight)
    response = client.options(
        "/api/v1/info",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )

    # Should have both CORS and security headers
    assert "access-control-allow-origin" in response.headers
    assert "Content-Security-Policy" in response.headers
    assert "X-Content-Type-Options" in response.headers


def test_request_id_header_still_present():
    """Request ID header should still work with security headers."""
    client = TestClient(app)
    response = client.get("/health")

    # Should have both security headers and request ID
    assert "X-Request-Id" in response.headers
    assert "Content-Security-Policy" in response.headers


def test_security_headers_do_not_interfere_with_json_response():
    """Security headers should not affect JSON response content."""
    client = TestClient(app)
    response = client.get("/health")

    # Should still get valid JSON
    assert response.json() == {"status": "ok"}

    # And have security headers
    assert "Content-Security-Policy" in response.headers
    assert "X-Content-Type-Options" in response.headers
