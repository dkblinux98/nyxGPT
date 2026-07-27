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

OPS_ACTIONS_TOTAL = Counter(
    "nyxgpt_ops_actions_total",
    "Total operator-initiated lifecycle actions (nyxgpt ops CLI or admin dashboard), "
    "by command, service, and result. Distinct from nyxgpt_selfheal_restarts_total, "
    "which counts only the watchdog's own autonomous restarts.",
    ["command", "service", "result"],
    registry=REGISTRY,
)

DEPLOY_ACTIVE_COLOR = Gauge(
    "nyxgpt_deploy_active_color",
    "Whether a blue/green color is currently receiving traffic (1) or not (0)",
    ["color"],
    registry=REGISTRY,
)

DEPLOY_SWITCHES_TOTAL = Counter(
    "nyxgpt_deploy_switches_total",
    "Total blue/green traffic switches attempted, by direction and outcome",
    ["from_color", "to_color", "result"],
    registry=REGISTRY,
)

DEPLOY_ROLLBACKS_TOTAL = Counter(
    "nyxgpt_deploy_rollbacks_total",
    "Total blue/green rollback attempts, by outcome",
    ["result"],
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

RESOURCE_QUEUE_DEPTH = Gauge(
    "nyxgpt_resource_queue_depth",
    "Current number of requests in the batch processing queue",
    registry=REGISTRY,
)


def update_resource_gauges(*, rss_mb: float, cpu_percent: float, queue_depth: int) -> None:
    """Refresh the resource-usage gauges from a live `ResourceMonitor` snapshot.

    Called just before `/metrics` is rendered rather than on every request,
    since Prometheus only needs the value at scrape time.
    """
    RESOURCE_MEMORY_RSS_MB.set(rss_mb)
    RESOURCE_CPU_PERCENT.set(cpu_percent)
    RESOURCE_QUEUE_DEPTH.set(queue_depth)


def render_metrics() -> tuple[bytes, str]:
    """Render all registered metrics in Prometheus text exposition format.

    Returns:
        A tuple of (body, content_type) suitable for a FastAPI Response.
    """
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
