"""Integration tests for model management API endpoints.

Tests the following endpoints:
- GET /api/v1/models - List all available models
- POST /api/v1/models/pull - Pull (download) a model
- DELETE /api/v1/models/{model_name} - Delete a model
- GET /api/v1/models/{model_name}/info - Get model information

These tests require Ollama to be running and accessible.
"""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.integration
def test_models_list_endpoint(api_base_url: str, require_ollama: None) -> None:
    """Test GET /api/v1/models returns list of available models."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.get("/api/v1/models")

    assert resp.status_code == 200
    data = resp.json()

    # Verify response structure
    assert "models" in data
    assert isinstance(data["models"], list)

    # Each model should be a string
    for model in data["models"]:
        assert isinstance(model, str)
        assert len(model) > 0


@pytest.mark.integration
def test_models_list_non_empty(api_base_url: str, require_ollama: None) -> None:
    """Test that models list is non-empty when Ollama is running."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.get("/api/v1/models")

    assert resp.status_code == 200
    data = resp.json()

    # With Ollama running and configured, we should have at least one model
    # If this fails, ensure at least one model is pulled in the test environment
    assert len(data["models"]) > 0


@pytest.mark.integration
def test_models_delete_nonexistent_model(
    api_base_url: str, require_ollama: None
) -> None:
    """Test DELETE /api/v1/models/{model_name} for nonexistent model."""
    nonexistent_model = "nonexistent-model:999"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.delete(f"/api/v1/models/{nonexistent_model}")

    # Should return error (400 or 502 depending on Ollama response)
    assert resp.status_code in [400, 502]
    data = resp.json()
    assert "error" in data


@pytest.mark.integration
def test_models_delete_empty_model_name(api_base_url: str) -> None:
    """Test DELETE /api/v1/models with empty model name."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.delete("/api/v1/models/ ")  # Whitespace-only name

    # Should return 400 or 404 for invalid model name
    assert resp.status_code in [400, 404]


@pytest.mark.integration
def test_models_info_nonexistent_model(api_base_url: str, require_ollama: None) -> None:
    """Test GET /api/v1/models/{model_name}/info for nonexistent model."""
    nonexistent_model = "nonexistent-model:999"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.get(f"/api/v1/models/{nonexistent_model}/info")

    # Should return error
    assert resp.status_code in [400, 502]
    data = resp.json()
    assert "error" in data


@pytest.mark.integration
def test_models_info_existing_model(api_base_url: str, require_ollama: None) -> None:
    """Test GET /api/v1/models/{model_name}/info for existing model."""
    # First, get the list of available models
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        list_resp = client.get("/api/v1/models")
        assert list_resp.status_code == 200
        models = list_resp.json()["models"]

        if not models:
            pytest.skip("No models available for testing")

        # Get info for the first available model
        model_name = models[0]
        info_resp = client.get(f"/api/v1/models/{model_name}/info")

    assert info_resp.status_code == 200
    data = info_resp.json()

    # Verify response structure
    assert "ok" in data
    assert data["ok"] is True
    assert "model" in data
    assert data["model"] == model_name
    assert "info" in data
    assert isinstance(data["info"], dict)


@pytest.mark.integration
def test_models_pull_missing_model_field(api_base_url: str) -> None:
    """Test POST /api/v1/models/pull with missing 'model' field."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.post("/api/v1/models/pull", json={})

    # Should return 400 for missing required field
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data


@pytest.mark.integration
def test_models_pull_empty_model_name(api_base_url: str) -> None:
    """Test POST /api/v1/models/pull with empty model name."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.post("/api/v1/models/pull", json={"model": ""})

    # Should return 400 for empty model name
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data


@pytest.mark.integration
def test_models_pull_whitespace_model_name(api_base_url: str) -> None:
    """Test POST /api/v1/models/pull with whitespace-only model name."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.post("/api/v1/models/pull", json={"model": "   "})

    # Should return 400 for whitespace-only model name
    assert resp.status_code == 400
    data = resp.json()
    assert "error" in data


@pytest.mark.integration
def test_models_info_special_characters(
    api_base_url: str, require_ollama: None
) -> None:
    """Test GET /api/v1/models/{model_name}/info with special characters."""
    # Test URL encoding and special character handling
    model_with_special_chars = "model:with/special@chars"

    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.get(f"/api/v1/models/{model_with_special_chars}/info")

    # Should handle gracefully (404 or 400)
    assert resp.status_code in [400, 404, 502]


@pytest.mark.integration
def test_models_list_response_format(api_base_url: str, require_ollama: None) -> None:
    """Test that models list response format is consistent."""
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        resp = client.get("/api/v1/models")

    assert resp.status_code == 200
    data = resp.json()

    # Verify exact response format
    assert set(data.keys()) == {"models"}
    assert isinstance(data["models"], list)

    # All entries should be non-empty strings
    for model in data["models"]:
        assert isinstance(model, str)
        assert model.strip() == model  # No leading/trailing whitespace
        assert len(model) > 0


@pytest.mark.integration
def test_models_info_response_format(api_base_url: str, require_ollama: None) -> None:
    """Test that model info response format is consistent."""
    # First, get an available model
    with httpx.Client(base_url=api_base_url, timeout=10.0) as client:
        list_resp = client.get("/api/v1/models")
        assert list_resp.status_code == 200
        models = list_resp.json()["models"]

        if not models:
            pytest.skip("No models available for testing")

        model_name = models[0]
        info_resp = client.get(f"/api/v1/models/{model_name}/info")

    assert info_resp.status_code == 200
    data = info_resp.json()

    # Verify response structure
    assert "ok" in data
    assert isinstance(data["ok"], bool)
    assert "model" in data
    assert isinstance(data["model"], str)
    assert "info" in data
    assert isinstance(data["info"], dict)

    # Model name should match request
    assert data["model"] == model_name
