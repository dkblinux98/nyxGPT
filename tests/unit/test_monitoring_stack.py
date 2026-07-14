"""Unit tests for the opt-in Grafana/Prometheus monitoring stack.

Validates the Docker Compose wiring, Prometheus config/alerts, and Grafana
dashboard JSON against the actual metric names/labels defined in
nyxgpt.metrics, so the dashboards can't silently drift from the metrics the
API exposes at /metrics.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from nyxgpt import metrics as prom_metrics

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWN_METRIC_NAMES = {
    "nyxgpt_http_requests_total",
    "nyxgpt_http_request_duration_seconds",
    "nyxgpt_http_request_duration_seconds_bucket",
    "nyxgpt_http_errors_total",
    "nyxgpt_chat_requests_total",
    "nyxgpt_rag_queries_total",
    "up",
}


def test_known_metric_names_match_registry() -> None:
    """Guard against the test's own metric name list drifting from metrics.py."""
    registered_counters_and_histograms = {
        f"{family.name}_total" if family.type == "counter" else family.name
        for family in prom_metrics.REGISTRY.collect()
    }
    assert registered_counters_and_histograms == {
        "nyxgpt_http_requests_total",
        "nyxgpt_http_request_duration_seconds",
        "nyxgpt_http_errors_total",
        "nyxgpt_chat_requests_total",
        "nyxgpt_rag_queries_total",
    }


def test_compose_defines_opt_in_monitoring_profile() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    for name in ("prometheus", "grafana"):
        assert name in services, f"{name} service missing from docker-compose.yml"
        assert services[name]["profiles"] == [
            "monitoring"
        ], f"{name} must only start under the opt-in 'monitoring' profile"

    assert "prometheus_data" in compose["volumes"]
    assert "grafana_data" in compose["volumes"]


def test_prometheus_config_scrapes_the_api_metrics_endpoint() -> None:
    prometheus_config = yaml.safe_load((REPO_ROOT / "docker" / "prometheus.yml").read_text())
    scrape_configs = prometheus_config["scrape_configs"]

    nyxgpt_job = next(job for job in scrape_configs if job["job_name"] == "nyxgpt-api")
    assert nyxgpt_job["metrics_path"] == "/metrics"
    targets = nyxgpt_job["static_configs"][0]["targets"]
    assert targets == ["api:8000"]


def _iter_promql_exprs(obj: object):
    if isinstance(obj, dict):
        if "expr" in obj and isinstance(obj["expr"], str):
            yield obj["expr"]
        for value in obj.values():
            yield from _iter_promql_exprs(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _iter_promql_exprs(item)


def _referenced_metric_names(expr: str) -> set[str]:
    return set(re.findall(r"\bnyxgpt_[a-z_]+|(?<![a-zA-Z_])up(?=\{|\s)", expr))


@pytest.mark.parametrize(
    "config_path",
    [
        REPO_ROOT / "docker" / "prometheus-alerts.yml",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "system-overview.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "rag-performance.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "api-metrics.json",
    ],
)
def test_promql_expressions_only_reference_known_metrics(config_path: Path) -> None:
    if config_path.suffix == ".json":
        doc = json.loads(config_path.read_text())
    else:
        doc = yaml.safe_load(config_path.read_text())

    exprs = list(_iter_promql_exprs(doc))
    assert exprs, f"expected at least one PromQL expression in {config_path.name}"

    for expr in exprs:
        for name in _referenced_metric_names(expr):
            assert (
                name in KNOWN_METRIC_NAMES
            ), f"{config_path.name} references unknown metric {name!r} in expr: {expr!r}"


def test_grafana_dashboards_are_provisioned() -> None:
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard_files = sorted(p.name for p in dashboards_dir.glob("*.json"))
    assert dashboard_files == [
        "api-metrics.json",
        "rag-performance.json",
        "system-overview.json",
    ]

    provider_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
        ).read_text()
    )
    assert provider_config["providers"][0]["options"]["path"] == (
        "/etc/grafana/provisioning/dashboards/json"
    )

    datasource_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources" / "datasource.yml"
        ).read_text()
    )
    datasource = datasource_config["datasources"][0]
    assert datasource["type"] == "prometheus"
    assert datasource["url"] == "http://prometheus:9090"
