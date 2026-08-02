"""Unit tests for Prometheus metrics (nyxgpt.metrics) and the /metrics endpoint."""

from __future__ import annotations

import builtins
import importlib
import sys
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
def test_chat_stream_endpoint_increments_rag_query_metric_when_rag_used() -> None:
    """A streamed reply carrying `__RAG_START__`/`__RAG_END__` markers must
    bump `nyxgpt_rag_queries_total{source="chat"}` (#3469 acceptance
    failure): the web UI's chat pane only ever calls `/chat/stream`, so
    before this fix every RAG query issued through the actual chat
    interface was invisible to the SPOG RAG panels -- only the
    non-streaming `/api/v1/chat` endpoint incremented this counter.

    Asserts on the counter's delta (not mere presence of a
    ``source="chat"`` sample): that counter is process-global and other
    tests in this file (e.g. ``test_chat_endpoint_increments_business_metrics``)
    already increment it via the non-streaming endpoint, so a presence-only
    assertion would pass even if the streaming-path fix regressed.
    """
    client = TestClient(app)
    before = prom_metrics.RAG_QUERIES_TOTAL.labels(source="chat")._value.get()

    def mock_chat_stream(*args, **kwargs):
        yield "before "
        yield '__RAG_START__{"chunks": []}__RAG_END__'
        yield "after"

    with (
        patch("nyxgpt.app.chat_stream", side_effect=mock_chat_stream),
        client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={
                "prompt": "hi",
                "session": "metrics-unit-test-rag-stream",
                "model": "metrics-stream-rag-model",
            },
        ) as response,
    ):
        assert response.status_code == 200
        list(response.iter_text())

    after = prom_metrics.RAG_QUERIES_TOTAL.labels(source="chat")._value.get()
    assert after == before + 1, (
        "expected nyxgpt_rag_queries_total{source=chat} to increase by exactly 1 "
        "for a streamed reply using RAG"
    )


@pytest.mark.unit
def test_resource_ingest_cache_and_rate_limit_metrics_are_registered() -> None:
    prom_metrics.RAG_INGESTS_TOTAL.labels(source="unit-test", result="success").inc()
    prom_metrics.CACHE_REQUESTS_TOTAL.labels(cache="unit-test", result="hit").inc()
    prom_metrics.RATE_LIMIT_REJECTIONS_TOTAL.labels(path="/unit-test").inc()
    prom_metrics.update_resource_gauges(
        rss_mb=123.4, cpu_percent=5.6, queue_depth=2, disk_percent=45.0, memory_percent=12.5
    )

    body, _ = prom_metrics.render_metrics()
    names = _sample_names(body.decode("utf-8"))

    assert "nyxgpt_rag_ingests_total" in names
    assert "nyxgpt_cache_requests_total" in names
    assert "nyxgpt_rate_limit_rejections_total" in names
    assert "nyxgpt_resource_memory_rss_mb" in names
    assert "nyxgpt_resource_memory_percent" in names
    assert "nyxgpt_resource_cpu_percent" in names
    assert "nyxgpt_resource_queue_depth" in names
    assert "nyxgpt_resource_disk_percent" in names

    disk_samples = _samples(body.decode("utf-8"), "nyxgpt_resource_disk_percent")
    assert disk_samples[0].value == 45.0
    memory_percent_samples = _samples(body.decode("utf-8"), "nyxgpt_resource_memory_percent")
    assert memory_percent_samples[0].value == 12.5


@pytest.mark.unit
def test_update_resource_gauges_defaults_disk_percent_to_zero() -> None:
    """Callers that don't pass `disk_percent` (e.g. older call sites) must
    not crash -- it's an additive kwarg with a safe default."""
    prom_metrics.update_resource_gauges(rss_mb=1.0, cpu_percent=1.0, queue_depth=0)

    body, _ = prom_metrics.render_metrics()
    assert "nyxgpt_resource_disk_percent" in _sample_names(body.decode("utf-8"))


@pytest.mark.unit
def test_selfheal_giveup_metric_is_registered() -> None:
    prom_metrics.SELFHEAL_GIVEUP_TOTAL.labels(service="unit-test-svc").inc()

    body, _ = prom_metrics.render_metrics()
    text = body.decode("utf-8")

    assert "nyxgpt_selfheal_giveup_total" in _sample_names(text)
    samples = _samples(text, "nyxgpt_selfheal_giveup_total")
    assert any(s.labels.get("service") == "unit-test-svc" for s in samples)


@pytest.mark.unit
def test_canary_auto_rollback_metric_is_registered() -> None:
    prom_metrics.CANARY_AUTO_ROLLBACK_TOTAL.labels(component="api").inc()

    body, _ = prom_metrics.render_metrics()
    text = body.decode("utf-8")

    assert "nyxgpt_canary_auto_rollback_total" in _sample_names(text)
    samples = _samples(text, "nyxgpt_canary_auto_rollback_total")
    assert any(s.labels.get("component") == "api" for s in samples)


@pytest.mark.unit
def test_initialize_known_rag_metric_series_populates_zero_samples() -> None:
    """Every known RAG query/ingest label combination must have a sample
    (value 0 if never incremented) after startup init, so the SPOG RAG
    panels render an honest zero instead of "No data" before the first
    real event -- see nyxgpt.metrics.initialize_known_rag_metric_series."""
    prom_metrics.initialize_known_rag_metric_series()

    body, _ = prom_metrics.render_metrics()
    text = body.decode("utf-8")

    query_samples = _samples(text, "nyxgpt_rag_queries_total")
    for source in ("chat", "rag_query"):
        assert any(
            s.labels.get("source") == source for s in query_samples
        ), f"expected a pre-initialized nyxgpt_rag_queries_total series for source={source!r}"

    ingest_samples = _samples(text, "nyxgpt_rag_ingests_total")
    for source in ("document", "upload", "repo"):
        for result in ("success", "failure"):
            assert any(
                s.labels.get("source") == source and s.labels.get("result") == result
                for s in ingest_samples
            ), f"expected a pre-initialized nyxgpt_rag_ingests_total series for {source=} {result=}"
    assert not any(s.labels.get("source") == "chat_attachment" for s in ingest_samples), (
        "chat_attachment must not be a nyxgpt_rag_ingests_total source -- paperclip "
        "attachments are a chat feature, not RAG ingestion (#3469); see "
        "nyxgpt_chat_attachments_total instead"
    )

    attachment_samples = _samples(text, "nyxgpt_chat_attachments_total")
    for result in ("success", "failure"):
        assert any(
            s.labels.get("result") == result for s in attachment_samples
        ), f"expected a pre-initialized nyxgpt_chat_attachments_total series for {result=}"


@pytest.mark.unit
def test_metrics_endpoint_reflects_live_resource_usage() -> None:
    """A real GET /metrics must refresh the resource gauges from the actual
    ResourceMonitor snapshot (initialized unconditionally at app startup),
    not just expose whatever value a previous test happened to `.set()`."""
    client = TestClient(app)

    response = client.get("/metrics")

    assert response.status_code == 200
    samples = _samples(response.text, "nyxgpt_resource_memory_rss_mb")
    assert samples, "expected a nyxgpt_resource_memory_rss_mb sample"
    assert samples[0].value > 0


@pytest.mark.unit
def test_rag_ingest_endpoint_increments_business_metrics() -> None:
    """A real POST to /api/v1/rag/ingest must bump
    nyxgpt_rag_ingests_total{source="document", result="success"}.
    """
    client = TestClient(app)
    fake_result = {
        "chunks_ingested": 1,
        "status": "created",
        "doc_hash": "abc123",
        "previous_hash": None,
    }

    with patch("nyxgpt.app.ingest_document", return_value=fake_result):
        response = client.post(
            "/api/v1/rag/ingest",
            json={"doc_id": "metrics-unit-test-doc", "text": "hello world"},
        )

    assert response.status_code == 200

    metrics_text = client.get("/metrics").text
    ingest_samples = _samples(metrics_text, "nyxgpt_rag_ingests_total")
    assert any(
        s.labels.get("source") == "document" and s.labels.get("result") == "success"
        for s in ingest_samples
    ), "expected an ingest counter sample for the real /api/v1/rag/ingest request"


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


@pytest.mark.unit
def test_metrics_falls_back_to_noop_when_prometheus_client_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#3487: a stale venv missing prometheus-client must not crash importing
    nyxgpt.metrics (and transitively every module that imports it) -- every
    call site must keep working as a no-op instead."""
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):  # type: ignore[no-untyped-def]
        if name == "prometheus_client":
            raise ModuleNotFoundError("No module named 'prometheus_client'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.delitem(sys.modules, "prometheus_client", raising=False)

    try:
        importlib.reload(prom_metrics)

        # Must not raise even though the real backend is gone.
        prom_metrics.HTTP_REQUESTS_TOTAL.labels(method="GET", path="/x", status="200").inc()
        body, content_type = prom_metrics.render_metrics()

        assert body == b""
        assert content_type.startswith("text/plain")
    finally:
        monkeypatch.undo()
        importlib.reload(prom_metrics)
