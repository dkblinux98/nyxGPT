"""Coverage for the non-streaming `/api/v1/chat` endpoint in src/nyxgpt/app.py.

Targets branches not already exercised by test_chat_usage_analytics.py:
- The optional rag_enabled/rag_filters/attachments/output_format kwargs
  forwarded to run_chat() when it supports them
- The batch-processor code path (submit success, timeout, batch error)
- The ValueError/ModelRuntimeError/generic-exception handlers
- The RAG chunk -> RagChunkInfo conversion when rag_context is present
- The usage-analytics-recording failure being swallowed (not surfaced to
  the client)
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nyxgpt import app as app_module
from nyxgpt.app import app
from nyxgpt.chat import ChatResult
from nyxgpt.ollama_client import ModelRuntimeError

pytestmark = pytest.mark.unit


def test_chat_endpoint_forwards_rag_filters_attachments_output_format() -> None:
    """When run_chat's real signature supports these kwargs, the endpoint must pass them."""
    client = TestClient(app)

    captured_kwargs: dict = {}

    def fake_run_chat(prompt, **kwargs):
        captured_kwargs.update(kwargs)
        return ChatResult(
            session=kwargs.get("session", "default"),
            model=kwargs.get("model") or "m",
            reply="ok",
            rag_used=False,
            rag_chunks=0,
            rag_context=None,
        )

    img_b64 = base64.b64encode(b"fake-image-bytes").decode()

    with patch("nyxgpt.app.run_chat", autospec=True, side_effect=fake_run_chat):
        response = client.post(
            "/api/v1/chat",
            json={
                "prompt": "hi",
                "session": "kwargs-test",
                "rag_filters": {"doc_ids": ["doc-1"]},
                "attachments": [
                    {
                        "type": "image",
                        "media_type": "image/png",
                        "data": img_b64,
                        "filename": "a.png",
                    }
                ],
                "output_format": {"type": "object"},
            },
        )

    assert response.status_code == 200
    assert captured_kwargs.get("rag_filters") == {
        "doc_ids": ["doc-1"],
        "filename": None,
        "tags": None,
        "date_from": None,
        "date_to": None,
        "collection": None,
    }
    assert captured_kwargs.get("attachments") == [
        {"type": "image", "media_type": "image/png", "data": img_b64, "filename": "a.png"}
    ]
    assert captured_kwargs.get("output_format") == {"type": "object"}


def test_chat_endpoint_rag_context_converted_to_rag_chunks() -> None:
    client = TestClient(app)
    fake_result = ChatResult(
        session="rag-test",
        model="m",
        reply="answer",
        rag_used=True,
        rag_chunks=1,
        rag_context=[
            {
                "text": "some source text",
                "score": 0.5,
                "doc_id": "doc-1",
                "chunk_id": 2,
                "similarity_score": 0.9,
            }
        ],
    )

    with patch("nyxgpt.app.run_chat", return_value=fake_result):
        response = client.post("/api/v1/chat", json={"prompt": "hi", "session": "rag-test"})

    assert response.status_code == 200
    body = response.json()
    assert body["rag_used"] is True
    assert len(body["rag_chunks"]) == 1
    chunk = body["rag_chunks"][0]
    assert chunk["text"] == "some source text"
    assert chunk["doc_id"] == "doc-1"
    assert chunk["chunk_id"] == 2
    assert chunk["similarity_score"] == 0.9


def test_chat_endpoint_usage_analytics_failure_does_not_break_response(caplog) -> None:
    client = TestClient(app)
    fake_result = ChatResult(
        session="analytics-fail-test",
        model="m",
        reply="ok",
        rag_used=False,
        rag_chunks=0,
        rag_context=None,
    )

    with (
        patch("nyxgpt.app.run_chat", return_value=fake_result),
        patch("nyxgpt.app.usage_analytics_module.record", side_effect=RuntimeError("db down")),
    ):
        response = client.post(
            "/api/v1/chat", json={"prompt": "hi", "session": "analytics-fail-test"}
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "ok"


def test_chat_endpoint_value_error_returns_422() -> None:
    client = TestClient(app)
    with patch("nyxgpt.app.run_chat", side_effect=ValueError("bad session name")):
        response = client.post("/api/v1/chat", json={"prompt": "hi", "session": "bad"})

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "bad session name"


def test_chat_endpoint_model_runtime_error_returns_502() -> None:
    client = TestClient(app)
    with patch("nyxgpt.app.run_chat", side_effect=ModelRuntimeError("Ollama HTTP 500: crashed")):
        response = client.post("/api/v1/chat", json={"prompt": "hi", "session": "crash-test"})

    assert response.status_code == 502
    assert "crashed" in response.json()["error"]["message"]


def test_chat_endpoint_generic_exception_returns_500() -> None:
    client = TestClient(app)
    with patch("nyxgpt.app.run_chat", side_effect=RuntimeError("boom")):
        response = client.post("/api/v1/chat", json={"prompt": "hi", "session": "boom-test"})

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Internal server error"


def test_chat_endpoint_batch_processor_success() -> None:
    client = TestClient(app)

    class _FakeBatchProcessor:
        def submit(self, data, priority=None, timeout=None):
            return {
                "session": data["session"] or "default",
                "model": data["model"],
                "reply": "batched reply",
                "rag_used": False,
                "rag_context": None,
            }

    with patch.object(app_module, "_batch_processor", _FakeBatchProcessor()):
        response = client.post(
            "/api/v1/chat", json={"prompt": "hi", "session": "batch-test", "model": "m"}
        )

    assert response.status_code == 200
    assert response.json()["reply"] == "batched reply"


def test_chat_endpoint_batch_processor_timeout_returns_504() -> None:
    client = TestClient(app)

    class _TimeoutBatchProcessor:
        def submit(self, data, priority=None, timeout=None):
            raise TimeoutError("no capacity")

    with patch.object(app_module, "_batch_processor", _TimeoutBatchProcessor()):
        response = client.post(
            "/api/v1/chat", json={"prompt": "hi", "session": "batch-timeout-test"}
        )

    assert response.status_code == 504
    assert "timed out" in response.json()["error"]["message"]


def test_chat_endpoint_batch_processor_model_runtime_error_returns_502() -> None:
    client = TestClient(app)

    class _ErrorBatchProcessor:
        def submit(self, data, priority=None, timeout=None):
            return {"error": "model crashed", "error_type": "ModelRuntimeError"}

    with patch.object(app_module, "_batch_processor", _ErrorBatchProcessor()):
        response = client.post("/api/v1/chat", json={"prompt": "hi", "session": "batch-error-test"})

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "model crashed"


def test_chat_endpoint_batch_processor_generic_error_returns_500() -> None:
    client = TestClient(app)

    class _ErrorBatchProcessor:
        def submit(self, data, priority=None, timeout=None):
            return {"error": "unexpected failure", "error_type": "Exception"}

    with patch.object(app_module, "_batch_processor", _ErrorBatchProcessor()):
        response = client.post(
            "/api/v1/chat", json={"prompt": "hi", "session": "batch-generic-error-test"}
        )

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "unexpected failure"
