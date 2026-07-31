"""OpenTelemetry distributed tracing for the nyxGPT API.

Tracing is opt-in and local-only: spans are exported via OTLP/HTTP to a
collector that only exists when the ``tracing`` Compose profile (OTel
collector + Jaeger all-in-one) is running. Nothing here talks to an
external/cloud exporter. When tracing is disabled, `init_tracing` is a
no-op and every other function in this module is a no-op too, so the rest
of the app pays no cost. The one exception is `instrument_fastapi_app`,
which must run unconditionally at module import time (see its docstring)
-- with no SDK provider installed it wires in an inert proxy tracer, not a
literal no-op call.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar
from urllib.parse import urlparse

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)

_TRACER_NAME = "nyxgpt"
_F = TypeVar("_F", bound=Callable[..., Any])

_enabled = False

# The exporter and instrumentors below are optional at import time: this
# module is imported by app.py, ollama_client.py, rag/rag.py, and
# rag/embeddings.py, so *every* nyxgpt process (API, CLI, MCP server) would
# crash at startup if one of these packages were missing -- even with
# tracing disabled in config. `opentelemetry-instrumentation-urllib` etc.
# were added to pyproject.toml alongside the #3430 OTel backbone, so a venv
# that predates that merge must degrade gracefully here rather than hard
# crash on the next restart (#3484).
try:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
except ImportError:
    OTLPSpanExporter = None  # type: ignore[assignment,misc]

try:
    from opentelemetry.instrumentation.cassandra import CassandraInstrumentor
except ImportError:
    CassandraInstrumentor = None  # type: ignore[assignment,misc]

try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
except ImportError:
    FastAPIInstrumentor = None  # type: ignore[assignment,misc]

try:
    from opentelemetry.instrumentation.urllib import URLLibInstrumentor
except ImportError:
    URLLibInstrumentor = None  # type: ignore[assignment,misc]

# Human-facing pip package name for each optional symbol above, keyed by the
# symbol's name in this module -- used both for the enabled-but-missing
# warning in `init_tracing` and for `missing_tracing_packages()` (the `nyxgpt
# ops doctor` check).
_OPTIONAL_TRACING_PACKAGES: dict[str, str] = {
    "OTLPSpanExporter": "opentelemetry-exporter-otlp-proto-http",
    "CassandraInstrumentor": "opentelemetry-instrumentation-cassandra",
    "FastAPIInstrumentor": "opentelemetry-instrumentation-fastapi",
    "URLLibInstrumentor": "opentelemetry-instrumentation-urllib",
}


def missing_tracing_packages() -> list[str]:
    """Pip package names for optional OTel packages not installed in this venv.

    Used by `nyxgpt ops doctor` to flag a stale venv (missing a package added
    after the operator's last `nyxgpt ops install`) as an actionable issue,
    the same #3350-style pattern as the OTLP-reachability check.
    """
    symbols = {
        "OTLPSpanExporter": OTLPSpanExporter,
        "CassandraInstrumentor": CassandraInstrumentor,
        "FastAPIInstrumentor": FastAPIInstrumentor,
        "URLLibInstrumentor": URLLibInstrumentor,
    }
    return [
        package for name, package in _OPTIONAL_TRACING_PACKAGES.items() if symbols[name] is None
    ]


def _warn_missing_package(symbol_name: str) -> None:
    """Log one bounded WARNING for a missing optional OTel package and its fix."""
    package = _OPTIONAL_TRACING_PACKAGES[symbol_name]
    logger.warning(
        "Tracing: %s unavailable (missing package %s); skipping it. "
        "Run `nyxgpt ops install` to fix.",
        symbol_name,
        package,
        extra={"component": "tracing", "missing_package": package},
    )


def instrument_fastapi_app(app: FastAPI) -> None:
    """Wire OTel's ASGI instrumentation into the app for HTTP request spans.

    Must be called before the app processes its first ASGI message of any
    kind -- including the `lifespan` startup event. Starlette builds and
    caches `app.middleware_stack` the first time it's called (that first
    call *is* the lifespan startup message uvicorn sends), so instrumenting
    from inside the app's own lifespan handler is silently a no-op: nothing
    raises, but the OTel middleware never actually gets woven into the
    stack, and every request serves with zero HTTP-level spans -- even
    though Cassandra/urllib spans (monkey-patched, not middleware-based)
    keep working and make it look like tracing is fine. Call this at module
    scope right after the `FastAPI()` app is constructed instead.

    Safe to call unconditionally, independent of whether tracing ends up
    enabled: with no SDK `TracerProvider` installed yet, OTel's API hands
    out a proxy tracer that's an inert no-op until `init_tracing` (if it
    ever runs) installs the real one -- see `ProxyTracerProvider` in the
    `opentelemetry-api` package.
    """
    if FastAPIInstrumentor is None:
        _warn_missing_package("FastAPIInstrumentor")
        return
    FastAPIInstrumentor.instrument_app(app)


def init_tracing(tracing_config: dict[str, Any]) -> None:
    """Set up the OTel SDK and instrument non-ASGI call sites, if enabled.

    Registers a TracerProvider that batches spans to a local OTLP collector,
    then instruments the Cassandra driver (one span per query) and
    `urllib.request` (one client span per outbound Ollama call, with a W3C
    `traceparent` header injected automatically) so chat/RAG traffic's
    storage calls and Ollama calls show up in Jaeger without call-site
    changes -- the browser/Next -> FastAPI -> Ollama correlation backbone
    (#3430). FastAPI's own request spans are wired up separately by
    `instrument_fastapi_app`, which must run before this (see its
    docstring for why).
    """
    global _enabled

    if not tracing_config.get("enabled"):
        _enabled = False
        return

    if OTLPSpanExporter is None:
        _warn_missing_package("OTLPSpanExporter")
        _enabled = False
        return

    resource = Resource.create({SERVICE_NAME: tracing_config["service_name"]})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=tracing_config["otlp_endpoint"])
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    if CassandraInstrumentor is not None:
        CassandraInstrumentor().instrument()
    else:
        _warn_missing_package("CassandraInstrumentor")

    if URLLibInstrumentor is not None:
        URLLibInstrumentor().instrument()
    else:
        _warn_missing_package("URLLibInstrumentor")

    _enabled = True
    logger.info(
        "Distributed tracing enabled",
        extra={
            "component": "tracing",
            "service_name": tracing_config["service_name"],
            "otlp_endpoint": tracing_config["otlp_endpoint"],
        },
    )


def is_tracing_enabled() -> bool:
    """Whether tracing was actually initialized for this process."""
    return _enabled


def current_trace_id() -> str | None:
    """The active span's trace id as 32 lowercase hex chars, or None.

    Used to derive `request_id` from the trace context (#3430) when a
    request arrives with no `X-Request-Id` header of its own -- ties the
    human-facing request id to the same trace Jaeger already has, instead
    of a disconnected UUID. Returns None whenever there's no valid active
    span (tracing disabled, not yet initialized, or genuinely no span),
    same no-op-by-default contract as the rest of this module.
    """
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")


def otlp_endpoint_reachable(otlp_endpoint: str, timeout: float = 2.0) -> bool:
    """Whether something is actually listening on the OTLP endpoint's host/port.

    ``OTLPSpanExporter`` is fire-and-forget: when nothing listens on
    ``otlp_endpoint`` (e.g. the otel-collector Compose service isn't running,
    or isn't publishing its port to the host in native mode -- see #3350),
    it silently drops every span rather than raising anywhere visible. A
    real TCP connect is the only way to tell "active and working" apart from
    "active but exporting into the void" from outside the exporter itself.
    """
    parsed = urlparse(otlp_endpoint)
    host = parsed.hostname or "localhost"
    port = parsed.port or 4318
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@contextmanager
def traced_span(name: str, **attributes: Any) -> Iterator[Span | None]:
    """Start a span named ``name``, or do nothing when tracing is disabled.

    Used to cover operations that automatic instrumentation can't see, such
    as the Ollama HTTP calls in ``ollama_client``/``embeddings`` (a plain
    ``urllib`` client, not an auto-instrumented HTTP library).
    """
    if not _enabled:
        yield None
        return

    tracer = trace.get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(name) as span:
        for key, value in attributes.items():
            span.set_attribute(key, value)
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


def traced(name: str) -> Callable[[_F], _F]:
    """Decorator that wraps a function call in a ``traced_span``."""

    def decorator(func: _F) -> _F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with traced_span(name):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
