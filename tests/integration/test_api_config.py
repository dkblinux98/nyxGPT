"""Integration tests for /api/v1/config endpoints.

These tests verify that the config GET/POST endpoints work correctly
and support the exact payload structure expected by the Next.js web UI.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
def test_config_get_endpoint(api_base_url: str) -> None:
    """Verify GET /api/v1/config returns expected structure."""
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200

    data = r.json()
    assert isinstance(data, dict)

    # Verify all expected fields are present
    assert "ollama_base_url" in data
    assert "default_model" in data
    assert "rag_enabled" in data
    assert "log_level" in data

    # Verify field types
    assert isinstance(data["ollama_base_url"], str)
    assert isinstance(data["default_model"], str)
    assert isinstance(data["rag_enabled"], bool)
    assert isinstance(data["log_level"], str)

    # Verify log_level is uppercase
    assert data["log_level"] in ["DEBUG", "INFO", "WARNING", "ERROR"]


@pytest.mark.integration
def test_config_post_endpoint(api_base_url: str) -> None:
    """Verify POST /api/v1/config accepts and applies configuration updates."""
    # First, get current config
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()

    # Send update with payload structure matching Next.js web UI
    update_payload = {
        "default_model": "llama3.1:8b",
        "rag_enabled": False,
        "log_level": "INFO",
    }

    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=update_payload,
        timeout=5.0,
    )
    assert r.status_code == 200

    data = r.json()
    assert isinstance(data, dict)

    # Verify response contains 'updated' and 'effective' fields
    assert "updated" in data
    assert "effective" in data

    # Verify updated fields list
    assert isinstance(data["updated"], list)

    # Verify effective config structure matches GET response
    effective = data["effective"]
    assert "ollama_base_url" in effective
    assert "default_model" in effective
    assert "rag_enabled" in effective
    assert "log_level" in effective

    # Verify the updates were applied
    assert effective["default_model"] == update_payload["default_model"]
    assert effective["rag_enabled"] == update_payload["rag_enabled"]
    assert effective["log_level"] == update_payload["log_level"]


@pytest.mark.integration
def test_config_post_partial_update(api_base_url: str) -> None:
    """Verify POST /api/v1/config accepts partial updates."""
    # Send update with only some fields (matching web UI behavior)
    partial_payload = {
        "log_level": "DEBUG",
    }

    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=partial_payload,
        timeout=5.0,
    )
    assert r.status_code == 200

    data = r.json()
    assert isinstance(data, dict)
    assert "effective" in data

    # Verify the partial update was applied
    assert data["effective"]["log_level"] == "DEBUG"

    # Verify other fields are still present
    assert "default_model" in data["effective"]
    assert "rag_enabled" in data["effective"]


@pytest.mark.integration
def test_config_post_ignores_unknown_fields(api_base_url: str) -> None:
    """Verify POST /api/v1/config ignores unknown fields gracefully."""
    payload = {
        "default_model": "llama3.1:8b",
        "unknown_field": "should_be_ignored",
        "another_unknown": 12345,
    }

    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=payload,
        timeout=5.0,
    )

    # Should succeed and ignore unknown fields
    assert r.status_code == 200

    data = r.json()
    assert "effective" in data
    assert data["effective"]["default_model"] == "llama3.1:8b"

    # Verify unknown fields are not in the response
    assert "unknown_field" not in data["effective"]
    assert "another_unknown" not in data["effective"]
