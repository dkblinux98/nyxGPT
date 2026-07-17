"""Unit tests for the opt-in Loki/promtail log aggregation stack.

Validates the Docker Compose wiring, Loki retention config, promtail scrape
config, and the Grafana Loki datasource + Logs Explorer dashboard.
"""

from __future__ import annotations

import json
from configparser import ConfigParser
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from nyxgpt.app import app
from nyxgpt.config import get_log_aggregation_config, get_log_aggregation_enabled

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_compose_defines_opt_in_logging_profile() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose["services"]

    for name in ("loki", "promtail"):
        assert name in services, f"{name} service missing from docker-compose.yml"
        assert services[name]["profiles"] == [
            "logging"
        ], f"{name} must only start under the opt-in 'logging' profile"

    assert "loki_data" in compose["volumes"]

    # promtail must read from the same volume the api service writes logs to.
    promtail_volumes = services["promtail"]["volumes"]
    assert any(v.startswith("nyxgpt_data:") for v in promtail_volumes)


def test_loki_config_has_retention_enabled() -> None:
    loki_config = yaml.safe_load((REPO_ROOT / "docker" / "loki-config.yml").read_text())

    assert loki_config["compactor"]["retention_enabled"] is True
    assert loki_config["limits_config"]["retention_period"]


def test_promtail_scrapes_nyxgpt_log_files_and_ships_to_loki() -> None:
    promtail_config = yaml.safe_load((REPO_ROOT / "docker" / "promtail-config.yml").read_text())

    assert promtail_config["clients"][0]["url"] == "http://loki:3100/loki/api/v1/push"

    scrape_config = promtail_config["scrape_configs"][0]
    labels = scrape_config["static_configs"][0]["labels"]
    assert labels["job"] == "nyxgpt"
    assert labels["__path__"].endswith("logs/*.log*")


def test_promtail_extracts_logger_as_a_label() -> None:
    """The per-component curated Loki queries (operational-logs.json) filter
    on a `logger` label, so promtail's pipeline must extract it from the
    default log format -- not just `level`, which is all it did before."""
    promtail_config = yaml.safe_load((REPO_ROOT / "docker" / "promtail-config.yml").read_text())
    pipeline_stages = promtail_config["scrape_configs"][0]["pipeline_stages"]

    regex_stage = next(stage["regex"] for stage in pipeline_stages if "regex" in stage)
    assert "?P<logger>" in regex_stage["expression"]

    labels_stage = next(stage["labels"] for stage in pipeline_stages if "labels" in stage)
    assert "logger" in labels_stage
    assert "level" in labels_stage


def test_operational_logs_dashboard_is_provisioned() -> None:
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard = json.loads((dashboards_dir / "operational-logs.json").read_text())

    assert dashboard["uid"] == "nyxgpt-operational-logs"
    panel_types = {panel["type"] for panel in dashboard["panels"]}
    assert panel_types == {"logs"}

    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            assert target["datasource"]["type"] == "loki"

    exprs = [target["expr"] for panel in dashboard["panels"] for target in panel["targets"]]
    assert any('logger="nyxgpt.self_heal"' in expr for expr in exprs)
    assert any('logger="nyxgpt.deploy"' in expr for expr in exprs)
    assert any('logger="nyxgpt.canary"' in expr for expr in exprs)
    assert any('logger="nyxgpt.chat"' in expr and "ERROR" in expr for expr in exprs)


def test_grafana_has_loki_datasource() -> None:
    datasource_config = yaml.safe_load(
        (
            REPO_ROOT / "docker" / "grafana" / "provisioning" / "datasources" / "datasource.yml"
        ).read_text()
    )
    loki_datasource = next(ds for ds in datasource_config["datasources"] if ds["name"] == "Loki")
    assert loki_datasource["type"] == "loki"
    assert loki_datasource["url"] == "http://loki:3100"


def test_logs_explorer_dashboard_is_provisioned() -> None:
    dashboards_dir = REPO_ROOT / "docker" / "grafana" / "dashboards"
    dashboard = json.loads((dashboards_dir / "logs-explorer.json").read_text())

    assert dashboard["uid"] == "nyxgpt-logs-explorer"
    panel_types = {panel["type"] for panel in dashboard["panels"]}
    assert "logs" in panel_types

    for panel in dashboard["panels"]:
        for target in panel["targets"]:
            assert target["datasource"]["type"] == "loki"


def _cfg(**log_aggregation_options: str) -> ConfigParser:
    cfg = ConfigParser()
    if log_aggregation_options:
        cfg["log_aggregation"] = log_aggregation_options
    return cfg


def test_get_log_aggregation_enabled_defaults_to_false() -> None:
    assert get_log_aggregation_enabled(_cfg()) is False


def test_get_log_aggregation_enabled_reads_config() -> None:
    assert get_log_aggregation_enabled(_cfg(enabled="true")) is True
    assert get_log_aggregation_enabled(_cfg(enabled="false")) is False


def test_get_log_aggregation_config_defaults() -> None:
    result = get_log_aggregation_config(_cfg())

    assert result == {
        "enabled": False,
        "grafana_explore_url": "http://localhost:3001/explore",
    }


def test_get_log_aggregation_config_reads_overrides() -> None:
    cfg = _cfg(enabled="true", grafana_explore_url="http://grafana:3000/explore")

    result = get_log_aggregation_config(cfg)

    assert result["enabled"] is True
    assert result["grafana_explore_url"] == "http://grafana:3000/explore"


def test_log_aggregation_status_endpoint_reports_disabled_by_default() -> None:
    """The test config fixture has no [log_aggregation] section, so the
    endpoint must report the safe default: disabled, not active."""
    client = TestClient(app)

    response = client.get("/api/v1/log-aggregation")

    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["active"] is False
    assert data["grafana_explore_url"] == "http://localhost:3001/explore"


def test_log_aggregation_status_endpoint_includes_curated_loki_queries() -> None:
    """The SRE overview links out to curated LogQL saved queries mirroring
    the per-component panels in the Operational Logs dashboard -- each must
    include the raw query text and a hint of what it filters."""
    client = TestClient(app)

    response = client.get("/api/v1/log-aggregation")

    assert response.status_code == 200
    curated_queries = response.json()["curated_queries"]
    assert len(curated_queries) >= 4
    labels = {q["label"] for q in curated_queries}
    assert {"Self-heal events", "Deploy events", "Canary events", "Chat errors"} <= labels
    for q in curated_queries:
        assert q["query"].startswith('{job="nyxgpt"')
        assert q["hint"]
