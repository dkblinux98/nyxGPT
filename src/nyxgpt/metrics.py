"""Prometheus metrics for the nyxGPT API.

Defines the metric collectors used across the app (HTTP request counts,
latency histograms, error rates, and business metrics for chat/RAG usage)
and a helper to render them in the Prometheus text exposition format for
the `/metrics` endpoint.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

HTTP_REQUESTS_TOTAL = Counter(
    "nyxgpt_http_requests_total",
    "Total HTTP requests handled by the API",
    ["method", "path", "status"],
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "nyxgpt_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    registry=REGISTRY,
)

HTTP_ERRORS_TOTAL = Counter(
    "nyxgpt_http_errors_total",
    "Total HTTP requests that resulted in a 5xx server error",
    ["method", "path"],
    registry=REGISTRY,
)

CHAT_REQUESTS_TOTAL = Counter(
    "nyxgpt_chat_requests_total",
    "Total chat requests processed, by model and whether streaming was used",
    ["model", "streaming"],
    registry=REGISTRY,
)

RAG_QUERIES_TOTAL = Counter(
    "nyxgpt_rag_queries_total",
    "Total RAG retrieval queries executed",
    ["source"],
    registry=REGISTRY,
)

SELFHEAL_UNHEALTHY_COMPONENTS = Gauge(
    "nyxgpt_selfheal_unhealthy_components",
    "Number of self-heal-monitored components currently unhealthy or stopped",
    registry=REGISTRY,
)

SELFHEAL_RESTARTS_TOTAL = Counter(
    "nyxgpt_selfheal_restarts_total",
    "Total self-heal restart attempts, by service and outcome",
    ["service", "result"],
    registry=REGISTRY,
)

SELFHEAL_RESTART_COUNT = Gauge(
    "nyxgpt_selfheal_restart_count",
    "Current consecutive-restart count per service (resets to 0 once healthy again)",
    ["service"],
    registry=REGISTRY,
)

SELFHEAL_LAST_RECOVERY_TIMESTAMP = Gauge(
    "nyxgpt_selfheal_last_recovery_timestamp",
    "Unix timestamp of the last successful self-heal restart, by service",
    ["service"],
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    """Render all registered metrics in Prometheus text exposition format.

    Returns:
        A tuple of (body, content_type) suitable for a FastAPI Response.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
