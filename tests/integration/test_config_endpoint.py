"""Integration tests for /api/v1/config endpoint.

These tests verify that the backend /api/v1/config endpoint exists,
is properly mounted under the /api/v1 prefix, and returns the exact
request/response format expected by the configuration wizard frontend.

Addresses acceptance failure #2922 from parent issue #2636, which
incorrectly claimed the endpoint was missing.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
def test_config_get_endpoint(api_base_url: str) -> None:
    """Verify that GET /api/v1/config returns expected configuration fields."""
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200

    data = r.json()
    assert isinstance(data, dict)

    # Verify all required fields are present
    assert "ollama_base_url" in data
    assert "default_model" in data
    assert "rag_enabled" in data
    assert "log_level" in data

    # Verify field types
    assert isinstance(data["ollama_base_url"], str)
    assert isinstance(data["default_model"], str)
    assert isinstance(data["rag_enabled"], bool)
    assert isinstance(data["log_level"], str)

    # Verify log_level is uppercase as expected by frontend
    assert data["log_level"].isupper()


@pytest.mark.integration
def test_config_post_endpoint(api_base_url: str) -> None:
    """Verify that POST /api/v1/config updates configuration correctly."""
    # First, get current config to restore later
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()

    # Update configuration with valid payload
    update_payload = {
        "default_model": "llama3.1:8b",
        "rag_enabled": True,
        "log_level": "DEBUG",
    }

    r = httpx.post(f"{api_base_url}/api/v1/config", json=update_payload, timeout=5.0)
    assert r.status_code == 200

    data = r.json()
    assert isinstance(data, dict)

    # Verify response structure
    assert "updated" in data
    assert "effective" in data
    assert isinstance(data["updated"], dict)
    assert isinstance(data["effective"], dict)

    # Verify effective config contains all required fields
    effective = data["effective"]
    assert "ollama_base_url" in effective
    assert "default_model" in effective
    assert "rag_enabled" in effective
    assert "log_level" in effective

    # Verify the updates were applied to effective config
    assert effective["default_model"] == update_payload["default_model"]
    assert effective["rag_enabled"] == update_payload["rag_enabled"]
    assert effective["log_level"] == update_payload["log_level"]

    # Restore original configuration
    restore_payload = {
        "default_model": original_config["default_model"],
        "rag_enabled": original_config["rag_enabled"],
        "log_level": original_config["log_level"],
    }
    r = httpx.post(f"{api_base_url}/api/v1/config", json=restore_payload, timeout=5.0)
    assert r.status_code == 200


@pytest.mark.integration
def test_config_post_partial_update(api_base_url: str) -> None:
    """Verify that POST /api/v1/config supports partial updates."""
    # Get current config
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()

    # Update only log_level
    partial_payload = {"log_level": "WARNING"}

    r = httpx.post(f"{api_base_url}/api/v1/config", json=partial_payload, timeout=5.0)
    assert r.status_code == 200

    data = r.json()
    effective = data["effective"]

    # Verify only log_level changed, others remain the same
    assert effective["log_level"] == "WARNING"
    assert effective["default_model"] == original_config["default_model"]
    assert effective["rag_enabled"] == original_config["rag_enabled"]

    # Restore original log_level
    restore_payload = {"log_level": original_config["log_level"]}
    r = httpx.post(f"{api_base_url}/api/v1/config", json=restore_payload, timeout=5.0)
    assert r.status_code == 200


@pytest.mark.integration
def test_config_post_ignores_unknown_fields(api_base_url: str) -> None:
    """Verify that POST /api/v1/config ignores unknown fields safely."""
    # Get current config
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()

    # Send payload with unknown field
    payload = {"log_level": "INFO", "unknown_field": "should_be_ignored"}

    r = httpx.post(f"{api_base_url}/api/v1/config", json=payload, timeout=5.0)
    assert r.status_code == 200

    data = r.json()
    # Should succeed and only apply known fields
    assert data["effective"]["log_level"] == "INFO"

    # Restore original config
    restore_payload = {"log_level": original_config["log_level"]}
    r = httpx.post(f"{api_base_url}/api/v1/config", json=restore_payload, timeout=5.0)
    assert r.status_code == 200


@pytest.mark.integration
def test_config_endpoint_format_matches_frontend_expectations(
    api_base_url: str,
) -> None:
    """Verify the config endpoint returns data in the exact format expected by the frontend.

    This test ensures the backend /api/v1/config endpoint is properly mounted and returns
    the exact response structure that the configuration wizard frontend expects.
    Addresses acceptance failure #2922 from parent issue #2636.
    """
    # Test GET endpoint
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200

    config = r.json()

    # Frontend expects these exact fields (web/src/app/admin/page.tsx:33-38)
    required_fields = ["ollama_base_url", "default_model", "rag_enabled", "log_level"]
    for field in required_fields:
        assert field in config, f"Missing required field: {field}"

    # Test POST endpoint with payload format from frontend
    # (web/src/app/admin/page.tsx:155-159)
    frontend_payload = {
        "default_model": config["default_model"],
        "rag_enabled": config["rag_enabled"],
        "log_level": config["log_level"],
    }

    r = httpx.post(f"{api_base_url}/api/v1/config", json=frontend_payload, timeout=5.0)
    assert r.status_code == 200

    response = r.json()

    # Frontend expects response with "updated" and "effective" fields
    # (implied by backend implementation src/nyxgpt/app.py:610-623)
    assert "updated" in response
    assert "effective" in response

    # Effective config must contain all required fields
    effective = response["effective"]
    for field in required_fields:
        assert field in effective, f"Missing field in effective config: {field}"
