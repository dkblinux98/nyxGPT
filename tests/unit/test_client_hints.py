"""Unit tests for client capability negotiation."""

import pytest
from nyxgpt.client_hints import (
    ClientCapabilities,
    detect_capabilities,
    get_response_format,
    should_include_metadata,
    should_include_rag_context,
    get_media_type,
)


class TestDetectCapabilities:
    """Test client capability detection."""

    def test_detect_sse_from_accept(self):
        """Should detect SSE support from Accept header."""
        headers = {"Accept": "text/event-stream"}
        caps = detect_capabilities(headers)

        assert caps.supports_sse is True
        assert caps.supports_streaming is True

    def test_detect_sse_from_custom_header(self):
        """Should detect SSE support from X-Supports-SSE header."""
        headers = {"X-Supports-SSE": "true"}
        caps = detect_capabilities(headers)

        assert caps.supports_sse is True

    def test_detect_structured_from_accept(self):
        """Should detect structured events from Accept header."""
        headers = {"Accept": "application/json"}
        caps = detect_capabilities(headers)

        assert caps.supports_structured_events is True

    def test_detect_plain_text(self):
        """Should detect plain text streaming."""
        headers = {"Accept": "text/plain"}
        caps = detect_capabilities(headers)

        assert caps.supports_streaming is True
        assert caps.supports_sse is False

    def test_case_insensitive_headers(self):
        """Should handle case-insensitive headers."""
        headers = {"accept": "text/event-stream", "X-CLIENT-VERSION": "1.0"}
        caps = detect_capabilities(headers)

        assert caps.supports_sse is True
        assert caps.client_version == "1.0"

    def test_version_detection(self):
        """Should extract client and API versions."""
        headers = {
            "X-Client-Version": "nyxGPT-web/1.2.3",
            "X-API-Version": "v1",
        }
        caps = detect_capabilities(headers)

        assert caps.client_version == "nyxGPT-web/1.2.3"
        assert caps.api_version == "v1"

    def test_feature_flags(self):
        """Should respect feature flag headers."""
        headers = {
            "X-Supports-RAG": "false",
            "X-Supports-Metadata": "false",
        }
        caps = detect_capabilities(headers)

        assert caps.supports_rag is False
        assert caps.supports_metadata is False

    def test_default_capabilities(self):
        """Should use sensible defaults for minimal headers."""
        headers = {}
        caps = detect_capabilities(headers)

        assert caps.supports_streaming is False
        assert caps.supports_sse is False
        assert caps.supports_rag is True  # RAG enabled by default
        assert caps.prefer_format == "plain"


class TestResponseFormat:
    """Test response format selection."""

    def test_prefer_sse_when_supported(self):
        """Should prefer SSE format when both SSE and structured are supported."""
        caps = ClientCapabilities(
            supports_sse=True,
            supports_structured_events=True,
            supports_streaming=True,
            supports_rag=True,
            supports_metadata=True,
            supports_retry_events=True,
            client_version=None,
            api_version=None,
            prefer_format="sse",
        )

        assert get_response_format(caps) == "sse"

    def test_fallback_to_json(self):
        """Should use JSON format when SSE not supported but structured is."""
        caps = ClientCapabilities(
            supports_sse=False,
            supports_structured_events=True,
            supports_streaming=False,
            supports_rag=True,
            supports_metadata=True,
            supports_retry_events=True,
            client_version=None,
            api_version=None,
            prefer_format="json",
        )

        assert get_response_format(caps) == "json"

    def test_fallback_to_plain(self):
        """Should use plain text for minimal clients."""
        caps = ClientCapabilities(
            supports_sse=False,
            supports_structured_events=False,
            supports_streaming=False,
            supports_rag=True,
            supports_metadata=False,
            supports_retry_events=False,
            client_version=None,
            api_version=None,
            prefer_format="plain",
        )

        assert get_response_format(caps) == "plain"


class TestMetadataInclusion:
    """Test metadata inclusion logic."""

    def test_include_metadata_when_supported(self):
        """Should include metadata when client supports it."""
        caps = ClientCapabilities(
            supports_sse=True,
            supports_structured_events=True,
            supports_streaming=True,
            supports_rag=True,
            supports_metadata=True,
            supports_retry_events=True,
            client_version=None,
            api_version=None,
            prefer_format="sse",
        )

        assert should_include_metadata(caps) is True

    def test_exclude_metadata_when_not_supported(self):
        """Should exclude metadata for simple clients."""
        caps = ClientCapabilities(
            supports_sse=False,
            supports_structured_events=False,
            supports_streaming=False,
            supports_rag=True,
            supports_metadata=False,
            supports_retry_events=False,
            client_version=None,
            api_version=None,
            prefer_format="plain",
        )

        assert should_include_metadata(caps) is False


class TestMediaType:
    """Test media type selection."""

    def test_sse_media_type(self):
        """Should return SSE media type."""
        assert get_media_type("sse") == "text/event-stream; charset=utf-8"

    def test_json_media_type(self):
        """Should return JSON media type."""
        assert get_media_type("json") == "application/json; charset=utf-8"

    def test_plain_media_type(self):
        """Should return plain text media type."""
        assert get_media_type("plain") == "text/plain; charset=utf-8"

    def test_unknown_media_type(self):
        """Should fall back to plain text for unknown formats."""
        assert get_media_type("unknown") == "text/plain; charset=utf-8"
