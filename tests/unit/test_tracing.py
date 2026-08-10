"""Unit tests for distributed tracing (nyxgpt.tracing and config getters)."""

from __future__ import annotations

import json
import logging
import sys
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


def _otel_context_detach_race_record() -> logging.LogRecord:
    """A log record shaped like `opentelemetry.context.detach`'s real one
    (#3593): logger name "opentelemetry.context", message "Failed to detach
    context", exc_info holding the exact ValueError contextvars raises when
    a Token is detached in a different Context than it was attached in."""
    try:
        raise ValueError(
            "<Token var=<ContextVar name='current_context' default={} at 0x111> "
            "at 0x222> was created in a different Context"
        )
    except ValueError:
        return logging.LogRecord(
            name="opentelemetry.context",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Failed to detach context",
            args=(),
            exc_info=sys.exc_info(),
        )


def test_suppress_context_detach_race_filter_drops_the_known_race() -> None:
    """The exact log shape #3593 floods GlitchTip with must be dropped."""
    filt = tracing._SuppressContextDetachRaceFilter()

    assert filt.filter(_otel_context_detach_race_record()) is False


def test_suppress_context_detach_race_filter_passes_through_other_records() -> None:
    """Only the specific known-benign detach race is suppressed -- any other
    log from the same logger, or the same message from a different one, or
    an unrelated exception type, must pass through untouched so a genuinely
    new problem doesn't go silently missing."""
    filt = tracing._SuppressContextDetachRaceFilter()

    other_logger_record = logging.LogRecord(
        name="opentelemetry.context",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="some other message",
        args=(),
        exc_info=None,
    )
    assert filt.filter(other_logger_record) is True

    wrong_logger_record = _otel_context_detach_race_record()
    wrong_logger_record.name = "some.other.logger"
    assert filt.filter(wrong_logger_record) is True

    try:
        raise RuntimeError("Failed to detach context but for an unrelated reason")
    except RuntimeError:
        different_exc_record = logging.LogRecord(
            name="opentelemetry.context",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Failed to detach context",
            args=(),
            exc_info=sys.exc_info(),
        )
    assert filt.filter(different_exc_record) is True


def test_instrument_fastapi_app_installs_context_detach_race_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """instrument_fastapi_app must guard the OTel context logger against the
    #3593 flood every time it wires up the ASGI instrumentation."""
    otel_logger = logging.getLogger("opentelemetry.context")
    for f in list(otel_logger.filters):
        if isinstance(f, tracing._SuppressContextDetachRaceFilter):
            otel_logger.removeFilter(f)

    class FakeInstrumentor:
        @staticmethod
        def instrument_app(app: Any) -> None:
            pass

    monkeypatch.setattr(tracing, "FastAPIInstrumentor", FakeInstrumentor)

    tracing.instrument_fastapi_app(object())  # type: ignore[arg-type]

    assert any(isinstance(f, tracing._SuppressContextDetachRaceFilter) for f in otel_logger.filters)


def test_instrument_fastapi_app_does_not_stack_duplicate_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling instrument_fastapi_app repeatedly (as tests in this module
    do) must not accumulate duplicate filters on the shared logger."""
    otel_logger = logging.getLogger("opentelemetry.context")

    class FakeInstrumentor:
        @staticmethod
        def instrument_app(app: Any) -> None:
            pass

    monkeypatch.setattr(tracing, "FastAPIInstrumentor", FakeInstrumentor)

    tracing.instrument_fastapi_app(object())  # type: ignore[arg-type]
    tracing.instrument_fastapi_app(object())  # type: ignore[arg-type]

    race_filters = [
        f for f in otel_logger.filters if isinstance(f, tracing._SuppressContextDetachRaceFilter)
    ]
    assert len(race_filters) == 1


def test_context_detach_race_is_suppressed_end_to_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full-stack regression guard for #3593: a real ContextVar Token
    detached in a different asyncio Context than it was attached in must
    not surface as a captured ERROR-level log once `instrument_fastapi_app`
    (called at import time by `nyxgpt.app`) has run."""
    import asyncio

    from opentelemetry import context as otel_context

    async def _detach_in_other_task(token: Any) -> None:
        otel_context.detach(token)

    async def _trigger_race() -> None:
        token = otel_context.attach(otel_context.set_value("nyxgpt-test-key", "value"))
        await asyncio.create_task(_detach_in_other_task(token))

    with caplog.at_level("DEBUG", logger="opentelemetry.context"):
        asyncio.run(_trigger_race())

    assert caplog.records == []


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


def test_tracing_module_imports_when_instrumentation_package_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core #3484 regression: nyxgpt.tracing -- and therefore every
    process that imports nyxgpt code (app.py, ollama_client.py, rag/*) --
    must not crash at import time just because an optional OTel
    instrumentation package isn't installed in this venv."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "opentelemetry.instrumentation.urllib":
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    try:
        importlib.reload(tracing)
        assert tracing.URLLibInstrumentor is None
        assert "opentelemetry-instrumentation-urllib" in tracing.missing_tracing_packages()
    finally:
        # Restore the real import hook *before* reloading -- reloading while
        # still patched would leave URLLibInstrumentor permanently None for
        # every later test in this process.
        monkeypatch.setattr(builtins, "__import__", real_import)
        importlib.reload(tracing)


def test_missing_tracing_packages_empty_when_all_present() -> None:
    """In this repo's test venv every optional OTel package is installed, so
    the doctor helper must report nothing missing."""
    assert tracing.missing_tracing_packages() == []


def test_missing_tracing_packages_reports_missing_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tracing, "URLLibInstrumentor", None)

    assert tracing.missing_tracing_packages() == ["opentelemetry-instrumentation-urllib"]


def test_init_tracing_disables_when_exporter_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The #3484 failure mode: a stale venv is missing the OTLP exporter
    package itself. `init_tracing` must not raise -- it must log a bounded
    WARNING naming the missing package and the remedy, then leave tracing
    disabled so the process starts and serves normally."""
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "OTLPSpanExporter", None)

    with caplog.at_level("WARNING", logger="nyxgpt.tracing"):
        tracing.init_tracing(
            tracing_config={
                "enabled": True,
                "service_name": "my-service",
                "otlp_endpoint": "http://collector:4318/v1/traces",
            },
        )

    assert tracing.is_tracing_enabled() is False
    assert len(caplog.records) == 1
    assert "opentelemetry-exporter-otlp-proto-http" in caplog.text
    assert "nyxgpt ops install" in caplog.text


def test_init_tracing_skips_missing_instrumentor_but_stays_enabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When only an instrumentor (not the exporter) is missing, init_tracing
    must skip that instrumentor, log a bounded WARNING, and still fully
    enable tracing -- spans still export, just without that instrumentation."""
    monkeypatch.setattr(tracing, "_enabled", False)
    monkeypatch.setattr(tracing, "URLLibInstrumentor", None)

    cassandra_instrumented = []

    class FakeCassandraInstrumentor:
        def instrument(self) -> None:
            cassandra_instrumented.append(True)

    class FakeExporter:
        def __init__(self, endpoint: str) -> None:
            pass

    class FakeProcessor:
        def __init__(self, exporter: Any) -> None:
            pass

    class FakeProvider:
        def __init__(self, resource: Any) -> None:
            pass

        def add_span_processor(self, processor: Any) -> None:
            pass

    monkeypatch.setattr(tracing, "CassandraInstrumentor", FakeCassandraInstrumentor)
    monkeypatch.setattr(tracing, "OTLPSpanExporter", FakeExporter)
    monkeypatch.setattr(tracing, "TracerProvider", FakeProvider)
    monkeypatch.setattr(tracing, "BatchSpanProcessor", FakeProcessor)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda provider: None)

    with caplog.at_level("WARNING", logger="nyxgpt.tracing"):
        tracing.init_tracing(
            tracing_config={
                "enabled": True,
                "service_name": "my-service",
                "otlp_endpoint": "http://collector:4318/v1/traces",
            },
        )

    assert tracing.is_tracing_enabled() is True
    assert cassandra_instrumented == [True]
    assert len(caplog.records) == 1
    assert "opentelemetry-instrumentation-urllib" in caplog.text
    assert "nyxgpt ops install" in caplog.text


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


def test_grafana_jaeger_search_panels_do_not_use_the_single_trace_panel_type() -> None:
    """Regression test for #3564 (acceptance-failure round 1 of #3472): the
    SPOG traces panel kept showing "No data found in response" against a
    live Jaeger that verifiably had matching traces (confirmed by querying
    Grafana's own /api/ds/query with the panel's exact target JSON). The
    query and service name were both correct -- the panel's `type` was
    "traces", which Grafana's core Traces panel only renders for a single
    trace's spans. A Jaeger `queryType: "search"` target returns a *list* of
    traces (Trace ID / Trace name / Start time / Duration columns), a shape
    the Traces panel can't display; it silently reports no data instead of
    erroring. The Table panel renders that list shape natively. Sweeps every
    dashboard so a future Jaeger search panel can't reintroduce this."""
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    search_panels = []
    for dashboard_path in sorted(dashboards_dir.glob("*.json")):
        dashboard = json.loads(dashboard_path.read_text())
        for panel, target in _iter_jaeger_panel_targets(dashboard):
            if target.get("queryType") == "search":
                search_panels.append((dashboard_path.name, panel["title"], panel.get("type")))

    assert search_panels, "expected at least one Jaeger search-query panel"
    for dashboard_name, panel_title, panel_type in search_panels:
        assert panel_type != "traces", (
            f"{dashboard_name}: panel {panel_title!r} runs a Jaeger "
            f"queryType=search (multi-trace list) query but uses the "
            f"single-trace 'traces' panel type, which renders it as empty "
            f"('No data found in response') even when Jaeger has matching "
            f"traces -- use 'table' instead"
        )
