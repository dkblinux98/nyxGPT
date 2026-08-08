"""Unit tests for the /api/v1/models* endpoints.

These exercise src/nyxgpt/app.py's models_list/models_pull/models_delete/
models_info route handlers -- the thin FastAPI glue around nyxgpt.models and
the Ollama HTTP helpers -- with the Ollama HTTP layer and nyxgpt.models
mocked out, so no Ollama daemon is needed.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt.app import app

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# GET /api/v1/models
# ---------------------------------------------------------------------------


def test_models_list_returns_names():
    ollama_response = {
        "models": [
            {"name": "llama3.1:8b", "size": 123},
            {"name": "mistral:7b", "size": 456},
            # A malformed entry (not a dict) should be skipped, not crash.
            "not-a-dict",
            # A dict without a string "name" should also be skipped.
            {"size": 789},
        ]
    }
    with patch("nyxgpt.app.get_json", return_value=ollama_response) as mock_get:
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json() == {"models": ["llama3.1:8b", "mistral:7b"]}
    mock_get.assert_called_once()


def test_models_list_handles_non_dict_response():
    """When Ollama returns something unexpected (not a dict), models list is empty."""
    with patch("nyxgpt.app.get_json", return_value=["unexpected"]):
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 200
    assert response.json() == {"models": []}


def test_models_list_translates_ollama_failure_to_502():
    with patch("nyxgpt.app.get_json", side_effect=RuntimeError("connection refused")):
        client = TestClient(app)
        response = client.get("/api/v1/models")

    assert response.status_code == 502
    body = response.json()
    assert "Failed to list models from Ollama" in body["error"]["message"]
    assert "connection refused" in body["error"]["message"]
    assert body["error"]["code"] == "http_error"
    assert "request_id" in body["error"]


# ---------------------------------------------------------------------------
# POST /api/v1/models/pull (non-streaming)
# ---------------------------------------------------------------------------


def test_models_pull_missing_model_returns_400():
    client = TestClient(app)
    response = client.post("/api/v1/models/pull", json={})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Missing 'model'"


def test_models_pull_blank_model_returns_400():
    client = TestClient(app)
    response = client.post("/api/v1/models/pull", json={"model": "   "})

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Missing 'model'"


def test_models_pull_non_streaming_success():
    ollama_result = {"status": "success"}
    with (
        patch("nyxgpt.app.post_json", return_value=ollama_result) as mock_post,
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.post(
            "/api/v1/models/pull", json={"model": " llama3.1:8b ", "stream": False}
        )

    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "model": "llama3.1:8b", "result": ollama_result}
    mock_post.assert_called_once()
    mock_record.assert_called_once_with("model.pull", "llama3.1:8b")


def test_models_pull_non_streaming_default_stream_is_false():
    """Omitting 'stream' entirely takes the non-streaming code path."""
    with (
        patch("nyxgpt.app.post_json", return_value={"status": "ok"}),
        patch("nyxgpt.app.admin_activity_module.record"),
    ):
        client = TestClient(app)
        response = client.post("/api/v1/models/pull", json={"model": "llama3.1:8b"})

    assert response.status_code == 200


def test_models_pull_non_streaming_ollama_failure_returns_502():
    with patch("nyxgpt.app.post_json", side_effect=RuntimeError("ollama down")):
        client = TestClient(app)
        response = client.post("/api/v1/models/pull", json={"model": "llama3.1:8b"})

    assert response.status_code == 502
    body = response.json()
    assert "Failed to pull model via Ollama" in body["error"]["message"]
    assert "ollama down" in body["error"]["message"]


# ---------------------------------------------------------------------------
# POST /api/v1/models/pull (streaming)
# ---------------------------------------------------------------------------


def test_models_pull_streaming_success_emits_progress_and_completion():
    progress_events = [
        {"status": "pulling manifest", "completed": 0, "total": 0},
        {"status": "downloading", "completed": 50, "total": 100},
        {"status": "verifying sha256", "completed": 100, "total": 100},
    ]

    with (
        patch(
            "nyxgpt.ollama_client.post_json_lines", return_value=iter(progress_events)
        ) as mock_lines,
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
        patch("nyxgpt.app.get_ollama_base_url", return_value="http://localhost:11434"),
        TestClient(app).stream(
            "POST",
            "/api/v1/models/pull",
            json={"model": "llama3.1:8b", "stream": True},
        ) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        chunks = list(response.iter_lines())

    body = "\n".join(chunks)
    assert '"status": "downloading"' in body or '"status":"downloading"' in body
    assert '"percent": 50.0' in body or '"percent":50.0' in body
    assert '"ok": true' in body or '"ok":true' in body
    assert '"model": "llama3.1:8b"' in body or '"model":"llama3.1:8b"' in body
    mock_lines.assert_called_once()
    mock_record.assert_called_once_with("model.pull", "llama3.1:8b")


def test_models_pull_streaming_error_emits_error_event():
    with (
        patch(
            "nyxgpt.ollama_client.post_json_lines",
            side_effect=RuntimeError("pull failed: no space left"),
        ),
        patch("nyxgpt.app.get_ollama_base_url", return_value="http://localhost:11434"),
        TestClient(app).stream(
            "POST",
            "/api/v1/models/pull",
            json={"model": "llama3.1:8b", "stream": True},
        ) as response,
    ):
        assert response.status_code == 200
        chunks = list(response.iter_lines())

    body = "\n".join(chunks)
    # The raw exception detail must NOT leak to the client; the SSE error event
    # carries only a generic message (CodeQL py/stack-trace-exposure, #26).
    assert "pull failed: no space left" not in body
    assert "Model pull failed" in body
    assert '"error"' in body


# ---------------------------------------------------------------------------
# DELETE /api/v1/models/{model_name}
# ---------------------------------------------------------------------------


def test_models_delete_success():
    with (
        patch("nyxgpt.app.models.delete_model") as mock_delete,
        patch("nyxgpt.app.admin_activity_module.record") as mock_record,
    ):
        client = TestClient(app)
        response = client.delete("/api/v1/models/llama3.1:8b")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "model": "llama3.1:8b"}
    mock_delete.assert_called_once()
    mock_record.assert_called_once_with("model.delete", "llama3.1:8b")


def test_models_delete_invalid_name_returns_400():
    with patch(
        "nyxgpt.app.models.delete_model", side_effect=ValueError("Model name cannot be empty")
    ):
        client = TestClient(app)
        response = client.delete("/api/v1/models/llama3.1:8b")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Model name cannot be empty"


def test_models_delete_ollama_failure_returns_502():
    with patch("nyxgpt.app.models.delete_model", side_effect=RuntimeError("ollama unreachable")):
        client = TestClient(app)
        response = client.delete("/api/v1/models/llama3.1:8b")

    assert response.status_code == 502
    body = response.json()
    assert "Failed to delete model via Ollama" in body["error"]["message"]
    assert "ollama unreachable" in body["error"]["message"]


# ---------------------------------------------------------------------------
# GET /api/v1/models/{model_name}/info
# ---------------------------------------------------------------------------


def test_models_info_success():
    info = {
        "modelfile": "FROM llama3.1:8b",
        "parameters": "temperature 0.7",
        "template": "{{ .Prompt }}",
        "size": 4_700_000_000,
        "modified_at": "2026-07-01T00:00:00Z",
    }
    with patch("nyxgpt.app.models.show_model", return_value=info) as mock_show:
        client = TestClient(app)
        response = client.get("/api/v1/models/llama3.1:8b/info")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "model": "llama3.1:8b", "info": info}
    mock_show.assert_called_once()


def test_models_info_invalid_name_returns_400():
    with patch(
        "nyxgpt.app.models.show_model", side_effect=ValueError("Model name cannot be empty")
    ):
        client = TestClient(app)
        response = client.get("/api/v1/models/llama3.1:8b/info")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Model name cannot be empty"


def test_models_info_ollama_failure_returns_502():
    with patch("nyxgpt.app.models.show_model", side_effect=RuntimeError("model not found")):
        client = TestClient(app)
        response = client.get("/api/v1/models/llama3.1:8b/info")

    assert response.status_code == 502
    body = response.json()
    assert "Failed to get model info via Ollama" in body["error"]["message"]
    assert "model not found" in body["error"]["message"]
