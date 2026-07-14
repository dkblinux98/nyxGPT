"""Unit tests for Prometheus metrics (nyxgpt.metrics) and the /metrics endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from nyxgpt import metrics as prom_metrics
from nyxgpt.app import app


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
