"""Unit tests for the opt-in Grafana/Prometheus monitoring stack.

Validates the Docker Compose wiring, Prometheus config/alerts, and Grafana
dashboard JSON against the actual metric names/labels defined in
nyxgpt.metrics, so the dashboards can't silently drift from the metrics the
API exposes at /metrics.
"""

from __future__ import annotations

import json
import re
from configparser import ConfigParser
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nyxgpt import metrics as prom_metrics
from nyxgpt.app import app
from nyxgpt.config import get_monitoring_config, get_monitoring_enabled

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
KNOWN_METRIC_NAMES = {
    "nyxgpt_http_requests_total",
    "nyxgpt_http_request_duration_seconds",
    "nyxgpt_http_request_duration_seconds_bucket",
    "nyxgpt_http_errors_total",
    "nyxgpt_chat_requests_total",
    "nyxgpt_rag_queries_total",
    "nyxgpt_selfheal_unhealthy_components",
    "nyxgpt_selfheal_restarts_total",
    "nyxgpt_selfheal_restart_count",
    "nyxgpt_selfheal_last_recovery_timestamp",
    "nyxgpt_deploy_active_color",
    "nyxgpt_deploy_switches_total",
    "nyxgpt_deploy_rollbacks_total",
    "nyxgpt_canary_rollout_active",
    "nyxgpt_canary_weight_percent",
    "nyxgpt_canary_evaluations_total",
    "nyxgpt_canary_events_total",
    "nyxgpt_rag_ingests_total",
    "nyxgpt_cache_requests_total",
    "nyxgpt_rate_limit_rejections_total",
    "nyxgpt_resource_memory_rss_mb",
    "nyxgpt_resource_cpu_percent",
    "nyxgpt_resource_queue_depth",
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
        "nyxgpt_selfheal_unhealthy_components",
        "nyxgpt_selfheal_restarts_total",
        "nyxgpt_selfheal_restart_count",
        "nyxgpt_selfheal_last_recovery_timestamp",
        "nyxgpt_deploy_active_color",
        "nyxgpt_deploy_switches_total",
        "nyxgpt_deploy_rollbacks_total",
        "nyxgpt_canary_rollout_active",
        "nyxgpt_canary_weight_percent",
        "nyxgpt_canary_evaluations_total",
        "nyxgpt_canary_events_total",
        "nyxgpt_rag_ingests_total",
        "nyxgpt_cache_requests_total",
        "nyxgpt_rate_limit_rejections_total",
        "nyxgpt_resource_memory_rss_mb",
        "nyxgpt_resource_cpu_percent",
        "nyxgpt_resource_queue_depth",
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


def test_grafana_volumes_do_not_nest_a_mount_inside_a_read_only_mount() -> None:
    # Regression test: Docker Desktop refuses to create a mountpoint inside a
    # `:ro` bind mount (e.g. mounting .../dashboards/json under a parent
    # mounted `:ro` at .../provisioning), so the dashboards bind mount must
    # target a path that isn't nested under another read-only mount.
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    volumes = compose["services"]["grafana"]["volumes"]

    parsed = []
    for entry in volumes:
        source, target, *mode = entry.split(":")
        parsed.append((target.rstrip("/"), mode == ["ro"]))

    for target, _ in parsed:
        for other_target, other_is_ro in parsed:
            if other_target == target or not other_is_ro:
                continue
            assert not target.startswith(
                other_target + "/"
            ), f"{target} is nested inside read-only mount {other_target}"


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
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "self-healing.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "deployment.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "canary.json",
        REPO_ROOT / "docker" / "grafana" / "dashboards" / "resource-usage.json",
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
        "canary.json",
        "deployment.json",
        "logs-explorer.json",
        "operational-logs.json",
        "rag-performance.json",
        "resource-usage.json",
        "self-healing.json",
        "system-overview.json",
    ]

    provider_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
        ).read_text()
    )
    assert provider_config["providers"][0]["options"]["path"] == ("/var/lib/grafana/dashboards")

    datasource_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources" / "datasource.yml"
        ).read_text()
    )
    datasource = datasource_config["datasources"][0]
    assert datasource["type"] == "prometheus"
    assert datasource["url"] == "http://prometheus:9090"


def _cfg(**monitoring_options: str) -> ConfigParser:
    cfg = ConfigParser()
    if monitoring_options:
        cfg["monitoring"] = monitoring_options
    return cfg


def test_get_monitoring_enabled_defaults_to_false() -> None:
    assert get_monitoring_enabled(_cfg()) is False


def test_get_monitoring_enabled_reads_config() -> None:
    assert get_monitoring_enabled(_cfg(enabled="true")) is True
    assert get_monitoring_enabled(_cfg(enabled="false")) is False


def test_get_monitoring_config_defaults() -> None:
    result = get_monitoring_config(_cfg())

    assert result == {
        "enabled": False,
        "grafana_ui_url": "http://localhost:3001",
        "prometheus_ui_url": "http://localhost:9090",
    }


def test_get_monitoring_config_reads_overrides() -> None:
    cfg = _cfg(
        enabled="true",
        grafana_ui_url="http://grafana:3000",
        prometheus_ui_url="http://prometheus:9090",
    )

    result = get_monitoring_config(cfg)

    assert result["enabled"] is True
    assert result["grafana_ui_url"] == "http://grafana:3000"
    assert result["prometheus_ui_url"] == "http://prometheus:9090"


def test_monitoring_status_endpoint_reports_disabled_by_default() -> None:
    """The test config fixture has no [monitoring] section, so the endpoint
    must report the safe default: disabled, not active."""
    client = TestClient(app)

    response = client.get("/api/v1/monitoring")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["active"] is False
    assert data["grafana_ui_url"] == "http://localhost:3001"
    assert data["prometheus_ui_url"] == "http://localhost:9090"
