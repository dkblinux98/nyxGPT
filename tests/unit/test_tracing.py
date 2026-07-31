"""Unit tests for distributed tracing (nyxgpt.tracing and config getters)."""

from __future__ import annotations

import json
from configparser import ConfigParser
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.resources import SERVICE_NAME
from opentelemetry.trace import StatusCode

from nyxgpt import tracing
from nyxgpt.app import app
from nyxgpt.config import get_tracing_config, get_tracing_enabled

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.status: Any = None
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_status(self, status: Any) -> None:
        self.status = status

    def record_exception(self, exc: BaseException) -> None:
        self.exceptions.append(exc)


class _FakeTracer:
    def __init__(self, span: _FakeSpan) -> None:
        self._span = span
        self.span_name: str | None = None

    @contextmanager
    def start_as_current_span(self, name: str):
        self.span_name = name
        yield self._span


def _cfg(**tracing_options: str) -> ConfigParser:
    cfg = ConfigParser()
    if tracing_options:
        cfg["tracing"] = tracing_options
    return cfg


def test_get_tracing_enabled_defaults_to_true() -> None:
    assert get_tracing_enabled(_cfg()) is True


def test_get_tracing_enabled_reads_config() -> None:
    assert get_tracing_enabled(_cfg(enabled="true")) is True
    assert get_tracing_enabled(_cfg(enabled="false")) is False


def test_get_tracing_config_defaults() -> None:
    result = get_tracing_config(_cfg())

    assert result == {
        "enabled": True,
        "service_name": "nyxgpt-api",
        "otlp_endpoint": "http://localhost:4318/v1/traces",
        "jaeger_ui_url": "http://localhost:16686",
    }


def test_get_tracing_config_reads_overrides() -> None:
    cfg = _cfg(
        enabled="true",
        service_name="my-service",
        otlp_endpoint="http://collector:4318/v1/traces",
        jaeger_ui_url="http://jaeger:16686",
    )

    result = get_tracing_config(cfg)

    assert result["enabled"] is True
    assert result["service_name"] == "my-service"
    assert result["otlp_endpoint"] == "http://collector:4318/v1/traces"
    assert result["jaeger_ui_url"] == "http://jaeger:16686"


def test_traced_span_is_noop_when_disabled() -> None:
    """With tracing disabled (the default), traced_span must not raise and
    must yield None -- callers shouldn't need to special-case it."""
    with tracing.traced_span("some.operation", key="value") as span:
        assert span is None


def test_traced_span_reraises_exceptions_when_disabled() -> None:
    with pytest.raises(ValueError), tracing.traced_span("some.operation"):
        raise ValueError("boom")


def test_traced_decorator_preserves_return_value_when_disabled() -> None:
    @tracing.traced("some.function")
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_init_tracing_is_noop_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """init_tracing must not touch the OTel SDK or instrument anything when
    the config says tracing is disabled (the default for every deployment
    unless the operator explicitly opts in)."""
    monkeypatch.setattr(tracing, "_enabled", True)

    tracing.init_tracing(tracing_config={"enabled": False})

    assert tracing.is_tracing_enabled() is False


def test_tracing_status_endpoint_reports_disabled_by_default() -> None:
    """The test config fixture has no [tracing] section, so the endpoint
    must report the safe default: disabled, not active."""
    client = TestClient(app)

    response = client.get("/api/v1/tracing")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["active"] is False
    assert data["jaeger_ui_url"] == "http://localhost:16686"


def test_tracing_status_endpoint_includes_curated_jaeger_views() -> None:
    """The SRE overview links out to curated Jaeger trace views for the main
    request flows (chat, RAG query/ingest, Ollama backend calls) -- each
    must deep-link into the configured Jaeger UI for the configured
    service, with a hint for which operation to pick."""
    client = TestClient(app)

    response = client.get("/api/v1/tracing")

    assert response.status_code == 200
    curated_views = response.json()["curated_views"]
    assert len(curated_views) >= 4
    labels = {view["label"] for view in curated_views}
    assert {"Chat requests", "RAG query", "RAG ingest", "Ollama backend calls"} <= labels
    for view in curated_views:
        assert view["url"].startswith("http://localhost:16686/search?service=nyxgpt-api")
        assert view["hint"]


def test_init_tracing_enables_and_instruments_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With tracing enabled, init_tracing must build a TracerProvider bound
    to the configured service name, export via the configured OTLP
    endpoint, instrument Cassandra + urllib, and flip _enabled to True.
    FastAPI instrumentation is covered separately by
    `instrument_fastapi_app` -- it must NOT run again from here (see
    `test_fastapi_app_is_instrumented_before_lifespan_runs` for why running
    it from inside app startup is the bug this module now avoids)."""
    monkeypatch.setattr(tracing, "_enabled", False)

    cassandra_instrumented = []
    urllib_instrumented = []
    exporter_endpoints = []
    provider_resources = []
    tracer_providers_set = []

    class FakeCassandraInstrumentor:
        def instrument(self) -> None:
            cassandra_instrumented.append(True)

    class FakeURLLibInstrumentor:
        def instrument(self) -> None:
            urllib_instrumented.append(True)

    class FakeExporter:
        def __init__(self, endpoint: str) -> None:
            exporter_endpoints.append(endpoint)

    class FakeProcessor:
        def __init__(self, exporter: Any) -> None:
            self.exporter = exporter

    class FakeProvider:
        def __init__(self, resource: Any) -> None:
            provider_resources.append(resource)
            self.processors: list[Any] = []

        def add_span_processor(self, processor: Any) -> None:
            self.processors.append(processor)

    monkeypatch.setattr(tracing, "CassandraInstrumentor", FakeCassandraInstrumentor)
    monkeypatch.setattr(tracing, "URLLibInstrumentor", FakeURLLibInstrumentor)
    monkeypatch.setattr(tracing, "OTLPSpanExporter", FakeExporter)
    monkeypatch.setattr(tracing, "TracerProvider", FakeProvider)
    monkeypatch.setattr(tracing, "BatchSpanProcessor", FakeProcessor)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", tracer_providers_set.append)

    tracing.init_tracing(
        tracing_config={
            "enabled": True,
            "service_name": "my-service",
            "otlp_endpoint": "http://collector:4318/v1/traces",
        },
    )

    assert tracing.is_tracing_enabled() is True
    assert cassandra_instrumented == [True]
    assert urllib_instrumented == [True]
    assert exporter_endpoints == ["http://collector:4318/v1/traces"]
    assert len(provider_resources) == 1
    assert provider_resources[0].attributes[SERVICE_NAME] == "my-service"
    assert len(tracer_providers_set) == 1


def test_instrument_fastapi_app_calls_the_otel_instrumentor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instrument_fastapi_app must delegate straight to
    FastAPIInstrumentor.instrument_app -- it's the only thing it does."""
    instrumented_apps = []

    class FakeInstrumentor:
        @staticmethod
        def instrument_app(app: Any) -> None:
            instrumented_apps.append(app)

    monkeypatch.setattr(tracing, "FastAPIInstrumentor", FakeInstrumentor)

    fake_app = object()
    tracing.instrument_fastapi_app(fake_app)  # type: ignore[arg-type]

    assert instrumented_apps == [fake_app]


def test_fastapi_app_is_instrumented_before_lifespan_runs() -> None:
    """Regression test for the bug this issue fixes: FastAPIInstrumentor
    used to be wired up from inside app.py's `lifespan` startup handler.
    Starlette caches `app.middleware_stack` on the very first ASGI message
    it receives -- which is the lifespan startup message itself -- so
    instrumenting from inside lifespan silently never took effect: no
    exception, but every request served with zero HTTP-level spans, even
    though Cassandra/urllib spans (monkey-patched, not middleware-based)
    kept working and made tracing look healthy.

    This exercises the real `nyxgpt.app.app` object end-to-end: a real
    OTel TracerProvider with an in-memory exporter, a real request through
    TestClient, and an assertion that a SERVER span was actually recorded.
    If FastAPI instrumentation ever moves back into `lifespan`/
    `init_tracing`, this test fails because no span is recorded."""
    from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.trace.set_tracer_provider(provider)

    with TestClient(app) as client:
        response = client.get("/api/v1/tracing")
    assert response.status_code == 200

    server_spans = [
        span for span in exporter.get_finished_spans() if span.name == "GET /api/v1/tracing"
    ]
    assert len(server_spans) == 1


def test_traced_span_enabled_sets_attributes_on_the_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When tracing is enabled, traced_span must start a real span and set
    every keyword argument as a span attribute."""
    monkeypatch.setattr(tracing, "_enabled", True)
    fake_span = _FakeSpan()
    fake_tracer = _FakeTracer(fake_span)
    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: fake_tracer)

    with tracing.traced_span("my.operation", foo="bar", count=1) as span:
        assert span is fake_span

    assert fake_tracer.span_name == "my.operation"
    assert fake_span.attributes == {"foo": "bar", "count": 1}
    assert fake_span.status is None
    assert fake_span.exceptions == []


def test_traced_span_enabled_records_exception_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the wrapped code raises, traced_span must record the error
    status and the exception on the span, then re-raise unchanged."""
    monkeypatch.setattr(tracing, "_enabled", True)
    fake_span = _FakeSpan()
    fake_tracer = _FakeTracer(fake_span)
    monkeypatch.setattr(tracing.trace, "get_tracer", lambda name: fake_tracer)

    with pytest.raises(ValueError, match="boom"), tracing.traced_span("my.operation"):
        raise ValueError("boom")

    assert fake_span.status is not None
    assert fake_span.status.status_code == StatusCode.ERROR
    assert len(fake_span.exceptions) == 1
    assert isinstance(fake_span.exceptions[0], ValueError)


def test_tracing_status_endpoint_reports_active_when_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once init_tracing has actually run (_enabled == True), the status
    endpoint must reflect that as active, independent of raw config."""
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "otlp_endpoint_reachable", lambda endpoint, **kw: True)
    client = TestClient(app)

    response = client.get("/api/v1/tracing")

    assert response.status_code == 200
    assert response.json()["active"] is True


def test_tracing_status_endpoint_reports_unreachable_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The #3350 failure mode: tracing is active (init_tracing ran) but
    nothing actually listens on the OTLP endpoint (e.g. otel-collector
    publishes no host port in native mode). The status endpoint must surface
    that distinction rather than just reporting "active"."""
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "otlp_endpoint_reachable", lambda endpoint, **kw: False)
    client = TestClient(app)

    response = client.get("/api/v1/tracing")

    assert response.status_code == 200
    data = response.json()
    assert data["active"] is True
    assert data["reachable"] is False


def test_tracing_status_endpoint_reachable_is_none_when_inactive() -> None:
    """When tracing isn't active at all, there's nothing to probe -- the
    reachability field must be None rather than implying a real check ran."""
    client = TestClient(app)

    response = client.get("/api/v1/tracing")

    assert response.status_code == 200
    assert response.json()["reachable"] is None


def test_otlp_endpoint_reachable_true_when_something_listens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(tracing.socket, "create_connection", lambda addr, timeout: _FakeSocket())

    assert tracing.otlp_endpoint_reachable("http://localhost:4318/v1/traces") is True


def test_otlp_endpoint_reachable_false_when_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(addr, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(tracing.socket, "create_connection", _raise)

    assert tracing.otlp_endpoint_reachable("http://localhost:4318/v1/traces") is False


def test_current_trace_id_is_none_without_active_span() -> None:
    """No active span (tracing disabled, or simply no span right now)."""
    assert tracing.current_trace_id() is None


def test_current_trace_id_returns_hex_trace_id_for_active_span() -> None:
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider()
    tracer = provider.get_tracer("test")

    with tracer.start_as_current_span("test-span"):
        trace_id = tracing.current_trace_id()

    assert trace_id is not None
    assert len(trace_id) == 32
    int(trace_id, 16)  # must be valid hex


def test_otlp_endpoint_reachable_parses_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    class _FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def _create_connection(addr, timeout):
        seen.append(addr)
        return _FakeSocket()

    monkeypatch.setattr(tracing.socket, "create_connection", _create_connection)

    tracing.otlp_endpoint_reachable("http://collector.internal:9999/v1/traces")

    assert seen == [("collector.internal", 9999)]


def _iter_jaeger_panel_targets(dashboard: dict[str, Any]):
    for panel in dashboard.get("panels", []):
        if panel.get("datasource", {}).get("type") != "jaeger":
            continue
        for target in panel.get("targets", []):
            if target.get("datasource", {}).get("type") == "jaeger":
                yield panel, target


def test_grafana_jaeger_panels_service_filter_matches_tracing_config() -> None:
    """Regression test for #3472: the SPOG traces panel showed "No data
    found in response" against a live Jaeger that had traces, because
    nothing kept the panel's hardcoded Jaeger query `service` string in
    sync with the `service.name` `tracing.py` actually exports. Sweeps
    every dashboard for every panel wired to the Jaeger datasource (not
    just the known one today) and asserts each target's `service` equals
    `get_tracing_config`'s default -- so a future service-name rename in
    either the panel JSON or `tracing.py` breaks this test, not the panel."""
    expected_service = get_tracing_config(ConfigParser())["service_name"]

    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    jaeger_targets = []
    for dashboard_path in sorted(dashboards_dir.glob("*.json")):
        dashboard = json.loads(dashboard_path.read_text())
        for panel, target in _iter_jaeger_panel_targets(dashboard):
            jaeger_targets.append((dashboard_path.name, panel["title"], target))

    assert jaeger_targets, "expected at least one panel wired to the Jaeger datasource"
    for dashboard_name, panel_title, target in jaeger_targets:
        assert target.get("service") == expected_service, (
            f"{dashboard_name}: panel {panel_title!r} queries Jaeger for "
            f"service={target.get('service')!r}, but tracing.py exports "
            f"service.name={expected_service!r}"
        )
