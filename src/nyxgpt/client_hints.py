"""Client capability negotiation and hints.

Allows server to adapt responses based on client capabilities and features.
Supports graceful degradation for older clients.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ClientCapabilities:
    """Client capabilities detected from headers and request context."""

    # Transport capabilities
    supports_sse: bool  # Server-Sent Events
    supports_structured_events: bool  # JSON structured events
    supports_streaming: bool  # Any streaming (plain or SSE)

    # Feature capabilities
    supports_rag: bool  # RAG context in responses
    supports_metadata: bool  # Metadata events
    supports_retry_events: bool  # Retry status events

    # Version info
    client_version: str | None  # Client version string (e.g., "nyxGPT-web/1.0")
    api_version: str | None  # Requested API version

    # Format preferences
    prefer_format: str  # "sse", "plain", "json"


def detect_capabilities(headers: dict[str, str]) -> ClientCapabilities:
    """Detect client capabilities from request headers.

    Args:
        headers: Request headers dict (case-insensitive)

    Returns:
        ClientCapabilities with detected features

    Recognized headers:
    - Accept: text/event-stream, application/json, text/plain
    - X-Client-Version: Client version string
    - X-API-Version: Requested API version
    - X-Supports-SSE: true/false
    - X-Supports-Structured-Events: true/false
    - X-Supports-RAG: true/false
    """
    # Normalize headers to lowercase for case-insensitive lookup
    headers_lower = {k.lower(): v for k, v in headers.items()}

    # Detect SSE support
    accept = headers_lower.get("accept", "")
    supports_sse = (
        "text/event-stream" in accept
        or headers_lower.get("x-supports-sse", "").lower() == "true"
    )

    # Detect structured events support
    supports_structured = (
        "application/json" in accept
        or headers_lower.get("x-supports-structured-events", "").lower() == "true"
    )

    # Detect streaming support (either SSE or plain)
    supports_streaming = (
        supports_sse
        or "text/plain" in accept
        or headers_lower.get("x-supports-streaming", "").lower() == "true"
    )

    # Feature detection
    supports_rag = headers_lower.get("x-supports-rag", "").lower() != "false"
    supports_metadata = headers_lower.get("x-supports-metadata", "").lower() != "false"
    supports_retry = headers_lower.get("x-supports-retry-events", "").lower() != "false"

    # Version info
    client_version = headers_lower.get("x-client-version", headers_lower.get("user-agent"))
    api_version = headers_lower.get("x-api-version")

    # Determine preferred format
    if supports_sse and supports_structured:
        prefer_format = "sse"
    elif supports_structured:
        prefer_format = "json"
    else:
        prefer_format = "plain"

    caps = ClientCapabilities(
        supports_sse=supports_sse,
        supports_structured_events=supports_structured,
        supports_streaming=supports_streaming,
        supports_rag=supports_rag,
        supports_metadata=supports_metadata,
        supports_retry_events=supports_retry,
        client_version=client_version,
        api_version=api_version,
        prefer_format=prefer_format,
    )

    logger.debug(
        "Detected client capabilities: sse=%s, structured=%s, format=%s, version=%s",
        caps.supports_sse,
        caps.supports_structured_events,
        caps.prefer_format,
        caps.client_version,
    )

    return caps


def get_response_format(capabilities: ClientCapabilities) -> str:
    """Determine the best response format based on client capabilities.

    Args:
        capabilities: Detected client capabilities

    Returns:
        Format string: "sse", "json", or "plain"
    """
    return capabilities.prefer_format


def should_include_metadata(capabilities: ClientCapabilities) -> bool:
    """Check if client supports metadata events.

    Args:
        capabilities: Client capabilities

    Returns:
        True if metadata should be included
    """
    return capabilities.supports_metadata and capabilities.supports_structured_events


def should_include_rag_context(capabilities: ClientCapabilities) -> bool:
    """Check if client supports RAG context in responses.

    Args:
        capabilities: Client capabilities

    Returns:
        True if RAG context should be included
    """
    return capabilities.supports_rag


def get_media_type(format: str) -> str:
    """Get the media type for a given format.

    Args:
        format: Format string ("sse", "json", or "plain")

    Returns:
        Media type string for Content-Type header
    """
    media_types = {
        "sse": "text/event-stream; charset=utf-8",
        "json": "application/json; charset=utf-8",
        "plain": "text/plain; charset=utf-8",
    }
    return media_types.get(format, "text/plain; charset=utf-8")
