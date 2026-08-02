"""Prometheus metrics for the nyxGPT API.

Defines the metric collectors used across the app (HTTP request counts,
latency histograms, error rates, and business metrics for chat/RAG usage)
and a helper to render them in the Prometheus text exposition format for
the `/metrics` endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
except ModuleNotFoundError:
    # prometheus-client is declared in pyproject.toml but a venv can be
    # missing it after a pull adds/bumps a dependency (#3487). Metrics are
    # collected unconditionally (no config-driven "enabled" gate like
    # tracing/error_tracking), so there's no init function to defer this
    # warning to -- log it once here and fall back to no-op collectors so
    # every call site across the app (chat, self_heal, cache, ops, ...)
    # keeps working without special-casing a missing metrics backend.
    logger.warning(
        "prometheus-client is not installed; metrics collection is disabled -- "
        "your environment is likely stale after a pull; run `pip install -e .` "
        "to install it.",
        extra={"component": "metrics"},
    )

    class _NoOpMetric:
        """Stand-in for Counter/Gauge/Histogram that drops every call."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Accept and ignore whatever arguments the real metric takes."""

        def labels(self, *_args: Any, **_kwargs: Any) -> _NoOpMetric:
            """Return self so chained `.labels(...).inc()` calls keep working."""
            return self

        def inc(self, *_args: Any, **_kwargs: Any) -> None:
            """No-op counter increment."""

        def set(self, *_args: Any, **_kwargs: Any) -> None:
            """No-op gauge set."""

        def observe(self, *_args: Any, **_kwargs: Any) -> None:
            """No-op histogram observation."""

    class _NoOpCollectorRegistry:
        """Stand-in for `CollectorRegistry` that holds nothing."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            """Accept and ignore whatever arguments the real registry takes."""

    CollectorRegistry = _NoOpCollectorRegistry  # type: ignore[assignment, misc]
    Counter = _NoOpMetric  # type: ignore[assignment, misc]
    Gauge = _NoOpMetric  # type: ignore[assignment, misc]
    Histogram = _NoOpMetric  # type: ignore[assignment, misc]
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    def generate_latest(_registry: Any) -> bytes:  # type: ignore[misc]
        """Stand-in for `generate_latest`: nothing was ever collected."""
        return b""


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

CHAT_ATTACHMENTS_TOTAL = Counter(
    "nyxgpt_chat_attachments_total",
    "Total chat document attachments decoded via the paperclip flow, by outcome. "
    "This is a chat feature, not RAG ingestion (#3469) -- these documents are "
    "prepended to the prompt inline and never touch the vectorstore, so this "
    "counter is deliberately separate from nyxgpt_rag_ingests_total.",
    ["result"],
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

SELFHEAL_COMPONENT_HEALTHY = Gauge(
    "nyxgpt_selfheal_component_healthy",
    "Whether a self-heal-monitored component is healthy (1) or unhealthy (0), by "
    "service -- names the component nyxgpt_selfheal_unhealthy_components can only "
    "count. A component with its observability profile disabled (desired=False) "
    "reports 1 even while stopped, matching nyxgpt_selfheal_unhealthy_components's "
    "own count semantics (see self_heal._record_health_check): it's intentionally "
    "down, not an operator concern.",
    ["service"],
    registry=REGISTRY,
)

SELFHEAL_LAST_CHECK_TIMESTAMP = Gauge(
    "nyxgpt_selfheal_last_check_timestamp",
    "Unix timestamp of the most recent self-heal health-check pass -- lets a "
    "dashboard panel show how stale nyxgpt_selfheal_unhealthy_components and "
    "nyxgpt_selfheal_component_healthy are relative to 'now', since both hold "
    "their last value between check passes rather than updating live.",
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

OPS_ACTIONS_TOTAL = Counter(
    "nyxgpt_ops_actions_total",
    "Total operator-initiated lifecycle actions (nyxgpt ops CLI or admin dashboard), "
    "by command, service, and result. Distinct from nyxgpt_selfheal_restarts_total, "
    "which counts only the watchdog's own autonomous restarts.",
    ["command", "service", "result"],
    registry=REGISTRY,
)

CANARY_ROLLOUT_ACTIVE = Gauge(
    "nyxgpt_canary_rollout_active",
    "Whether a canary rollout is currently in progress (1) or idle (0)",
    registry=REGISTRY,
)

CANARY_WEIGHT_PERCENT = Gauge(
    "nyxgpt_canary_weight_percent",
    "Current canary traffic weight percentage (0-100)",
    registry=REGISTRY,
)

CANARY_EVALUATIONS_TOTAL = Counter(
    "nyxgpt_canary_evaluations_total",
    "Total canary metric evaluations, by result (pass/insufficient_data/regression)",
    ["result"],
    registry=REGISTRY,
)

CANARY_EVENTS_TOTAL = Counter(
    "nyxgpt_canary_events_total",
    "Total canary rollout lifecycle events, by action and outcome",
    ["action", "result"],
    registry=REGISTRY,
)

CANARY_TRACK_VERSION_INFO = Gauge(
    "nyxgpt_canary_track_version_info",
    "Info metric: 1 for the (track, version) currently observed on that track's Deployment "
    "image tag. Stale (track, version) series from a prior deploy/promote aren't cleared -- "
    "query with topk(1, ...) or a recent time range to see only the current version.",
    ["track", "version"],
    registry=REGISTRY,
)

# Per-component (api/web) canary metrics, additive since #3419 -- the four
# CANARY_* metrics above stay api-only and unlabeled exactly as before, so
# existing dashboards/alerts/tests querying them are unaffected. These add a
# `component` label and are populated for every component (api included),
# so a single query/panel can show all components at once.
CANARY_COMPONENT_ROLLOUT_ACTIVE = Gauge(
    "nyxgpt_canary_component_rollout_active",
    "Whether a canary rollout is currently in progress (1) or idle (0), by component",
    ["component"],
    registry=REGISTRY,
)

CANARY_COMPONENT_WEIGHT_PERCENT = Gauge(
    "nyxgpt_canary_component_weight_percent",
    "Current canary traffic weight percentage (0-100), by component",
    ["component"],
    registry=REGISTRY,
)

CANARY_COMPONENT_EVALUATIONS_TOTAL = Counter(
    "nyxgpt_canary_component_evaluations_total",
    "Total canary metric evaluations, by component and result "
    "(pass/insufficient_data/regression)",
    ["component", "result"],
    registry=REGISTRY,
)

CANARY_COMPONENT_EVENTS_TOTAL = Counter(
    "nyxgpt_canary_component_events_total",
    "Total canary rollout lifecycle events, by component, action, and outcome",
    ["component", "action", "result"],
    registry=REGISTRY,
)

CANARY_COMPONENT_TRACK_VERSION_INFO = Gauge(
    "nyxgpt_canary_component_track_version_info",
    "Info metric: 1 for the (component, track, version) currently observed on that "
    "component's track Deployment image tag. Stale series from a prior deploy/promote "
    "aren't cleared -- query with topk(1, ...) or a recent time range to see only the "
    "current version.",
    ["component", "track", "version"],
    registry=REGISTRY,
)

RAG_INGESTS_TOTAL = Counter(
    "nyxgpt_rag_ingests_total",
    "Total RAG document ingestion attempts, by source and outcome",
    ["source", "result"],
    registry=REGISTRY,
)

CACHE_REQUESTS_TOTAL = Counter(
    "nyxgpt_cache_requests_total",
    "Total cache lookups, by cache name and outcome (hit/miss)",
    ["cache", "result"],
    registry=REGISTRY,
)

RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "nyxgpt_rate_limit_rejections_total",
    "Total requests rejected by the per-client rate limiter, by path",
    ["path"],
    registry=REGISTRY,
)

RESOURCE_MEMORY_RSS_MB = Gauge(
    "nyxgpt_resource_memory_rss_mb",
    "Resident set size of the API process, in MB",
    registry=REGISTRY,
)

RESOURCE_CPU_PERCENT = Gauge(
    "nyxgpt_resource_cpu_percent",
    "CPU usage percentage of the API process",
    registry=REGISTRY,
)

RESOURCE_MEMORY_PERCENT = Gauge(
    "nyxgpt_resource_memory_percent",
    "Percentage of system memory used by the API process",
    registry=REGISTRY,
)

RESOURCE_QUEUE_DEPTH = Gauge(
    "nyxgpt_resource_queue_depth",
    "Current number of requests in the batch processing queue",
    registry=REGISTRY,
)

RESOURCE_DISK_PERCENT = Gauge(
    "nyxgpt_resource_disk_percent",
    "Disk usage percentage of the filesystem backing ~/.nyxGPT",
    registry=REGISTRY,
)

SELFHEAL_GIVEUP_TOTAL = Counter(
    "nyxgpt_selfheal_giveup_total",
    "Total times self-heal gave up on a component after exhausting its "
    "consecutive-restart budget",
    ["service"],
    registry=REGISTRY,
)

CANARY_AUTO_ROLLBACK_TOTAL = Counter(
    "nyxgpt_canary_auto_rollback_total",
    "Total canary rollouts automatically rolled back due to a metrics regression -- "
    'distinct from nyxgpt_canary_events_total{action="rollback"}, which also counts '
    "operator-initiated rollbacks",
    ["component"],
    registry=REGISTRY,
)


def update_resource_gauges(
    *,
    rss_mb: float,
    cpu_percent: float,
    queue_depth: int,
    disk_percent: float = 0.0,
    memory_percent: float = 0.0,
) -> None:
    """Refresh the resource-usage gauges from a live `ResourceMonitor` snapshot.

    Called just before `/metrics` is rendered rather than on every request,
    since Prometheus only needs the value at scrape time.
    """
    RESOURCE_MEMORY_RSS_MB.set(rss_mb)
    RESOURCE_MEMORY_PERCENT.set(memory_percent)
    RESOURCE_CPU_PERCENT.set(cpu_percent)
    RESOURCE_QUEUE_DEPTH.set(queue_depth)
    RESOURCE_DISK_PERCENT.set(disk_percent)


def initialize_known_rag_metric_series() -> None:
    """Touch every known RAG query/ingest (and chat-attachment) label
    combination at startup.

    A `Counter` child only exists in the registry once `.labels(...)` has
    been called on it, so on a fresh instance the RAG panels show "No data"
    (series absent) rather than a real zero (series present, value 0) until
    the first matching event happens -- indistinguishable in Grafana from a
    broken metric. Touching every known combination up front (without
    incrementing) makes every legitimate zero render honestly.
    """
    for source in ("chat", "rag_query"):
        RAG_QUERIES_TOTAL.labels(source=source)
    for source in ("document", "upload", "repo"):
        for result in ("success", "failure"):
            RAG_INGESTS_TOTAL.labels(source=source, result=result)
    for result in ("success", "failure"):
        CHAT_ATTACHMENTS_TOTAL.labels(result=result)


def render_metrics() -> tuple[bytes, str]:
    """Render all registered metrics in Prometheus text exposition format.

    Returns:
        A tuple of (body, content_type) suitable for a FastAPI Response.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
