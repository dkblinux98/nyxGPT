"""OpenTelemetry distributed tracing for the nyxGPT API.

Tracing is opt-in and local-only: spans are exported via OTLP/HTTP to a
collector that only exists when the ``tracing`` Compose profile (OTel
collector + Jaeger all-in-one) is running. Nothing here talks to an
external/cloud exporter. When tracing is disabled (the default), every
function in this module is a no-op so the rest of the app pays no cost.
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
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.cassandra import CassandraInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

logger = logging.getLogger(__name__)

_TRACER_NAME = "nyxgpt"
_F = TypeVar("_F", bound=Callable[..., Any])

_enabled = False


def init_tracing(app: FastAPI, tracing_config: dict[str, Any]) -> None:
    """Set up the OTel SDK and instrument the app, if tracing is enabled.

    Registers a TracerProvider that batches spans to a local OTLP collector,
    then instruments the FastAPI app (one span per request) and the
    Cassandra driver (one span per query) so chat/RAG traffic and its
    storage calls show up in Jaeger without call-site changes.
    """
    global _enabled

    if not tracing_config.get("enabled"):
        _enabled = False
        return

    resource = Resource.create({SERVICE_NAME: tracing_config["service_name"]})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=tracing_config["otlp_endpoint"])
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    CassandraInstrumentor().instrument()

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
