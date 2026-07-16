"""Unit tests for the /api/v1/models* endpoints in src/nyxgpt/app.py.

These exercise models_list, models_pull (streaming and non-streaming),
models_delete, and models_info with the Ollama HTTP layer mocked out, so no
real Ollama server is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


# ============================================================================
# GET /api/v1/models
# ============================================================================


def test_models_list_success():
    payload = {
        "models": [
            {"name": "llama3.1:8b", "size": 123},
            {"name": "mistral:7b", "size": 456},
        ]
    }
    with patch("nyxgpt.app._ollama_get_json", return_value=payload) as mock_get:
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json() == {"models": ["llama3.1:8b", "mistral:7b"]}
    mock_get.assert_called_once()


def test_models_list_empty():
    with patch("nyxgpt.app._ollama_get_json", return_value={"models": []}):
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json() == {"models": []}


def test_models_list_ignores_malformed_entries():
    """Entries that are not dicts, or lack a string 'name', are skipped."""
    payload = {
        "models": [
            {"name": "good-model"},
            {"no_name_field": True},
            "just-a-string",
            123,
            {"name": 42},
        ]
    }
    with patch("nyxgpt.app._ollama_get_json", return_value=payload):
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json() == {"models": ["good-model"]}


def test_models_list_non_dict_response():
    """If Ollama returns something that isn't a dict, treat as no models."""
    with patch("nyxgpt.app._ollama_get_json", return_value=["unexpected"]):
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json() == {"models": []}


def test_models_list_ollama_unreachable():
    with patch("nyxgpt.app._ollama_get_json", side_effect=ConnectionError("refused")):
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 502
    assert "Failed to list models from Ollama" in response.json()["error"]["message"]


# ============================================================================
# POST /api/v1/models/pull (non-streaming)
# ============================================================================


def test_models_pull_missing_model_field():
    client = TestClient(app)
    response = client.post("/api/v1/models/pull", json={})
    assert response.status_code == 400
    assert "Missing 'model'" in response.json()["error"]["message"]


def test_models_pull_blank_model_field():
    client = TestClient(app)
    response = client.post("/api/v1/models/pull", json={"model": "   "})
    assert response.status_code == 400


def test_models_pull_non_streaming_success():
    with (
        patch("nyxgpt.app._ollama_post_json", return_value={"status": "success"}) as mock_post,
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.post(
            "/api/v1/models/pull", json={"model": "  llama3.1:8b  ", "stream": False}
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "model": "llama3.1:8b", "result": {"status": "success"}}
    mock_post.assert_called_once()
    mock_record.assert_called_once_with("model.pull", "llama3.1:8b")


def test_models_pull_non_streaming_defaults_to_non_stream():
    """Omitting 'stream' entirely should take the non-streaming path."""
    with patch("nyxgpt.app._ollama_post_json", return_value={"status": "success"}):
        client = TestClient(app)
        response = client.post("/api/v1/models/pull", json={"model": "llama3.1:8b"})

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_models_pull_non_streaming_failure():
    with patch("nyxgpt.app._ollama_post_json", side_effect=RuntimeError("connection reset")):
        client = TestClient(app)
        response = client.post(
            "/api/v1/models/pull", json={"model": "llama3.1:8b", "stream": False}
        )

    assert response.status_code == 502
    assert "Failed to pull model via Ollama" in response.json()["error"]["message"]


# ============================================================================
# POST /api/v1/models/pull (streaming / SSE)
# ============================================================================


def test_models_pull_streaming_success():
    events = [
        {"status": "downloading", "completed": 50, "total": 100},
        {"status": "downloading", "completed": 100, "total": 100},
        {"status": "success"},
    ]
    with (
        patch("nyxgpt.app.get_ollama_base_url", return_value="http://localhost:11434"),
        patch("nyxgpt.ollama_client.post_json_lines", return_value=iter(events)),
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.post("/api/v1/models/pull", json={"model": "llama3.1:8b", "stream": True})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    text = response.text
    assert '"percent": 50.0' in text
    assert '"percent": 100.0' in text
    assert '"status": "success", "ok": true' in text
    mock_record.assert_called_once_with("model.pull", "llama3.1:8b")


def test_models_pull_streaming_zero_total_percent():
    """When total is 0/absent, percent should default to 0.0 without dividing by zero."""
    events = [{"status": "starting", "completed": 0, "total": 0}]
    with (
        patch("nyxgpt.app.get_ollama_base_url", return_value="http://localhost:11434"),
        patch("nyxgpt.ollama_client.post_json_lines", return_value=iter(events)),
        patch("nyxgpt.app.admin_activity_module.record"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/models/pull", json={"model": "llama3.1:8b", "stream": True})

    assert response.status_code == 200
    assert '"percent": 0.0' in response.text


def test_models_pull_streaming_error_event():
    """If the generator raises mid-stream, an error event is yielded (no 500)."""

    def _raise(*_args, **_kwargs):
        raise ConnectionError("ollama down")
        yield  # pragma: no cover - makes this a generator

    with (
        patch("nyxgpt.app.get_ollama_base_url", return_value="http://localhost:11434"),
        patch("nyxgpt.ollama_client.post_json_lines", side_effect=_raise),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/models/pull", json={"model": "llama3.1:8b", "stream": True})

    assert response.status_code == 200
    assert '"error": "ollama down"' in response.text


# ============================================================================
# DELETE /api/v1/models/{model_name}
# ============================================================================


def test_models_delete_success():
    with (
        patch("nyxgpt.app.models.delete_model", return_value=None) as mock_delete,
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.delete("/api/v1/models/llama3.1:8b")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "model": "llama3.1:8b"}
    mock_delete.assert_called_once()
    mock_record.assert_called_once_with("model.delete", "llama3.1:8b")


def test_models_delete_invalid_name():
    with patch("nyxgpt.app.models.delete_model", side_effect=ValueError("bad name")):
        client = TestClient(app)
        response = client.delete("/api/v1/models/bad-model")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "bad name"


def test_models_delete_upstream_failure():
    with patch("nyxgpt.app.models.delete_model", side_effect=RuntimeError("not found upstream")):
        client = TestClient(app)
        response = client.delete("/api/v1/models/missing-model")

    assert response.status_code == 502
    assert "Failed to delete model via Ollama" in response.json()["error"]["message"]


# ============================================================================
# GET /api/v1/models/{model_name}/info
# ============================================================================


def test_models_info_success():
    info = {"modelfile": "FROM llama3.1:8b", "parameters": "temperature 0.7"}
    with patch("nyxgpt.app.models.show_model", return_value=info) as mock_show:
        client = TestClient(app)
        response = client.get("/api/v1/models/llama3.1:8b/info")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "model": "llama3.1:8b", "info": info}
    mock_show.assert_called_once()


def test_models_info_invalid_name():
    with patch("nyxgpt.app.models.show_model", side_effect=ValueError("bad name")):
        client = TestClient(app)
        response = client.get("/api/v1/models//info")

    # An empty model_name segment doesn't match the route; FastAPI 404s before
    # ever calling the handler. Use a non-empty-but-invalid name instead.
    assert response.status_code in (404, 400)


def test_models_info_invalid_name_via_handler():
    with patch("nyxgpt.app.models.show_model", side_effect=ValueError("bad name")):
        client = TestClient(app)
        response = client.get("/api/v1/models/%20/info")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "bad name"


def test_models_info_upstream_failure():
    with patch("nyxgpt.app.models.show_model", side_effect=RuntimeError("model not found")):
        client = TestClient(app)
        response = client.get("/api/v1/models/missing-model/info")

    assert response.status_code == 502
    assert "Failed to get model info via Ollama" in response.json()["error"]["message"]
