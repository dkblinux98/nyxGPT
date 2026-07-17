"""Unit tests for Prometheus metrics (nyxgpt.metrics) and the /metrics endpoint."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from nyxgpt import metrics as prom_metrics
from nyxgpt.app import app
from nyxgpt.chat import ChatResult


def _sample_names(text: str) -> set[str]:
    """Flatten every sample name (e.g. the `_total`/`_bucket`/`_sum` suffixed
    series) across all parsed metric families.

    prometheus_client groups samples under a family name with the `_total`
    (or similar) suffix stripped, so tests must inspect sample names rather
    than family names to find e.g. "nyxgpt_http_requests_total".
    """
    return {
        sample.name for family in text_string_to_metric_families(text) for sample in family.samples
    }


def _samples(text: str, name: str) -> list:
    return [
        sample
        for family in text_string_to_metric_families(text)
        for sample in family.samples
        if sample.name == name
    ]


@pytest.mark.unit
def test_render_metrics_returns_prometheus_text_format() -> None:
    prom_metrics.HTTP_REQUESTS_TOTAL.labels(method="GET", path="/unit-test", status="200").inc()

    body, content_type = prom_metrics.render_metrics()

    assert content_type.startswith("text/plain")
    assert "nyxgpt_http_requests_total" in _sample_names(body.decode("utf-8"))


@pytest.mark.unit
def test_chat_and_rag_counters_are_registered() -> None:
    prom_metrics.CHAT_REQUESTS_TOTAL.labels(model="test-model", streaming="false").inc()
    prom_metrics.RAG_QUERIES_TOTAL.labels(source="unit-test").inc()

    body, _ = prom_metrics.render_metrics()
    text = body.decode("utf-8")

    assert "nyxgpt_chat_requests_total" in text
    assert "nyxgpt_rag_queries_total" in text


@pytest.mark.unit
def test_selfheal_metrics_are_registered() -> None:
    prom_metrics.SELFHEAL_UNHEALTHY_COMPONENTS.set(2)
    prom_metrics.SELFHEAL_RESTARTS_TOTAL.labels(service="unit-test-svc", result="ok").inc()
    prom_metrics.SELFHEAL_RESTART_COUNT.labels(service="unit-test-svc").set(3)
    prom_metrics.SELFHEAL_LAST_RECOVERY_TIMESTAMP.labels(service="unit-test-svc").set(1700000000)

    body, _ = prom_metrics.render_metrics()
    text = body.decode("utf-8")
    names = _sample_names(text)

    assert "nyxgpt_selfheal_unhealthy_components" in names
    assert "nyxgpt_selfheal_restarts_total" in names
    assert "nyxgpt_selfheal_restart_count" in names
    assert "nyxgpt_selfheal_last_recovery_timestamp" in names

    restart_samples = _samples(text, "nyxgpt_selfheal_restarts_total")
    assert any(
        s.labels.get("service") == "unit-test-svc" and s.labels.get("result") == "ok"
        for s in restart_samples
    )


@pytest.mark.unit
def test_metrics_endpoint_exposes_http_metrics() -> None:
    client = TestClient(app)

    client.get("/health")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")

    names = _sample_names(response.text)
    assert "nyxgpt_http_requests_total" in names
    assert "nyxgpt_http_request_duration_seconds_bucket" in names

    health_samples = [
        sample
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
        if sample.name == "nyxgpt_http_requests_total"
        and sample.labels.get("path") == "/health"
        and sample.labels.get("method") == "GET"
    ]
    assert health_samples, "expected a sample for GET /health"
    assert health_samples[0].value >= 1


@pytest.mark.unit
def test_metrics_endpoint_is_unauthenticated() -> None:
    """The /metrics endpoint must stay reachable without an API key, like /health.

    api_key_auth() only protects paths starting with /api/v1, so /metrics
    (a root-level route like /health) bypasses it regardless of auth config.
    """
    client = TestClient(app)

    response = client.get("/metrics")

    assert response.status_code == 200


@pytest.mark.unit
def test_chat_endpoint_increments_business_metrics() -> None:
    """A real POST to /api/v1/chat must bump nyxgpt_chat_requests_total and
    nyxgpt_rag_queries_total{source="chat"} — not just prove the metric
    objects exist, but that the endpoint actually updates them.
    """
    client = TestClient(app)
    fake_result = ChatResult(
        session="metrics-unit-test",
        model="metrics-test-model",
        reply="hello",
        rag_used=True,
        rag_chunks=0,
        rag_context=None,
    )

    with patch("nyxgpt.app.run_chat", return_value=fake_result):
        response = client.post(
            "/api/v1/chat",
            json={"prompt": "hi", "session": "metrics-unit-test"},
        )

    assert response.status_code == 200

    metrics_text = client.get("/metrics").text
    chat_samples = _samples(metrics_text, "nyxgpt_chat_requests_total")
    assert any(
        s.labels.get("model") == "metrics-test-model" and s.labels.get("streaming") == "false"
        for s in chat_samples
    ), "expected a chat counter sample for the real /api/v1/chat request"

    rag_samples = _samples(metrics_text, "nyxgpt_rag_queries_total")
    assert any(
        s.labels.get("source") == "chat" for s in rag_samples
    ), "expected a rag counter sample labeled source=chat since rag_used=True"


@pytest.mark.unit
def test_chat_stream_endpoint_increments_business_metrics() -> None:
    """A real POST to /api/v1/chat/stream must bump nyxgpt_chat_requests_total
    with streaming="true", mirroring the mocking pattern used by
    tests/unit/test_streaming_request_id.py.
    """
    client = TestClient(app)

    def mock_chat_stream(*args, **kwargs):
        yield "Hello"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "hi",
                "session": "metrics-unit-test",
                "model": "metrics-stream-model",
            },
        ) as response,
    ):
        assert response.status_code == 200
        list(response.iter_text())

    metrics_text = client.get("/metrics").text
    chat_samples = _samples(metrics_text, "nyxgpt_chat_requests_total")
    assert any(
        s.labels.get("model") == "metrics-stream-model" and s.labels.get("streaming") == "true"
        for s in chat_samples
    ), "expected a chat counter sample for the real /api/v1/chat/stream request"


@pytest.mark.unit
def test_rag_query_endpoint_increments_business_metrics() -> None:
    """A real POST to /api/v1/rag/query must bump
    nyxgpt_rag_queries_total{source="rag_query"}.
    """
    client = TestClient(app)
    fake_rows = [
        {"doc_id": "doc-1", "chunk_id": 0, "text": "chunk text", "score": 0.9},
    ]

    with patch("nyxgpt.app.retrieve_context", return_value=fake_rows):
        response = client.post("/api/v1/rag/query", json={"query": "test query"})

    assert response.status_code == 200

    metrics_text = client.get("/metrics").text
    rag_samples = _samples(metrics_text, "nyxgpt_rag_queries_total")
    assert any(
        s.labels.get("source") == "rag_query" for s in rag_samples
    ), "expected a rag counter sample for the real /api/v1/rag/query request"
