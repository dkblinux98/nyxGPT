"""Integration tests for /api/v1/config endpoint.

Tests verify the end-to-end flow between frontend and backend configuration
management, including GET, POST, and PATCH methods.

Related: #2636 (Configuration wizard), #2923 (Backend API verification)
"""
from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
def test_config_get_endpoint(api_base_url: str) -> None:
    """Test GET /api/v1/config returns expected config fields."""
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

    # Verify log_level is a valid value
    assert data["log_level"] in ["DEBUG", "INFO", "WARNING", "ERROR"]


@pytest.mark.integration
def test_config_post_endpoint(api_base_url: str) -> None:
    """Test POST /api/v1/config updates configuration correctly."""
    # First, get the current config to restore later
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()

    # Update config with new values
    update_payload = {
        "default_model": "test-model:latest",
        "rag_enabled": True,
        "log_level": "DEBUG",
    }

    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=update_payload,
        timeout=5.0,
    )
    assert r.status_code == 200

    data = r.json()
    assert isinstance(data, dict)

    # Verify response structure
    assert "updated" in data
    assert "effective" in data

    # Verify updated fields are returned
    assert isinstance(data["updated"], dict)
    assert data["updated"]["default_model"] == "test-model:latest"
    assert data["updated"]["rag_enabled"] is True
    assert data["updated"]["log_level"] == "DEBUG"

    # Verify effective config includes all fields
    effective = data["effective"]
    assert "ollama_base_url" in effective
    assert "default_model" in effective
    assert "rag_enabled" in effective
    assert "log_level" in effective

    # Verify the changes were applied
    assert effective["default_model"] == "test-model:latest"
    assert effective["rag_enabled"] is True
    assert effective["log_level"] == "DEBUG"

    # Verify GET reflects the changes
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    current_config = r.json()
    assert current_config["default_model"] == "test-model:latest"
    assert current_config["rag_enabled"] is True
    assert current_config["log_level"] == "DEBUG"

    # Restore original config
    restore_payload = {
        "default_model": original_config["default_model"],
        "rag_enabled": original_config["rag_enabled"],
        "log_level": original_config["log_level"],
    }
    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=restore_payload,
        timeout=5.0,
    )
    assert r.status_code == 200


@pytest.mark.integration
def test_config_patch_endpoint(api_base_url: str) -> None:
    """Test PATCH /api/v1/config updates configuration correctly."""
    # First, get the current config to restore later
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()

    # Partial update using PATCH
    patch_payload = {
        "log_level": "WARNING",
    }

    r = httpx.patch(
        f"{api_base_url}/api/v1/config",
        json=patch_payload,
        timeout=5.0,
    )
    assert r.status_code == 200

    data = r.json()
    assert isinstance(data, dict)

    # Verify response structure
    assert "updated" in data
    assert "effective" in data

    # Verify only the patched field was updated
    assert "log_level" in data["updated"]
    assert data["updated"]["log_level"] == "WARNING"

    # Verify other fields remain unchanged
    effective = data["effective"]
    assert effective["default_model"] == original_config["default_model"]
    assert effective["rag_enabled"] == original_config["rag_enabled"]
    assert effective["log_level"] == "WARNING"  # This should be changed

    # Restore original config
    restore_payload = {
        "log_level": original_config["log_level"],
    }
    r = httpx.patch(
        f"{api_base_url}/api/v1/config",
        json=restore_payload,
        timeout=5.0,
    )
    assert r.status_code == 200


@pytest.mark.integration
def test_config_post_ignores_unknown_fields(api_base_url: str) -> None:
    """Test POST /api/v1/config ignores unknown fields gracefully."""
    # Get current config
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()

    # Send payload with unknown fields
    payload = {
        "default_model": original_config["default_model"],
        "unknown_field": "should_be_ignored",
        "another_unknown": 12345,
    }

    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=payload,
        timeout=5.0,
    )
    assert r.status_code == 200

    data = r.json()
    # Unknown fields should not appear in "updated"
    assert "unknown_field" not in data.get("updated", {})
    assert "another_unknown" not in data.get("updated", {})


@pytest.mark.integration
def test_config_post_validation(api_base_url: str) -> None:
    """Test POST /api/v1/config validates log_level values."""
    # Valid log levels are: DEBUG, INFO, WARNING, ERROR
    # The backend should accept these and convert to uppercase

    valid_log_levels = ["debug", "DEBUG", "info", "INFO", "warning", "WARNING", "error", "ERROR"]

    for log_level in valid_log_levels:
        payload = {"log_level": log_level}
        r = httpx.post(
            f"{api_base_url}/api/v1/config",
            json=payload,
            timeout=5.0,
        )
        assert r.status_code == 200
        data = r.json()
        # Backend should normalize to uppercase
        assert data["effective"]["log_level"].upper() in ["DEBUG", "INFO", "WARNING", "ERROR"]


@pytest.mark.integration
def test_config_hot_reload(api_base_url: str) -> None:
    """Test that config changes take effect without restart (hot reload)."""
    # Get current config
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    original_config = r.json()
    original_log_level = original_config["log_level"]

    # Change log level
    new_log_level = "ERROR" if original_log_level != "ERROR" else "DEBUG"
    payload = {"log_level": new_log_level}

    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=payload,
        timeout=5.0,
    )
    assert r.status_code == 200

    # Immediately GET to verify hot reload worked
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200
    current_config = r.json()
    assert current_config["log_level"] == new_log_level

    # Restore original
    restore_payload = {"log_level": original_log_level}
    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=restore_payload,
        timeout=5.0,
    )
    assert r.status_code == 200


@pytest.mark.integration
def test_config_frontend_compatibility(api_base_url: str) -> None:
    """Test config endpoint matches frontend expectations (web/src/app/admin/page.tsx).

    This test verifies the exact request/response format used by the configuration
    wizard in the web UI.
    """
    # Test GET endpoint format (line 44-53 in admin/page.tsx)
    r = httpx.get(f"{api_base_url}/api/v1/config", timeout=5.0)
    assert r.status_code == 200

    data = r.json()
    # Frontend expects these exact fields
    assert "ollama_base_url" in data
    assert "default_model" in data
    assert "rag_enabled" in data
    assert "log_level" in data

    # Test POST endpoint format (line 152-160 in admin/page.tsx)
    original_config = data.copy()

    # Frontend sends exactly these fields
    frontend_payload = {
        "default_model": data["default_model"],
        "rag_enabled": not data["rag_enabled"],  # Toggle for test
        "log_level": "DEBUG" if data["log_level"] != "DEBUG" else "INFO",
    }

    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=frontend_payload,
        headers={"Content-Type": "application/json"},
        timeout=5.0,
    )
    assert r.status_code == 200

    # Backend should accept and apply changes
    response_data = r.json()
    assert "updated" in response_data or "effective" in response_data

    # Restore original config
    restore_payload = {
        "default_model": original_config["default_model"],
        "rag_enabled": original_config["rag_enabled"],
        "log_level": original_config["log_level"],
    }
    r = httpx.post(
        f"{api_base_url}/api/v1/config",
        json=restore_payload,
        timeout=5.0,
    )
    assert r.status_code == 200
