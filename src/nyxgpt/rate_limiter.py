from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


@dataclass
class ClientBucket:
    """Token bucket for a single client."""

    tokens: float
    last_refill: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)


class RateLimiter:
    """In-memory rate limiter using token bucket algorithm.

    Thread-safe implementation that tracks per-client request rates.
    Uses token bucket algorithm for efficient and burst-tolerant rate limiting.

    Attributes:
        requests_per_second: Maximum sustained request rate per client
        burst_size: Maximum burst of requests allowed above the rate
    """

    def __init__(self, requests_per_second: int, burst_size: int):
        """Initialize rate limiter with specified limits.

        Args:
            requests_per_second: Sustained rate limit (tokens added per second)
            burst_size: Maximum tokens in bucket (allows bursts)
        """
        self._clients: dict[str, ClientBucket] = {}
        self._clients_lock = threading.Lock()
        self._requests_per_second = requests_per_second
        self._burst_size = burst_size
        self._last_cleanup = time.time()

    def is_allowed(self, client_id: str) -> tuple[bool, dict[str, str]]:
        """Check if request is allowed under rate limit.

        Token bucket algorithm:
        1. Refill tokens based on time elapsed
        2. If tokens available, consume one and allow
        3. Otherwise, deny request

        Args:
            client_id: Unique identifier for client (typically IP address)

        Returns:
            Tuple of (allowed, headers):
            - allowed: True if request is within rate limit
            - headers: Dict of X-RateLimit-* headers to add to response
        """
        self._maybe_cleanup()

        # Get or create client bucket
        with self._clients_lock:
            if client_id not in self._clients:
                self._clients[client_id] = ClientBucket(
                    tokens=float(self._burst_size), last_refill=time.time()
                )
            bucket = self._clients[client_id]

        # Refill tokens based on elapsed time
        with bucket.lock:
            now = time.time()
            elapsed = now - bucket.last_refill
            bucket.tokens = min(
                self._burst_size, bucket.tokens + (elapsed * self._requests_per_second)
            )
            bucket.last_refill = now

            # Check if request can be allowed
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                allowed = True
            else:
                allowed = False

            # Generate rate limit headers
            remaining = max(0, int(bucket.tokens))
            # Reset time is when bucket will be full again
            reset_time = now + ((self._burst_size - bucket.tokens) / self._requests_per_second)

            headers = {
                "X-RateLimit-Limit": str(self._burst_size),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(int(reset_time)),
            }

            return allowed, headers

    def get_client_ip(self, request: Request) -> str:
        """Extract client IP address from request.

        Handles X-Forwarded-For header for proxied requests.

        Args:
            request: FastAPI Request object

        Returns:
            Client IP address as string
        """
        # Check X-Forwarded-For header (for proxied requests)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # X-Forwarded-For can contain multiple IPs (client, proxy1, proxy2, ...)
            # Use the first (leftmost) IP as the client
            return str(forwarded_for.split(",")[0]).strip()

        # Fall back to direct client IP
        if request.client:
            return str(request.client.host)

        # Default fallback (should rarely happen)
        return "unknown"

    def _maybe_cleanup(self) -> None:
        """Cleanup stale client entries periodically.

        Removes clients that haven't made requests in the last 5 minutes.
        This prevents unbounded memory growth with many unique clients.
        """
        now = time.time()
        # Only cleanup every 60 seconds
        if now - self._last_cleanup < 60:
            return

        with self._clients_lock:
            # Remove clients with no requests in last 5 minutes
            stale_threshold = now - 300  # 5 minutes
            self._clients = {
                client_id: bucket
                for client_id, bucket in self._clients.items()
                if bucket.last_refill > stale_threshold
            }
            self._last_cleanup = now


__all__ = ["RateLimiter", "ClientBucket"]
