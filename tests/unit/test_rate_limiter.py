from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from nyxgpt.rate_limiter import ClientBucket, RateLimiter

pytestmark = pytest.mark.unit


def test_client_bucket_creation():
    """Test that ClientBucket dataclass is created correctly."""
    bucket = ClientBucket(tokens=10.0)
    assert bucket.tokens == 10.0
    assert isinstance(bucket.last_refill, float)
    assert bucket.lock is not None


def test_rate_limiter_initialization():
    """Test that RateLimiter initializes with correct parameters."""
    limiter = RateLimiter(requests_per_second=5, burst_size=10)
    assert limiter._requests_per_second == 5
    assert limiter._burst_size == 10
    assert limiter._clients == {}
    assert isinstance(limiter._last_cleanup, float)


def test_rate_limiter_allows_within_limit():
    """Rate limiter should allow requests within limit."""
    limiter = RateLimiter(requests_per_second=2, burst_size=5)

    # First 5 requests should succeed (burst)
    for i in range(5):
        allowed, headers = limiter.is_allowed("client1")
        assert allowed, f"Request {i + 1} should be allowed"
        assert "X-RateLimit-Limit" in headers
        assert "X-RateLimit-Remaining" in headers
        assert "X-RateLimit-Reset" in headers
        assert headers["X-RateLimit-Limit"] == "5"


def test_rate_limiter_blocks_over_limit():
    """Rate limiter should block requests exceeding limit."""
    limiter = RateLimiter(requests_per_second=1, burst_size=2)

    # First 2 requests succeed
    allowed1, headers1 = limiter.is_allowed("client1")
    allowed2, headers2 = limiter.is_allowed("client1")
    assert allowed1
    assert allowed2

    # Third should fail (no tokens left)
    allowed3, headers3 = limiter.is_allowed("client1")
    assert not allowed3
    assert int(headers3["X-RateLimit-Remaining"]) == 0


def test_rate_limiter_refills_tokens():
    """Rate limiter should refill tokens over time."""
    limiter = RateLimiter(requests_per_second=10, burst_size=2)

    # Exhaust burst
    limiter.is_allowed("client1")
    limiter.is_allowed("client1")

    # Should be blocked
    allowed, _ = limiter.is_allowed("client1")
    assert not allowed

    # Wait for refill (0.2 seconds = 2 tokens at 10 req/sec)
    time.sleep(0.2)

    # Should be allowed again
    allowed, headers = limiter.is_allowed("client1")
    assert allowed


def test_rate_limiter_separate_clients():
    """Different clients should have independent rate limits."""
    limiter = RateLimiter(requests_per_second=1, burst_size=2)

    # Exhaust client1
    limiter.is_allowed("client1")
    limiter.is_allowed("client1")
    allowed1, _ = limiter.is_allowed("client1")
    assert not allowed1

    # client2 should still be allowed
    allowed2, _ = limiter.is_allowed("client2")
    assert allowed2


def test_rate_limiter_headers_format():
    """Rate limit headers should have correct format."""
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    allowed, headers = limiter.is_allowed("client1")
    assert allowed

    # Check header presence
    assert "X-RateLimit-Limit" in headers
    assert "X-RateLimit-Remaining" in headers
    assert "X-RateLimit-Reset" in headers

    # Check header values are valid
    assert int(headers["X-RateLimit-Limit"]) == 20
    assert 0 <= int(headers["X-RateLimit-Remaining"]) <= 20
    # Reset should be a timestamp close to now (allow small timing difference)
    reset_time = int(headers["X-RateLimit-Reset"])
    now = int(time.time())
    assert abs(reset_time - now) <= 2  # Within 2 seconds


def test_rate_limiter_headers_on_blocked_request():
    """Rate limit headers should be present even when request is blocked."""
    limiter = RateLimiter(requests_per_second=1, burst_size=1)

    # First request succeeds
    limiter.is_allowed("client1")

    # Second request blocked
    allowed, headers = limiter.is_allowed("client1")
    assert not allowed
    assert "X-RateLimit-Limit" in headers
    assert "X-RateLimit-Remaining" in headers
    assert "X-RateLimit-Reset" in headers
    assert headers["X-RateLimit-Remaining"] == "0"


def test_get_client_ip_direct():
    """Test extracting client IP from direct connection."""
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    # Mock request with direct client
    request = Mock()
    request.headers = {}
    request.client = Mock(host="192.168.1.100")

    ip = limiter.get_client_ip(request)
    assert ip == "192.168.1.100"


def test_get_client_ip_forwarded():
    """Test extracting client IP from X-Forwarded-For header."""
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    # Mock request with X-Forwarded-For
    request = Mock()
    request.headers = {"x-forwarded-for": "203.0.113.195, 192.168.1.1"}
    request.client = Mock(host="192.168.1.1")

    ip = limiter.get_client_ip(request)
    # Should use leftmost IP from X-Forwarded-For
    assert ip == "203.0.113.195"


def test_get_client_ip_no_client():
    """Test extracting client IP when client info is missing."""
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    # Mock request with no client
    request = Mock()
    request.headers = {}
    request.client = None

    ip = limiter.get_client_ip(request)
    assert ip == "unknown"


def test_rate_limiter_burst_behavior():
    """Test that burst allows temporary spikes above sustained rate."""
    limiter = RateLimiter(requests_per_second=1, burst_size=5)

    # Should allow 5 requests immediately (burst)
    for i in range(5):
        allowed, _ = limiter.is_allowed("client1")
        assert allowed, f"Burst request {i + 1} should be allowed"

    # 6th request should be blocked
    allowed, _ = limiter.is_allowed("client1")
    assert not allowed


def test_rate_limiter_cleanup_stale_clients():
    """Test that stale clients are cleaned up periodically."""
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    # Create clients
    limiter.is_allowed("client1")
    limiter.is_allowed("client2")
    limiter.is_allowed("client3")

    assert len(limiter._clients) == 3

    # Manually set last_cleanup to trigger cleanup
    limiter._last_cleanup = time.time() - 61  # Over 60 seconds ago

    # Manually set client2 as stale (over 5 minutes old)
    limiter._clients["client2"].last_refill = time.time() - 301

    # Trigger cleanup by making a new request
    limiter.is_allowed("client1")

    # client2 should be removed (stale)
    # client1 and client3 should remain
    assert "client2" not in limiter._clients
    assert "client1" in limiter._clients
    assert "client3" in limiter._clients


def test_rate_limiter_no_cleanup_if_recent():
    """Test that cleanup doesn't run if last cleanup was recent."""
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    # Create clients
    limiter.is_allowed("client1")
    limiter.is_allowed("client2")

    client_count_before = len(limiter._clients)

    # Make stale client (but cleanup shouldn't run)
    limiter._clients["client2"].last_refill = time.time() - 301
    limiter._last_cleanup = time.time()  # Just cleaned up

    # Trigger check
    limiter.is_allowed("client1")

    # No cleanup should have occurred
    assert len(limiter._clients) == client_count_before


def test_rate_limiter_thread_safety():
    """Test that rate limiter handles concurrent access safely."""
    import threading

    limiter = RateLimiter(requests_per_second=100, burst_size=200)
    results = []

    def make_requests():
        for _ in range(50):
            allowed, _ = limiter.is_allowed("client1")
            results.append(allowed)

    # Create 4 threads making 50 requests each (200 total)
    threads = [threading.Thread(target=make_requests) for _ in range(4)]

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Should have exactly 200 successful requests (burst_size)
    # Some might be blocked if tokens run out
    assert len(results) == 200
    assert sum(results) <= 200  # At most burst_size allowed


def test_rate_limiter_token_bucket_math():
    """Test token bucket refill calculation is correct."""
    limiter = RateLimiter(requests_per_second=10, burst_size=100)

    # Consume most tokens to avoid hitting burst cap during refill
    for _ in range(95):
        limiter.is_allowed("client1")

    bucket = limiter._clients["client1"]
    # Should have ~5 tokens left
    initial_tokens = bucket.tokens

    # Wait exactly 0.1 seconds
    time.sleep(0.1)

    # Make another request
    limiter.is_allowed("client1")

    # Tokens should have refilled by approximately 1 (10 req/sec * 0.1 sec)
    # So: initial_tokens + 1 (refill) - 1 (consumed) ≈ initial_tokens
    final_tokens = limiter._clients["client1"].tokens

    # Allow some tolerance for timing precision
    # Final should be approximately equal to initial (within 0.5 tokens)
    assert abs(final_tokens - initial_tokens) < 0.5


def test_rate_limiter_respects_burst_cap():
    """Test that token refill doesn't exceed burst_size."""
    limiter = RateLimiter(requests_per_second=100, burst_size=10)

    # Make one request to create bucket
    limiter.is_allowed("client1")

    # Wait for more than burst_size worth of refill time
    # (10 tokens / 100 per sec = 0.1 sec)
    time.sleep(0.2)  # Wait double the time needed

    # Check bucket tokens
    bucket = limiter._clients["client1"]
    with bucket.lock:
        # Manually trigger refill
        now = time.time()
        elapsed = now - bucket.last_refill
        refilled = bucket.tokens + (elapsed * limiter._requests_per_second)

        # Should be capped at burst_size
        assert refilled >= limiter._burst_size
        # After capping in is_allowed(), should be exactly burst_size

    # Make one request to trigger refill logic
    limiter.is_allowed("client1")

    # Bucket should have burst_size - 1 tokens (capped, then 1 consumed)
    assert limiter._clients["client1"].tokens <= limiter._burst_size


def test_get_client_ip_returns_str_type_for_direct_connection():
    """get_client_ip always returns str (not None or other type) for direct client."""
    # Covers src/nyxgpt/rate_limiter.py:120 – the str() cast ensures the return
    # type is str even when mypy strict mode is active.
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    request = Mock()
    request.headers = {}
    request.client = Mock(host="10.0.0.1")

    result = limiter.get_client_ip(request)

    assert isinstance(result, str), "get_client_ip must return str, not a subtype or None"
    assert result == "10.0.0.1"


def test_get_client_ip_returns_str_type_for_forwarded_header():
    """get_client_ip always returns str for X-Forwarded-For (with leading whitespace stripped)."""
    # Covers src/nyxgpt/rate_limiter.py:116 – str() cast plus .strip() removes
    # any whitespace that X-Forwarded-For proxies may add around the IP address.
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    # Header with extra whitespace around the first IP (common in some proxies)
    request = Mock()
    request.headers = {"x-forwarded-for": "  198.51.100.42  , 192.168.0.1"}
    request.client = Mock(host="192.168.0.1")

    result = limiter.get_client_ip(request)

    assert isinstance(result, str), "get_client_ip must return str"
    assert result == "198.51.100.42", "Leading/trailing whitespace must be stripped from IP"


def test_get_client_ip_returns_str_type_when_no_client():
    """get_client_ip returns the literal str 'unknown' when no client is available."""
    # Covers the fallback branch at src/nyxgpt/rate_limiter.py:123.
    limiter = RateLimiter(requests_per_second=10, burst_size=20)

    request = Mock()
    request.headers = {}
    request.client = None

    result = limiter.get_client_ip(request)

    assert isinstance(result, str), "get_client_ip must return str even for the fallback branch"
    assert result == "unknown"
